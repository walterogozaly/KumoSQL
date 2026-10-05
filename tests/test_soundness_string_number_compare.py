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

# (left, right): the comparison reads differently on the engines, so neither prover may call the pair equivalent.
# An exact integer string against an integer is read as a number on MySQL, DuckDB and PostgreSQL (see FOLDED) and
# still declined on BigQuery, where it is a type error.
BIGQUERY_DECLINED = [
    ("SELECT t.a FROM t WHERE '2' <> 2", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' = 2", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' < 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE 2 <> '2'", "SELECT t.a FROM t"),
]
DECLINED = [
    ("SELECT t.a FROM t WHERE '2' BETWEEN 1 AND 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' IN (2, 3)", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE WHEN '2.5' = 2 THEN 1 ELSE 0 END = 0", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE '2' WHEN 2 THEN FALSE ELSE TRUE END", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE NULLIF('2', 2) IS NULL", "SELECT t.a FROM t WHERE FALSE"),
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
    ("SELECT t.a FROM t WHERE t.a = 1 AND t.a = 2", "SELECT t.a FROM t WHERE FALSE"),
]

# two different strings against one untyped column: proven where the column must be text, not on MySQL, where an integer
# column reads both as 0 (tests/test_untyped_string_columns.py)
TEXT_ONLY = [("SELECT t.a FROM t WHERE t.a = 'x' AND t.a = 'y'", "SELECT t.a FROM t WHERE FALSE")]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", DECLINED + [("SELECT t.a FROM t WHERE '2' <> CAST(2 AS INT)", "SELECT t.a FROM t")])
def test_string_versus_number_is_not_proven(prover, dialect, left, right):
    result = prover(left, right, dialect=dialect)
    assert not result.proven, (dialect, left, result.reason)
    if "CAST" not in left:  # a cast number is a literal's twin once folded: refuted, never proven
        assert result.status is SmtStatus.NOT_PROVEN


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", BIGQUERY_DECLINED + [("SELECT t.a FROM t WHERE CASE WHEN '2' = 2 THEN 1 ELSE 0 END = 0", "SELECT t.a FROM t")])
def test_bigquery_keeps_declining_a_string_against_a_number(prover, left, right):
    result = prover(left, right, dialect="bigquery")
    assert result.status is SmtStatus.NOT_PROVEN, (left, result.reason)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", PROVEN)
def test_same_kind_comparisons_stay_proven(prover, dialect, left, right):
    assert prover(left, right, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT, (dialect, left)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", TEXT_ONLY)
def test_different_strings_against_an_untyped_column(prover, dialect, left, right):
    assert (prover(left, right, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT) == (dialect != "mysql")
    assert prover(left, right, dialect=dialect, types={"t": {"a": "VARCHAR"}}).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("prover", PROVERS)
def test_declared_types_count(prover, dialect):
    numeric = {"t": {"a": "INT"}}
    text = {"t": {"a": "VARCHAR"}}
    # an exact integer string is read as a number outside BigQuery (see test_exact_integer_strings_read_as_numbers)
    exact = prover("SELECT t.a FROM t WHERE t.a = '2' AND t.a = 3", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=numeric).proven
    assert exact == (dialect != "bigquery")
    assert not prover("SELECT t.a FROM t WHERE t.a = '2.5' AND t.a = 3", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=numeric).proven
    assert not prover("SELECT t.a FROM t WHERE t.a = 2 AND t.a = 'x'", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=text).proven
    # the declared type of a string column agrees with a string literal
    assert prover("SELECT t.a FROM t WHERE t.a = 'x' AND t.a = 'y'", "SELECT t.a FROM t WHERE FALSE", dialect=dialect, types=text).proven


@pytest.mark.parametrize("dialect", ("mysql", "duckdb", "postgres"))
@pytest.mark.parametrize("prover", PROVERS)
def test_the_same_converted_comparison_on_both_sides_stays_proven(prover, dialect):
    """A semijoin turned into a join does not depend on how the engine converts ``sal + 1 = job``."""

    types = {"emp": {"deptno": "INT", "sal": "INT", "job": "VARCHAR"}}
    schema = {"emp": ["deptno", "sal", "job"]}
    left = "SELECT e.deptno FROM emp AS e WHERE e.deptno IN (SELECT d.deptno FROM emp AS d WHERE d.sal + 1 = d.job)"
    right = "SELECT e.deptno FROM emp AS e JOIN (SELECT DISTINCT d.deptno FROM emp AS d WHERE d.sal + 1 = d.job) AS x ON e.deptno = x.deptno"
    assert prover(left, right, dialect=dialect, schema=schema, types=types).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("dialect", DIALECTS)
def test_converted_comparison_is_never_refuted(dialect):
    """The result of the conversion is unknown to the prover, so a model of it is not a counterexample."""

    result = prove_equivalent_algebraic(
        "SELECT t.a FROM t WHERE '2.5' = 2", "SELECT t.a FROM t WHERE 2 = '2.5'", dialect=dialect, search_counterexample=True
    )
    assert result.status is SmtStatus.NOT_PROVEN


def test_problem_names_the_comparison():
    assert string_number_compare.problem("SELECT a FROM t WHERE '2' <> 2", "mysql")
    assert string_number_compare.problem("SELECT a FROM t WHERE a = 'abc' AND a = 0", "duckdb")
    assert string_number_compare.problem("SELECT a FROM t WHERE a = 2 AND b = 'x'", "bigquery") is None
    assert string_number_compare.problem("SELECT a FROM t WHERE a IN (SELECT b FROM u)", "bigquery") is None
    # a plain comparison is left to the SMT prover's opaque reading; the other forms are still declined
    assert string_number_compare.problem("SELECT a FROM t WHERE '2' <> 2", "mysql", plain_ok=True) is None
    assert string_number_compare.problem("SELECT a FROM t WHERE '2' IN (2)", "mysql", plain_ok=True)
    assert string_number_compare.problem("SELECT a FROM t WHERE a = 'abc' AND a = 0", "mysql", plain_ok=True)
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
