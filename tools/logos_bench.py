"""Logos' TPC-H, DSB and TPC-DS rewrite pairs through KumoSQL's provers, checked on generated data.

Logos (https://github.com/WindOctober/Logos, MIT; ``benchmarks/core``) collects SQL rewrite pairs
with their provenance. Three of its folders hold pairs no other KumoSQL eval scores:

* ``rbot/tpch`` (22 pairs) and ``rbot/dsb`` (37 pairs): an R-Bot workload query (``queryN_0.sql``)
  and the SQL that R-Bot's pinned Calcite engine prints after applying one rule to it
  (``queryN_1.sql``); ``rbot/rewrite-pairs.manifest.json`` names the rule. A Calcite rule's output
  is a golden rewrite, not a proven label: Logos itself does not assume the pairs are equivalent.
* ``tpcds/variants`` (14 pairs): a TPC-DS query and the same query from the kit's variant
  template (``query_variants/queryNa.tpl``), which the TPC-DS specification offers as an
  alternative formulation.

Its ``verieql`` and ``wetune`` folders are subsets of the VeriEQL and WeTune benchmarks KumoSQL
already scores (``python tools/logos_bench.py overlap`` checks that by text).

Every pair goes through two checks that do not trust each other:

* **proof**: the algebraic prover (``kumosql.algebraic_equivalence``) on the pair converted to
  BigQuery, then on the pair converted to MySQL (the route ``tools/rbot_bench.py`` takes), with
  the NOT NULL columns and primary keys of the workload's ``create_tables.sql``;
* **data**: both queries run in DuckDB on generated data (TPC-H at scale 0.1 from
  ``tpchgen-cli``; TPC-DS at scale 1 from DSB's ``dsdgen``; see ``tools/benchmark_corpora.py``)
  and on small constraint-respecting databases built for the pair
  (``kumosql.counterexample.find_counterexample``: random ones, then the targeted suite). A
  difference counts only when DuckDB returns the same rows with its optimizer off
  (``kumosql.duckdb_load.run_unoptimized``); under a root ``LIMIT`` the rows must differ with the
  ``LIMIT`` removed too, so a different pick among ties is not a difference.

Outcomes: ``proven``; ``refuted`` (not proved, and a database separates the two queries: a label
failure, kept as a negative that must never be proved); ``unknown``; ``unsupported`` (no route
reads the pair); ``timeout``; ``error``; and ``wrong`` (proved, yet a database separates them;
must stay 0). One case in five (by SHA-1 of its id) is held out.

The query text derives from the TPC-H and TPC-DS kits (``licenses/TPCDS-EULA.txt`` in Logos), so
it is fetched at a pinned commit and never committed; ``tests/fixtures/logos/pairs.json`` holds
only file digests, rule names and this eval's own labels.

    python tools/benchmark_corpora.py fetch logos tpch-data tpcds-data
    python tools/logos_bench.py                         # every pair
    python tools/logos_bench.py --split dev --show      # the pairs used while developing
    python tools/logos_bench.py --write-results         # also write benchmarks/results/logos-core-*.json
    python tools/logos_bench.py overlap                 # VeriEQL and WeTune overlap, by text
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

import benchmark_corpora as corpora  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "logos" / "pairs.json"
COMMIT = corpora.PINS[corpora.LOGOS_URL]
FAMILIES = {  # family -> (folder under benchmarks/core, generated database)
    "rbot-tpch": ("rbot/tpch", "tpch"),
    "rbot-dsb": ("rbot/dsb", "tpcds"),
    "tpcds-variants": ("tpcds/variants", "tpcds"),
}
PROOF_ROUTES = ("bigquery", "mysql")
PROOF_TIMEOUT_S = 120
QUERY_TIMEOUT_S = 120
TRIALS = 150
BASELINE = (
    "Baseline before any harness fix: 23/69 proved, 4 refuted, 0 wrong (5/11 held out). The two harness fixes (moving Calcite's "
    "derived-table column lists into the derived table with KumoSQL's expand_alias_columns, and one renamed TPC-DS column on the "
    "generated data) were made on the dev split, but the baseline listing showed every pair's failure reason, held-out ones included "
    "(tuned on test, exposure only; the held-out score did not move)."
)


class DataUnavailable(OSError):
    """The Logos files could not be fetched or do not match the pinned digests."""


@dataclass
class Case:
    id: str  # family/queryN
    family: str
    rule: str  # the Calcite rule R-Bot applied, or "tpcds-variant"
    statements: list[tuple[str, str]]  # (before, after) PostgreSQL statements
    adaptations: list[str] = field(default_factory=list)

    @property
    def held_out(self) -> bool:
        return corpora.held_out(self.id)


# ---------------------------------------------------------------- loading


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def core_root() -> Path:
    """``benchmarks/core`` of the pinned Logos checkout, fetched on first use and checked file by file."""

    root = corpora.BENCH_DIR / "logos" / "benchmarks" / "core"
    if not root.exists():
        try:
            corpora.fetch_logos()
        except (OSError, subprocess.CalledProcessError) as error:
            raise DataUnavailable(f"cannot fetch Logos: {error}") from error
    for name, digest in fixture()["files"].items():
        path = root / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise DataUnavailable(f"{path} does not match the pinned version; delete {corpora.BENCH_DIR / 'logos'} to fetch again")
    return root


_DAYS = re.compile(r"(cast\('[^']+' as date\))\s*\+\s*(\d+)\s+days\b", re.I)
_TOP = re.compile(r"\b(select)\s+top\s+(\d+)\b", re.I)


def statements(text: str) -> tuple[list[str], list[str]]:
    """The PostgreSQL statements of one query file, and the adaptations that made them PostgreSQL.

    The TPC-DS kit's ``ansi`` profile prints ``SELECT TOP n`` (always on the outermost select) and
    ``date + 14 days``; they become ``LIMIT n`` and ``date + INTERVAL '14' DAY``.
    """

    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    adaptations = []
    if _DAYS.search(body):
        body = _DAYS.sub(r"\1 + interval '\2' day", body)
        adaptations.append("date + n days as an interval")
    tops = [int(n) for _, n in _TOP.findall(body)]
    if tops:
        body = _TOP.sub(r"\1", body)
        adaptations.append("TOP n as LIMIT n")
    trees = [t for t in sqlglot.parse(body, read="postgres") if t is not None]
    if tops:
        if len(tops) != len(trees):
            raise ValueError("TOP is not on the outermost select of every statement")
        trees = [t.limit(n) for t, n in zip(trees, tops)]
    return [t.sql(dialect="postgres") for t in trees], adaptations


def load_cases(root: Path | None = None) -> list[Case]:
    root = root or core_root()
    cases = []
    for case_id, meta in fixture()["cases"].items():
        before, adapt_before = statements((root / meta["source"]).read_text(encoding="utf-8"))
        after, adapt_after = statements((root / meta["target"]).read_text(encoding="utf-8"))
        if meta["family"].startswith("rbot-"):
            # Logos' case policy: a source file with two statements (DSB query039) contributes its first
            before, after = before[:1], after[:1]
        if len(before) != len(after):
            raise ValueError(f"{case_id}: {len(before)} statements before, {len(after)} after")
        adaptations = sorted(set(adapt_before) | set(adapt_after))
        cases.append(Case(case_id, meta["family"], meta["rule"], list(zip(before, after)), adaptations))
    return cases


# ---------------------------------------------------------------- schemas


@dataclass
class Workload:
    columns: dict[str, list[str]]
    types: dict[str, dict[str, str]]
    not_null: dict[str, set[str]]
    keys: dict[str, list[tuple[str, ...]]]


def load_workload(path: Path) -> Workload:
    """Tables, declared types, NOT NULL columns and primary keys of a ``create_tables.sql``."""

    out = Workload({}, {}, {}, {})
    for statement in sqlglot.parse(path.read_text(encoding="utf-8"), read="postgres"):
        if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
            continue
        table = statement.this.this.name.lower()
        out.columns[table], out.types[table], out.not_null[table], out.keys[table] = [], {}, set(), []
        for item in statement.this.expressions:
            if isinstance(item, exp.ColumnDef):
                name = item.name.lower()
                out.columns[table].append(name)
                out.types[table][name] = item.args["kind"].sql(dialect="postgres").upper()
                declared = " ".join(c.sql().upper() for c in item.args.get("constraints") or [])
                if "NOT NULL" in declared or "PRIMARY KEY" in declared:
                    out.not_null[table].add(name)
                if "PRIMARY KEY" in declared:
                    out.keys[table].append((name,))
            elif isinstance(item, exp.PrimaryKey):
                key = tuple(e.name.lower() for e in item.expressions)
                out.keys[table].append(key)
                out.not_null[table].update(key)
    return out


def prover_constraints(workload: Workload) -> dict:
    from kumosql.smt_equivalence import TableConstraints

    return {t: TableConstraints(not_null=frozenset(workload.not_null[t]), keys=tuple(workload.keys[t])) for t in workload.columns}


def counterexample_spec(workload: Workload):
    from kumosql import counterexample as cx

    def kind(declared: str) -> str:
        base = declared.split("(")[0].strip()
        if base in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
            return "INT"
        if base in ("DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL"):
            return "NUMERIC"
        if base in ("DATE", "TIME"):
            return base
        return "VARCHAR"

    tables = {}
    for table, columns in workload.columns.items():
        keys = workload.keys[table]
        tables[table] = cx.Table(
            table,
            [cx.Column(c, kind(workload.types[table][c]), c in workload.not_null[table]) for c in columns],
            primary_key=keys[0] if keys else (),
            unique=list(keys[1:]),
        )
    return cx.Spec(tables)


# ---------------------------------------------------------------- checks


def _converted(sql: str, route: str, columns: dict[str, list[str]] | None = None) -> str:
    """The PostgreSQL query in the route's dialect.

    Calcite names a derived table's columns in its alias (``AS t (a, b)``), which BigQuery and MySQL
    cannot say; KumoSQL's ``expand_alias_columns`` moves the names into the derived table first.
    """

    from kumosql.ast_utils import UnmodeledConstruct, expand_alias_columns

    tree = sqlglot.parse_one(sql, read="postgres")
    if any(a.args.get("columns") for a in tree.find_all(exp.TableAlias)):
        try:
            sql = expand_alias_columns(tree, columns).sql(dialect="postgres")
        except UnmodeledConstruct:
            pass
    if route == "bigquery":
        return corpora.to_bigquery(sql)
    return sqlglot.transpile(sql, read="postgres", write=route, unsupported_level=sqlglot.ErrorLevel.RAISE)[0]


class _Timeout(Exception):
    pass


def _alarm(signum, frame):  # noqa: ARG001
    raise _Timeout()


def prove(left: str, right: str, workload: Workload) -> dict:
    """``{"status": proven | unknown | unsupported | timeout | error, "route", "reason"}`` for one statement pair."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    constraints = prover_constraints(workload)
    reasons, statuses = [], []
    for route in PROOF_ROUTES:
        try:
            a, b = _converted(left, route, workload.columns), _converted(right, route, workload.columns)
        except Exception as error:  # noqa: BLE001
            reasons.append(f"{route}: unsupported: cannot convert ({str(error)[:80]})")
            statuses.append("unsupported")
            continue
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(PROOF_TIMEOUT_S)
        try:
            result = prove_equivalent_algebraic(
                a, b, schema=workload.columns, constraints=constraints, types=workload.types,
                compare_names=False, dialect=route, exact_arithmetic=True,
            )
        except _Timeout:
            reasons.append(f"{route}: timeout")
            statuses.append("timeout")
            continue
        except Exception as error:  # noqa: BLE001 - a crash is a failure to prove, never a proof
            reasons.append(f"{route}: crash: {type(error).__name__}: {str(error)[:80]}")
            statuses.append("error")
            continue
        finally:
            signal.alarm(0)
        if result.proven:
            return {"status": "proven", "route": route, "reason": result.reason[:160]}
        reason = result.reason[:160]
        reasons.append(f"{route}: {reason}")
        statuses.append("unsupported" if reason.startswith(("unsupported", "parse error")) else "unknown")
    for status in ("unknown", "timeout", "error"):
        if status in statuses:
            return {"status": status, "route": None, "reason": " | ".join(reasons)}
    return {"status": "unsupported", "route": None, "reason": " | ".join(reasons)}


# Columns the generated database names differently from Logos' DDL (DSB's tpcds.sql keeps an older name)
DATA_RENAMES = {"tpcds": {"c_last_review_date_sk": "c_last_review_date"}}


def _duck(sql: str, database: str | None = None) -> str:
    from kumosql.counterexample import to_duckdb

    text = to_duckdb(sql, "postgres")
    renames = DATA_RENAMES.get(database or "", {})
    if renames and any(old in text for old in renames):
        tree = sqlglot.parse_one(text, read="duckdb")
        for column in tree.find_all(exp.Column):
            if column.name.lower() in renames:
                column.set("this", exp.to_identifier(renames[column.name.lower()]))
        text = tree.sql(dialect="duckdb")
    return text


def _bag(rows) -> Counter:
    from kumosql.counterexample import _bag as bag

    return bag(rows)


def _without_root_limit(sql: str) -> str | None:
    tree = sqlglot.parse_one(sql, read="duckdb")
    if not any(tree.args.get(k) is not None for k in ("limit", "offset", "fetch")):
        return None
    for key in ("order", "limit", "offset", "fetch"):
        tree.set(key, None)
    return tree.sql(dialect="duckdb")


def _run(con, sql: str, timeout: float = QUERY_TIMEOUT_S):
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def real_data(left: str, right: str, database: str) -> dict:
    """Both queries on the generated benchmark database: ``same``, ``different``, ``ties`` (rows differ only
    under a root LIMIT), ``unconfirmed`` (different, but the unoptimized run did not finish or disagrees),
    ``no_data``, ``error`` or ``timeout``."""

    import duckdb

    path = corpora.BENCH_DIR / f"{database}.duckdb"
    if not path.exists():
        return {"status": "no_data"}
    from kumosql.duckdb_load import run_unoptimized

    try:
        left_sql, right_sql = _duck(left, database), _duck(right, database)
    except sqlglot.errors.SqlglotError as error:
        return {"status": "error", "detail": f"cannot translate: {str(error)[:100]}"}
    con = duckdb.connect(str(path), read_only=True, config={"threads": 1, "memory_limit": "2GB"})
    try:
        try:
            a, b = _bag(_run(con, left_sql)), _bag(_run(con, right_sql))
        except duckdb.Error as error:
            status = "timeout" if "INTERRUPT" in str(error).upper() else "error"
            return {"status": status, "detail": str(error)[:120]}
        record = {"status": "same", "rows": [sum(a.values()), sum(b.values())]}
        if a == b:
            return record
        stripped = [_without_root_limit(left_sql), _without_root_limit(right_sql)]
        if any(stripped):
            left_sql, right_sql = stripped[0] or left_sql, stripped[1] or right_sql
            try:
                a, b = _bag(_run(con, left_sql)), _bag(_run(con, right_sql))
            except duckdb.Error as error:
                return {"status": "unconfirmed", "detail": f"without LIMIT: {str(error)[:100]}"}
            if a == b:
                return {**record, "status": "ties"}
        try:
            timer = threading.Timer(QUERY_TIMEOUT_S, con.interrupt)
            timer.start()
            try:
                plain = [_bag(rows) for rows in run_unoptimized(con, left_sql, right_sql)]
            finally:
                timer.cancel()
        except duckdb.Error as error:
            return {**record, "status": "unconfirmed", "detail": f"unoptimized run: {str(error)[:100]}"}
        if plain != [a, b]:
            return {**record, "status": "unconfirmed", "detail": "the unoptimized run returns other rows"}
        only_left, only_right = a - b, b - a
        return {
            **record,
            "status": "different",
            "only_left": [list(map(str, r)) for r in list(only_left)[:2]],
            "only_right": [list(map(str, r)) for r in list(only_right)[:2]],
        }
    finally:
        con.close()


def small_databases(left: str, right: str, workload: Workload) -> dict:
    """Small databases built for the pair: ``different`` (with the database), ``none`` or ``rejected``."""

    from kumosql import counterexample as cx

    spec = counterexample_spec(workload)
    found = cx.find_counterexample(spec, left, right, dialect="postgres", trials=TRIALS)
    if found is False:
        return {"status": "rejected"}
    if found is None:
        return {"status": "none"}
    return {
        "status": "different",
        "database": {t: [list(map(str, r)) for r in rows] for t, rows in found.tables.items() if rows},
        "left_rows": [list(map(str, r)) for r in found.left_rows[:3]],
        "right_rows": [list(map(str, r)) for r in found.right_rows[:3]],
    }


def outcome(record: dict) -> str:
    """One outcome per case from its statements' checks (every statement pair must be proved)."""

    parts = record["statements"]
    differs = any(p["data"]["status"] == "different" or p["small"]["status"] == "different" for p in parts)
    if all(p["proof"]["status"] == "proven" for p in parts):
        return "wrong" if differs else "proven"
    if differs:
        return "refuted"
    for status in ("timeout", "error"):
        if any(p["proof"]["status"] == status for p in parts):
            return status
    if any(p["proof"]["status"] == "unsupported" for p in parts) and not any(p["proof"]["status"] == "unknown" for p in parts):
        return "unsupported"
    return "unknown"


def run_case(case: Case, root: Path | None = None) -> dict:
    root = root or core_root()
    folder, database = FAMILIES[case.family]
    workload = load_workload(root / folder / "create_tables.sql")
    started = time.perf_counter()
    parts = []
    for left, right in case.statements:
        part = {"proof": prove(left, right, workload)}
        part["data"] = real_data(left, right, database)
        try:
            part["small"] = small_databases(left, right, workload)
        except Exception as error:  # noqa: BLE001
            part["small"] = {"status": "error", "detail": f"{type(error).__name__}: {str(error)[:100]}"}
        parts.append(part)
    record = {
        "id": case.id, "family": case.family, "rule": case.rule, "held_out": case.held_out,
        "adapted": bool(case.adaptations), "adaptations": case.adaptations, "statements": parts,
    }
    record["outcome"] = outcome(record)
    record["seconds"] = round(time.perf_counter() - started, 1)
    return record


def _worker(case: Case) -> dict:
    import logging

    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    try:
        return run_case(case)
    except Exception as error:  # noqa: BLE001
        return {"id": case.id, "family": case.family, "rule": case.rule, "held_out": case.held_out, "adapted": bool(case.adaptations),
                "adaptations": case.adaptations, "statements": [], "outcome": "error", "detail": f"{type(error).__name__}: {error}"[:200]}


def run(cases: list[Case], jobs: int = 1) -> list[dict]:
    if jobs <= 1:
        return [_worker(c) for c in cases]
    with ProcessPoolExecutor(jobs) as pool:
        return list(pool.map(_worker, cases))


# ---------------------------------------------------------------- reporting


def summary(records: list[dict]) -> dict:
    counts = Counter(r["outcome"] for r in records)
    refuted = counts["refuted"]
    data = Counter()
    for r in records:
        statuses = [p["data"]["status"] for p in r["statements"]]
        data["different" if "different" in statuses else statuses[0] if statuses and len(set(statuses)) == 1 else "mixed"] += 1
    return {
        "total": len(records),
        "scored": len(records) - refuted,
        "counts": dict(counts),
        "data": dict(data),
        "small_refuted": sum(any(p["small"]["status"] == "different" for p in r["statements"]) for r in records),
    }


def line(records: list[dict]) -> str:
    s = summary(records)
    c = Counter(s["counts"])
    return (
        f"{c['proven']}/{s['scored']} proved ({s['total']} pairs), {c['refuted']} refuted (label failures), "
        f"{c['unknown']} unknown, {c['unsupported']} unsupported, {c['timeout']} timeout, {c['error']} error, {c['wrong']} wrong"
    )


def show(records: list[dict]) -> None:
    for r in sorted(records, key=lambda r: r["id"]):
        mark = " (held out)" if r["held_out"] else ""
        print(f"{r['id']:28} {r['rule']:38} {r['outcome']:11} {r.get('seconds', '')}{mark}")
        for p in r["statements"]:
            print(f"    proof {p['proof']['status']}: {p['proof']['reason'][:150]}")
            print(f"    data {p['data']['status']} {p['data'].get('detail', '')}  small {p['small']['status']}")


# ---------------------------------------------------------------- overlap


WETUNE_COMMIT = "f99ee9ea0a1a4aa37d2a6f29f120fdaa92809bd4"  # WeTune/WeTune-code head on 2026-10-03
OVERLAP_FILES = {  # Logos' copies (fetched at the pin; VeriEQL is CC BY-NC-SA, so never committed) and their upstreams
    "verieql-literature": "benchmarks/core/verieql/literature/literature-rewrite.jsonlines",
    "verieql-calcite": "benchmarks/core/verieql/calcite/calcite2.jsonlines",
    "wetune-issues": "benchmarks/core/wetune/issues/issues.tsv",
}


def _download(url: str, path: Path) -> Path:
    import urllib.request

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        partial = path.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(path)
    return path


def overlap() -> dict:
    """Logos' VeriEQL and WeTune folders against the files KumoSQL's VeriEQL and WeTune evals read, by text.

    Texts are compared with whitespace, quotes and case ignored. A Logos pair that matches nothing is
    reported with the index of the most similar upstream pair.
    """

    import difflib

    import verieql_bench

    def squash(sql: str) -> str:
        return re.sub(r"[\s;`\"]", "", sql).lower()

    cache = corpora.BENCH_DIR / "logos-overlap"
    logos = {k: _download(f"https://raw.githubusercontent.com/WindOctober/Logos/{COMMIT}/{p}", cache / COMMIT[:12] / Path(p).name) for k, p in OVERLAP_FILES.items()}
    out = {}
    for suite in ("literature", "calcite"):
        upstream = [json.loads(l)["pair"] for l in verieql_bench.fetch(verieql_bench.SUITES[suite][0]).read_text(encoding="utf-8").splitlines() if l.strip()]
        ours = [json.loads(l)["pair"] for l in logos[f"verieql-{suite}"].read_text(encoding="utf-8").splitlines() if l.strip()]
        index = {tuple(squash(q) for q in pair): i for i, pair in enumerate(upstream)}
        unmatched = {}
        for number, pair in enumerate(ours):
            if tuple(squash(q) for q in pair) not in index:
                text = " || ".join(pair)
                unmatched[number] = max(range(len(upstream)), key=lambda i: difflib.SequenceMatcher(None, text, " || ".join(upstream[i])).ratio())
        out[f"verieql-{suite}"] = {"logos": len(ours), "upstream": len(upstream), "identical": len(ours) - len(unmatched), "nearest_upstream_of_the_others": unmatched}
    issues = _download(f"https://raw.githubusercontent.com/WeTune/WeTune-code/{WETUNE_COMMIT}/wtune_data/issues/issues", cache / f"wetune-{WETUNE_COMMIT[:12]}.tsv")
    upstream = {(r[0], squash(r[4]), squash(r[5])) for r in (l.split("\t") for l in issues.read_text(encoding="utf-8").splitlines()) if len(r) >= 6}
    ours = [(r[0], squash(r[4]), squash(r[5])) for r in (l.split("\t") for l in logos["wetune-issues"].read_text(encoding="utf-8").splitlines()) if len(r) >= 6]
    out["wetune-issues"] = {"logos": len(ours), "upstream": len(upstream), "identical": sum(r in upstream for r in ours)}
    return out


# ---------------------------------------------------------------- results files


def results_rows(records: list[dict], date: str, commit: str) -> dict[str, dict]:
    s = summary(records)
    c = Counter(s["counts"])
    held = [r for r in records if r["held_out"]]
    hc = Counter(r["outcome"] for r in held)
    adapted = [r for r in records if r["adapted"]]
    ac = Counter(r["outcome"] for r in adapted)
    families = {f: Counter(r["outcome"] for r in records if r["family"] == f) for f in FAMILIES}
    by_family = "; ".join(f"{f} {fc['proven']}/{sum(fc.values()) - fc['refuted']} proved, {fc['refuted']} refuted" for f, fc in families.items())
    coverage = {k: c[k] for k in ("proven", "refuted", "unknown", "unsupported", "timeout", "error") if c[k]}
    common = {
        "size": s["total"],
        "docs": "docs/evals/logos.md",
        "command": "python tools/logos_bench.py --jobs 4",
        "date": date,
        "held_out": f"{hc['proven']}/{len(held) - hc['refuted']} proved, {hc['refuted']} refuted, {hc['wrong']} wrong ({len(held)} held-out pairs)",
    }
    caveats = (
        f"Logos {COMMIT[:12]}; query text fetched at run time, never committed. By family: {by_family}. "
        f"{len(adapted)} TPC-DS variant pairs are adapted (TOP n read as LIMIT n, date + n days as an interval): "
        f"{ac['proven']} proved, {ac['refuted']} refuted among them. TPC-H data is scale 0.1 (tpchgen-cli); TPC-DS data is scale 1 "
        "from DSB's dsdgen, whose distributions differ from the official kit's. The TPC-H schema declares no keys, so proofs hold "
        f"over every database with its NOT NULL columns. Prover routes time out after {PROOF_TIMEOUT_S} s of wall-clock time, so "
        f"a loaded machine can turn a few proofs into timeouts. {BASELINE} Measured on master {commit}; no prover change was made for this eval."
    )
    return {
        "logos-core-proof": {
            "suite": "Logos TPC-H, DSB, TPC-DS pairs (proofs)",
            "order": 22,
            "score": f"{c['proven']}/{s['scored']} proved, {c['wrong']} wrong",
            "metric": "Pairs proved equivalent by the algebraic prover over every database meeting the declared NOT NULL columns and keys; pairs a database shows different (label failures) leave the denominator.",
            "evidence": "proof",
            "correctness": f"{c['wrong']} false proofs: every proof's two queries return the same rows on the generated TPC-H or TPC-DS data and on {TRIALS} small random databases plus the targeted suite",
            "coverage": coverage,
            "caveats": caveats,
            **common,
        },
        "logos-core-executed": {
            "suite": "Logos TPC-H, DSB, TPC-DS pairs (on data)",
            "order": 23,
            "score": f"{c['refuted']}/{s['total']} shown different, {c['wrong']} wrong",
            "metric": "Calcite (R-Bot) outputs and TPC-DS variants that return different rows from their source query on a database: the generated benchmark data or a small constraint-respecting database. They are label failures, kept as negatives that must never be proved.",
            "evidence": "executed",
            "correctness": "Every difference repeats with DuckDB's optimizer off (run_unoptimized); a small database's difference also survives row shuffles; under a root LIMIT the rows differ with the LIMIT removed too",
            "coverage": {"refuted": c["refuted"], "unknown": s["total"] - c["refuted"]},
            "caveats": f"{s['small_refuted']} pairs are separated by a small database, {s['data'].get('different', 0)} on the generated data. " + caveats,
            **common,
            "held_out": f"{hc['refuted']}/{len(held)} shown different, {hc['wrong']} wrong",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("what", nargs="?", default="run", choices=("run", "overlap"))
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--family", choices=tuple(FAMILIES))
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--show", action="store_true", help="print every case's checks")
    parser.add_argument("--json", type=Path, help="write every case's record here")
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args(argv)
    from bench_common import quiet, today, write_results

    quiet()
    if args.what == "overlap":
        print(json.dumps(overlap(), indent=2))
        return 0
    try:
        root = core_root()
    except DataUnavailable as error:
        print(error)
        return 2
    cases = load_cases(root)
    if args.split != "all":
        cases = [c for c in cases if c.held_out == (args.split == "held-out")]
    if args.family:
        cases = [c for c in cases if c.family == args.family]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    records = run(cases, args.jobs)
    if args.show:
        show(records)
    print(f"logos {line(records)}")
    if args.json:
        args.json.write_text(json.dumps(records, indent=1) + "\n", encoding="utf-8")
    if args.write_results:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        for name, row in results_rows(records, today(), head).items():
            write_results(name, row)
    return 1 if any(r["outcome"] == "wrong" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
