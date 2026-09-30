"""One bad asset must not cost the rest of the report."""

from __future__ import annotations

import json
import os
import sys

import pytest

from kumosql.pipeline import PipelineLoadError, load_compiled_graph, load_sqlx_project

GOOD = "SELECT id, name FROM `p.d.src`"
SECRET = "TOP_SECRET_VALUE_123"


def _project(tmp_path):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    defs = tmp_path / "definitions"
    defs.mkdir()
    (defs / "good.sqlx").write_text(f'config {{ type: "table" }}\n{GOOD}\n')
    (defs / "reader.sqlx").write_text('config { type: "table" }\nSELECT id FROM ${ref("good")}\n')
    return defs


def _assets(report):
    return {d["asset"]: d for d in report["diagnostics"]}


def test_undecodable_file_is_a_diagnostic_and_rest_renders(tmp_path):
    defs = _project(tmp_path)
    (defs / "bad.sqlx").write_bytes(b"SELECT '" + SECRET.encode() + b"' \xff\xfe\xfd")
    report = load_sqlx_project(tmp_path).report()
    entry = _assets(report)["definitions/bad.sqlx"]
    assert entry["code"] == "read_error" and entry["analysis_incomplete"]
    assert "p.d.good" in report["order"] and "p.d.reader" in report["order"]
    assert report["diagnostic_summary"]["assets_not_analyzed"] == 1
    assert SECRET not in json.dumps(report["diagnostics"])


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs non-root POSIX",
)
def test_unreadable_file_and_directory(tmp_path):
    defs = _project(tmp_path)
    locked = defs / "locked.sqlx"
    locked.write_text("SELECT 1")
    locked.chmod(0)
    hidden = defs / "hidden"
    hidden.mkdir()
    hidden.chmod(0)
    try:
        report = load_sqlx_project(tmp_path).report()
    finally:
        locked.chmod(0o600)
        hidden.chmod(0o700)
    codes = {d["code"] for d in report["diagnostics"]}
    assert {"read_error", "unreadable_directory"} <= codes
    assert "p.d.good" in report["order"]


def test_broken_symlink_is_a_diagnostic(tmp_path):
    defs = _project(tmp_path)
    (defs / "dangling.sqlx").symlink_to(tmp_path / "nowhere")
    report = load_sqlx_project(tmp_path).report()
    assert _assets(report)["definitions/dangling.sqlx"]["code"] == "read_error"
    assert "p.d.good" in report["order"]


def test_malformed_settings_fall_back_with_diagnostic(tmp_path):
    _project(tmp_path)
    (tmp_path / "workflow_settings.yaml").unlink()
    (tmp_path / "dataform.json").write_text("{not json " + SECRET)
    report = load_sqlx_project(tmp_path).report()
    assert any(d["code"] == "settings_unreadable" for d in report["diagnostics"])
    assert SECRET not in json.dumps(report["diagnostics"])
    assert report["models"] == 2


def test_duplicate_model_is_reported(tmp_path):
    defs = _project(tmp_path)
    (defs / "sub").mkdir()
    (defs / "a.sql").write_text("SELECT 1 AS x")
    (defs / "sub" / "a.sql").write_text("SELECT 2 AS x")
    report = load_sqlx_project(tmp_path).report()
    assert any(d["code"] == "duplicate_model" for d in report["diagnostics"])


def test_unparseable_model_counts_as_incomplete(tmp_path):
    defs = _project(tmp_path)
    (defs / "broken.sqlx").write_text('config { type: "table" }\nSELECT FROM WHERE ((\n')
    report = load_sqlx_project(tmp_path).report()
    assert report["diagnostic_summary"]["analysis_incomplete"] is True
    assert "p.d.good" in report["order"]
    assert all(set(d) >= {"asset", "message", "code"} for d in report["diagnostics"])


def test_compiled_graph_bad_entries_are_local():
    graph = {
        "tables": [
            {"target": {"database": "p", "schema": "d", "name": "ok"}, "query": GOOD},
            {"target": "not-an-object", "query": "SELECT 1"},
            {"target": {"name": "q"}, "queries": [1, 2]},
            "junk",
        ],
        "declarations": [{"target": None}, {"target": {"database": "p", "schema": "d", "name": "src"}}],
        "assertions": "oops",
    }
    report = load_compiled_graph(graph).report()
    assert "p.d.ok" in report["order"]
    codes = [d["code"] for d in report["diagnostics"]]
    assert codes.count("invalid_entry") >= 4


def test_compiled_graph_invalid_json_is_one_clean_error(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text('{"tables": ' + SECRET)
    with pytest.raises(PipelineLoadError) as info:
        load_compiled_graph(path)
    assert SECRET not in str(info.value)


def test_report_section_failure_keeps_diagnostics(tmp_path, monkeypatch):
    _project(tmp_path)
    pipeline = load_sqlx_project(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(type(pipeline), "duplicate_selects", boom)
    report = pipeline.report()
    entry = _assets(report)["duplicates"]
    assert entry["code"] == "section_failed" and SECRET not in entry["message"]
    assert report["order"] and report["duplicates"] == []


def test_cli_missing_root_is_one_line_error(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    code = pipeline_main([str(tmp_path / "missing")])
    err = capsys.readouterr().err
    assert code == 2 and err.count("\n") == 1 and err.startswith("error:")


def test_cli_bad_graph_json_exit_code(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    path = tmp_path / "graph.json"
    path.write_text("not json")
    assert pipeline_main([str(path)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_reports_partial_success(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    defs = _project(tmp_path)
    (defs / "bad.sqlx").write_bytes(b"\xff\xfe")
    assert pipeline_main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["diagnostic_summary"]["assets_not_analyzed"] == 1
    assert "1 asset could not be analyzed" in captured.err
