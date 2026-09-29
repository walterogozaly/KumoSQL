from bq_sql_tools import count_inline_subqueries, lift_subqueries


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
    assert result.sql.index("__lifted_subquery_001 AS") < result.sql.index("__lifted_subquery_002 AS")


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
