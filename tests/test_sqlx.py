from kumosql import apply_rule, count_inline_subqueries, lift_subqueries
from kumosql.engine import RewriteRule


def test_lifts_sqlx_query_while_preserving_config_and_ref_interpolations():
    source = '''config {
  type: "table"
}

SELECT c.id
FROM ${ref("customers")} AS c
JOIN (SELECT customer_id FROM ${ref("orders")}) AS o
  ON o.customer_id = c.id'''

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    assert result.remaining_inline_subqueries == 0
    assert 'config {\n  type: "table"\n}' in result.sql
    assert '${ref("customers")}' in result.sql
    assert '${ref("orders")}' in result.sql
    assert count_inline_subqueries(result.sql) == 0


def test_sqlx_preserves_pre_and_post_operations_byte_for_byte():
    source = '''config { type: "table" }
pre_operations {
  DECLARE run_id INT64 DEFAULT 1;
}
SELECT * FROM (SELECT id FROM ${ref("customers")}) AS c
post_operations {
  GRANT `roles/bigquery.dataViewer` ON TABLE ${self()} TO "group:analysts";
}'''

    result = lift_subqueries(source)

    assert result.success
    assert 'pre_operations {\n  DECLARE run_id INT64 DEFAULT 1;\n}' in result.sql
    assert 'post_operations {\n  GRANT `roles/bigquery.dataViewer` ON TABLE ${self()} TO "group:analysts";\n}' in result.sql
    assert '${self()}' in result.sql


def test_sqlx_when_expression_and_optional_clause_are_restored():
    expression = '''config { type: "table" }
SELECT *
FROM ${ref("customers")}
WHERE ${when(incremental(), "id > 1", "TRUE")}'''
    clause = '''config { type: "table" }
SELECT *
FROM ${ref("customers")} ${when(incremental(), `WHERE id > 1`, ``)}'''

    expression_result = lift_subqueries(expression)
    clause_result = lift_subqueries(clause)

    assert expression_result.success
    assert clause_result.success
    assert '${when(incremental(), "id > 1", "TRUE")}' in expression_result.sql
    assert '${when(incremental(), `WHERE id > 1`, ``)}' in clause_result.sql


def test_sqlx_incremental_filter_and_ref_placeholder_survive_subquery_lift():
    incremental_filter = (
        '${when(incremental(), '
        '`updated_at > (SELECT MAX(updated_at) FROM ${self()})`, `TRUE`)}'
    )
    source = '''config { type: "incremental" }
SELECT q.id
FROM (
  SELECT id FROM ${ref("source")}
  WHERE ''' + incremental_filter + '''
) AS q'''

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 1
    assert incremental_filter in result.sql
    assert '${ref("source")}' in result.sql
    assert count_inline_subqueries(result.sql) == 0


def test_sqlx_restoration_failure_is_fatal_and_returns_no_rewritten_output():
    class DropWhereRule(RewriteRule):
        name = "test_drop_sqlx_filter"
        summary = "Test rule that removes a masked filter"

        def rewrite_statement(self, statement, index):
            statement.set("where", None)
            return 1, []

    source = '''config { type: "incremental" }
SELECT id FROM input
WHERE ${when(incremental(), "id > 0", "TRUE")}'''

    rule = DropWhereRule()
    result = apply_rule(rule.name, source, overrides={rule.name: rule})

    assert not result.success
    assert result.sql == source
    assert result.changes == 0
    assert any(d.code == "sqlx_restore_error" for d in result.diagnostics)


def test_sqlx_noop_is_returned_byte_for_byte():
    source = '''config { type: "table" }
-- preserve this SQLX comment
SELECT * FROM ${ref("customers")}
'''

    result = lift_subqueries(source)

    assert result.success
    assert result.lifted_subqueries == 0
    assert result.sql == source


def test_malformed_sqlx_block_is_not_claimed_successfully():
    source = 'config { type: "table"\nSELECT * FROM (SELECT 1) AS x'

    result = lift_subqueries(source)

    assert not result.success
    assert result.sql == source
    assert any(d.code == "sqlx_parse_error" for d in result.diagnostics)
