from bq_sql_tools import VerificationStatus, verify_rewrite


def test_changed_create_target_is_unproven():
    result = verify_rewrite(
        "CREATE TABLE `p.d.a` AS SELECT 1 AS x",
        "CREATE TABLE `p.d.b` AS SELECT 1 AS x",
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert "outside the query" in result.details[0]


def test_dropped_order_by_is_unproven_even_though_bags_match():
    result = verify_rewrite(
        "SELECT id FROM `p.d.t` ORDER BY id",
        "SELECT id FROM `p.d.t`",
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert "ORDER BY" in result.details[0]


def test_statement_count_change_is_unproven():
    result = verify_rewrite("SELECT 1; SELECT 2", "SELECT 1")

    assert result.status is VerificationStatus.UNPROVEN


def test_changed_non_query_statement_is_unproven():
    result = verify_rewrite("DROP TABLE `p.d.a`", "DROP TABLE `p.d.b`")

    assert result.status is VerificationStatus.UNPROVEN


def test_changed_sqlx_config_block_is_unproven():
    result = verify_rewrite(
        'config { type: "table" }\nSELECT 1 AS x',
        'config { type: "view" }\nSELECT 1 AS x',
    )

    assert result.status is VerificationStatus.UNPROVEN


def test_changed_sqlx_interpolation_is_unproven():
    result = verify_rewrite(
        'config { type: "table" }\nSELECT id FROM ${ref("a")}',
        'config { type: "table" }\nSELECT id FROM ${ref("b")}',
    )

    assert result.status is VerificationStatus.UNPROVEN


def test_formatting_only_change_is_proven():
    result = verify_rewrite(
        "SELECT id FROM `p.d.t` WHERE id > 1",
        "SELECT\n    id\nFROM `p.d.t`\nWHERE\n    id > 1",
    )

    assert result.status is VerificationStatus.PROVEN
