"""The ``qualify_columns`` rule: bare columns get their source, and only where that is certain."""

from __future__ import annotations

import pytest

from kumosql import VerificationStatus, apply_rule, apply_rules, canonical_rule_order
from kumosql.prover_context import use_columns

TABLES = {
    "p.d.orders": ["id", "total", "cid"],
    "p.d.customers": ["name", "cid", "region"],
    "a": ["x", "k", "arr"],
    "b": ["y", "k"],
}


def run(sql: str, tables: dict | None = None):
    with use_columns(TABLES if tables is None else tables):
        return apply_rule("qualify_columns", sql)


def flat(sql: str) -> str:
    return " ".join(sql.split())


def test_qualifies_columns_with_their_alias_and_name():
    result = run("SELECT id, name FROM `p.d.orders` AS o JOIN `p.d.customers` ON o.cid = customers.cid WHERE total > 1")

    assert flat(result.sql) == (
        "SELECT o.id, `customers`.name FROM `p.d.orders` AS o JOIN `p.d.customers` ON o.cid = customers.cid WHERE o.total > 1"
    )
    assert result.changes == 3
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details


def test_single_source_select_is_left_alone():
    result = run("SELECT id, total FROM `p.d.orders`")

    assert result.changes == 0
    assert result.verification.status is VerificationStatus.UNCHANGED


def test_using_column_stays_bare_and_the_rest_are_qualified():
    result = run("SELECT k, x, y FROM a JOIN b USING (k)")

    assert flat(result.sql) == "SELECT k, a.x, b.y FROM a JOIN b USING (k)"
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details


def test_using_column_is_kept_bare_even_when_only_one_source_lists_it():
    result = run("SELECT k, x FROM a LEFT JOIN b USING (k)", {"a": ["x", "k"], "b": ["y"]})

    assert flat(result.sql) == "SELECT k, a.x FROM a LEFT JOIN b USING (k)"


def test_natural_join_is_left_alone():
    result = run("SELECT x, y FROM a NATURAL JOIN b")

    assert result.changes == 0


def test_ambiguous_and_unknown_columns_are_left_alone():
    result = run("SELECT k, nothing, _PARTITIONTIME FROM a JOIN b ON a.x = b.y")

    assert result.changes == 0


def test_unknown_table_leaves_the_whole_select_alone():
    result = run("SELECT x, y FROM a JOIN mystery ON a.k = mystery.k")

    assert result.changes == 0


def test_select_alias_wins_in_group_by_and_having_but_not_in_where():
    grouped = run("SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y HAVING n > 0")
    assert flat(grouped.sql) == "SELECT a.x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y HAVING n > 0"

    filtered = run("SELECT x AS y FROM a JOIN b ON a.k = b.k WHERE y > 1")
    assert flat(filtered.sql) == "SELECT a.x AS y FROM a JOIN b ON a.k = b.k WHERE b.y > 1"


def test_final_order_by_text_is_kept_so_the_rewrite_stays_provable():
    result = run("SELECT x FROM a JOIN b ON a.k = b.k ORDER BY y")

    assert flat(result.sql) == "SELECT a.x FROM a JOIN b ON a.k = b.k ORDER BY y"
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details


def test_order_by_inside_a_limited_subquery_is_qualified():
    result = run("SELECT * FROM (SELECT x FROM a JOIN b ON a.k = b.k ORDER BY y LIMIT 3)")

    assert "ORDER BY b.y" in flat(result.sql)
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details


def test_ctes_and_derived_tables_supply_their_own_columns():
    cte = run("WITH s AS (SELECT 1 AS p, 2 AS k), t AS (SELECT 3 AS q, 2 AS k) SELECT p, q FROM s JOIN t ON s.k = t.k")
    assert flat(cte.sql).endswith("SELECT s.p, t.q FROM s JOIN t ON s.k = t.k")
    assert cte.verification.status is VerificationStatus.PROVEN, cte.verification.details

    derived = run("SELECT p, q FROM (SELECT 1 AS p, 2 AS k) AS s JOIN (SELECT 3 AS q, 2 AS k) AS t ON s.k = t.k")
    assert flat(derived.sql) == (
        "SELECT s.p, t.q FROM (SELECT 1 AS p, 2 AS k) AS s JOIN (SELECT 3 AS q, 2 AS k) AS t ON s.k = t.k"
    )


def test_star_derived_table_is_unknown():
    result = run("SELECT p FROM (SELECT * FROM a) AS s JOIN b ON s.k = b.k", {"a": ["p", "k"], "b": ["y", "k"]})

    assert result.changes == 0


def test_unnest_value_and_offset_stay_bare():
    result = run("SELECT x, v, o FROM a, UNNEST(a.arr) AS v WITH OFFSET AS o")

    assert flat(result.sql) == "SELECT a.x, v, o FROM a CROSS JOIN UNNEST(a.arr) AS v WITH OFFSET AS o"
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details


def test_unaliased_unnest_leaves_the_select_alone():
    assert run("SELECT x FROM a, UNNEST(a.arr)").changes == 0


def test_star_except_names_stay_bare():
    result = run("SELECT * EXCEPT (x) FROM a JOIN b ON a.k = b.k")

    assert result.changes == 0


def test_duplicate_source_names_leave_the_select_alone():
    assert run("SELECT x FROM a JOIN a ON a.k = a.k").changes == 0


def test_each_select_is_judged_on_its_own_sources():
    result = run("SELECT x, y FROM a JOIN b ON a.k = b.k WHERE x IN (SELECT total FROM `p.d.orders`)")

    assert flat(result.sql) == "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k WHERE a.x IN (SELECT total FROM `p.d.orders`)"


def test_correlated_outer_reference_is_not_claimed_by_the_inner_select():
    result = run(
        "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` o ON b.k = o.cid WHERE y = x)"
    )

    assert flat(result.sql) == (
        "SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` AS o ON b.k = o.cid WHERE b.y = x)"
    )


def test_set_operation_order_by_names_output_columns():
    result = run("SELECT x FROM a JOIN b ON a.k = b.k UNION ALL SELECT y FROM b JOIN a ON a.k = b.k ORDER BY x")

    assert "ORDER BY x" in flat(result.sql)
    assert "SELECT a.x" in flat(result.sql) and "SELECT b.y" in flat(result.sql)


def test_data_changing_statements_are_untouched():
    sql = "UPDATE a SET x = 1 FROM b WHERE a.k = b.k AND y = 2"

    assert run(sql).changes == 0


def test_rule_is_idempotent_and_opt_in():
    once = run("SELECT x, y FROM a JOIN b ON a.k = b.k")
    again = run(once.sql)

    assert again.changes == 0 and again.sql == once.sql
    assert "qualify_columns" not in canonical_rule_order()


def test_no_known_tables_means_no_change_for_plain_tables():
    with use_columns({}):
        assert apply_rule("qualify_columns", "SELECT x, y FROM a JOIN b ON a.k = b.k").changes == 0


duckdb = pytest.importorskip("duckdb")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM `p.d.orders` o JOIN `p.d.customers` c ON o.cid = c.cid WHERE total > 1",
        "SELECT cid, id, name FROM `p.d.orders` o JOIN `p.d.customers` c USING (cid)",
        "SELECT region, SUM(total) AS t FROM `p.d.orders` o LEFT JOIN `p.d.customers` c ON o.cid = c.cid GROUP BY region",
    ],
)
def test_results_are_identical_on_generated_data(sql):
    from kumosql.result_equivalence import assert_result_equivalent

    types = {
        "p.d.orders": {"id": "INT64", "total": "INT64", "cid": "INT64"},
        "p.d.customers": {"name": "STRING", "cid": "INT64", "region": "STRING"},
    }
    with use_columns(TABLES):
        result = apply_rules(["qualify_columns"], sql)
    assert result.sql != sql
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details
    assert_result_equivalent(sql, result.sql, types, seeds=range(6))
