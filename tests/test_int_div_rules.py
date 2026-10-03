"""``a DIV b`` of non-negative integer literals folds to its quotient; nothing else does."""

import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.int_div_rules import fold_literal_int_div

SCHEMA = {"emp": ["empno", "ename"]}
TYPES = {"emp": {"empno": "INTEGER", "ename": "VARCHAR(10)"}}


def _fold(sql):
    tree = sqlglot.parse_one(sql, read="mysql")
    out = fold_literal_int_div(tree)
    return out.sql(dialect="mysql") if out is not None else None


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="mysql", compare_names=False, exact_arithmetic=True).proven


def _differ_on_duckdb(left, right, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE emp (empno INTEGER, ename VARCHAR)")
    insert_rows(db, "emp", rows)
    a, b = run_unoptimized(db, sqlglot.transpile(left, read="mysql", write="duckdb")[0], sqlglot.transpile(right, read="mysql", write="duckdb")[0])
    return sorted(a) != sorted(b)


def test_literal_division_folds():
    assert _fold("SELECT empno + (10 DIV 2) FROM emp") == "SELECT empno + 5 FROM emp"
    assert _fold("SELECT 10 DIV 3, 0 DIV 7, (20 DIV 2) DIV 5 FROM emp") == "SELECT 3, 0, 2 FROM emp"


def test_division_that_depends_on_rounding_or_input_stays():
    assert _fold("SELECT -10 DIV 3 FROM emp") is None  # -3 truncated, -4 floored
    assert _fold("SELECT 10 DIV -3 FROM emp") is None
    assert _fold("SELECT 10 DIV 0 FROM emp") is None  # NULL in MySQL, an error elsewhere
    assert _fold("SELECT 10.5 DIV 2, '10' DIV 2, empno DIV 2 FROM emp") is None
    assert _fold("SELECT 10 / 4 FROM emp") is None


def test_cast_of_folded_sum_matches_plain_sum():
    left = "SELECT * FROM emp WHERE CAST((empno + (10 DIV 2)) AS INTEGER) = 13"
    assert _proven(left, "SELECT * FROM emp WHERE (empno + 5) = 13")
    assert _proven("SELECT * FROM emp WHERE empno + (10 DIV 3) = 13", "SELECT * FROM emp WHERE empno + 3 = 13")


def test_wrong_quotient_is_not_proven():
    left, right = "SELECT * FROM emp WHERE empno + (10 DIV 3) = 13", "SELECT * FROM emp WHERE empno + 4 = 13"
    assert not _proven(left, right)
    assert _differ_on_duckdb(left, right, [(9, "a"), (10, "b")])


def test_div_is_not_real_division():
    left, right = "SELECT * FROM emp WHERE empno + (10 DIV 4) = 12", "SELECT * FROM emp WHERE empno + 10 / 4 = 12"
    assert not _proven(left, right)
    assert _differ_on_duckdb(left, right, [(10, "a")])


def test_negative_division_is_not_floored():
    left, right = "SELECT * FROM emp WHERE empno + (-10 DIV 3) = 0", "SELECT * FROM emp WHERE empno - 4 = 0"
    assert not _proven(left, right)
    assert _differ_on_duckdb(left, right, [(3, "a"), (4, "b")])
