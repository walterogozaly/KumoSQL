"""Witnesses from the rule-level fuzzer (``tools/rule_fuzz.py``) for the empty-relation rules, each with a near miss
that must still hold."""

import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.empty_rules import canonical_empty, is_empty, propagate_empty

SCHEMA = {"t": ["id", "x", "y"], "u": ["k", "v"]}
TYPES = {"t": {"id": "INT64", "x": "INT64", "y": "INT64"}, "u": {"k": "INT64", "v": "STRING"}}


def _prove(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="bigquery")


def _parse(sql: str):
    return sqlglot.parse_one(sql, read="bigquery")


def test_dropping_an_empty_left_join_keeps_a_nested_scopes_own_alias():
    # the inner q is t, not the empty derived table that the outer query calls q
    sql = "SELECT d.c FROM (SELECT q.x AS c FROM t AS q) AS d LEFT JOIN (SELECT u.k FROM u LIMIT 0) AS q ON TRUE"
    assert propagate_empty(_parse(sql)).sql("bigquery") == "SELECT d.c FROM (SELECT q.x AS c FROM t AS q) AS d"
    assert not _prove(sql, "SELECT CAST(NULL AS INT64) AS c FROM t").proven
    assert _prove(sql, "SELECT t.x AS c FROM t").proven


def test_dropping_an_empty_left_join_nulls_the_columns_that_read_it():
    sql = "SELECT d.c, q.k FROM (SELECT t.x AS c FROM t) AS d LEFT JOIN (SELECT u.k FROM u LIMIT 0) AS q ON TRUE"
    assert propagate_empty(_parse(sql)).sql("bigquery") == "SELECT d.c, NULL AS k FROM (SELECT t.x AS c FROM t) AS d"
    assert _prove(sql, "SELECT t.x AS c, CAST(NULL AS INT64) AS k FROM t").proven
    correlated = "SELECT t.x FROM t LEFT JOIN (SELECT u.k FROM u WHERE FALSE) AS q ON TRUE WHERE EXISTS (SELECT 1 FROM u WHERE u.k = q.k)"
    assert "q.k" not in propagate_empty(_parse(correlated)).sql("bigquery")


@pytest.mark.parametrize(
    "group",
    ["CUBE (a.x)", "ROLLUP (a.x)", "GROUPING SETS ((a.x), ())", "GROUPING SETS (ROLLUP (a.x))", "()"],
)
def test_a_grand_total_grouping_returns_a_row_over_no_input(group):
    sql = f"SELECT a.x AS s FROM (SELECT t.x FROM t LIMIT 0) AS a GROUP BY {group}"
    assert not is_empty(_parse(sql))
    assert canonical_empty(_parse(sql)).sql("bigquery") == _parse(sql).sql("bigquery")


@pytest.mark.parametrize("group", ["a.x", "GROUPING SETS ((a.x))", "a.x, ROLLUP (a.y)"])
def test_a_grouping_without_the_empty_set_returns_no_row_over_no_input(group):
    assert is_empty(_parse(f"SELECT a.x AS s FROM (SELECT t.x, t.y FROM t LIMIT 0) AS a GROUP BY {group}"))


def test_grand_total_over_an_empty_source_is_not_proved_empty():
    sql = "SELECT c2 AS s FROM (SELECT p.id AS c2, RANK() OVER () AS y FROM (SELECT t.x AS id FROM t LIMIT 0) AS p) AS u GROUP BY CUBE (c2)"
    assert not _prove(sql, "SELECT CAST(NULL AS INT64) AS s FROM t WHERE FALSE").proven
    plain = "SELECT p.id AS s FROM (SELECT t.x AS id FROM t LIMIT 0) AS p GROUP BY p.id"
    assert _prove(plain, "SELECT CAST(NULL AS INT64) AS s FROM t WHERE FALSE").proven


def test_aggregates_outside_the_list_or_under_a_window_return_a_row():
    assert not is_empty(_parse("SELECT SUM(COUNT(*)) OVER () AS n FROM (SELECT t.x FROM t WHERE FALSE) AS a"))
    assert not is_empty(_parse("SELECT 1 AS one FROM (SELECT t.x FROM t WHERE FALSE) AS a HAVING COUNT(*) = 0"))
    assert is_empty(_parse("SELECT SUM(a.x) OVER () AS n FROM (SELECT t.x FROM t WHERE FALSE) AS a"))
