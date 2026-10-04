"""IS [NOT] DISTINCT FROM next to another comparison, written without parentheses.

sqlglot parses ``a = b IS NOT DISTINCT FROM c`` as ``a = (b IS NOT DISTINCT FROM c)`` and ``a IS DISTINCT FROM b = c``
as ``(a IS DISTINCT FROM b) = c``; DuckDB and PostgreSQL read ``(a = b) IS NOT DISTINCT FROM c`` and
``a IS DISTINCT FROM (b = c)``. Both provers used to prove each text equal to the other grouping. Such text is now
declined; with parentheses it is modeled as before.
"""

import itertools

import pytest
import sqlglot

pytest.importorskip("z3")

from sqlglot_support import OLD_SQLGLOT

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct, canonical_negation, check_modeled
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import prove_equivalent_smt

SCHEMA = {"s": ["a", "b", "c", "d"]}
DIALECTS = ("duckdb", "postgres", "mysql", "bigquery")


def _bags_differ(left: str, right: str) -> bool:
    """Run both queries in DuckDB on every combination of TRUE, FALSE and NULL in s(a, b, c, d)."""

    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE s (a BOOLEAN, b BOOLEAN, c BOOLEAN, d BOOLEAN)")
    db.executemany("INSERT INTO s VALUES (?, ?, ?, ?)", list(itertools.product((None, True, False), repeat=4)))
    first, second = run_unoptimized(db, left, right)
    return sorted(first, key=repr) != sorted(second, key=repr)


def _where(condition: str) -> str:
    return f"SELECT * FROM s WHERE {condition}"


# The unparenthesized text, and the grouping sqlglot gave it (which the engines do not).
WRONG_PROOFS = [
    pytest.param("a = b IS NOT DISTINCT FROM c", "a = (b IS NOT DISTINCT FROM c)", id="eq-then-is-not-distinct"),
    pytest.param("a = b IS DISTINCT FROM c", "a = (b IS DISTINCT FROM c)", id="eq-then-is-distinct"),
    pytest.param("a <> b IS DISTINCT FROM c", "a <> (b IS DISTINCT FROM c)", id="neq-then-is-distinct"),
    pytest.param("a < b IS NOT DISTINCT FROM c", "a < (b IS NOT DISTINCT FROM c)", id="lt-then-is-not-distinct"),
    pytest.param("a IS NOT DISTINCT FROM b = c", "(a IS NOT DISTINCT FROM b) = c", id="is-not-distinct-then-eq"),
    pytest.param("a IS DISTINCT FROM b = c", "(a IS DISTINCT FROM b) = c", id="is-distinct-then-eq"),
    pytest.param("a IS DISTINCT FROM b IN (c)", "(a IS DISTINCT FROM b) IN (c)", id="is-distinct-then-in"),
    pytest.param("a IS DISTINCT FROM b BETWEEN c AND d", "(a IS DISTINCT FROM b) BETWEEN c AND d", id="is-distinct-then-between"),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("left,right", WRONG_PROOFS)
def test_unparenthesized_is_distinct_from_beside_a_comparison_is_never_proven(left, right, dialect):
    left, right = _where(left), _where(right)
    assert _bags_differ(left, right)
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, schema=SCHEMA, dialect=dialect).proven, prove.__name__


# Parenthesized in either grouping, the same comparisons are modeled and proved as before.
STILL_PROVEN = [
    pytest.param("a = (b IS NOT DISTINCT FROM c)", "a = (b IS NOT DISTINCT FROM c)", id="inner-grouping-itself"),
    pytest.param("(a = b) IS NOT DISTINCT FROM c", "(a = b) IS NOT DISTINCT FROM c", id="outer-grouping-itself"),
    pytest.param("a = (b IS NOT DISTINCT FROM c)", "(b IS NOT DISTINCT FROM c) = a", id="inner-grouping-commuted"),
    pytest.param("(a = b) IS NOT DISTINCT FROM c", "c IS NOT DISTINCT FROM (a = b)", id="outer-grouping-commuted"),
    pytest.param("(a = b) IS DISTINCT FROM c", "NOT ((a = b) IS NOT DISTINCT FROM c)", id="outer-grouping-negated"),
    pytest.param("a IS DISTINCT FROM (b = c)", "NOT ((b = c) IS NOT DISTINCT FROM a)", id="right-operand-grouped"),
    pytest.param("a IS NOT DISTINCT FROM b AND c", "c AND b IS NOT DISTINCT FROM a", id="beside-and"),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_parenthesized_is_distinct_from_stays_proven(left, right, dialect):
    left, right = _where(left), _where(right)
    assert not _bags_differ(left, right)
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert prove(left, right, schema=SCHEMA, dialect=dialect).proven, prove.__name__


def _declined(condition: str, dialect: str) -> bool:
    try:
        check_modeled(canonical_negation(sqlglot.parse_one(_where(condition), read=dialect)))
    except UnmodeledConstruct:
        return True
    except sqlglot.errors.ParseError:
        if OLD_SQLGLOT:  # sqlglot 26 cannot read the shape at all, which no prover models either
            return True
        raise
    return False


@pytest.mark.parametrize("dialect", DIALECTS)
def test_which_shapes_are_declined(dialect):
    for condition in (
        "a IS NOT DISTINCT FROM b IS NOT DISTINCT FROM c",  # DuckDB and PostgreSQL refuse the chain
        "a IS DISTINCT FROM b IS NULL",
        "a IS NULL IS DISTINCT FROM b",
        "a IS DISTINCT FROM b NOT LIKE c",
        "a LIKE b IS DISTINCT FROM c",
        "a IN (b) IS DISTINCT FROM c",
        "a BETWEEN b AND c IS DISTINCT FROM d",
        "a >= b IS DISTINCT FROM c",
    ):
        assert _declined(condition, dialect), condition
    for condition in (
        "a IN (b IS DISTINCT FROM c, d)",
        "(a IS DISTINCT FROM b) IN (c)",
        "a + 1 IS DISTINCT FROM c",
        "NOT a IS DISTINCT FROM b",
        "a IS DISTINCT FROM b AND c = d",
        "CASE WHEN a IS DISTINCT FROM b THEN 1 END = 1",
        "COALESCE(a = b, FALSE) IS DISTINCT FROM c",
        "EXISTS (SELECT 1) IS DISTINCT FROM TRUE",
    ):
        assert not _declined(condition, dialect), condition
