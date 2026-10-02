"""IS NOT NULL / NOT LIKE must never be read as the positive test, in any dialect.

sqlglot (30.x) parses ``x IS NOT NULL`` under PostgreSQL as ``Is(negate=True)``. The provers once read only the
node type, so ``x IS NULL`` and ``x IS NOT NULL`` were "proven" equivalent when the query was read as PostgreSQL.
Found by the query-containment eval (tools/containment_bench.py, baseline run).
"""

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

import sqlglot

DIALECTS = ["postgres", "bigquery", "mysql", "sqlite", "duckdb"]
SCHEMA = {"orders": ["id", "amount"]}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT id FROM orders WHERE amount IS NULL", "SELECT id FROM orders WHERE amount IS NOT NULL"),
        ("SELECT id FROM orders WHERE amount LIKE 'a%'", "SELECT id FROM orders WHERE amount NOT LIKE 'a%'"),
        ("SELECT id FROM orders WHERE amount > 10", "SELECT id FROM orders WHERE amount IS NOT NULL"),
    ],
)
def test_positive_and_negative_tests_are_never_equivalent(left, right, dialect):
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        result = prove(left, right, schema=SCHEMA, dialect=dialect, compare_names=False)
        assert result.status is not SmtStatus.PROVEN_EQUIVALENT, (prove.__name__, dialect, result.reason)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_two_spellings_of_a_negation_are_equivalent(dialect):
    pairs = [
        ("SELECT id FROM orders WHERE amount IS NOT NULL", "SELECT id FROM orders WHERE NOT (amount IS NULL)"),
        ("SELECT id FROM orders WHERE amount NOT LIKE 'a%'", "SELECT id FROM orders WHERE NOT (amount LIKE 'a%')"),
    ]
    for left, right in pairs:
        result = prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect=dialect, compare_names=False)
        assert result.status is SmtStatus.PROVEN_EQUIVALENT, (left, dialect, result.reason)


def test_canonical_negation_rewrites_negate_flags():
    tree = sqlglot.parse_one("SELECT a FROM t WHERE a IS NOT NULL AND b NOT ILIKE 'x'", read="postgres")
    fixed = canonical_negation(tree)
    assert not any(node.args.get("negate") for node in fixed.walk())
    assert fixed.sql("postgres").count("NOT") == 2
