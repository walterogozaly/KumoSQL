"""Exercise opt-in planner evidence without contacting BigQuery."""

import json

import pytest

from kumosql import (
    RewriteResult,
    Verification,
    VerificationCheck,
    VerificationStatus,
    apply_rule,
    apply_rules,
    attach_planner_check,
    verify_rewrite,
)


def planned(fields, bytes_processed="1000"):
    return 200, {
        "statistics": {
            "totalBytesProcessed": bytes_processed,
            "query": {"schema": {"fields": fields}},
        }
    }


def rejected(message):
    return 400, {
        "error": {
            "message": message,
            "errors": [{"reason": "invalidQuery", "message": message}],
        }
    }


class FakeBigQuery:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def __call__(self, url, headers, body):
        query = json.loads(body)["configuration"]["query"]["query"]
        self.queries.append(query)
        return self.responses[query]


def changed_result(before="SELECT 1 AS value", after="SELECT 2 AS value"):
    return RewriteResult(
        "test_rule",
        before,
        after,
        1,
        (),
        verify_rewrite(before, after),
        rule_success=True,
    )


def check_named(result, outcome):
    return next(
        check
        for check in result.verification.checks
        if check.kind == "planner"
        and check.outcome == outcome
        and dict(check.evidence).get("scope") == "end_to_end"
    )


def test_same_plan_and_schema_adds_only_planner_checked_evidence():
    before = "SELECT 1 AS value"
    after = "SELECT 2 AS value"
    fake = FakeBigQuery(
        {
            before: planned([{"name": "value", "type": "INTEGER"}], "1000"),
            after: planned([{"name": "value", "type": "INT64"}], "1300"),
        }
    )

    result = attach_planner_check(
        changed_result(before, after), "billing", token="token", transport=fake
    )

    assert result.verification.status is VerificationStatus.PLANNER_CHECKED
    assert not result.success
    check = check_named(result, "passed")
    assert check.outcome == "passed"
    evidence = dict(check.evidence)
    assert evidence["original_planned"] is True
    assert evidence["rewritten_planned"] is True
    assert evidence["schema_matches"] is True
    assert evidence["schema_differences"] == ()
    assert evidence["estimated_bytes_delta"] == 300
    assert evidence["results_compared"] is False
    assert fake.queries == [before, after]
    assert "results were not compared" in check.detail
    assert result.verification.to_json()["checks"][-1]["evidence"]["schema_differences"] == []


def test_schema_mismatch_downgrades_even_a_structurally_proven_rewrite():
    before = "SELECT 1 AS value"
    after = "SELECT\n  1 AS value"
    base = changed_result(before, after)
    assert base.verification.status is VerificationStatus.PROVEN
    fake = FakeBigQuery(
        {
            before: planned([{"name": "value", "type": "INTEGER"}]),
            after: planned([{"name": "value", "type": "STRING"}]),
        }
    )

    result = attach_planner_check(base, "billing", token="token", transport=fake)

    assert result.verification.status is VerificationStatus.UNPROVEN
    assert not result.success
    check = check_named(result, "failed")
    assert check.outcome == "failed"
    assert dict(check.evidence)["schema_matches"] is False
    assert dict(check.evidence)["schema_differences"]


@pytest.mark.parametrize(
    ("responses", "reason", "original_planned", "rewritten_planned"),
    [
        (
            {"SELECT 1 AS value": rejected("Bad input"), "SELECT 2 AS value": planned([])},
            "original SQL could not be planned",
            False,
            True,
        ),
        (
            {"SELECT 1 AS value": planned([]), "SELECT 2 AS value": rejected("Bad output")},
            "rewritten SQL could not be planned",
            True,
            False,
        ),
    ],
)
def test_planning_failures_identify_the_side_without_proving_results(
    responses, reason, original_planned, rewritten_planned
):
    result = attach_planner_check(
        changed_result(), "billing", token="token", transport=FakeBigQuery(responses)
    )

    assert result.verification.status is VerificationStatus.UNPROVEN
    check = check_named(result, "failed")
    assert reason in check.detail
    assert dict(check.evidence)["original_planned"] is original_planned
    assert dict(check.evidence)["rewritten_planned"] is rewritten_planned
    assert dict(check.evidence)["schema_matches"] is None
    assert dict(check.evidence)["results_compared"] is False


def test_pipeline_planner_label_uses_end_to_end_pair_and_replaces_old_check(monkeypatch):
    original = "SELECT 1 AS value"
    intermediate = "SELECT 2 AS value"
    final = "SELECT 3 AS value"
    first_step = RewriteResult(
        "first_rule",
        original,
        intermediate,
        1,
        (),
        verify_rewrite(
            original,
            intermediate,
            planner_check=VerificationCheck("planner", "passed", "Only the step pair planned."),
        ),
        rule_success=True,
    )
    second_step = RewriteResult(
        "second_rule",
        intermediate,
        final,
        1,
        (),
        verify_rewrite(intermediate, final),
        rule_success=True,
    )
    monkeypatch.setattr(
        "kumosql.rewrite.apply_rule",
        lambda name, sql, overrides=None: first_step if name == "first_rule" else second_step,
    )
    pipeline = apply_rules(["first_rule", "second_rule"], original)
    assert pipeline.verification.status is VerificationStatus.UNPROVEN
    fake = FakeBigQuery(
        {
            original: planned([{"name": "value", "type": "INTEGER"}]),
            final: planned([{"name": "value", "type": "INTEGER"}]),
        }
    )

    result = attach_planner_check(pipeline, "billing", token="token", transport=fake)

    assert result.verification.status is VerificationStatus.PLANNER_CHECKED
    check = check_named(result, "passed")
    assert dict(check.evidence)["scope"] == "end_to_end"
    assert fake.queries == [original, final]
    step_planner = next(
        check
        for check in result.verification.checks
        if check.kind == "planner" and dict(check.evidence).get("scope") == "step"
    )
    assert step_planner.outcome == "passed"

    second_fake = FakeBigQuery(
        {
            original: planned([{"name": "value", "type": "INTEGER"}], "1000"),
            final: planned([{"name": "value", "type": "INTEGER"}], "1100"),
        }
    )
    rechecked = attach_planner_check(
        result, "billing", token="token", transport=second_fake
    )
    end_to_end_checks = [
        check
        for check in rechecked.verification.checks
        if check.kind == "planner" and dict(check.evidence).get("scope") == "end_to_end"
    ]
    assert len(end_to_end_checks) == 1
    assert dict(end_to_end_checks[0].evidence)["estimated_bytes_delta"] == 100
    assert any(
        check.kind == "planner" and dict(check.evidence).get("scope") == "step"
        for check in rechecked.verification.checks
    )


def test_sqlx_is_not_planned_until_both_compiled_inputs_are_supplied():
    before = 'config { type: "view" }\nSELECT 1 AS value'
    after = 'config { type: "view" }\nSELECT 2 AS value'
    source = changed_result(before, after)

    skipped = attach_planner_check(
        source,
        "billing",
        transport=lambda *args: pytest.fail("SQLX source should not reach the planner"),
    )

    assert skipped.verification.status is VerificationStatus.UNPROVEN
    check = next(check for check in skipped.verification.checks if check.kind == "planner")
    assert check.outcome == "not_run"
    assert "SQLX must be compiled" in check.detail
    assert dict(check.evidence)["compiled_sql_used"] is False

    compiled_before = "SELECT 1 AS value"
    compiled_after = "SELECT 2 AS value"
    fake = FakeBigQuery(
        {
            compiled_before: planned([{"name": "value", "type": "INTEGER"}]),
            compiled_after: planned([{"name": "value", "type": "INTEGER"}]),
        }
    )
    checked = attach_planner_check(
        source,
        "billing",
        token="token",
        transport=fake,
        compiled_original_sql=compiled_before,
        compiled_rewritten_sql=compiled_after,
    )

    assert checked.verification.status is VerificationStatus.PLANNER_CHECKED
    assert dict(check_named(checked, "passed").evidence)["compiled_sql_used"] is True


def test_multi_statement_and_dml_are_explicitly_not_planned():
    before = "UPDATE source SET value = 1"
    after = "UPDATE source SET value = 2"
    result = RewriteResult(
        "test_rule",
        before,
        after,
        1,
        (),
        Verification(VerificationStatus.UNPROVEN, "equivalence could not be established"),
        rule_success=True,
    )

    checked = attach_planner_check(
        result,
        "billing",
        transport=lambda *args: pytest.fail("DML should not reach this query planner path"),
    )

    check = next(check for check in checked.verification.checks if check.kind == "planner")
    assert checked.verification.status is VerificationStatus.UNPROVEN
    assert check.outcome == "not_run"
    assert "single SELECT statements" in check.detail


def test_compiled_sql_overrides_are_skipped_for_plain_sql():
    fake = FakeBigQuery({})

    result = attach_planner_check(
        changed_result(),
        "billing",
        compiled_original_sql="SELECT 10 AS value",
        compiled_rewritten_sql="SELECT 20 AS value",
        transport=fake,
    )

    check = next(check for check in result.verification.checks if check.kind == "planner")
    assert check.outcome == "not_run"
    assert "only accepted for SQLX" in check.detail
    assert fake.queries == []


def test_missing_credentials_report_not_run_and_preserve_rewrite_result(monkeypatch):
    base = changed_result()
    monkeypatch.setattr(
        "kumosql.rewrite.check_rewrite",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no credentials")),
    )

    result = attach_planner_check(base, "billing")

    assert result.verification.status is VerificationStatus.UNPROVEN
    check = next(check for check in result.verification.checks if check.kind == "planner")
    assert check.outcome == "not_run"
    assert "no credentials" in check.detail


def test_fatal_rewrite_and_unchanged_sql_skip_network():
    fatal = apply_rule("lift_subqueries", "SELECT * FROM")
    fail_if_called = lambda *args, **kwargs: pytest.fail("planner should not be called")

    failed_result = attach_planner_check(fatal, "billing", transport=fail_if_called)
    assert failed_result.verification.status is VerificationStatus.FAILED
    assert any(
        check.kind == "planner" and check.outcome == "not_run"
        for check in failed_result.verification.checks
    )

    unchanged = apply_rules(["format_sql"], "SELECT 1")
    unchanged_result = attach_planner_check(unchanged, "billing", transport=fail_if_called)
    assert unchanged_result.verification.status is VerificationStatus.UNCHANGED


def test_rewrite_pipeline_does_not_call_planner_without_explicit_attachment(monkeypatch):
    monkeypatch.setattr(
        "kumosql.rewrite.check_rewrite",
        lambda *args, **kwargs: pytest.fail("ordinary rewriting must stay offline"),
    )

    result = apply_rules(["format_sql"], "SELECT 1")

    assert result.verification.status is VerificationStatus.UNCHANGED
