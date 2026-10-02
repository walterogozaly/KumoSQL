"""Rewrites of statements sqlglot cannot read: layout-only proofs and pipe syntax."""

import pytest
from sqlglot.tokens import TokenType

from kumosql import rewrite
from kumosql.formatting import format_sql
from kumosql.layout_equivalence import layout_only_change

CLEANUP = ("remove_trivial_predicates", "remove_redundant_parentheses", "remove_unused_ctes", "inline_single_use_ctes")


@pytest.mark.parametrize(
    "before,after",
    [
        ("select a from t order by a", "SELECT a\nFROM t\nORDER\n  BY a"),
        ("select count(x) from t", "SELECT COUNT(x) FROM t"),
        ("select a -- note\nfrom t", "SELECT a -- note\nFROM t"),
        (
            "LOAD DATA OVERWRITE `p.d.t` (id INT64) PARTITION BY DATE(ts) FROM FILES (format = 'PARQUET')",
            "LOAD DATA OVERWRITE `p.d.t` (\n  id INT64\n) PARTITION BY DATE(ts) FROM FILES (\n  format = 'PARQUET'\n)",
        ),
        ("CALL `p.d.proc`(1, x)", "CALL `p.d.proc` (1, x)"),
        ("REPEAT\nSET i = i + 1;\nUNTIL i >= 3\nEND REPEAT", "REPEAT\n  SET i = i + 1;\n  UNTIL i >= 3\nEND REPEAT"),
        ("ALTER SCHEMA `p.d` SET OPTIONS (x = 2)", "ALTER SCHEMA `p.d` SET OPTIONS (\n  x = 2\n)"),
    ],
)
def test_layout_only_changes_are_proven(before, after):
    assert layout_only_change(before, after)
    assert rewrite.verify_rewrite(before, after).status is rewrite.VerificationStatus.PROVEN


@pytest.mark.parametrize(
    "before,after",
    [
        ("select a from ds.Range", "select a from ds.RANGE"),  # table names are case sensitive
        ("select a from Date", "select a from DATE"),  # an unreserved keyword can be a table name
        ("select myFunc(x) from t", "select MYFUNC(x) from t"),  # user-defined function names too
        ("CREATE TEMP FUNCTION count(x INT64) AS (x); SELECT count(1)", "CREATE TEMP FUNCTION COUNT(x INT64) AS (x); SELECT COUNT(1)"),
        ("select 'abc' from t", "select 'ABC' from t"),
        ("select a from `t`", "select a from `T`"),
        ("select a -- note\nfrom t", "select a from t"),
        ("select a from t where x = 1", "select a from t where x = 1.0"),
        ("SELECT @@error.message", "SELECT @ @error.message"),
        ("CALL `p.d.proc`(1)", "CALL `p.d.proc`(2)"),
        ("SELECT r'a' FROM t", "SELECT 'a' FROM t"),
    ],
)
def test_meaningful_changes_are_not_layout(before, after):
    assert not layout_only_change(before, after)


def test_formatter_keeps_the_case_of_user_defined_functions():
    formatted = format_sql(
        "create temp function addOne(x int64) as (x + 1);\nselect addOne(age), myUdf(age), count(*) from t"
    )
    assert "addOne(x INT64)" in formatted and "addOne(age)" in formatted and "myUdf(age)" in formatted
    assert "COUNT(*)" in formatted and "SELECT" in formatted


def test_formatting_an_opaque_statement_is_proven():
    sql = "CREATE ROW ACCESS POLICY p ON `p.d.t` GRANT TO ('user:a@example.com') FILTER USING (region = 'US')"
    result = rewrite.apply_rule("format_sql", sql)
    assert result.sql != sql
    assert result.verification.status is rewrite.VerificationStatus.PROVEN


def test_cleanup_leaves_pipe_syntax_as_written():
    sql = "FROM `p.d.t` |> AS u |> WHERE u.age > 1 |> SELECT u.id"
    result = rewrite.apply_rules(CLEANUP, sql)
    assert result.sql == sql and result.success
    with_cte = "WITH base AS (SELECT * FROM `p.d.t`) FROM base |> SELECT id"
    assert rewrite.apply_rules(CLEANUP, with_cte).sql == with_cte


@pytest.mark.skipif(not hasattr(TokenType, "PIPE_GT"), reason="sqlglot 26.0.0 has no pipe syntax")
def test_prover_still_reads_pipe_syntax():
    from kumosql.equivalence import prove_equivalent

    sql = "FROM `p.d.t` |> WHERE age > 1 |> SELECT id"
    assert prove_equivalent(sql, sql).proven


def test_formatter_formats_the_statements_sqlfluff_can_read():
    sql = (
        "select a,b from `p.d.t` where x=1;\n"
        "GRANT `roles/bigquery.dataViewer` ON TABLE `p.d.t` TO 'user:a@example.com';\n"
        "-- note\n"
        "select   count(*) from `p.d.u`\n"
    )
    result = rewrite.apply_rule("format_sql", sql)
    assert result.verification.status is rewrite.VerificationStatus.PROVEN
    assert "GRANT `roles/bigquery.dataViewer` ON TABLE `p.d.t` TO 'user:a@example.com';\n-- note\n" in result.sql
    assert "WHERE x = 1;" in result.sql and "SELECT COUNT(*) FROM `p.d.u`" in result.sql
    assert [d.code for d in result.diagnostics] == ["statements_not_formatted"]


def test_formatter_reports_a_statement_it_cannot_read():
    result = rewrite.apply_rule("format_sql", "GRANT `roles/x` ON TABLE `p.d.t` TO 'user:a@example.com'")
    assert [d.code for d in result.diagnostics] == ["parse_error"]
    with pytest.raises(ValueError):
        format_sql("GRANT `roles/x` ON TABLE `p.d.t` TO 'user:a@example.com'; GRANT `roles/y` ON TABLE `p.d.u` TO 'user:b@example.com'")
