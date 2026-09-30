"""Opt-in synthetic-data evidence on rewrite results (synthetic tables only)."""

import importlib.util
import json

import pytest

from kumosql import (
    RewriteResult,
    Verification,
    VerificationStatus,
    apply_rules,
    attach_synthetic_check,
    summarize_evidence,
    verify_rewrite,
)
from kumosql.cli import rewrite_main

pytest.importorskip("duckdb")

SCHEMA = {"p.d.t": {"x": "INT64", "y": "INT64"}}
BASE = "SELECT x FROM `p.d.t`"
AGREEING = "SELECT x FROM `p.d.t` WHERE x IS NOT NULL OR x IS NULL"  # unproven, same rows
DIFFERING = "SELECT x FROM `p.d.t` WHERE x > 2"  # unproven, different rows
NONDETERMINISTIC = "SELECT x, RAND() AS r FROM `p.d.t`"
NONDETERMINISTIC_AFTER = "SELECT x, RAND() AS r FROM `p.d.t` WHERE TRUE"
PROVABLE = "SELECT x FROM `p.d.t` WHERE TRUE AND x > 1"


def make(before, after, status=None):
    verification = verify_rewrite(before, after)
    if status is not None:
        verification = Verification(status, "forced", (), verification.checks)
    return RewriteResult("r", before, after, 1, (), verification, True)


def synthetic_check(result):
    checks = [c for c in result.verification.checks if c.kind == "synthetic_results"]
    assert len(checks) == 1
    return checks[0]


def test_agreement_is_recorded_but_never_trusts_an_unproven_result():
    result = make(BASE, AGREEING)
    assert result.verification.status is VerificationStatus.UNPROVEN
    attached = attach_synthetic_check(result, SCHEMA)
    check = synthetic_check(attached)
    assert check.outcome == "passed"
    assert "not proof" in check.detail
    assert attached.verification.status is VerificationStatus.UNPROVEN
    assert not attached.verification.trusted and not attached.success
    assert dict(check.evidence)["seeds"] == tuple(range(8))
    assert dict(check.evidence)["seeds_checked"] == tuple(range(8))
    assert dict(check.evidence)["failing_seed"] is None


def test_agreement_leaves_a_proven_result_proven():
    result = apply_rules(["remove_trivial_predicates"], PROVABLE)
    assert result.verification.status is VerificationStatus.PROVEN
    attached = attach_synthetic_check(result, SCHEMA)
    assert synthetic_check(attached).outcome == "passed"
    assert attached.verification.status is VerificationStatus.PROVEN
    assert attached.verification.trusted


def test_summary_counts_passing_agreement_as_useful_evidence():
    results = [attach_synthetic_check(make(BASE, AGREEING), SCHEMA) for _ in range(2)]
    results.append(attach_synthetic_check(make(BASE, DIFFERING), SCHEMA))
    results.append(make(BASE, AGREEING))
    summary = summarize_evidence(results, min_changed=1)
    assert summary.synthetic_agreed == 2
    assert summary.useful_evidence == 2
    assert summary.proven == 0


def test_disagreement_fails_the_check_and_demotes_without_query_text():
    attached = attach_synthetic_check(make(BASE, DIFFERING), SCHEMA)
    check = synthetic_check(attached)
    assert check.outcome == "failed"
    assert attached.verification.status is VerificationStatus.UNPROVEN
    evidence = dict(check.evidence)
    seed = evidence["failing_seed"]
    assert isinstance(seed, int) and f"seed {seed}" in check.detail
    assert evidence["rows_only_in_original"] > 0
    blob = (
        json.dumps(check.to_json())
        + attached.verification.reason
        + " ".join(attached.verification.details)
    )
    for fragment in ("SELECT", "FROM", "p.d.t", "WHERE"):
        assert fragment not in blob
    # A failed check is not useful evidence.
    assert summarize_evidence([attached], min_changed=1).synthetic_agreed == 0


def test_disagreement_demotes_a_proven_result():
    forced = make(BASE, DIFFERING, status=VerificationStatus.PROVEN)
    assert forced.verification.trusted
    attached = attach_synthetic_check(forced, SCHEMA)
    assert synthetic_check(attached).outcome == "failed"
    assert attached.verification.status is VerificationStatus.UNPROVEN
    assert not attached.success


def test_nondeterministic_query_is_inconclusive_and_changes_nothing():
    result = make(NONDETERMINISTIC, NONDETERMINISTIC_AFTER)
    before = result.verification.status
    attached = attach_synthetic_check(result, SCHEMA)
    check = synthetic_check(attached)
    assert check.outcome == "inconclusive"
    assert attached.verification.status is before
    assert attached.verification.trusted == result.verification.trusted
    assert summarize_evidence([attached], min_changed=1).synthetic_agreed == 0


def test_seeds_are_recorded_and_the_check_is_reproducible():
    first = attach_synthetic_check(make(BASE, DIFFERING), SCHEMA, seeds=[3, 5, 9])
    second = attach_synthetic_check(make(BASE, DIFFERING), SCHEMA, seeds=[3, 5, 9])
    assert synthetic_check(first) == synthetic_check(second)
    assert dict(synthetic_check(first).evidence)["seeds"] == (3, 5, 9)


def test_reattaching_replaces_the_earlier_synthetic_check():
    once = attach_synthetic_check(make(BASE, AGREEING), SCHEMA)
    twice = attach_synthetic_check(once, SCHEMA, seeds=[1])
    assert dict(synthetic_check(twice).evidence)["seeds"] == (1,)


def test_missing_duckdb_is_not_run(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "duckdb" else real(name, *a)
    )
    result = make(BASE, AGREEING)
    attached = attach_synthetic_check(result, SCHEMA)
    check = synthetic_check(attached)
    assert check.outcome == "not_run" and "duckdb" in check.detail
    assert attached.verification.status is result.verification.status


def test_unchanged_failed_and_unusable_inputs_are_not_run():
    same = attach_synthetic_check(make(BASE, BASE), SCHEMA)
    assert synthetic_check(same).outcome == "not_run"
    assert same.verification.status is VerificationStatus.UNCHANGED
    failed = RewriteResult(
        "r", BASE, AGREEING, 0, (), Verification(VerificationStatus.FAILED, "x"), False
    )
    assert synthetic_check(attach_synthetic_check(failed, SCHEMA)).outcome == "not_run"
    missing_table = attach_synthetic_check(make(BASE, AGREEING), {"p.d.other": {"x": "INT64"}})
    assert synthetic_check(missing_table).outcome == "not_run"
    assert "SELECT" not in synthetic_check(missing_table).detail
    bad_type = attach_synthetic_check(make(BASE, AGREEING), {"p.d.t": {"x": "GEOGRAPHY"}})
    assert synthetic_check(bad_type).outcome == "not_run"


def test_pipeline_results_are_supported():
    result = apply_rules(["remove_trivial_predicates"], PROVABLE)
    attached = attach_synthetic_check(result, SCHEMA)
    assert attached.steps == result.steps
    assert synthetic_check(attached).outcome == "passed"


def test_no_seeds_is_an_error():
    with pytest.raises(ValueError):
        attach_synthetic_check(make(BASE, AGREEING), SCHEMA, seeds=[])


def test_cli_flag_prints_the_check_and_keeps_trust_unchanged(tmp_path, capsys):
    source = tmp_path / "in.sql"
    source.write_text(PROVABLE + "\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    status = rewrite_main(
        [
            str(source),
            "-r",
            "remove_trivial_predicates",
            "--synthetic-check",
            "--synthetic-schema",
            str(schema),
            "--synthetic-seeds",
            "3",
        ]
    )
    err = capsys.readouterr().err
    assert status == 0
    assert "check synthetic_results=passed" in err
    assert "seeds=[0, 1, 2]" in err


def test_cli_requires_schema_and_rejects_orphan_flags(tmp_path):
    source = tmp_path / "in.sql"
    source.write_text("SELECT 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        rewrite_main([str(source), "-r", "remove_trivial_predicates", "--synthetic-check"])
    with pytest.raises(SystemExit):
        rewrite_main([str(source), "-r", "remove_trivial_predicates", "--synthetic-seeds", "2"])
