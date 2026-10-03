"""Score KumoSQL on query pairs taken from real optimizer wrong-result bugs: none may be proved.

Each case in ``tests/fixtures/optimizer_bugs/cases.jsonl`` comes from a public bug report in which a
database's optimizer rewrote a query into one that returns different rows (Calcite, Spark,
CockroachDB, DuckDB, MySQL, ClickHouse). ``left`` is the query as written and ``right`` is the
rewrite the optimizer made, as SQL. The two differ on the case's own data, so the set is
**must-not-prove**: a proof is a soundness bug.

Each case gets one outcome:

1. **proven**: the algebraic prover (DuckDB dialect, with the keys and NOT NULL columns the setup
   declares) proved the pair. This is **wrong**.
2. **refuted**: the prover itself found the two queries differ.
3. **unknown**: anything else.

The harness also runs both queries on the case's data (DuckDB with its optimizer off, as
``kumosql.duckdb_load.run_unoptimized`` does, or SQLite for a pair DuckDB cannot run) and checks they
really differ; ``confirmed`` counts those. One case in five (by a hash of its id) is held out.

    python tools/optimizer_bugs_bench.py
    python tools/optimizer_bugs_bench.py --show proven,unknown
    python tools/optimizer_bugs_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "optimizer_bugs"
PROVER_TIMEOUT_MS = 10000


@dataclass(frozen=True)
class Case:
    id: str
    tracker: str
    source: str
    family: str
    setup: str
    data: dict
    left: str
    right: str
    engine: str
    note: str = ""

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"optimizer-bugs\n{self.id}".encode()).hexdigest(), 16) % 5 == 0


def load_cases(fixtures: Path = FIXTURES) -> list[Case]:
    return [Case(**json.loads(line)) for line in (fixtures / "cases.jsonl").read_text().splitlines() if line.strip()]


def declared(case: Case) -> tuple[dict[str, list[str]], dict]:
    """The setup's tables, their columns and the keys and NOT NULL columns it declares (CHECKs are ignored)."""

    from kumosql.smt_equivalence import TableConstraints

    schema, constraints = {}, {}
    for statement in sqlglot.parse(case.setup, read="duckdb") if case.setup else ():
        if not (isinstance(statement, exp.Create) and statement.kind == "TABLE"):
            continue
        table = statement.this.this.name
        columns, keys, not_null = [], [], set()
        for item in statement.this.expressions:
            if isinstance(item, exp.ColumnDef):
                columns.append(item.name)
                for constraint in item.args.get("constraints") or ():
                    kind = constraint.args.get("kind")
                    if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        keys.append((item.name,))
                        not_null.add(item.name)
                    elif isinstance(kind, exp.NotNullColumnConstraint):
                        not_null.add(item.name)
                    elif isinstance(kind, exp.UniqueColumnConstraint):
                        keys.append((item.name,))
            elif isinstance(item, exp.PrimaryKey):
                key = tuple(e.name for e in item.expressions)
                keys.append(key)
                not_null |= set(key)
        schema[table] = columns
        constraints[table] = TableConstraints(not_null=frozenset(not_null), keys=tuple(keys))
    return schema, constraints


def results(case: Case) -> tuple[list[tuple], list[tuple]]:
    """Both queries' rows on the case's data."""

    if case.engine == "sqlite":
        db = sqlite3.connect(":memory:")
        return db.execute(case.left).fetchall(), db.execute(case.right).fetchall()
    import duckdb
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    if case.setup:
        db.execute(case.setup)
    for table, rows in case.data.items():
        for row in rows:
            db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(row))})", row)
    left, right = run_unoptimized(db, case.left, case.right)
    return left, right


def differs(case: Case) -> bool:
    left, right = results(case)
    if "ORDER BY" in case.left.upper() and "ORDER BY" in case.right.upper():
        return left != right
    return Counter(left) != Counter(right)


def prove(case: Case) -> str:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    schema, constraints = declared(case)
    try:
        result = prove_equivalent_algebraic(
            case.left, case.right, schema=schema or None, constraints=constraints or None,
            compare_names=False, dialect="duckdb", timeout_ms=PROVER_TIMEOUT_MS,
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return "unknown"
    return {SmtStatus.PROVEN_EQUIVALENT: "proven", SmtStatus.NOT_EQUIVALENT: "refuted"}.get(result.status, "unknown")


def decide(case: Case) -> dict:
    outcome = prove(case)
    return {
        "id": case.id, "tracker": case.tracker, "family": case.family, "outcome": outcome,
        "confirmed": differs(case), "wrong": outcome == "proven", "held_out": case.held_out,
    }


def run(cases: list[Case]) -> list[dict]:
    return [decide(case) for case in cases]


def results_row(rows: list[dict]) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bench_common import today

    counts = Counter(r["outcome"] for r in rows)
    held = [r for r in rows if r["held_out"]]
    return {
        "suite": "Optimizer wrong-result bugs",
        "order": 38,
        "size": len(rows),
        "score": f"{counts['refuted']}/{len(rows)} refuted, {counts['proven']} proved, {sum(r['wrong'] for r in rows)} wrong",
        "metric": "Query pairs from public optimizer wrong-result bug reports (a query and the rewrite an optimizer made of it), which return different rows on the report's data: none may be proved; refuted means the prover itself found a difference.",
        "evidence": "executed",
        "correctness": "Every pair is checked to differ on its own data (DuckDB with the optimizer off, or SQLite), so any proof counts as wrong.",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown") if counts[k]},
        "held_out": f"{sum(r['outcome'] == 'refuted' for r in held)}/{len(held)} refuted, {sum(r['outcome'] == 'proven' for r in held)} proved",
        "docs": "docs/evals/optimizer-bugs.md",
        "command": "python tools/optimizer_bugs_bench.py --write-results",
        "date": today(),
        "caveats": "The pairs were collected by an outside research assistant and re-checked here; one was dropped (it ran a single query under two optimizer settings). Most rewrites are spelled out from the reports' plans, so they are SQL readings of a plan, not SQL the reporters wrote. Every case, held-out ones included, was seen while building the harness; no prover change was made for this eval.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", default="", help="comma-separated outcomes to print, e.g. proven,unknown")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args(argv)

    rows = run(load_cases())
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    shown = set(filter(None, args.show.split(",")))
    for row in rows:
        if row["outcome"] in shown or not row["confirmed"]:
            print(f"{row['id']:10} {row['tracker']:15} {row['outcome']:8} confirmed={row['confirmed']}  {row['family']}")
    row = results_row(rows)
    print(f"{row['score']}; confirmed {sum(r['confirmed'] for r in rows)}/{len(rows)}; held out {row['held_out']}")
    if args.write_results:
        path = ROOT / "benchmarks" / "results" / "optimizer-bugs.json"
        path.write_text(json.dumps(row, indent=2) + "\n")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
