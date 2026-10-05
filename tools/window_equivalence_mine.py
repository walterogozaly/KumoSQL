"""Mine window-function query pairs from the DuckDB sqllogictest suite for ``tools/window_equivalence_bench.py``.

``python tools/window_equivalence_bench.py --make-slt-cases`` runs this and rewrites ``benchmarks/window_equivalence/slt_cases.jsonl``.
The suites are read through ``tools/engine_suites.py`` (a sparse clone in ``~/.cache/kumosql-suites``); the queries and the rows of
the tables they read are copied into the cases file (DuckDB's tests are MIT licensed). The SQLite sqllogictest corpus is scanned
too and holds no window query.

For every ``query`` record that uses ``OVER`` and reads small tables (at most 30 rows, five column types) the miner keeps the query only if
KumoSQL's BigQuery reading of it runs on DuckDB and returns what the source's own run returns. Pairs per query:

* ``slt-rule``: the query against the output of KumoSQL's canonical rewrite pipeline, when that changes the query and still agrees on the data;
* ``slt-wrap``: the query against ``SELECT * FROM (query)`` and against the query behind an unused CTE (equivalent by construction);
* ``slt-mutant``: the query against a copy with one window clause changed (a sort direction, a frame, ``ROW_NUMBER`` for ``RANK``,
  ``LAG`` for ``LEAD``, a dropped ``PARTITION BY``), kept only when the query returns the same rows in every row order and the mutant
  returns other rows on the source's data: a proven difference, labelled ``not_equivalent``.
"""

from __future__ import annotations

from collections import Counter
import json
import logging
from pathlib import Path
import random
import re
import sys
import time

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

MAX_ROWS = 30
PER_FILE = 2
TOTAL = 96
FILE_SECONDS = 90
KINDS = {"BIGINT": "INT64", "INTEGER": "INT64", "SMALLINT": "INT64", "TINYINT": "INT64", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64", "VARCHAR": "STRING", "BOOLEAN": "BOOL", "DATE": "DATE"}
OVER = re.compile(r"\bOVER\b", re.IGNORECASE)


def _tables_read(tree: exp.Expression) -> list[str]:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted({t.name for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes and not t.db})


def _fixture(connection, names: list[str]) -> dict | None:
    tables, constraints = {}, {}
    for name in names:
        try:
            described = connection.execute(f'DESCRIBE "{name}"').fetchall()
            count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except Exception:
            return None
        if count == 0 or count > MAX_ROWS:
            return None
        columns = []
        for column, kind, *_ in described:
            base = kind.split("(")[0].upper()
            if base not in KINDS:
                return None
            columns.append([column, KINDS[base]])
        rows = [list(r) for r in connection.execute(f'SELECT * FROM "{name}"').fetchall()]
        if any(isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))) for row in rows for v in row):
            return None
        tables[name] = {"columns": columns, "rows": [[v.isoformat() if hasattr(v, "isoformat") else v for v in row] for row in rows]}
    return {"tables": tables, "constraints": constraints}


def _mutants(tree: exp.Expression) -> list[tuple[str, exp.Expression]]:
    """One-clause changes of the first window of a query."""

    out = []
    windows = list(tree.find_all(exp.Window))
    if not windows:
        return out
    for index, window in enumerate(windows[:2]):
        def fresh():
            copy = tree.copy()
            return list(copy.find_all(exp.Window))[index]

        order = window.args.get("order")
        if order is not None and order.expressions:
            w = fresh()
            first = w.args["order"].expressions[0]
            first.set("desc", not first.args.get("desc"))
            first.set("nulls_first", None)
            out.append(("flip the sort direction of the window's ORDER BY", w.root()))
        if window.args.get("partition_by"):
            w = fresh()
            w.set("partition_by", None)
            out.append(("drop PARTITION BY", w.root()))
        func = window.this
        swaps = {exp.RowNumber: "RANK", exp.Rank: "ROW_NUMBER", exp.Lag: "LEAD", exp.Lead: "LAG"}
        for kind, name in swaps.items():
            if isinstance(func, kind):
                w = fresh()
                if kind in (exp.Lag, exp.Lead):
                    replacement = exp.Lead(this=w.this.this, offset=w.this.args.get("offset"), default=w.this.args.get("default")) if kind is exp.Lag else exp.Lag(
                        this=w.this.this, offset=w.this.args.get("offset"), default=w.this.args.get("default"))
                else:
                    replacement = exp.Rank() if kind is exp.RowNumber else exp.RowNumber()
                w.set("this", replacement)
                out.append((f"{type(func).__name__} for {name}", w.root()))
        spec = window.args.get("spec")
        if spec is not None and order is not None:
            w = fresh()
            s = w.args["spec"]
            s.set("kind", "RANGE" if str(s.args.get("kind")).upper() == "ROWS" else "ROWS")
            out.append(("ROWS for RANGE in the frame", w.root()))
    return out


def mine_file(relative: str, seed: int = 5) -> tuple[list[dict], dict]:
    """The cases of one test file (run in its own process: some DuckDB tests crash the interpreter)."""

    import engine_suites as es
    import window_equivalence_bench as W
    from kumosql import apply_rules
    from kumosql.rewrite import canonical_rule_order

    root = es.fetch_suite("duckdb-slt")
    revision = es.checkout_revision(root)
    rng = random.Random(f"{seed}{relative}")
    stats: Counter = Counter()
    started = time.time()
    records, skipped = es.parse_slt((root / relative).read_text(encoding="utf-8", errors="ignore"))
    if skipped:
        return [], {"file skipped": 1}
    connection = es._connect()
    taken = 0
    file_cases: list[dict] = []
    try:
        for record in records:
            if record.skip:
                continue
            if record.kind == "statement":
                try:
                    es._run(connection, record.sql)
                except Exception:
                    pass
                continue
            if taken >= PER_FILE or time.time() - started > FILE_SECONDS or not record.sql or not OVER.search(record.sql) or es.NONDETERMINISTIC.search(record.sql):
                continue
            tree = es._single_query(record.sql, "duckdb")
            if tree is None or not isinstance(tree, exp.Select):
                stats["not a single select"] += 1
                continue
            names = _tables_read(tree)
            if not names:
                stats["reads no table"] += 1
                continue
            fixture = _fixture(connection, names)
            if fixture is None:
                stats["table too big or typed oddly"] += 1
                continue
            try:
                bigquery = tree.sql(dialect="bigquery")
                native_columns, native_rows = es._run(connection, tree.sql(dialect="duckdb"))
            except Exception:
                stats["translation or run failed"] += 1
                continue
            if native_columns is None or re.search(r"ROW_NUMBER\(\s*\S", bigquery):  # DuckDB's ROW_NUMBER(ORDER BY ..) is not GoogleSQL
                continue
            made = _pairs(W, apply_rules, canonical_rule_order, fixture, bigquery, native_rows, es, rng)
            if made is None:
                stats["does not run as BigQuery or disagrees with DuckDB"] += 1
                continue
            taken += 1
            stem = relative.removeprefix("test/sql/").removesuffix(".test").replace("/", ".")
            counter: Counter = Counter()
            for label, origin, family, left, right, tie, why in made:
                kind = origin.removeprefix("slt-")
                counter[kind] += 1
                file_cases.append({
                    "id": f"slt-{stem}-L{record.line}-{kind}-{counter[kind]}", "source": "slt", "family": family, "fixture": fixture, "left": left,
                    "right": right, "label": label, "tie_dependent": tie, "why": why, "origin": origin,
                    "licence": "MIT (DuckDB test suite, Copyright Stichting DuckDB Foundation)", "dialect": "bigquery", "executable": True, "witness": None,
                    "detail": {"suite": "duckdb-slt", "revision": revision, "file": relative, "line": record.line, "sql": record.sql},
                })
    finally:
        connection.close()
    return file_cases, dict(stats)


def mine(seed: int = 5) -> list[dict]:
    import subprocess

    import engine_suites as es

    root = es.fetch_suite("duckdb-slt")
    files = sorted(
        str(p.relative_to(root)) for p in (root / "test" / "sql").rglob("*.test")
        if (any(part in p.parts for part in ("window", "qualify")) or "window" in p.name) and OVER.search(p.read_text(encoding="utf-8", errors="ignore"))
    )
    random.Random(seed).shuffle(files)  # the budget below stops early; a seeded shuffle keeps the choice of files independent of their names
    cases: list[dict] = []
    stats: Counter = Counter()
    for relative in files:
        if len({(c["detail"]["file"], c["detail"]["line"]) for c in cases}) >= TOTAL // 2:
            break  # about two cases per query: enough queries for the budget
        try:
            done = subprocess.run([sys.executable, __file__, "--file", relative], capture_output=True, text=True, timeout=300)
            payload = json.loads(done.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
            stats["file crashed or timed out"] += 1
            print(f"{relative}: crashed or timed out", flush=True)
            continue
        cases.extend(payload["cases"])
        stats.update(payload["stats"])
        stats["files read"] += 1
        print(f"{relative}: {len(payload['cases'])} cases ({len(cases)} so far)", flush=True)
    rng = random.Random(seed)
    # keep whole queries together: group by query, then trim to the budget
    by_query: dict[tuple, list[dict]] = {}
    for case in cases:
        by_query.setdefault((case["detail"]["file"], case["detail"]["line"]), []).append(case)
    kept: list[dict] = []
    for key in sorted(by_query, key=hash_key):
        if len(kept) >= TOTAL:
            break
        kept.extend(by_query[key])
    del rng
    print(dict(stats), f"{len(by_query)} queries, {len(kept)} cases kept of {len(cases)}")
    return sorted(kept, key=lambda c: c["id"])


def hash_key(key: tuple) -> str:
    import hashlib

    return hashlib.sha256(repr(key).encode()).hexdigest()


def _pairs(W, apply_rules, canonical_rule_order, fixture, bigquery, native_rows, es, rng):
    import itertools

    runner = W.Runner(fixture)
    try:
        rows = {t: spec["rows"] for t, spec in fixture["tables"].items()}
        runner.load(rows)
        try:
            base = runner.bag(bigquery)
        except Exception:
            return None
        if es.multiset(base) != es.multiset(native_rows):
            return None
        # permutation stability: the same rows in six shuffled orders
        stable = True
        for trial in range(6):
            shuffled = {t: rng.sample(r, len(r)) for t, r in rows.items()}
            runner.load(shuffled)
            if runner.bag(bigquery) != base:
                stable = False
                break
        runner.load(rows)

        def agrees(sql: str) -> bool:
            try:
                runner.load(rows)
                return runner.bag(sql) == base
            except Exception:
                return False

        made = []
        try:
            treated = apply_rules(canonical_rule_order(), bigquery).sql
        except Exception:
            treated = None
        if treated and sqlglot.parse_one(treated, read="bigquery") != sqlglot.parse_one(bigquery, read="bigquery") and agrees(treated):
            made.append(("equivalent", "slt-rule", "pipeline", bigquery, treated, not stable,
                         "The query against the output of KumoSQL's rewrite pipeline; the rules are sound and the two agree on the source's rows (not itself a proof)."))
        for name, wrapped in (
            ("subquery", f"SELECT * FROM ({bigquery}) AS _s"),
            ("unused-cte", f"WITH _u AS (SELECT 1 AS z) {bigquery}"),
        ):
            if agrees(wrapped):
                made.append(("equivalent", "slt-wrap", "wrap", bigquery, wrapped, not stable,
                             f"The query against itself wrapped as a {name.replace('-', ' ')}: the same rows by construction."))
        if stable:
            tree = sqlglot.parse_one(bigquery, read="bigquery")
            for description, mutant in _mutants(tree):
                try:
                    text = mutant.sql(dialect="bigquery")
                    runner.load(rows)
                    other = runner.bag(text)
                except Exception:
                    continue
                if other != base:
                    made.append(("not_equivalent", "slt-mutant", "mutant", bigquery, text, False,
                                 f"One clause changed ({description}); the query returns the same rows in every row order and the mutant returns other rows on the source's data."))
                    break
        return made
    finally:
        runner.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--file"]:  # worker: one file, JSON on the last line of stdout
        cases, stats = mine_file(argv[1])
        print(json.dumps({"cases": cases, "stats": stats}))
        return 0
    cases = mine()
    path = ROOT / "benchmarks" / "window_equivalence" / "slt_cases.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    print(f"wrote {len(cases)} cases to {path.relative_to(ROOT)}", dict(Counter(c["origin"] for c in cases)), dict(Counter(c["label"] for c in cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
