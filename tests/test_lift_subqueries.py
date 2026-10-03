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


def test_correlated_derived_tables_stay_in_place():
    # sqlfluff ST05's refusal cases: a derived table that reads the relation before it (in any branch of a set
    # operation, or inside a nested predicate) would lose that relation in a top-level CTE
    for source in (
        "SELECT pd.* FROM person_dates AS pd JOIN (SELECT * FROM events AS ce WHERE ce.name = pd.name) AS e ON TRUE",
        "SELECT pd.* FROM person_dates AS pd JOIN (SELECT name FROM events AS ce UNION ALL "
        "SELECT name FROM events2 AS ce2 WHERE ce2.name = pd.name) AS e ON TRUE",
        "SELECT * FROM a JOIN (SELECT * FROM b WHERE EXISTS (SELECT 1 FROM c WHERE c.y = a.y)) AS s ON TRUE",
    ):
        result = lift_subqueries(source)
        assert result.sql == source
        assert result.success and result.lifted_subqueries == 0
        assert [d.code for d in result.diagnostics] == ["correlated_subquery_kept"]
        assert count_inline_subqueries(source) == 0


def test_correlated_inner_subquery_moves_with_its_uncorrelated_parent():
    source = "SELECT * FROM (SELECT * FROM a, (SELECT * FROM b WHERE b.x = a.x) AS s) AS t"
    result = lift_subqueries(source)
    assert result.lifted_subqueries == 1
    assert "(SELECT * FROM b WHERE b.x = a.x) AS s" in result.sql  # still next to `a`
    # a name that only shadows an outer alias is not a correlation
    assert lift_subqueries("SELECT * FROM a AS b JOIN (SELECT * FROM b WHERE b.x = 1) AS s ON TRUE").lifted_subqueries == 1


def test_subquery_reading_a_nested_with_name_stays_in_place():
    source = "SELECT * FROM (WITH c AS (SELECT 1 AS x) SELECT * FROM (SELECT x FROM c) AS d) AS e"
    result = lift_subqueries(source)
    assert result.lifted_subqueries == 1  # only the outer one: at the top level `c` would be the base table
    assert "(SELECT x FROM c) AS d" in result.sql
