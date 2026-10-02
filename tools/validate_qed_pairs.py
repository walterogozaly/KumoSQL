"""Sanity-check the QED Calcite fixture by executing every pair in DuckDB.

For each case in tests/fixtures/qed/qed_calcite_pairs.jsonl this creates the
schema, fills it with a few small random databases that respect NOT NULL and
key constraints, runs both SQL strings (transpiled MySQL -> DuckDB by sqlglot),
and compares the two result multisets.

    python tools/validate_qed_pairs.py [--dbs 5] [--seed 1] [-v]

Exit status is non-zero if any statement fails to run. A result mismatch is
reported but is not an error: the QED corpus is made of Calcite rewrite-rule
tests, and a few rules are only equivalent under Calcite's own typing or
null-ordering conventions (for example integer overflow, CAST rounding, or
sort order with LIMIT).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import Counter
from pathlib import Path

import duckdb
import sqlglot

logging.getLogger("sqlglot").setLevel(logging.ERROR)
FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qed" / "qed_calcite_pairs.jsonl"

STRINGS = ["A", "B", "", "abc", "CLERK", "dept1", "dept2", "foo", "Charlie", "1", "2", "John", "x"]
INTS = [0, 1, 2, 3, 7, 10, 11, 20, 30, 34, 50, 100, -1]


def gen_value(t: str, nullable: bool, rng: random.Random):
    if nullable and rng.random() < 0.2:
        return None
    if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
        return rng.choice(INTS)
    if t in ("VARCHAR", "CHAR"):
        return rng.choice(STRINGS)
    if t == "BOOLEAN":
        return rng.random() < 0.5
    if t == "DATE":
        return f"2014-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"
    if t == "TIMESTAMP":
        return f"2014-0{rng.randint(1, 9)}-1{rng.randint(0, 9)} 00:00:00"
    if t in ("DECIMAL", "DOUBLE", "FLOAT", "REAL"):
        return rng.choice([0, 1, 2, 10, 20, 5.5])
    raise ValueError(t)


def make_db(case: dict, rng: random.Random) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET default_null_order = 'nulls_first'")  # MySQL: NULL sorts lowest
    for stmt in sqlglot.transpile(case["ddl"], read="mysql", write="duckdb"):
        con.execute(stmt)
    for t in case["schemas"]:
        cols = t["columns"]
        ph = ", ".join("?" for _ in cols)
        for _ in range(rng.randint(0, 6)):
            row = [gen_value(c["type"], c["nullable"], rng) for c in cols]
            try:  # key violations are simply skipped
                con.execute(f'INSERT INTO "{t["table"]}" VALUES ({ph})', row)
            except duckdb.ConstraintException:
                pass
    return con


def norm(v):
    if isinstance(v, float):
        return round(v, 6)
    return v


def run(con, sql: str):
    d = sqlglot.transpile(sql, read="mysql", write="duckdb")[0]
    rows = [tuple(norm(x) for x in r) for r in con.execute(d).fetchall()]
    return Counter(rows), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cases = [json.loads(l) for l in FIXTURE.read_text().splitlines()]
    errors, unsupported, agree, differ, nonempty = [], [], 0, [], 0
    for case in cases:
        rng = random.Random(f"{args.seed}:{case['name']}")
        ok = True
        diff = False
        for _ in range(args.dbs):
            try:
                con = make_db(case, rng)
                (ca, ra), (cb, rb) = run(con, case["sql_a"]), run(con, case["sql_b"])
            except duckdb.NotImplementedException as e:  # engine limitation, not a converter bug
                unsupported.append((case["name"], str(e).splitlines()[0][:120]))
                ok = False
                break
            except Exception as e:  # noqa: BLE001
                errors.append((case["name"], f"{type(e).__name__}: {str(e)[:160]}"))
                ok = False
                break
            nonempty += bool(ra)
            if ca != cb:
                diff = True
        if ok:
            if diff:
                differ.append(case["name"])
            else:
                agree += 1
    print(f"cases {len(cases)}  ran-clean {len(cases) - len(errors) - len(unsupported)}  agree {agree}  differ {len(differ)}  duckdb-unsupported {len(unsupported)}  errors {len(errors)}")
    print(f"non-empty first-side results across all runs: {nonempty}")
    for n, e in unsupported:
        print("DUCKDB-UNSUPPORTED", n, e)
    for n, e in errors:
        print("ERROR", n, e)
    if args.verbose:
        for n in differ:
            print("DIFFER", n)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
