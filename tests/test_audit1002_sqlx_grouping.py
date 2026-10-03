"""Parentheses around a Dataform ``${...}`` expression are kept by the cleanup rules.

A rule sees a masked ``${...}`` expression as one identifier, but the expression
can expand to any text, so the parentheses around it decide how it groups:
``(${"1 + 2"}) * 3`` is 9 and ``${"1 + 2"} * 3`` is 7.
"""

import duckdb
import pytest

from kumosql import VerificationStatus, apply_rule


def _body(sql):
    return " ".join(sql.split())


def _run(sql, expansions):
    """Expand each ``${...}`` as Dataform would and run the query in DuckDB."""

    for template, text in expansions.items():
        sql = sql.replace(template, text)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE t AS SELECT x, y, b, 1 AS a FROM (VALUES (TRUE), (FALSE)) AS p(x), "
            "(VALUES (TRUE), (FALSE)) AS q(y), (VALUES (TRUE), (FALSE)) AS r(b)"
        )
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_parentheses_around_an_interpolation_in_arithmetic_are_kept():
    source = 'SELECT (${"1 + 2"}) * 3 AS x'

    result = apply_rule("remove_redundant_parentheses", source)

    assert '(${"1 + 2"}) * 3' in result.sql
    expansions = {'${"1 + 2"}': "1 + 2"}
    assert _run(result.sql, expansions) == _run(source, expansions) == [(9,)]


@pytest.mark.parametrize(
    "source",
    [
        "SELECT a FROM t WHERE (${cond}) AND b",
        "SELECT a FROM t WHERE (a = ${v}) AND b",
        "SELECT a FROM t WHERE (b AND ${c}) AND b",
        "SELECT a FROM t WHERE (${c}) OR b",
        "SELECT (${x}.col) * 3 AS y FROM t",
        "SELECT a FROM t WHERE NOT (${c})",
    ],
)
def test_parentheses_holding_an_interpolation_in_a_predicate_are_kept(source):
    result = apply_rule("remove_redundant_parentheses", source)

    assert _body(result.sql) == _body(source)


def test_comparison_grouping_with_an_interpolation_keeps_its_result():
    source = "SELECT COUNT(*) AS n FROM t WHERE (a = ${v}) AND b"
    expansions = {"${v}": "1 OR x"}

    result = apply_rule("remove_redundant_parentheses", source)

    assert _run(result.sql, expansions) == _run(source, expansions)


def test_trivial_predicate_removal_keeps_the_group_around_an_interpolation():
    source = "SELECT COUNT(*) AS n FROM t WHERE (${c} OR FALSE) AND b"
    expansions = {"${c}": "x OR y"}

    result = apply_rule("remove_trivial_predicates", source)

    assert "OR FALSE" not in result.sql
    assert "(${c}" in result.sql
    assert _run(result.sql, expansions) == _run(source, expansions) == [(3,)]


def test_doubled_parentheses_around_an_interpolation_keep_one_pair():
    result = apply_rule("remove_redundant_parentheses", "SELECT a FROM t WHERE ((${c}))")

    assert _body(result.sql).replace("( ", "(") == "SELECT a FROM t WHERE (${c})"


def test_parentheses_around_a_call_holding_an_interpolation_are_still_removed():
    result = apply_rule("remove_redundant_parentheses", "SELECT (COALESCE(${x}, 0)) * 3 AS y FROM t")

    assert _body(result.sql) == "SELECT COALESCE(${x}, 0) * 3 AS y FROM t"


def test_parentheses_around_a_plain_column_are_still_removed_and_proven():
    result = apply_rule("remove_redundant_parentheses", "SELECT (c) * 3 AS x, a FROM t WHERE (a = 1) AND b")

    assert _body(result.sql) == "SELECT c * 3 AS x, a FROM t WHERE a = 1 AND b"
    assert result.verification.status is VerificationStatus.PROVEN


def test_parentheses_around_a_plain_column_in_sqlx_are_still_removed():
    source = 'config { type: "table" }\nSELECT (c) * 3 AS x FROM ${ref("t")} WHERE (a = 1) AND b\n'

    result = apply_rule("remove_redundant_parentheses", source)

    assert result.sql.startswith('config { type: "table" }\n')
    assert _body(result.sql).endswith('SELECT c * 3 AS x FROM ${ref("t")} WHERE a = 1 AND b')
