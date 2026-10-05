"""Lifting a nested membership test out of ``x IN (SELECT c ..)`` needs ``x`` and ``c`` to have one type.

``x IN (SELECT c FROM t WHERE p AND c IN q)`` was read as ``x IN (SELECT c FROM t WHERE p) AND x IN q``: the tests
written for ``c`` moved to ``x``. They are the same tests only while ``x`` and ``c`` agree on the type: ``IN``
compares a FLOAT64 with an INT64 as doubles, so an integer test (``c = 9007199254740993``) still holds exactly
for ``c`` but, written for the double ``x``, also accepts the neighbouring integer ``9007199254740992``.
DuckDB, with the optimizer on and off, returns different rows for the pair below. The near miss lifts the same
tests off the same column of the same table, where the type cannot differ.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import TableConstraints

COLUMNS = {"t": ["id", "f", "x"], "u": ["k", "w"], "p": ["id"]}
TYPES = {"t": {"id": "INT64", "f": "FLOAT64", "x": "INT64"}, "u": {"k": "INT64", "w": "INT64"}, "p": {"id": "INT64"}}
DDL = [
    "CREATE TABLE t(id BIGINT, f DOUBLE, x BIGINT)",
    "CREATE TABLE u(k BIGINT, w BIGINT)",
    "CREATE TABLE p(id BIGINT)",
    "INSERT INTO t VALUES (1, 9007199254740992.0, 1), (2, 1.0, 2), (3, NULL, NULL)",
    "INSERT INTO u VALUES (1, 9007199254740992), (2, 1), (3, 3)",
    "INSERT INTO p VALUES (1), (3), (9007199254740993)",
]


def prove(left, right):
    return prove_equivalent_algebraic(
        left,
        right,
        schema=COLUMNS,
        types=TYPES,
        constraints={name: TableConstraints() for name in COLUMNS},
        compare_names=False,
        dialect="bigquery",
    ).proven


def rows(sql):
    db = duckdb.connect()
    for statement in DDL:
        db.execute(statement)
    return Counter(run_unoptimized(db, sql)[0]), Counter(db.execute(sql).fetchall())


def test_a_test_on_an_integer_column_does_not_move_to_a_float_column():
    left = "SELECT t.id FROM t WHERE t.f IN (SELECT u.w FROM u WHERE u.w = 9007199254740993)"
    right = "SELECT t.id FROM t WHERE t.f IN (SELECT u.w FROM u) AND t.f = 9007199254740993"
    assert not prove(left, right)
    (off_left, on_left), (off_right, on_right) = rows(left), rows(right)
    assert off_left != off_right and on_left != on_right


def test_a_nested_test_does_not_move_to_a_column_of_another_table():
    left = "SELECT t.id FROM t WHERE t.f IN (SELECT u.w FROM u WHERE u.w IN (SELECT p.id FROM p))"
    right = "SELECT t.id FROM t WHERE t.f IN (SELECT u.w FROM u) AND t.f IN (SELECT p.id FROM p)"
    assert not prove(left, right)
    (off_left, on_left), (off_right, on_right) = rows(left), rows(right)
    assert off_left != off_right and on_left != on_right


def test_the_same_column_of_the_same_table_still_lifts():
    left = "SELECT t.id FROM t WHERE t.x IN (SELECT t2.x FROM t AS t2 WHERE t2.x > 0 AND t2.x IN (SELECT p.id FROM p))"
    right = "SELECT t.id FROM t WHERE t.x IN (SELECT t2.x FROM t AS t2 WHERE t2.x > 0) AND t.x IN (SELECT p.id FROM p)"
    assert prove(left, right)
    assert rows(left)[0] == rows(right)[0]
