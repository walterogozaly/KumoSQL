"""Cleanup and CTE rules leave SQLX statements alone when they hold a ``${...}`` expression other than ref()/self().

The rules see a placeholder name where the expression sits, but the expression compiles to arbitrary SQL text: a
predicate that binds looser than its neighbours, a clause continuation, or a query that reads a CTE.
"""

import pytest

from kumosql import apply_rule

CONFIG = 'config { type: "table" }\n'


@pytest.mark.parametrize(
    ("rule", "body"),
    [
        # `z AND ${"x OR y"}` compiles to `z AND x OR y`
        ("remove_redundant_parentheses", 'SELECT a FROM ${ref("t")} WHERE z AND (${"x OR y"})'),
        # `WHERE ${when(...)}` compiles to `WHERE AND b > 1` or to a bare `WHERE`
        ("remove_trivial_predicates", 'SELECT a FROM ${ref("t")} WHERE TRUE ${when(incremental(), "AND b > 1")}'),
        # the CTE is read only inside the expression
        ("remove_unused_ctes", 'WITH c AS (SELECT 1 AS a) SELECT a FROM ${ref("t")} WHERE a IN (${"SELECT a FROM c"})'),
        ("deduplicate_ctes", 'WITH c AS (SELECT 1 AS a), d AS (SELECT 1 AS a) SELECT a FROM c WHERE a IN (${"SELECT a FROM d"})'),
        ("inline_single_use_ctes", 'WITH c AS (SELECT 1 AS a) SELECT a FROM c WHERE a IN (${"SELECT a FROM c"})'),
    ],
)
def test_statement_with_an_expression_is_kept(rule, body):
    source = CONFIG + body
    result = apply_rule(rule, source)
    assert result.sql == source
    assert result.changes == 0
    assert any(d.code == "sqlx_expression_kept" for d in result.diagnostics)


@pytest.mark.parametrize(
    ("rule", "body", "expected"),
    [
        ("remove_redundant_parentheses", 'SELECT a FROM ${ref("t")} WHERE z AND (x = 1)', "z AND x = 1"),
        ("remove_trivial_predicates", 'SELECT a FROM ${ref("t")} WHERE 1 = 1 AND b > 1', "WHERE b > 1"),
        ("remove_unused_ctes", 'WITH c AS (SELECT 1 AS a) SELECT a FROM ${ref("t")} AS t', 'SELECT a FROM ${ref("t")} AS t'),
        ("inline_single_use_ctes", 'WITH c AS (SELECT a FROM ${self()}) SELECT a FROM c', "FROM (SELECT a FROM ${self()})"),
    ],
)
def test_table_references_alone_still_rewrite(rule, body, expected):
    result = apply_rule(rule, CONFIG + body)
    assert result.changes >= 1
    assert expected in " ".join(result.sql.split())
    assert not any(d.code == "sqlx_expression_kept" for d in result.diagnostics)


def test_only_the_statement_with_the_expression_is_kept():
    source = CONFIG + 'SELECT a FROM ${ref("t")} WHERE (x = 1);\nSELECT a FROM ${ref("t")} WHERE z AND (${"x OR y"})'
    result = apply_rule("remove_redundant_parentheses", source)
    assert "WHERE x = 1;" in " ".join(result.sql.split())
    assert '(${"x OR y"})' in result.sql
