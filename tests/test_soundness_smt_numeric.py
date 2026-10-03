"""Numeric coercions the SMT encoding must keep (S008-004, S008-005, S008-006).

A FLOAT64 operand converts an INT64 value to FLOAT64, which rounds it past 2**53: ``x * 1e0`` and
``COALESCE(x, CAST(1 AS FLOAT64))`` return 9007199254740992.0 for ``x = 9007199254740993``, so neither
is ``x``. A CASE, IF, COALESCE, NULLIF or set operation converts every branch to the common type even
when the FLOAT64 branch is never taken. Each wrong pair below returns different rows in DuckDB on
the database next to it; the near misses keep the same types (or are equivalent anyway) and stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import EXACT_ARITHMETIC_ASSUMPTION, MIXED_NUMERIC_ASSUMPTION, prove_equivalent_smt

SCHEMA = {"t": ["x", "y"]}
DDL = "CREATE TABLE t (x BIGINT, y BIGINT)"
BIG = 2**53 + 1  # 9007199254740993: the nearest FLOAT64 is 9007199254740992
ROWS = [(BIG, BIG), (BIG, 1), (None, None)]
NOT_NULL = "WHERE x IS NOT NULL"


def _bags_differ(left: str, right: str) -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute(DDL)
    db.executemany("INSERT INTO t VALUES (?, ?)", ROWS)
    left_rows, right_rows = run_unoptimized(db, left, right)
    return Counter(left_rows) != Counter(right_rows)


def _duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


# (left, right, DuckDB spelling of left when transpiling would change its types)
WRONG_PROOFS = [
    pytest.param("SELECT x * 1e0 AS v FROM t", "SELECT x AS v FROM t", None, id="S008-004-times-float-one"),
    pytest.param(
        f"SELECT COALESCE(x, CAST(1 AS FLOAT64)) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", None,
        id="S008-005-coalesce-float-fallback",
    ),
    pytest.param(
        f"SELECT CASE WHEN x IS NOT NULL THEN x ELSE CAST(1 AS FLOAT64) END AS v FROM t {NOT_NULL}",
        f"SELECT x AS v FROM t {NOT_NULL}",
        None,
        id="S008-006-case-float-branch",
    ),
    # BigQuery's 1.0 and 2.5 are FLOAT64 (DuckDB's are DECIMAL, so the witness spells them as DOUBLE).
    pytest.param("SELECT x * 1.0 AS v FROM t", "SELECT x AS v FROM t", "SELECT x * 1.0::DOUBLE AS v FROM t", id="times-bigquery-decimal-one"),
    pytest.param(
        f"SELECT IF(x IS NULL, 2.5, x) AS v FROM t {NOT_NULL}",
        f"SELECT x AS v FROM t {NOT_NULL}",
        f"SELECT CASE WHEN x IS NULL THEN 2.5::DOUBLE ELSE x END AS v FROM t {NOT_NULL}",
        id="if-float-literal-branch",
    ),
    pytest.param(
        f"SELECT COALESCE(x, y * 1e0) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", None,
        id="coalesce-float-arithmetic-fallback",
    ),
    pytest.param(
        f"SELECT x FROM t {NOT_NULL} UNION ALL SELECT CAST(y AS FLOAT64) FROM t WHERE FALSE",
        f"SELECT x FROM t {NOT_NULL}",
        None,
        id="union-all-with-an-empty-float-branch",
    ),
    pytest.param(
        f"SELECT x FROM t {NOT_NULL} UNION DISTINCT SELECT CAST(y AS FLOAT64) FROM t WHERE FALSE",
        f"SELECT DISTINCT x FROM t {NOT_NULL}",
        None,
        id="union-distinct-with-an-empty-float-branch",
    ),
]


@pytest.mark.parametrize("left,right,duckdb_left", WRONG_PROOFS)
@pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
def test_pairs_that_round_are_never_proven(left, right, duckdb_left, prove):
    assert _bags_differ(duckdb_left or _duckdb(left), _duckdb(right))
    assert not prove(left, right, schema=SCHEMA).proven


@pytest.mark.parametrize("left,right,duckdb_left", WRONG_PROOFS[1:3] + WRONG_PROOFS[4:])
def test_branch_coercion_is_not_covered_by_exact_arithmetic(left, right, duckdb_left):
    # Exact arithmetic is about + - and *; a CASE or set operation still converts its branches.
    assert not prove_equivalent_smt(left, right, schema=SCHEMA, exact_arithmetic=True).proven


def test_exact_arithmetic_states_the_assumption_that_covers_a_float_factor():
    result = prove_equivalent_smt("SELECT x * 1e0 AS v FROM t", "SELECT x AS v FROM t", schema=SCHEMA, exact_arithmetic=True)
    assert result.proven
    assert EXACT_ARITHMETIC_ASSUMPTION in result.assumptions


STILL_PROVEN = [
    pytest.param("SELECT x * 1 AS v FROM t", "SELECT x AS v FROM t", "bigquery", id="times-integer-one"),
    pytest.param("SELECT 1 * x AS v FROM t", "SELECT x AS v FROM t", "bigquery", id="integer-one-times"),
    pytest.param("SELECT x * (2 - 1) AS v FROM t", "SELECT x AS v FROM t", "bigquery", id="times-folded-integer-one"),
    pytest.param("SELECT x * 1.0 AS v FROM t", "SELECT x AS v FROM t", "postgres", id="times-exact-decimal-one"),
    pytest.param(f"SELECT COALESCE(x, 1) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", "bigquery", id="coalesce-integer-fallback"),
    pytest.param(
        f"SELECT CASE WHEN x IS NOT NULL THEN x ELSE 1 END AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", "bigquery",
        id="case-integer-branches",
    ),
    pytest.param(f"SELECT IF(x IS NULL, 2, x) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", "bigquery", id="if-integer-branch"),
    pytest.param("SELECT CASE WHEN x > 0 THEN x ELSE x END AS v FROM t", "SELECT x AS v FROM t", "bigquery", id="case-same-branch"),
    pytest.param(
        f"SELECT COALESCE(x, 1.5) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", "postgres", id="coalesce-exact-decimal-fallback",
    ),
    pytest.param(
        f"SELECT COALESCE(CAST(x AS FLOAT64), 1.5) AS v FROM t {NOT_NULL}", f"SELECT CAST(x AS FLOAT64) AS v FROM t {NOT_NULL}", "bigquery",
        id="coalesce-of-float-branches",
    ),
    pytest.param("SELECT COALESCE(x, 1.5) AS v FROM t", "SELECT IFNULL(x, 1.5) AS v FROM t", "bigquery", id="coalesce-is-ifnull"),
    pytest.param(
        "SELECT COALESCE(x, 1.5) AS v FROM t", "SELECT CASE WHEN x IS NULL THEN 1.5 ELSE x END AS v FROM t", "bigquery",
        id="coalesce-is-case",
    ),
    pytest.param(
        f"SELECT x FROM t {NOT_NULL} UNION ALL SELECT 1 FROM t WHERE FALSE", f"SELECT x FROM t {NOT_NULL}", "bigquery",
        id="union-all-with-an-empty-integer-branch",
    ),
    pytest.param(
        "SELECT x FROM t UNION ALL SELECT y * 1.5 FROM t", "SELECT x FROM t WHERE TRUE UNION ALL SELECT y * 1.5 FROM t", "bigquery",
        id="union-all-with-a-float-branch-matches-itself",
    ),
]


@pytest.mark.parametrize("left,right,dialect", STILL_PROVEN)
@pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
def test_same_type_near_misses_stay_proven(left, right, dialect, prove):
    assert prove(left, right, schema=SCHEMA, dialect=dialect).proven


def test_duckdb_converts_to_an_exponent_literal_but_not_to_a_decimal_one():
    # DuckDB reads 1e0 as DOUBLE and 1.5 as an exact DECIMAL that every BIGINT fits.
    left, right = f"SELECT COALESCE(x, 1e0) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}"
    assert _bags_differ(left, right)
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        assert not prove(left, right, schema=SCHEMA, dialect="duckdb").proven, prove.__name__
    left = f"SELECT COALESCE(x, 1.5) AS v FROM t {NOT_NULL}"
    assert not _bags_differ(left, right)
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        assert prove(left, right, schema=SCHEMA, dialect="duckdb").proven, prove.__name__


# x INT64 and y FLOAT64: the common type of a CASE, COALESCE or union column holding both is FLOAT64.
TYPED_DDL = "CREATE TABLE t (x BIGINT, y DOUBLE)"
TYPES = {"t": {"x": "INT64", "y": "FLOAT64"}}
UNDECLARED_MIXING = [
    pytest.param(f"SELECT COALESCE(x, y) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", id="coalesce-two-columns"),
    pytest.param(
        f"SELECT CASE WHEN x IS NULL THEN y ELSE x END AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", id="case-two-columns"
    ),
    pytest.param(
        f"SELECT x FROM t {NOT_NULL} UNION ALL SELECT y FROM t WHERE FALSE", f"SELECT x FROM t {NOT_NULL}", id="union-two-columns"
    ),
]


@pytest.mark.parametrize("left,right", UNDECLARED_MIXING)
@pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
def test_declared_types_convert_an_int64_branch(left, right, prove):
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute(TYPED_DDL)
    db.executemany("INSERT INTO t VALUES (?, ?)", [(BIG, 1.0), (None, None)])
    left_rows, right_rows = run_unoptimized(db, _duckdb(left), _duckdb(right))
    assert Counter(left_rows) != Counter(right_rows)
    assert not prove(left, right, schema=SCHEMA, types=TYPES).proven


@pytest.mark.parametrize("left,right", UNDECLARED_MIXING)
@pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
def test_undeclared_mixing_is_proven_under_a_stated_assumption(left, right, prove):
    result = prove(left, right, schema=SCHEMA)
    assert result.proven
    assert MIXED_NUMERIC_ASSUMPTION in result.assumptions


NO_MIXING = [
    pytest.param(f"SELECT COALESCE(x, 1) AS v FROM t {NOT_NULL}", f"SELECT x AS v FROM t {NOT_NULL}", None, id="literal-fallback"),
    pytest.param("SELECT CASE WHEN x > 0 THEN x ELSE x END AS v FROM t", "SELECT x AS v FROM t", None, id="same-column-branches"),
    pytest.param(f"SELECT COALESCE(x, y) AS v FROM t {NOT_NULL}", f"SELECT COALESCE(x, y) AS v FROM t {NOT_NULL} AND TRUE", {"t": {"x": "INT64", "y": "INT64"}}, id="declared-same-type"),
    pytest.param("SELECT x FROM t UNION ALL SELECT x FROM t", "SELECT x FROM t UNION ALL SELECT x FROM t WHERE TRUE", None, id="union-same-column"),
]


@pytest.mark.parametrize("left,right,types", NO_MIXING)
@pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
def test_no_mixing_adds_no_assumption(left, right, types, prove):
    result = prove(left, right, schema=SCHEMA, types=types)
    assert result.proven
    assert MIXED_NUMERIC_ASSUMPTION not in result.assumptions
