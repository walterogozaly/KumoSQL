"""Dataform workflow configurations: matching, rows, caching and which models they schedule."""

import pytest

from kumosql import bigquery_catalog as catalog, live_graph, state, workflow_configs as wf
from kumosql.pipeline import load_sqlx_project

URL = "https://github.com/Org/Repo"
PARENT = "projects/p1/locations/us-central1/repositories"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.setattr(catalog, "_disk_loaded", False)
    catalog._memory.clear()
    wf._errors.clear()
    yield
    catalog._memory.clear()


@pytest.mark.parametrize("variant", [
    "https://github.com/org/repo", "https://GitHub.com/Org/Repo.git", "https://github.com/org/repo/",
    "git@github.com:org/repo.git", "ssh://git@github.com/org/repo.git", "https://user@github.com/org/repo.git",
])
def test_url_forms_compare_equal(variant):
    assert wf.norm_git_url(variant) == "github.com/org/repo"


def test_different_repositories_do_not_match():
    assert wf.norm_git_url("https://github.com/org/repo2") != wf.norm_git_url(URL)


def fake_api(monkeypatch, calls=None):
    config = lambda name, **kw: {"name": f"{PARENT}/r1/workflowConfigs/{name}", **kw}
    pages = {
        f"{wf._API}/{PARENT}": [{"repositories": [
            {"name": f"{PARENT}/r1", "gitRemoteSettings": {"url": "git@github.com:org/repo.git"}},
            {"name": f"{PARENT}/other", "gitRemoteSettings": {"url": "https://github.com/org/else"}}]}],
        f"{wf._API}/{PARENT}/r1/workflowConfigs": [
            {"workflowConfigs": [config("nightly", cronSchedule="0 3 * * *", timeZone="UTC",
                                        releaseConfig=f"{PARENT}/r1/releaseConfigs/production",
                                        invocationConfig={"includedTags": ["daily"], "transitiveDependenciesIncluded": True}),
                                 config("off", cronSchedule="0 1 * * *", disabled=True,
                                        releaseConfig=f"{PARENT}/r1/releaseConfigs/production")],
             "nextPageToken": "t"},
            {"workflowConfigs": [config("adhoc", releaseConfig=f"{PARENT}/r1/releaseConfigs/staging",
                                        invocationConfig={"includedTargets": [{"database": "d", "schema": "s", "name": "a"}],
                                                          "transitiveDependentsIncluded": True})]}],
    }

    def get(url):
        if calls is not None:
            calls.append(url)
        base = url.split("?")[0]
        if base not in pages:
            return {}
        return pages[base].pop(0) if len(pages[base]) > 1 else pages[base][0]

    monkeypatch.setattr(wf, "_get", get)


def test_fetch_finds_matching_repository_and_builds_rows(monkeypatch):
    fake_api(monkeypatch)
    data = wf.fetch(URL, ["p1"], "us-central1")
    assert data["repositories"] == ["r1"]
    rows = {row["WORKFLOW_CONFIGURATION"]: row for row in data["configs"]}
    assert set(rows) == {"adhoc", "nightly", "off"}  # second page followed
    assert rows["nightly"]["ACTIVE_PRODUCTION"] is True
    assert rows["nightly"]["INCLUDE_DEPENDENCIES"] is True and rows["nightly"]["INCLUDE_DEPENDENTS"] is False
    assert rows["nightly"]["TAGS"] == "daily"
    assert rows["off"]["ACTIVE_PRODUCTION"] is False  # disabled
    assert rows["adhoc"]["ACTIVE_PRODUCTION"] is False  # no cron, not production
    assert rows["adhoc"]["SPECIFIC_ACTIONS"] == "d.s.a" and rows["adhoc"]["INCLUDE_DEPENDENTS"] is True


def test_no_match_is_reported_not_raised(monkeypatch):
    fake_api(monkeypatch)
    state.set_section("bigquery", {"projects": ["p1"]})
    result = wf.summary("https://github.com/org/unknown", refresh=True)
    assert result["state"] == "no_match" and "No Dataform repository" in result["message"]


def test_needs_projects_until_chosen():
    assert wf.summary(URL)["state"] == "needs_projects"


def test_summary_caches_and_refresh_calls_api_again(monkeypatch):
    calls = []
    fake_api(monkeypatch, calls)
    state.set_section("bigquery", {"projects": ["p1"]})
    assert wf.summary(URL)["state"] == "not_loaded"
    assert calls == []  # reading never calls the API
    loaded = wf.summary(URL, refresh=True)
    assert loaded["state"] == "loaded" and loaded["configs"] == 3 and loaded["active_production"] == 1
    made = len(calls)
    assert wf.summary(URL)["configs"] == 3 and len(calls) == made


def test_failure_keeps_saved_copy_and_reports_error(monkeypatch):
    fake_api(monkeypatch)
    state.set_section("bigquery", {"projects": ["p1"]})
    wf.summary(URL, refresh=True)

    def boom(url):
        raise wf.WorkflowConfigError("Dataform returned HTTP 403: denied")

    monkeypatch.setattr(wf, "_get", boom)
    again = wf.summary(URL, refresh=True)
    assert again["state"] == "loaded" and again["stale"] is True
    other = wf.summary("https://github.com/org/new", refresh=True)
    assert other["state"] == "error" and "403" in other["message"]


def test_override_replaces_selected_projects(monkeypatch):
    state.set_section("bigquery", {"projects": ["p1"]})
    wf.save_settings(URL, "px, py", "europe-west1")
    assert wf.search_for("git@github.com:org/repo.git") == {
        "projects": ["px", "py"], "location": "europe-west1", "override": True}
    wf.save_settings(URL, "", "")
    assert wf.search_for(URL)["projects"] == ["p1"]
    with pytest.raises(ValueError):
        wf.save_settings(URL, "bad id!", "")


def project(tmp_path):
    files = {
        "workflow_settings.yaml": "defaultProject: d\ndefaultDataset: s\n",
        "definitions/a.sqlx": 'config { type: "table", tags: ["daily", "x"] }\nSELECT 1 AS id',
        "definitions/b.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}',
        "definitions/c.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("b")}',
        "definitions/z.sqlx": 'config { type: "table" }\nSELECT 2 AS id',
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def test_sqlx_tags_are_read(tmp_path):
    assert project(tmp_path).models["d.s.a"].tags == ("daily", "x")


def test_scheduled_models_follow_tags_targets_and_dependency_flags(tmp_path):
    pipeline = project(tmp_path)
    base = {"ACTIVE_PRODUCTION": True, "REPO": "r", "CRON_SCHEDULE": "0 3 * * *", "TIME_ZONE": "UTC",
            "INCLUDE_DEPENDENCIES": False, "INCLUDE_DEPENDENTS": False,
            "included_tags": [], "included_targets": []}
    rows = [{**base, "WORKFLOW_CONFIGURATION": "by-tag", "included_tags": ["daily"], "INCLUDE_DEPENDENTS": True},
            {**base, "WORKFLOW_CONFIGURATION": "by-name", "included_targets": ["s.z"]},
            {**base, "WORKFLOW_CONFIGURATION": "inactive", "ACTIVE_PRODUCTION": False, "included_tags": ["x"]}]
    scheduled = wf.scheduled_models(pipeline, rows)
    assert set(scheduled) == {"d.s.a", "d.s.b", "d.s.c", "d.s.z"}
    assert [e["config"] for e in scheduled["d.s.c"]] == ["by-tag"]
    assert [e["config"] for e in scheduled["d.s.z"]] == ["by-name"]
    only_downstream_flag = wf.scheduled_models(pipeline, [{**rows[0], "INCLUDE_DEPENDENTS": False}])
    assert set(only_downstream_flag) == {"d.s.a"}
    everything = wf.scheduled_models(pipeline, [{**base, "WORKFLOW_CONFIGURATION": "all"}])
    assert len(everything) == 4


def test_graph_payload_marks_scheduled_nodes(tmp_path, monkeypatch):
    pipeline = project(tmp_path)
    live_graph.set_project(pipeline, "t", remote={"url": URL, "branch": None})
    try:
        assert live_graph.graph_or_empty()["workflow"] == {"state": "not_loaded"}
        state.set_section("bigquery", {"projects": ["p1"]})
        fake_api(monkeypatch)
        wf.summary(URL, refresh=True)
        payload = live_graph.graph_or_empty()
        by_id = {node["id"]: node for node in payload["nodes"]}
        assert by_id["d.s.a"]["schedules"][0]["config"] == "nightly"
        assert "schedules" not in by_id["d.s.z"]
        assert payload["workflow"]["active_production"] == 1
    finally:
        live_graph.clear_project()


def test_csv_has_the_script_columns():
    text = wf.to_csv([{"REPO": "r", "ACTIVE_PRODUCTION": True, "included_tags": []}])
    assert text.splitlines()[0].startswith("REPO,WORKFLOW_CONFIGURATION,TAGS")
