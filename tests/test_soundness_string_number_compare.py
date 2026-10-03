"""A string compared with a number is not "never equal".

``WHERE '2' <> 2`` was proven equal to the query without the filter because the provers call values of different kinds
unequal. MySQL compares a string with a number as numbers, DuckDB and PostgreSQL cast the string, and BigQuery rejects the
comparison, so no engine returns every row. The provers decline such pairs; near misses that compare two numbers or two
strings stay proven. See ``kumosql.string_number_compare``.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")

from kumosql import string_number_compare
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import prove_equivalent_smt, SmtStatus

DIALECTS = ("mysql", "duckdb", "bigquery", "postgres")
PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)

# (left, right): the comparison reads differently on the engines, so neither prover may call the pair equivalent
DECLINED = [
    ("SELECT t.a FROM t WHERE '2' <> 2", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' = 2", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' < 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE 2 <> '2'", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' BETWEEN 1 AND 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' IN (2, 3)", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE WHEN '2' = 2 THEN 1 ELSE 0 END = 0", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE '2' WHEN 2 THEN FALSE ELSE TRUE END", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE NULLIF('2', 2) IS NULL", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' <> 1 + 1", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' <> CAST(2 AS INT)", "SELECT t.a FROM t"),
    # the same column against a string and a number: 'abc' reads as 0 on MySQL, so a = 0 satisfies both
    ("SELECT t.a FROM t WHERE t.a = 'abc' AND t.a = 0", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE t.a = '2.0' AND t.a = 2", "SELECT t.a FROM t WHERE FALSE"),
]

# a pair of numbers, or of strings, compares the same way everywhere
PROVEN = [
    ("SELECT t.a FROM t WHERE 2 <> 2", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' <> '2'", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' = '3'", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE 2 = 2", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE t.a = 'x' AND t.a = 'y'", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE t.a = 1 AND t.a = 2", "SELECT t.a FROM t WHERE FALSE"),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", DECLINED)
def test_string_versus_number_is_not_proven(prover, dialect, left, right):
    result = prover(left, right, dialect=dialect)
    assert not result.proven, (dialect, left, result.reason)
    assert result.status is SmtStatus.NOT_PROVEN


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", PROVEN)
def test_same_kind_comparisons_stay_proven(prover, dialect, left, right):
    assert prover(left, right, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT, (dialect, left)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
def test_declared_types_count(prover, dialect):
    numeric = {"t": {"a": "INT"}}
    text = {"t": {"a": "VARCHAR"}}
    assert not prover("SELECT t.a FROM t WHERE t.a = '2' AND t.a = 3", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=numeric).proven
    assert not prover("SELECT t.a FROM t WHERE t.a = 2 AND t.a = 'x'", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=text).proven
    # the declared type of a string column agrees with a string literal
    assert prover("SELECT t.a FROM t WHERE t.a = 'x' AND t.a = 'y'", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=text).proven


def test_problem_names_the_comparison():
    assert string_number_compare.problem("SELECT a FROM t WHERE '2' <> 2", "mysql")
    assert string_number_compare.problem("SELECT a FROM t WHERE a = 'abc' AND a = 0", "duckdb")
    assert string_number_compare.problem("SELECT a FROM t WHERE a = 2 AND b = 'x'", "bigquery") is None
    assert string_number_compare.problem("SELECT a FROM t WHERE a IN (SELECT b FROM u)", "bigquery") is None
    assert string_number_compare.problem("not sql at all (", "bigquery") is None


def test_witness_differs_on_duckdb():
    """The witness returns no rows on DuckDB, which casts the string, so the proof was wrong there too."""

    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1), (2)")
    left, right = run_unoptimized(db, "SELECT t.a FROM t WHERE '2' <> 2", "SELECT t.a FROM t")
    assert Counter(left) != Counter(right)
