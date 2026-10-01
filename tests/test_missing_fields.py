"""Dataform projects and workflow configs with fields left out must load, not crash."""

import pytest

from kumosql import live_graph
from kumosql.pipeline_loading import _config_tags, load_sqlx_project
from kumosql import workflow_configs

CONFIGS = {
    "empty_list": 'config { type: "table", tags: [] }\nselect 1 as a',
    "blank_list": 'config { type: "table", tags: [ ] }\nselect 1 as a',
    "empty_string": 'config { type: "table", tags: "" }\nselect 1 as a',
    "no_tags": 'config { type: "table" }\nselect 1 as a',
    "empty_config": "config {}\nselect 1 as a",
    "no_config": "select 1 as a",
}


@pytest.mark.parametrize("text", ["tags: []", "tags: [ ]", 'tags: ""', "", "tags:"])
def test_empty_tags_are_none_not_a_crash(text):
    assert _config_tags(text) == ()


def test_every_missing_field_shape_loads(tmp_path):
    for name, text in CONFIGS.items():
        (tmp_path / "definitions").mkdir(exist_ok=True)
        (tmp_path / "definitions" / f"{name}.sqlx").write_text(text)
    pipeline = load_sqlx_project(tmp_path)
    assert len(pipeline.models) == len(CONFIGS)
    assert not [d for d in pipeline.diagnostics if d.code != "duplicate_model"]


def test_git_style_load_with_empty_tags(tmp_path):
    files = {f"definitions/{n}.sqlx": t for n, t in CONFIGS.items()}
    files["workflow_settings.yaml"] = "defaultProject: p\ndefaultDataset: d\n"
    live_graph.load_files(files, "x (main @ abc)")
    assert live_graph.loaded()["label"] == "x (main @ abc)"


def test_workflow_config_rows_survive_missing_fields(monkeypatch):
    monkeypatch.setattr(workflow_configs, "_list_all", lambda url, key: [
        {}, {"name": None, "invocationConfig": None, "releaseConfig": None, "cronSchedule": None},
        {"name": "p/r/workflowConfigs/a", "invocationConfig": {"includedTags": None, "includedTargets": [None, {}]}},
    ])
    rows = workflow_configs.config_rows("projects/p/locations/l/repositories/r", "now")
    assert len(rows) == 3 and rows[2]["WORKFLOW_CONFIGURATION"] == "a"


@pytest.mark.parametrize("value", [None, "", 5, "git@github.com:Owner/Repo.git"])
def test_norm_git_url_accepts_anything(value):
    assert isinstance(workflow_configs.norm_git_url(value), str)


def test_unexpected_failures_name_the_step_and_repository(monkeypatch):
    from kumosql import git_repo

    monkeypatch.setattr(git_repo, "fetch_project", lambda *a, **k: {
        "repository": "team/dataform", "branch": "main", "commit": "abc", "files": {"definitions/a.sqlx": "select 1"}})

    def boom(*a, **k):
        raise TypeError("expected string or bytes-like object, got 'NoneType'")

    monkeypatch.setattr(live_graph, "load_files", boom)
    with pytest.raises(git_repo.GitRepoError, match=r"team/dataform \(main @ abc\).*building the graph|Reading the SQL files of team/dataform"):
        git_repo.load_into_graph("https://example.com/team/dataform.git")
