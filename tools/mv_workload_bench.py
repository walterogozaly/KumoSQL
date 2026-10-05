"""Materialized-view rewriting on the edx-h MV benchmark workloads: find views, rewrite queries, prove every rewrite.

Source: https://github.com/edx-h/Benchmarking-MV-Based-Rewriting at commit 83da57fbc7 (the paper's workloads
and schemas; its own enumerators, rewriters and engines need Hive, Doris and friends and are not run). The
repository has no licence file, so the files are downloaded on demand into a cache folder, checked against the
digests below and never stored in this repository. Only the python-forward part is scored:

* ``mined``: ``kumosql.view_candidates`` reads the development queries of a workload, proposes the joins that
  several queries share and keeps ``--budget`` views. Every query of the workload (development and held-out) is
  then rewritten over the best view it can use by ``kumosql.model_reuse.rewrite_over_model``, which returns a
  rewrite only when the prover proves it equal to the query; each rewrite is re-run against the original on random
  DuckDB databases. A mismatch is **wrong**.
* ``given``: the ten candidate views the benchmark ships (``excel/quick_start_input/default_mv_list.xlsx``, JOB),
  tried on every JOB query that joins all of a view's tables.
* ``poisoned``: a control. The chosen views with a condition no row meets; a query cannot be answered from an empty
  view, so every rewrite proven there counts as **wrong**.
* ``baseline``: the existing prover alone, which can only say that a whole view equals a whole query.

Splits: JOB queries are held out by query family (``1a``..``1d`` are one family; one family in four), the other
workloads by a hash of the query text (one query in four, so the held-out queries are other instances of the same
templates). Views are mined from the development queries only. Nothing reads the held-out queries until they are
rewritten. Workloads other than JOB are large (500 to 1,449 queries), so a fixed hash-selected sample is scored
(``--sample``); the sample does not depend on the results.

    python tools/mv_workload_bench.py                      # JOB, all 113 queries
    python tools/mv_workload_bench.py --workload stats --sample 100
    python tools/mv_workload_bench.py --all --json out.json

Outcomes per query: ``rewritten`` (proven and verified), ``no_rewrite`` (a view applied, no proven rewrite),
``no_view`` (no chosen view joins tables the query joins), ``unsupported`` (the engine cannot read the query),
``timeout``, ``error``, and ``wrong`` (a verified mismatch; must stay 0).
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.getLogger("sqlglot").setLevel(logging.ERROR)

from kumosql import model_reuse as mr  # noqa: E402
from kumosql import view_candidates as vc  # noqa: E402
from kumosql import view_generalize as vg  # noqa: E402
from kumosql.random_check import CheckError, Column, Schema, Table, find_difference  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

COMMIT = "83da57fbc719b7693e7ddfbbb29a3e4f43a3c333"
BASE = f"https://raw.githubusercontent.com/edx-h/Benchmarking-MV-Based-Rewriting/{COMMIT}/"
FILES = {
    "excel/workload/imdb_job.xlsx": "091b2ce3ff13eeb9a20fb257be2ba06c7c6320b4abfe2a8809e3c3070e7ac363",
    "excel/workload/scale.xlsx": "fec11552af6531f148a4a4a0e710a2653f982fceb9c69936e58bd305c593f416",
    "excel/workload/stats.xlsx": "8c1ddaf3fb8cb5b8edefe05300b3afc39ee205aae67b0a4e09d696efb3cae459",
    "excel/workload/tpcds.xlsx": "1d615855f3aeec06bf93cacece96d448f25f75d16f00d2fa4ca2d5d820437cce",
    "excel/quick_start_input/default_mv_list.xlsx": "06dda68866d935357d2743afb383568a04d4ae6525425467385df79fb5cb6cee",
    "data/schema/imdb_job.sql": "8139cd798fcfe9b16f5a06296b95b95ef780607da59fce5ad57786e10c430d86",
    "data/schema/stats.sql": "e39c727115b2687c6dbb2079c537f7731c2eb4c4e403736a30726df5d521c0af",
    "data/schema/bigquery/tpcds.sql": "609cb3a85eece9ecf720027242c1e7e84fe48b65727085e5e9983b8aafd2d724",
}
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "mv-benchmark"

# workload -> (workbook, SQL column, schema file); SCALE queries run on the IMDB schema
WORKLOADS = {
    "job": ("excel/workload/imdb_job.xlsx", "raw_sql", "data/schema/imdb_job.sql"),
    "scale": ("excel/workload/scale.xlsx", "raw_sql", "data/schema/imdb_job.sql"),
    "stats": ("excel/workload/stats.xlsx", "raw_sql", "data/schema/stats.sql"),
    "tpcds": ("excel/workload/tpcds.xlsx", "raw_sql", "data/schema/bigquery/tpcds.sql"),
}
BUDGET = 12
TIMEOUT_MS = 10000
TRIALS = 40


# ---------------------------------------------------------------------------------------------
# data


def fetch(relative: str, data: Path | None) -> Path:
    if data is not None:
        path = data / relative
        if not path.exists():
            raise OSError(f"{path} not found")
        return path
    path = CACHE / relative
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != FILES[relative]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE + relative, timeout=120) as response:
            body = response.read()
        if hashlib.sha256(body).hexdigest() != FILES[relative]:
            raise OSError(f"{relative}: the downloaded file does not match the pinned digest")
        path.write_bytes(body)
    return path


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_sheet(path: Path) -> list[dict[str, str | None]]:
    """The first sheet of an .xlsx file as rows keyed by the header row (standard library only)."""

    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{_NS}si"):
                strings.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.iter(f"{_NS}row"):
        cells = {}
        for c in row.findall(f"{_NS}c"):
            column = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find(f"{_NS}v")
            if c.get("t") == "s" and v is not None:
                cells[column] = strings[int(v.text)]
            elif c.get("t") == "inlineStr":
                cells[column] = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
            else:
                cells[column] = v.text if v is not None else None
        rows.append(cells)
    header = rows[0]
    return [{header[k]: v for k, v in row.items() if k in header} for row in rows[1:]]


def parse_schema(path: Path) -> Schema:
    tables = []
    for m in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\n\s*\)\s*;", path.read_text(encoding="utf-8"), re.S | re.I):
        columns, keys = [], []
        for line in re.split(r",\s*\n", m.group(2)):
            line = line.strip()
            if not line or line.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT")):
                continue
            name, kind = line.split(None, 2)[:2]
            kind = kind.lower()
            upper = line.upper()
            if kind.startswith(("int", "bigint", "smallint")):
                typ = "int"
            elif kind.startswith(("float", "double", "numeric", "decimal", "real")):
                typ = "float"
            elif kind.startswith(("date", "timestamp")):
                typ = "date"
            else:
                typ = "text"
            primary = "PRIMARY KEY" in upper
            columns.append(Column(name.lower(), typ, "NOT NULL" in upper or primary))
            if primary:
                keys.append((name.lower(),))
        tables.append(Table(m.group(1).lower(), columns, keys))
    return Schema(tables)


def constraints_of(schema: Schema) -> dict[str, TableConstraints]:
    return {t.name: TableConstraints(not_null=frozenset(schema.not_null(t)), keys=tuple(tuple(k) for k in t.keys)) for t in schema.tables}


def digest(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def load_workload(name: str, data: Path | None, sample: int | None) -> tuple[dict[str, dict], Schema]:
    """The queries of a workload (id -> record with sql, family, held_out) and its schema."""

    workbook, column, schema_file = WORKLOADS[name]
    rows = read_sheet(fetch(workbook, data))
    schema = parse_schema(fetch(schema_file, data))
    queries: dict[str, dict] = {}
    for position, row in enumerate(rows):
        if name == "job":
            ident, sql = row["turn_index"], row[column]
            family = re.match(r"\d+", ident).group()
        else:
            sql = row[column]
            ident = f"{name}-{digest(sql) % 10**8:08d}"
            family = ident
        if not sql or ident in queries:
            continue
        queries[ident] = {"sql": sql.strip().rstrip(";"), "family": family}
    if sample is not None and len(queries) > sample:
        keep = sorted(queries, key=lambda q: digest("select:" + q))[:sample]
        queries = {q: queries[q] for q in sorted(keep)}
    for ident, record in queries.items():
        record["held_out"] = digest("split:" + record["family"]) % 4 == 0
    return queries, schema


def load_given(data: Path | None) -> dict[str, str]:
    rows = read_sheet(fetch("excel/quick_start_input/default_mv_list.xlsx", data))
    return {f"mv{r['mv_candidate_index']}": r["sql"] for r in rows if r.get("sql")}


# ---------------------------------------------------------------------------------------------
# scoring


def applicable(graph: vc.Graph | None, view_tables: list[str]) -> bool:
    if graph is None:
        return False
    have = Counter(graph.tables.values())
    return all(have[t] >= n for t, n in Counter(view_tables).items())


def try_rewrite(task: dict) -> dict:
    """Rewrite one query over the views that apply to it (largest first); verify the first proven rewrite."""

    schema: Schema = task["schema"]
    start = time.time()
    record = {"id": task["id"], "held_out": task["held_out"], "track": task["track"], "tried": 0}
    graph = vc.graph_of(task["sql"], schema.columns)
    if graph is None:
        record.update(status="unsupported", seconds=round(time.time() - start, 2))
        return record
    attempts = []  # (tables replaced, view, slice of a general view)
    for view in task["views"]:
        general = view.get("general")
        if general is None:
            if applicable(graph, view["tables"]):
                attempts.append((len(view["tables"]), view, None))
        else:
            attempts.extend((len(general.core.tables) + len(chosen), view, chosen) for chosen in general.slices_for(graph))
    record["applicable"] = len({a[1]["name"] for a in attempts})
    if not attempts:
        record.update(status="no_view", seconds=round(time.time() - start, 2))
        return record
    cons = constraints_of(schema)
    outcome = "no_rewrite"
    for size, view, chosen in sorted(attempts, key=lambda a: (-a[0], a[1]["name"])):
        record["tried"] += 1
        try:
            if task["baseline"]:
                reuse = baseline(task["sql"], view["sql"], schema, cons)
            elif chosen is None:
                reuse = mr.rewrite_over_model(task["sql"], view["sql"], schema=schema.columns, constraints=cons, timeout_ms=TIMEOUT_MS)
            else:
                reuse = rewrite_over_slice(task["sql"], view, chosen, schema, cons)
        except Exception as error:  # noqa: BLE001 - reported, never a result
            outcome = "error"
            record["reason"] = f"{type(error).__name__}: {error}"[:200]
            continue
        if reuse.status == "timeout":
            outcome = "timeout"
            continue
        if reuse.rewritten:
            record.update(view=view["name"], view_tables=size, strategy=reuse.strategy)
            inlined = reuse.inlined_sql or view["sql"]
            try:
                witness = find_difference(schema, reuse.query_sql or task["sql"], inlined, mode="bag", trials=TRIALS)
            except CheckError as error:
                record["check"] = f"unchecked: {error}"[:200]
            else:
                record["check"] = "verified" if witness is None else "WRONG"
            outcome = "rewritten" if record["check"] != "WRONG" and task["track"] != "poisoned" else "wrong"
            break
    record.update(status=outcome, seconds=round(time.time() - start, 2))
    return record


def rewrite_over_slice(sql: str, view: dict, chosen: frozenset[int], schema: Schema, cons) -> mr.ModelReuse:
    """Rewrite over one slice of a general (LEFT JOIN) view.

    The engine proves the rewrite over the slice's inner join; the prover then proves that inner join equals the
    stored view filtered to the rows where the slice's extras are present, so the rewrite reads the stored view."""

    general: vg.GeneralView = view["general"]
    poison = view.get("poison", False)
    reuse = mr.rewrite_over_model(sql, general.inner_sql(chosen, poison), schema=schema.columns, constraints=cons, timeout_ms=TIMEOUT_MS)
    if not reuse.rewritten:
        return reuse
    if not general.lemma(chosen, schema.columns, cons, TIMEOUT_MS, poison):
        return mr.ModelReuse("no_rewrite", "the slice is not proven equal to the stored view")
    stored = general.stored_sql(chosen, "mv_stored")
    replacement = mr.sqlglot.parse_one(reuse.sql, read="postgres")
    composed = mr._inline(replacement, "mv0", stored)
    inlined = mr._inline(replacement, "mv0", general.stored_inline(chosen, poison))
    return mr.ModelReuse("rewritten", reuse.reason, composed, reuse.strategy, reuse.assumptions, reuse.model_columns, reuse.candidates_tried, inlined, reuse.query_sql)


def baseline(sql: str, view_sql: str, schema: Schema, cons) -> mr.ModelReuse:
    """The existing prover alone: does the view, as a whole, equal the query?"""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    result = prove_equivalent_algebraic(sql, view_sql, schema=schema.columns, constraints=cons, timeout_ms=TIMEOUT_MS, dialect="postgres", compare_names=False)
    if result.proven:
        return mr.ModelReuse("rewritten", "the view equals the query", "SELECT * FROM mv0", "whole-view")
    return mr.ModelReuse("no_rewrite", "not the same query")


def view_record(candidate: vc.Candidate | vg.GeneralView, name: str) -> dict:
    if isinstance(candidate, vg.GeneralView):
        return {"name": name, "sql": candidate.sql(), "tables": candidate.tables, "general": candidate}
    return {"name": name, "sql": candidate.sql(), "tables": list(candidate.shape.tables)}


def poisoned(view: dict) -> dict:
    """The same view with a condition that no row meets: a query cannot be answered from it, so no rewrite may be proven."""

    if view.get("general") is not None:
        return {**view, "name": view["name"] + "-empty", "sql": view["general"].sql(poison=True), "poison": True}
    sql = view["sql"]
    column = re.match(r"SELECT t0\.(\w+) AS", sql).group(1)
    contradiction = f"t0.{column} IS NULL AND t0.{column} IS NOT NULL"
    sql = sql + (f" AND {contradiction}" if " WHERE " in sql else f" WHERE {contradiction}")
    return {**view, "name": view["name"] + "-empty", "sql": sql}


def given_record(name: str, sql: str, schema: Schema) -> dict | None:
    graph = vc.graph_of(sql, schema.columns)
    if graph is None:
        return None
    return {"name": name, "sql": sql, "tables": sorted(graph.tables.values())}


def summarize(records: list[dict]) -> dict:
    out = {}
    for label, subset in (("all", records), ("dev", [r for r in records if not r["held_out"]]), ("held_out", [r for r in records if r["held_out"]])):
        counts = Counter(r["status"] for r in subset)
        rewritten = [r for r in subset if r["status"] == "rewritten"]
        out[label] = {
            "queries": len(subset),
            "statuses": dict(counts),
            "rewritten": len(rewritten),
            "verified": sum(1 for r in rewritten if r.get("check") == "verified"),
            "unchecked": sum(1 for r in rewritten if str(r.get("check", "")).startswith("unchecked")),
            "wrong": counts.get("wrong", 0),
            "queries_with_a_view": sum(1 for r in subset if r["status"] not in ("no_view", "unsupported")),
            "mean_tables_in_view": round(sum(r["view_tables"] for r in rewritten) / len(rewritten), 2) if rewritten else 0,
            "seconds": round(sum(r["seconds"] for r in subset), 1),
        }
    return out


def run_workload(name: str, data: Path | None, sample: int | None, budget: int, jobs: int, use_baseline: bool, tracks: list[str], dev_only: bool = False, generalize: bool = True) -> dict:
    queries, schema = load_workload(name, data, sample)
    dev = {q: r["sql"] for q, r in queries.items() if not r["held_out"]}
    start = time.time()
    mined = vc.mine(dev, schema.columns)
    if generalize:
        graphs = {q: g for q, g in ((q, vc.graph_of(sql, schema.columns)) for q, sql in dev.items()) if g is not None}
        chosen = vg.select(vg.generalize(mined, constraints_of(schema), graphs), graphs, budget)
    else:
        chosen = vc.select(mined, budget)
    views = [view_record(c, f"view{i}") for i, c in enumerate(chosen)]
    result: dict = {
        "workload": name,
        "queries": len(queries),
        "dev_queries": len(dev),
        "held_out_queries": len(queries) - len(dev),
        "candidates_mined": len(mined),
        "views_chosen": len(chosen),
        "view_sizes": sorted(Counter(len(v["tables"]) for v in views).items()),
        "views_with_left_joins": sum(1 for v in views if v.get("general") is not None and v["general"].extras),
        "mining_seconds": round(time.time() - start, 1),
        "tracks": {},
        "records": {},
    }
    plans = {"mined": views}
    if "poisoned" in tracks:
        plans["poisoned"] = [poisoned(v) for v in views]
    if name == "job" and "given" in tracks:
        plans["given"] = [g for g in (given_record(n, s, schema) for n, s in load_given(data).items()) if g]
    for track, track_views in plans.items():
        tasks = [{"id": q, "sql": r["sql"], "held_out": r["held_out"], "schema": schema, "views": track_views, "track": track, "baseline": use_baseline} for q, r in queries.items() if not (dev_only and r["held_out"])]
        if jobs > 1:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                records = list(pool.map(try_rewrite, tasks, chunksize=4))
        else:
            records = [try_rewrite(t) for t in tasks]
        result["tracks"][track] = summarize(records)
        result["records"][track] = records
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workload", choices=list(WORKLOADS), action="append")
    parser.add_argument("--all", action="store_true", help="every workload")
    parser.add_argument("--sample", type=int, default=None, help="queries scored per workload other than JOB (default 100)")
    parser.add_argument("--budget", type=int, default=BUDGET, help="views kept per workload")
    parser.add_argument("--baseline", action="store_true", help="the existing prover alone (whole view equals whole query)")
    parser.add_argument("--data", type=Path, help="a local checkout of the benchmark instead of the download")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--track", choices=["mined", "given", "poisoned"], action="append")
    parser.add_argument("--no-generalize", action="store_true", help="keep the mined inner-join views as they are (no LEFT JOIN onto unique keys)")
    parser.add_argument("--dev-only", action="store_true", help="rewrite and score only the development queries (held-out queries are never read by a rewrite)")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    names = list(WORKLOADS) if args.all else (args.workload or ["job"])
    tracks = args.track or ["mined", "given", "poisoned"]
    report = {}
    for name in names:
        sample = None if name == "job" else (args.sample or 100)
        report[name] = run_workload(name, args.data, sample, args.budget, args.jobs, args.baseline, tracks, args.dev_only, not args.no_generalize)
        head = report[name]
        print(f"{name}: {head['queries']} queries ({head['held_out_queries']} held out), {head['candidates_mined']} candidate views mined, {head['views_chosen']} kept, sizes {head['view_sizes']}")
        for track, summary in head["tracks"].items():
            for label, row in summary.items():
                print(f"  {track}/{label}: {json.dumps(row)}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    wrong = sum(s["all"]["wrong"] for r in report.values() for s in r["tracks"].values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
