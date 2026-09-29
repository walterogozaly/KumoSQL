"""Differential check of the SMT prover against SQLite on random databases.

Every proof must hold on random instances, and every counterexample must
really separate the two queries. The generated subset (integers, NULLs,
joins, filters, GROUP BY) has the same semantics in SQLite and BigQuery.
"""

from collections import Counter
import random
import sqlite3

import pytest

pytest.importorskip("z3")

from bq_sql_tools.smt_equivalence import SmtStatus, prove_equivalent_smt


def _atom(rng, cols):
    col = rng.choice(cols)
    roll = rng.random()
    if roll < 0.4:
        return f"{col} {rng.choice(['=', '<>', '<', '<=', '>', '>='])} {rng.randint(0, 3)}"
    if roll < 0.7:
        return f"{col} {rng.choice(['=', '<', '>='])} {rng.choice(cols)}"
    if roll < 0.85:
        return f"{col} IS {rng.choice(['', 'NOT '])}NULL"
    return f"{col} IN ({rng.randint(0, 3)}, {rng.randint(0, 3)})"


def _pred(rng, cols, depth=2):
    if depth == 0 or rng.random() < 0.3:
        return _atom(rng, cols)
    op = rng.choice(["AND", "OR", "NOT"])
    if op == "NOT":
        return f"NOT ({_pred(rng, cols, depth - 1)})"
    return f"({_pred(rng, cols, depth - 1)}) {op} ({_pred(rng, cols, depth - 1)})"


def _query(rng):
    if rng.random() < 0.5:
        cols = ["x.a", "x.b", "y.a", "y.b"]
        source = f"t AS x JOIN {rng.choice(['t', 'u'])} AS y ON {_pred(rng, cols, 1)}"
    else:
        cols = ["a", "b"]
        source = "t"
    key = rng.choice(cols)
    if rng.random() < 0.3:
        aggregate = rng.choice(["COUNT(*)", f"SUM({cols[1]})", f"MIN({cols[0]})", f"COUNT({cols[1]})"])
        return f"SELECT {key} AS k, {aggregate} AS v FROM {source} WHERE {_pred(rng, cols)} GROUP BY {key}"
    distinct = "DISTINCT " if rng.random() < 0.2 else ""
    return f"SELECT {distinct}{key} AS k FROM {source} WHERE {_pred(rng, cols)}"


_REWRITES = [
    ("AND", "OR"),
    (" < ", " <= "),
    ("NOT (", "("),
    ("COUNT(*)", "COUNT(b)"),
    ("x.a", "y.a"),
    (" = ", " >= "),
    ("t AS x JOIN u", "u AS x JOIN t"),
    ("WHERE ", "WHERE TRUE AND "),
]


def _database(rng, rows=None):
    db = sqlite3.connect(":memory:")
    for table in ("t", "u"):
        db.execute(f"CREATE TABLE {table} (a INTEGER, b INTEGER)")
        if rows is None:
            data = [[rng.choice([None, 0, 1, 2, 3]) for _ in range(2)] for _ in range(rng.randint(0, 4))]
        else:
            data = [[row.get("a"), row.get("b")] for row in rows.get(table, [])]
        db.executemany(f"INSERT INTO {table} VALUES (?, ?)", data)
    return db


def _run(db, sql):
    return Counter(db.execute(sql).fetchall())


def test_prover_agrees_with_sqlite():
    rng = random.Random(20260929)
    seen = Counter()
    for _ in range(250):
        left = _query(rng)
        if rng.random() < 0.7:
            old, new = rng.choice(_REWRITES)
            right = left.replace(old, new, 1)
        else:
            right = _query(rng)
        result = prove_equivalent_smt(left, right)
        seen[result.status] += 1
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            for _ in range(25):
                db = _database(rng)
                assert _run(db, left) == _run(db, right), (left, right)
        elif result.status is SmtStatus.NOT_EQUIVALENT:
            db = _database(rng, result.counterexample.tables)
            assert _run(db, left) != _run(db, right), (left, right, result.counterexample)
    assert seen[SmtStatus.PROVEN_EQUIVALENT] > 50
    assert seen[SmtStatus.NOT_EQUIVALENT] > 20
