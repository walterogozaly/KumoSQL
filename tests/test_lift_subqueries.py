import sqlglot
from sqlglot import exp

from kumosql import count_inline_subqueries, lift_subqueries


def _assert_root_ctes_follow_dependencies(sql):
    statement = sqlglot.parse_one(sql, read="bigquery")
    with_clause = statement.args.get("with_") or statement.args.get("with")
    assert with_clause is not None

    ctes = with_clause.expressions
    names = {
        cte.args["alias"].this.name
        for cte in ctes
        if cte.args.get("alias") is not None
    }
    positions = {cte.args["alias"].this.name: index for index, cte in enumerate(ctes)}
    for owner_index, cte in enumerate(ctes):
        for table in cte.find_all(exp.Table):
            if not table.db and not table.catalog and table.name in names:
                assert positions[table.name] < owner_index, (
                    f"CTE {table.name!r} must appear before its dependent CTE"
                )


def test_lifts_from_subquery_and_preserves_source_alias():
    source = "SELECT c.id FROM (SELECT id FROM `p.d.customers`) AS c"

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    assert result.remaining_inline_subqueries == 0
    assert "__lifted_subquery_001 AS" in result.sql
    assert "FROM __lifted_subquery_001 AS c" in result.sql
    assert count_inline_subqueries(result.sql) == 0


def test_lifts_nested_relations_into_dependency_order():
    source = """SELECT *
FROM (
  SELECT * FROM (SELECT id FROM `p.d.customers`) AS inner_c
) AS outer_c"""

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 2
    _assert_root_ctes_follow_dependencies(result.sql)


def test_lifts_from_existing_cte_before_that_cte():
    source = """WITH STOPS AS (
  SELECT * FROM (SELECT stop_id FROM `p.d.stops`) AS s
), DRIVERS AS (
  SELECT driver_id FROM `p.d.drivers`
)
SELECT * FROM STOPS JOIN DRIVERS ON TRUE"""

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    assert result.sql.index("__lifted_subquery_001 AS") < result.sql.index("STOPS AS")
    assert result.sql.index("STOPS AS") < result.sql.index("DRIVERS AS")
    _assert_root_ctes_follow_dependencies(result.sql)


def test_generic_lift_is_ordered_before_ctes_that_depend_on_it():
    source = """WITH base AS (
  SELECT * FROM (SELECT item_id FROM source_table) AS nested_source
), child AS (
  SELECT item_id FROM base
)
SELECT item_id FROM child"""

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    _assert_root_ctes_follow_dependencies(result.sql)


def test_noop_sql_is_returned_byte_for_byte():
    source = "-- preserve this comment\nSELECT id FROM `p.d.customers`;\n"

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 0
    assert result.sql == source


def test_undefined_lifted_cte_reference_fails_closed():
    source = """WITH __lifted_subquery_001 AS (
  SELECT * FROM __lifted_subquery_002
)
SELECT * FROM __lifted_subquery_001"""

    result = lift_subqueries(source)

    assert not result.success
    assert any(d.code == "cte_dependency_error" for d in result.diagnostics)


def test_forward_cte_dependency_is_a_failed_rewrite():
    source = """WITH first_cte AS (
  SELECT * FROM later_cte
), later_cte AS (
  SELECT item_id FROM source_table
)
SELECT * FROM first_cte"""

    result = lift_subqueries(source)

    assert not result.success
    assert result.sql == source
    assert any(
        diagnostic.code == "cte_dependency_error"
        and "referenced before it is defined" in diagnostic.message
        for diagnostic in result.diagnostics
    )


def test_lifted_sql_uses_four_space_indentation():
    result = lift_subqueries("SELECT * FROM (SELECT id FROM `p.d.customers`) AS c")

    assert result.success
    assert "\n    *\nFROM" in result.sql


def test_lifts_join_subquery_and_leaves_scalar_subquery_in_scope():
    source = """SELECT c.id
FROM `p.d.customers` AS c
JOIN (SELECT customer_id FROM `p.d.orders`) AS o
  ON o.customer_id = c.id
WHERE c.id IN (SELECT customer_id FROM `p.d.vip_customers`)"""

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    assert result.remaining_inline_subqueries == 0
    assert "c.id IN" in result.sql
    assert "vip_customers" in result.sql


def test_non_query_statements_are_preserved_as_supported_input():
    result = lift_subqueries("DROP TABLE `p.d.old_table`;\nCALL `p.d.refresh`()")

    assert result.success
    assert result.lifted_subqueries == 0
    assert "DROP TABLE" in result.sql
    assert "CALL" in result.sql


def test_parse_failure_is_not_reported_as_success():
    source = "SELECT * FROM (SELECT id FROM"

    result = lift_subqueries(source)

    assert not result.success
    assert result.sql == source
    assert result.diagnostics


def test_lifted_cte_never_shadows_a_table_the_query_reads():
    # Eval-integrity audit 2026-10-02: naming the CTE __lifted_subquery_001 hid the
    # physical table of that name, turning (1, 7) into (1, 1).
    sql = "SELECT a.x AS ax, b.x AS bx FROM (SELECT 1 AS x) a JOIN __lifted_subquery_001 b ON TRUE"

    lifted = lift_subqueries(sql).sql

    statement = sqlglot.parse_one(lifted, read="bigquery")
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    assert "__lifted_subquery_001" not in cte_names
    assert "JOIN __lifted_subquery_001 AS b" in lifted


def test_lifted_cte_names_skip_tables_in_any_case():
    sql = "SELECT * FROM (SELECT 1 AS x) a JOIN __LIFTED_SUBQUERY_001 b ON TRUE"

    statement = sqlglot.parse_one(lift_subqueries(sql).sql, read="bigquery")

    assert {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)} == {"__lifted_subquery_002"}


def test_verified_lift_keeps_reading_a_table_named_like_a_lifted_cte():
    # With the physical table = {9} the original returns (1, 9); a CTE reusing its name returned (1, 1).
    from kumosql import apply_rule

    result = apply_rule(
        "lift_subqueries", "SELECT x.a, y.a AS b FROM (SELECT 1 AS a) AS x CROSS JOIN __lifted_subquery_001 AS y"
    )

    statement = sqlglot.parse_one(result.sql, read="bigquery")
    assert "__lifted_subquery_001" not in {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
