import json

import pytest

from kumosql import load_sqlx_project
from kumosql.cli import pipeline_main
from kumosql.coverage import sample_impact_reports, score_verdicts, wilson_interval


def write(root, name, text):
    (root / name).write_text(text, encoding="utf-8")


def complete_project(tmp_path):
    write(tmp_path, "a.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "b.sql", "SELECT id FROM a")
    return load_sqlx_project(tmp_path)


def test_complete_pipeline_coverage(tmp_path):
    coverage = complete_project(tmp_path).report()["coverage"]

    assert coverage["complete"] is True
    assert coverage["assets_total"] == coverage["assets_analyzed"] == 2
    assert coverage["statements_total"] == coverage["statements_matched"] == 2
    assert coverage["statements_matched_ratio"] == 1.0
    assert coverage["edges_total"] == sum(coverage["edges_by_source"].values())
    assert coverage["edges_total"] == sum(coverage["edges_by_confidence"].values())
    assert coverage["sampled_impact_accuracy"] is None and coverage["sample_size"] == 0


def test_incomplete_analysis_is_visible_and_anonymous(tmp_path):
    write(tmp_path, "secret_orders.sql", "SELECT id FROM `proj.raw.secret_people`")
    write(tmp_path, "broken.sql", "SELECT id FROM secret_orders WHERE (")
    write(tmp_path, "script.sql", "SELECT 1 AS x; SELECT id FROM secret_orders")
    pipeline = load_sqlx_project(tmp_path)

    coverage = pipeline.coverage()

    assert coverage["complete"] is False
    assert coverage["assets_analyzed"] < coverage["assets_total"]
    assert coverage["statements_total"] == 4 and coverage["statements_matched"] == 2
    assert coverage["blocking_gaps"] >= 2
    text = json.dumps(coverage)
    for leaked in ("secret", "broken", "script", "SELECT", "proj"):
        assert leaked not in text


def test_verdict_scoring_is_pure_and_correct():
    score = score_verdicts(
        {"a": {"correct": 8, "false_positive": 2}, "b": {"correct": 7, "missed": 3}}
    )
    assert score["sample_size"] == 2
    assert score["precision"] == round(15 / 17, 4)
    assert score["recall"] == 0.8333
    assert score["sampled_impact_accuracy"] == round(15 / 20, 4)
    low, high = score["precision_interval"]
    assert low < score["precision"] < high
    assert score_verdicts(None)["sampled_impact_accuracy"] is None
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        score_verdicts({"a": {"correct": -1}})


def test_verdicts_flow_into_report_and_sample_is_deterministic(tmp_path):
    pipeline = complete_project(tmp_path)
    report = pipeline.report()
    first = sample_impact_reports(report, 5, seed="s")
    assert first == sample_impact_reports(report, 5, seed="s")
    assert all(row["impacted"] for row in first)

    verdicts = {row["sample_id"]: {"correct": 1} for row in first}
    coverage = pipeline.coverage(verdicts=verdicts)
    assert coverage["sample_size"] == len(first)
    assert coverage["sampled_impact_accuracy"] == 1.0


def test_cli_gate_and_sampling(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    write(project, "a.sql", "SELECT id FROM `proj.raw.people`")
    out = tmp_path / "r.json"
    assert pipeline_main([str(project), "--min-coverage", "0.9", "-o", str(out)]) == 0
    sheet = tmp_path / "sheet.json"
    assert pipeline_main([str(project), "--sample-impact", "3", "-o", str(sheet)]) == 0
    assert isinstance(json.loads(sheet.read_text()), list)

    write(project, "broken.sql", "SELECT id FROM a WHERE (")
    assert pipeline_main([str(project), "--min-coverage", "0.1", "-o", str(out)]) == 3


def test_thresholds_gate_in_report_and_validation(tmp_path):
    from kumosql.coverage import Thresholds

    pipeline = complete_project(tmp_path)
    gate = pipeline.coverage(thresholds=Thresholds(min_assets_analyzed=0.9, require_complete=True))["gate"]
    assert gate["passed"] is True and gate["failed"] == []
    assert pipeline.coverage()["gate"] is None

    gate = pipeline.coverage(thresholds=Thresholds(min_accuracy=0.8, min_sample_size=5))["gate"]
    assert gate["passed"] is False and gate["failed"] == ["min_accuracy", "min_sample_size"]
    assert gate["checks"][0]["reason"] == "not_measured"

    report = pipeline.report()
    verdicts = {row["sample_id"]: {"correct": 9, "false_positive": 1} for row in sample_impact_reports(report, 2)}
    gate = pipeline.coverage(verdicts=verdicts, thresholds=Thresholds(min_precision=0.85, min_sample_size=2))["gate"]
    assert gate["passed"] is True

    with pytest.raises(ValueError):
        Thresholds(min_recall=1.5)
    with pytest.raises(ValueError):
        Thresholds.from_json({"min_nope": 1})


def test_scoped_coverage_counts_only_the_scope(tmp_path):
    from kumosql.scopes import Scope

    write(tmp_path, "good.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "bad.sql", "SELECT id FROM good WHERE (")
    write(tmp_path, "other.sql", "SELECT id FROM good")
    pipeline = load_sqlx_project(tmp_path)

    whole = pipeline.coverage()
    assert whole["scoped"] is False and whole["assets_total"] == 3 and whole["complete"] is False

    fine = pipeline.coverage(scope=Scope("Sample Team", {"name": ("good", "other")}))
    assert fine["scoped"] is True
    assert fine["assets_total"] == 2 and fine["statements_total"] == 2 and fine["statements_matched"] == 2
    assert fine["complete"] is True
    assert "Sample" not in json.dumps(fine)

    broken = pipeline.coverage(scope=Scope("Other", {"name": ("bad",)}))
    assert broken["assets_total"] == 1 and broken["statements_matched"] == 0 and broken["complete"] is False
    assert pipeline.report(scope=Scope("Other", {"name": ("bad",)}))["coverage"] == broken


def test_cli_thresholds_and_scoped_gate(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    write(project, "a.sql", "SELECT id FROM `proj.raw.people`")
    write(project, "broken.sql", "SELECT id FROM a WHERE (")
    out = tmp_path / "r.json"
    assert pipeline_main([str(project), "--require-complete", "-o", str(out)]) == 3
    assert json.loads(out.read_text())["coverage"]["gate"]["failed"] == ["require_complete"]

    file = tmp_path / "t.json"
    file.write_text(json.dumps({"min_columns_traced": 0.5, "require_complete": True}))
    assert pipeline_main([str(project), "--thresholds", str(file), "-o", str(out)]) == 3
    assert pipeline_main([str(project), "--min-accuracy", "0.9", "-o", str(out)]) == 3
    with pytest.raises(SystemExit):
        pipeline_main([str(project), "--min-recall", "2"])
    file.write_text('{"bogus": 1}')
    with pytest.raises(SystemExit):
        pipeline_main([str(project), "--thresholds", str(file)])
