from kumosql import (
    RewriteResult,
    VerificationCheck,
    VerificationStatus,
    apply_rule,
    apply_rules,
    verify_rewrite,
)


def test_verification_statuses_are_single_stable_evidence_labels():
    assert {status.value for status in VerificationStatus} == {
        "unchanged",
        "proven",
        "planner_checked",
        "unproven",
        "failed",
    }


def test_unchanged_result_reports_change_detection_check():
    result = verify_rewrite("SELECT 1", "SELECT 1")

    assert result.status is VerificationStatus.UNCHANGED
    assert result.trusted
    assert [(check.kind, check.outcome) for check in result.checks] == [
        ("change_detection", "passed")
    ]


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
    assert result.trusted
    assert [(check.kind, check.outcome) for check in result.checks] == [
        ("equivalence_proof", "passed"),
        ("planner", "not_run"),
    ]


def test_proof_and_planner_checks_stay_separate_under_one_proven_label():
    result = verify_rewrite(
        "SELECT id FROM source WHERE id > 1",
        "SELECT\n    id\nFROM source\nWHERE id > 1",
        planner_check=VerificationCheck("planner", "passed", "Planner accepted both queries."),
    )

    assert result.status is VerificationStatus.PROVEN
    assert result.trusted
    assert [(check.kind, check.outcome) for check in result.checks] == [
        ("equivalence_proof", "passed"),
        ("planner", "passed"),
    ]


def test_planner_only_result_is_visible_but_not_trusted():
    result = verify_rewrite(
        "SELECT 1 AS value",
        "SELECT 2 AS value",
        planner_check=VerificationCheck("planner", "passed", "Planner accepted the candidate."),
    )

    assert result.status is VerificationStatus.PLANNER_CHECKED
    assert not result.trusted
    assert [(check.kind, check.outcome) for check in result.checks] == [
        ("equivalence_proof", "not_proven"),
        ("planner", "passed"),
    ]


def test_planner_rejection_prevents_proven_label():
    result = verify_rewrite(
        "SELECT id FROM source WHERE id > 1",
        "SELECT\n    id\nFROM source\nWHERE id > 1",
        planner_check=VerificationCheck("planner", "failed", "Planner rejected the candidate."),
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert not result.trusted
    assert [(check.kind, check.outcome) for check in result.checks] == [
        ("equivalence_proof", "passed"),
        ("planner", "failed"),
    ]


def test_pipeline_does_not_upgrade_a_planner_rejection_to_proven(monkeypatch):
    source = "SELECT id FROM source WHERE id > 1"
    candidate = "SELECT\n    id\nFROM source\nWHERE id > 1"
    step_verification = verify_rewrite(
        source,
        candidate,
        planner_check=VerificationCheck("planner", "failed", "Planner rejected candidate."),
    )
    step = RewriteResult(
        "test_rule", source, candidate, 1, (), step_verification, rule_success=True
    )
    monkeypatch.setattr("kumosql.rewrite.apply_rule", lambda name, sql, overrides=None: step)

    result = apply_rules(["test_rule"], source)

    assert result.verification.status is VerificationStatus.UNPROVEN
    assert not result.success
    assert any(
        check.kind == "planner" and check.outcome == "failed"
        for check in result.verification.checks
    )


def test_failed_rule_overrides_unchanged_text():
    source = "SELECT * FROM"

    result = apply_rule("lift_subqueries", source)

    assert result.sql == source
    assert not result.rule_success
    assert result.verification.status is VerificationStatus.FAILED
    assert not result.success
    assert [(check.kind, check.outcome) for check in result.verification.checks] == [
        ("change_detection", "passed"),
        ("rewrite", "failed"),
    ]
