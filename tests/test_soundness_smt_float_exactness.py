"""FLOAT64 rounding the SMT encoding must keep in comparisons and literals.

The SMT value sort is one exact rational sort, so two roundings were lost:

* A comparison of an INT64 operand with a FLOAT64 one converts the INT64 side first, which rounds past
  2**53: ``a = b AND b = c`` holds for ``a = 2**53``, ``b = 2**53``, ``c = 2**53 + 1`` although
  ``a = c`` does not. With declared types the INT64 side now reads as ``CAST(.. AS FLOAT64)``.
* A decimal or exponent literal outside the normal FLOAT64 range is not its exact value: ``1e-324`` and
  ``2e-324`` are both 0, ``1e400`` overflows. And an exponent literal is DOUBLE in DuckDB and MySQL even
  though their plain decimals are exact, so ``1e-1 + 2e-1`` is not ``3e-1`` there.

Each wrong pair returns different rows in DuckDB (whose DOUBLE and BIGINT round as FLOAT64 and INT64 do)
on the database next to it; the near misses were proven before the fix and stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import MIXED_NUMERIC_ASSUMPTION, prove_equivalent_smt

PROVERS = pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])
BIG = 2**53  # 9007199254740992; 2**53 + 1 rounds to it as FLOAT64
MIXED_DDL = "CREATE TABLE mixed (a BIGINT, b DOUBLE, c BIGINT)"
MIXED_ROWS = [(BIG, float(BIG), BIG + 1), (BIG + 1, float(BIG), BIG)]
MIXED_SCHEMA = {"mixed": ["a", "b", "c"]}
MIXED_TYPES = {"mixed": {"a": "INT64", "b": "FLOAT64", "c": "INT64"}}
INT_TYPES = {"mixed": {"a": "INT64", "b": "INT64", "c": "INT64"}}
FLOAT_TYPES = {"mixed": {"a": "FLOAT64", "b": "FLOAT64", "c": "FLOAT64"}}


def _bags_differ(left: str, right: str, ddl: str, rows: list, dialect: str = "bigquery") -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute(ddl)
    table = ddl.split()[2]
    db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    left, right = (sqlglot.transpile(sql, read=dialect, write="duckdb")[0] for sql in (left, right))
    left_rows, right_rows = run_unoptimized(db, left, right)
    return Counter(left_rows) != Counter(right_rows)


COMPARED_THROUGH_FLOAT = [
    pytest.param(
        "SELECT a FROM mixed WHERE a = b AND b = c", "SELECT a FROM mixed WHERE a = b AND b = c AND a = c", id="F5-transitivity"
    ),
    pytest.param("SELECT a AS v FROM mixed WHERE a = b", "SELECT b AS v FROM mixed WHERE a = b", id="equal-to-a-float-column"),
    pytest.param(
        "SELECT a FROM mixed WHERE a IN (b) AND c BETWEEN b AND b", "SELECT a FROM mixed WHERE a = c AND a IN (b)", id="in-and-between"
    ),
    pytest.param(
        "SELECT a FROM mixed WHERE CASE b WHEN a THEN TRUE END AND c = b",
        "SELECT a FROM mixed WHERE a = b AND c = a",
        id="simple-case-operand",
    ),
    pytest.param(
        "SELECT a FROM mixed m WHERE m.a IN (SELECT n.b FROM mixed n WHERE n.b = m.c)",
        "SELECT a FROM mixed m WHERE m.a IN (SELECT n.b FROM mixed n WHERE n.b = m.c) AND m.a = m.c",
        id="in-subquery",
    ),
]


@pytest.mark.parametrize("left,right", COMPARED_THROUGH_FLOAT)
@PROVERS
def test_declared_float_comparison_converts_the_int64_side(left, right, prove):
    assert _bags_differ(left, right, MIXED_DDL, MIXED_ROWS)
    assert not prove(left, right, schema=MIXED_SCHEMA, types=MIXED_TYPES).proven


@PROVERS
def test_undeclared_comparison_states_the_mixed_numeric_assumption(prove):
    # Without types a, b and c may all be INT64 (or all FLOAT64), where the pair is equivalent: proven, but
    # only under the stated assumption that compared values have the same numeric type.
    result = prove(*COMPARED_THROUGH_FLOAT[0].values, schema=MIXED_SCHEMA)
    assert result.proven
    assert MIXED_NUMERIC_ASSUMPTION in result.assumptions


BIG_FLOAT_LITERAL = (
    "SELECT a FROM mixed WHERE a = 1e16 AND c = 1e16",
    "SELECT a FROM mixed WHERE a = 1e16 AND c = 1e16 AND a = c",
)


@pytest.mark.parametrize("types", [None, MIXED_TYPES], ids=["undeclared", "declared"])
@PROVERS
def test_a_float_literal_past_2_53_converts_the_int64_side(types, prove):
    # 10**16 + 1 rounds to the FLOAT64 literal 1e16, so a and c both equal it without being equal.
    assert _bags_differ(*BIG_FLOAT_LITERAL, MIXED_DDL, [(10**16, 0.0, 10**16 + 1)])
    assert not prove(*BIG_FLOAT_LITERAL, schema=MIXED_SCHEMA, types=types).proven


LITERAL_DDL = "CREATE TABLE t (a BIGINT, x DOUBLE)"
LITERAL_ROWS = [(1, 0.5), (2, None)]
LITERAL_SCHEMA = {"t": ["a", "x"]}
ROUNDED_LITERALS = [
    pytest.param("SELECT a FROM t WHERE 1e-324 < 2e-324", "SELECT a FROM t", "bigquery", id="F8-underflow-to-zero"),
    pytest.param("SELECT a FROM t WHERE 5e-324 < 7e-324", "SELECT a FROM t", "bigquery", id="same-subnormal"),
    pytest.param("SELECT a FROM t WHERE 1e400 > 1e399", "SELECT a FROM t", "bigquery", id="overflow-to-infinity"),
    pytest.param("SELECT a FROM t WHERE 1e-1 + 2e-1 = 3e-1", "SELECT a FROM t", "duckdb", id="duckdb-exponent-arithmetic"),
    pytest.param("SELECT a FROM t WHERE 1e-1 + 2e-1 = 3e-1", "SELECT a FROM t", "mysql", id="mysql-exponent-arithmetic"),
]


@pytest.mark.parametrize("left,right,dialect", ROUNDED_LITERALS)
@PROVERS
def test_rounded_float_literals_are_not_exact(left, right, dialect, prove):
    assert _bags_differ(left, right, LITERAL_DDL, LITERAL_ROWS, dialect)
    assert not prove(left, right, schema=LITERAL_SCHEMA, dialect=dialect).proven


STILL_PROVEN = [
    pytest.param(
        "SELECT a FROM mixed WHERE a = b AND b = c", "SELECT a FROM mixed WHERE a = b AND b = c AND a = c", INT_TYPES, "bigquery",
        id="int64-transitivity",
    ),
    pytest.param(
        "SELECT a FROM mixed WHERE a = b AND b = c", "SELECT a FROM mixed WHERE a = b AND b = c AND a = c", FLOAT_TYPES, "bigquery",
        id="float64-transitivity",
    ),
    pytest.param(
        "SELECT a FROM mixed WHERE a = b AND b = 1.5", "SELECT a FROM mixed WHERE a = 1.5 AND b = 1.5", FLOAT_TYPES, "bigquery",
        id="float64-columns-equal-a-literal",
    ),
    pytest.param(
        "SELECT a FROM mixed WHERE a > 1.5 AND a > 1", "SELECT a FROM mixed WHERE a > 1.5", MIXED_TYPES, "bigquery",
        id="int64-against-a-small-float-literal",
    ),
    pytest.param("SELECT a FROM mixed WHERE 0.5 < 2.0", "SELECT a FROM mixed", None, "bigquery", id="representable-literals"),
    pytest.param("SELECT a FROM mixed WHERE 1e-300 < 2e-300", "SELECT a FROM mixed", None, "bigquery", id="normal-range-exponents"),
    pytest.param("SELECT a FROM mixed WHERE 0.1 + 0.2 = 0.3", "SELECT a FROM mixed", None, "duckdb", id="duckdb-exact-decimals"),
    pytest.param("SELECT a FROM mixed WHERE 1e-1 + 2e-1 = 3e-1", "SELECT a FROM mixed", None, "postgres", id="postgres-numeric-exponents"),
]


@pytest.mark.parametrize("left,right,types,dialect", STILL_PROVEN)
@PROVERS
def test_near_misses_stay_proven(left, right, types, dialect, prove):
    assert prove(left, right, schema=MIXED_SCHEMA, types=types, dialect=dialect).proven


def test_bigquery_float_arithmetic_on_literals_is_not_computed():
    # 0.1 + 0.2 is 0.30000000000000004 in FLOAT64, so the WHERE is FALSE; the prover leaves the sum uninterpreted.
    left, right = "SELECT a FROM t WHERE 0.1 + 0.2 = 0.3", "SELECT a FROM t"
    assert _bags_differ("SELECT a FROM t WHERE 0.1::DOUBLE + 0.2::DOUBLE = 0.3::DOUBLE", right, LITERAL_DDL, LITERAL_ROWS, "duckdb")
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        assert not prove(left, right, schema=LITERAL_SCHEMA).proven, prove.__name__
