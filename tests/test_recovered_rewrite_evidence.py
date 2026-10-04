"""Parser recovery must never authorize automatic acceptance of a rule result."""

import pytest

from kumosql import VerificationStatus, apply_rule, apply_rules
from kumosql.evidence_summary import summarize_evidence


@pytest.mark.parametrize("rule", ["lift_subqueries", "inline_single_use_ctes"])
@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM t WHERE 1 =",
    "SELECT 1; SELECT 1 FROM t WHERE 1 =",
])
def test_recovered_no_op_is_unproven(rule, sql):
    result = apply_rule(rule, sql)
    assert any(d.code == "recovered_parse" for d in result.diagnostics)
    assert result.sql == sql
    assert result.rule_success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert not result.success
    assert any(c.kind == "strict_parse" and c.outcome == "not_proven"
               for c in result.verification.checks)


def test_pipeline_cannot_rescue_a_recovered_no_op():
    sql = "SELECT 1 FROM t WHERE 1 ="
    result = apply_rules(["lift_subqueries", "inline_single_use_ctes"], sql)
    assert result.sql == sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert not result.success


def test_strict_no_op_stays_trusted():
    result = apply_rule("lift_subqueries", "SELECT 1 FROM t")
    assert result.success
    assert result.verification.status is VerificationStatus.UNCHANGED


def test_recovery_does_not_hide_a_fatal_sqlx_restoration_failure():
    sql = 'config { type: "table" }\nSELECT 1 FROM ${ref("t")} WHERE 1 ='
    result = apply_rule("lift_subqueries", sql)
    assert any(d.code == "recovered_parse" for d in result.diagnostics)
    assert result.verification.status is VerificationStatus.FAILED
    assert not result.success


def test_recovered_changed_output_is_untrusted_too():
    result = apply_rule("lift_subqueries", "SELECT * FROM (SELECT 1 AS a) AS q garbage extra")
    assert any(d.code == "recovered_parse" for d in result.diagnostics)
    assert result.sql != result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert not result.success


def test_evidence_summary_counts_recovered_no_op_as_unchanged_text():
    result = apply_rule("lift_subqueries", "SELECT 1 FROM t WHERE 1 =")
    summary = summarize_evidence([result])
    assert summary.unchanged == 1
    assert summary.changed == summary.useful_evidence == 0


@pytest.mark.parametrize("allow", [False, True])
def test_recovered_no_op_cli_requires_explicit_acceptance(tmp_path, capsys, allow):
    from kumosql.cli import rewrite_main

    source = tmp_path / "input.sql"
    source.write_text("SELECT 1 FROM t WHERE 1 =", encoding="utf-8")
    args = [str(source), "--rule", "lift_subqueries"]
    if allow:
        args.append("--allow-unproven")
    assert rewrite_main(args) == (0 if allow else 3)
    assert "unproven" in capsys.readouterr().err


def test_pipeline_evidence_summary_keeps_recovered_no_op():
    result = apply_rules(["lift_subqueries", "inline_single_use_ctes"],
                         "SELECT 1 FROM t WHERE 1 =")
    assert summarize_evidence([result]).unchanged == 1
