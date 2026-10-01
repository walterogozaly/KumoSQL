"""The graph page serves the loaded project, or an empty state when nothing is loaded (issues #24, #26, #28)."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kumosql import github_repo, live_graph
from kumosql.ui import UIHandler

FILES = {
    "dataform.json": '{"defaultDataset": "stg", "defaultProject": "proj"}',
    "definitions/stg_orders.sqlx": 'config { type: "table" }\nSELECT id, amount * 2 AS amt FROM `proj.raw.orders`',
    "definitions/fct.sqlx": 'config { type: "table", schema: "marts" }\n'
                            'SELECT id, SUM(amt) AS total FROM ${ref("stg_orders")} GROUP BY id',
}
BROKEN = {**FILES, "definitions/broken.sqlx": 'config { type: "table", schema: "marts" }\nSELECT FROM WHERE ('}


@pytest.fixture(autouse=True)
def _reset():
    live_graph.clear_project()
    yield
    live_graph.clear_project()


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def get(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def post(base, path, payload):
    request = Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        return json.load(response)


def test_nothing_loaded_serves_an_empty_state_not_sample_data(server):
    payload = get(server, "/api/graph")
    assert payload["empty"] is True and payload["needs"] == "project"
    assert payload["source"]["kind"] == "none"
    assert "nodes" not in payload and "preview" not in payload


def test_loaded_project_is_served(server):
    post(server, "/api/project", {"files": FILES, "label": "demo"})
    payload = get(server, "/api/graph")
    assert "empty" not in payload and "preview" not in payload
    assert payload["source"] == {"kind": "project", "label": "demo", "git": False, "jobs": None}
    ids = {node["id"] for node in payload["nodes"]}
    assert {"marts.fct", "stg_orders", "proj.raw.orders"} <= ids
    kinds = {node["id"]: node["kind"] for node in payload["nodes"]}
    assert kinds["marts.fct"] == "model" and kinds["proj.raw.orders"] == "source"
    for edge in payload["edges"]:
        assert edge["from"] in ids and edge["to"] in ids
        assert edge["source"] in ("declared", "observed", "both", "parsed")
        assert edge["confidence"] in ("high", "medium", "low")
    columns = {node["id"]: node["columns"] for node in payload["nodes"]}
    assert columns["marts.fct"] == ["id", "total"]
    assert "amount" in columns["proj.raw.orders"]
    for row in payload["column_lineage"]:
        assert row["column"] in columns[row["node"]]
        assert all(s["column"] in columns[s["node"]] for s in row["sources"])
    # Tables outside the project are listed, but they do not make the graph partial.
    assert payload["coverage"]["complete"] is True
    assert payload["completeness"]["complete"] is True
    assert all(not gap["blocking"] for gap in payload["gaps"])


def test_partial_project_reports_its_gaps(server):
    post(server, "/api/project", {"files": BROKEN, "label": "partial"})
    payload = get(server, "/api/graph")
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["assets_analyzed"] < payload["coverage"]["assets_total"]
    assert payload["completeness"]["complete"] is False
    assert not any(payload["completeness"]["views"].values())
    blocking = [gap for gap in payload["gaps"] if gap["blocking"]]
    assert {gap["kind"] for gap in blocking} >= {"parse_error", "unattributed_reads"}
    assert {gap["asset"] for gap in blocking} == {"marts.broken"}
    node = next(n for n in payload["nodes"] if n["id"] == "marts.broken")
    assert "note" in node


def test_clear_returns_to_the_empty_state(server):
    post(server, "/api/project", {"files": FILES})
    assert post(server, "/api/project/clear", {}) == {"loaded": False}
    assert get(server, "/api/graph")["empty"] is True


@pytest.mark.parametrize("files", [
    {}, {"../evil.sqlx": "SELECT 1"}, {"/abs.sqlx": "SELECT 1"}, {"notes.txt": "x"}, {"a.sqlx": 5},
])
def test_invalid_project_files_are_rejected(server, files):
    with pytest.raises(HTTPError) as error:
        post(server, "/api/project", {"files": files})
    assert error.value.code == 400
    assert get(server, "/api/graph")["empty"] is True


def test_repository_load_builds_the_graph(server, tmp_path, monkeypatch):
    from kumosql import git_repo

    monkeypatch.setattr(
        git_repo, "fetch_project",
        lambda url, branch=None, refresh=False: {
            "repository": "repo", "branch": branch or "main", "commit": "abc1234", "files": FILES},
    )
    result = post(server, "/api/project/git", {"url": "git@example.com:org/repo.git", "branch": "dev"})
    assert {k: result[k] for k in ("loaded", "label", "files")} == {"loaded": True, "label": "repo (dev @ abc1234)", "files": 3}
    assert get(server, "/api/graph")["source"]["label"] == "repo (dev @ abc1234)"


def test_repository_load_reports_git_errors(server, tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    with pytest.raises(HTTPError) as error:
        post(server, "/api/project/git", {"url": str(tmp_path / "nope.git")})
    assert error.value.code == 400
    assert "git clone failed" in error.value.read().decode()


def test_fetch_project_reads_config_and_refuses_large_projects(monkeypatch):
    listing = {"repository": "org/repo", "branch": "main",
               "files": ["definitions/a.sqlx"], "config": ["dataform.json"]}
    monkeypatch.setattr(github_repo, "connect", lambda url: listing)
    monkeypatch.setattr(github_repo, "_read_file", lambda url, branch, path: {"path": path, "content": "x"})
    fetched = github_repo.fetch_project("https://github.com/org/repo")
    assert list(fetched["files"]) == ["dataform.json", "definitions/a.sqlx"]
    listing["files"] = [f"definitions/{i}.sqlx" for i in range(github_repo.MAX_PROJECT_FILES + 1)]
    with pytest.raises(github_repo.GitHubRepoError, match="limited to"):
        github_repo.fetch_project("https://github.com/org/repo")


def test_cli_project_option_loads_a_folder(tmp_path, monkeypatch):
    from kumosql import ui

    for name, text in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    class Stop(Exception):
        pass

    def boom(*_args, **_kwargs):
        raise Stop

    monkeypatch.setattr(ui, "UIServer", boom)
    with pytest.raises(Stop):
        ui.main(["--project", str(tmp_path), "--no-browser"])
    assert live_graph.loaded()["label"] == str(tmp_path)


def get(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def test_impact_endpoint_needs_a_project_and_validates(server):
    for path in ("/api/impact?node=a&column=b&change=drop", "/api/overlaps?node=a",
                 "/api/impact?node=a", "/api/impact", "/api/overlaps"):
        with pytest.raises(HTTPError) as error:
            get(server, path)
        assert error.value.code == 400
    live_graph.load_files(FILES, "demo")
    with pytest.raises(HTTPError) as error:
        get(server, "/api/impact?node=a&column=b&change=explode")
    assert error.value.code == 400


def test_impact_endpoint_uses_the_loaded_project_and_its_job_history(server):
    pipeline = live_graph.load_files(FILES, "demo")
    live_graph.set_project(
        pipeline, "demo",
        observed_reads=[{"job_id": "j1", "creation_time": "2026-09-20T06:00:00Z",
                        "destination": "proj.rep.board", "referenced_tables": ["stg_orders"]}],
    )
    result = get(server, "/api/impact?node=stg.stg_orders&column=amt&change=drop")
    assert result["source"]["label"] == "demo" and result["source"]["jobs"]["count"] == 1
    assert [a["model"] for a in result["affected"]] == ["marts.fct"]
    assert [(o["model"], o["depth"]) for o in result["observed"]] == [("proj.rep.board", 1)]
    graph = get(server, "/api/graph")
    assert any(e["source"] == "observed" for e in graph["edges"])


def test_overlaps_endpoint_compares_the_loaded_project(server):
    from test_overlap_report import BY_REGION, RENAMED, build

    live_graph.set_project(build({"state_totals": BY_REGION, "revenue": RENAMED}), "demo")
    result = get(server, "/api/overlaps?node=proj.core.revenue")
    assert result["source"]["label"] == "demo" and result["status"] == "ok"
    assert [(m["key"], m["kind"]) for m in result["matches"]] == [("proj.core.state_totals", "same_meaning")]
    assert "compared 1 of 1 tables" in result["summary"]
    for path in ("/api/overlaps?node=proj.raw.orders", "/api/overlaps?node=proj.core.revenue&scope=missing"):
        with pytest.raises(HTTPError) as error:
            get(server, path)
        assert error.value.code == 400


def _put_scopes(base, scopes):
    request = Request(base + "/api/settings/scopes", data=json.dumps(scopes).encode(), method="PUT",
                      headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        return json.load(response)


JOBS = [
    {"job_id": "j1", "creation_time": "2026-09-20T06:00:00Z", "destination": "proj.rep.board",
     "referenced_tables": ["stg_orders"], "submitter": "ana@co.com"},
    {"job_id": "j2", "creation_time": "2026-09-20T07:00:00Z", "destination": "proj.rep.other",
     "referenced_tables": ["stg_orders"], "submitter": "bo@co.com"},
]


def test_active_scope_limits_the_graph_to_models_and_job_history(server):
    live_graph.set_project(live_graph.load_files(FILES, "demo"), "demo", observed_reads=JOBS)
    _put_scopes(server, [
        {"name": "Ana", "rule": {"field": "submitter", "op": "in", "value": ["ana@co.com"]}},
        {"name": "Marts", "rule": {"field": "dataset", "op": "eq", "value": "marts"}},
        {"name": "Typo", "rule": {"field": "submiter", "op": "eq", "value": "x"}},
    ])
    everything = get(server, "/api/graph")
    assert everything["scope"] is None

    ana = get(server, "/api/graph?scope=Ana")
    assert ana["scope"]["applied_to"] == ["job history"] and "models have no" in ana["scope"]["note"]
    assert sum(e["observed_count"] for e in ana["edges"]) == 1
    assert {n["id"] for n in ana["nodes"]} >= {n["id"] for n in everything["nodes"] if n["kind"] == "model"}

    marts = get(server, "/api/graph?scope=Marts")
    assert marts["scope"]["applied_to"] == ["models"]
    assert "marts.fct" in {n["id"] for n in marts["nodes"]}
    assert len(marts["column_lineage"]) < len(everything["column_lineage"])

    for bad, words in (("/api/graph?scope=Missing", "no saved scope"), ("/api/graph?scope=Typo", "cannot be applied")):
        with pytest.raises(HTTPError) as error:
            get(server, bad)
        assert error.value.code == 400
        assert words in json.load(error.value)["error"]


def test_active_scope_limits_impact_and_notes_missing_project(server):
    live_graph.set_project(live_graph.load_files(FILES, "demo"), "demo", observed_reads=JOBS)
    _put_scopes(server, [{"name": "Ana", "rule": {"field": "submitter", "op": "eq", "value": "ana@co.com"}}])
    result = get(server, "/api/impact?node=stg.stg_orders&column=amt&change=drop&scope=Ana")
    assert [o["model"] for o in result["observed"]] == ["proj.rep.board"]
    assert result["scope_plan"]["applied_to"] == ["job history"]

    live_graph.clear_project()
    for page in ("/api/graph", "/api/cost", "/api/changes"):
        note = get(server, page + "?scope=Ana")["scope"]
        assert note["applied_to"] == [] and "Load a project" in note["note"]
        assert get(server, page)["scope"] is None


def test_scope_built_from_other_scopes_applies_to_job_history(server):
    live_graph.set_project(live_graph.load_files(FILES, "demo"), "demo", observed_reads=JOBS)
    _put_scopes(server, [
        {"name": "ana", "rule": {"field": "submitter", "op": "eq", "value": "ana@co.com"}},
        {"name": "bo", "rule": {"field": "submitter", "op": "eq", "value": "bo@co.com"}},
        {"name": "both_teams", "rule": {"any": [{"scope": "ana"}, {"scope": "bo"}]}},
        {"name": "only_shared", "rule": {"all": [{"scope": "ana"}, {"scope": "bo"}]}},
    ])
    union = get(server, "/api/graph?scope=both_teams")
    assert sum(e["observed_count"] for e in union["edges"]) == 2
    assert union["scope"]["rule"] == "in scope “ana” OR in scope “bo”"
    assert sum(e["observed_count"] for e in get(server, "/api/graph?scope=only_shared")["edges"]) == 0
    with pytest.raises(HTTPError) as error:
        _put_scopes(server, [{"name": "loop", "rule": {"scope": "loop"}}])
    assert error.value.code == 400 and "cannot refer to themselves" in json.load(error.value)["error"]


HISTORY = [{"job_id": "j1", "creation_time": "2026-09-20T06:00:00Z", "destination_table": "marts.fct",
            "referenced_tables": [{"project_id": "proj", "dataset_id": "stg", "table_id": "stg_orders"}],
            "total_bytes_billed": 2 ** 40, "total_bytes_processed": 2 ** 40, "user_email": "ana@co.com"}]


def test_job_history_loads_from_the_page_in_every_format(server):
    import csv, io

    post(server, "/api/project", {"files": FILES, "label": "demo"})
    as_json = json.dumps(HISTORY)
    as_lines = "\n".join(json.dumps(row) for row in HISTORY)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(HISTORY[0]))
    writer.writeheader()
    writer.writerow({k: json.dumps(v) if isinstance(v, list) else v for k, v in HISTORY[0].items()})
    for name, text in (("jobs.json", as_json), ("jobs.jsonl", as_lines), ("jobs.csv", buffer.getvalue())):
        assert post(server, "/api/jobs", {"filename": name, "text": text}) == {"jobs": 1}
        source = get(server, "/api/graph")["source"]
        assert source["jobs"] == {"label": name, "count": 1}
    assert post(server, "/api/jobs/clear", {}) == {"jobs": 0}
    assert get(server, "/api/graph")["source"]["jobs"] is None


@pytest.mark.parametrize("text", ["", "   ", "[1, 2]", "[]", '{"a": '])
def test_bad_job_history_is_rejected(server, text):
    post(server, "/api/project", {"files": FILES})
    with pytest.raises(HTTPError) as error:
        post(server, "/api/jobs", {"filename": "jobs.json", "text": text})
    assert error.value.code == 400


def test_job_history_needs_a_project(server):
    with pytest.raises(HTTPError) as error:
        post(server, "/api/jobs", {"filename": "jobs.json", "text": json.dumps(HISTORY)})
    assert error.value.code == 400 and "load a project" in json.load(error.value)["error"]


def test_a_new_project_keeps_the_loaded_job_history(server):
    post(server, "/api/project", {"files": FILES})
    post(server, "/api/jobs", {"filename": "jobs.json", "text": json.dumps(HISTORY)})
    post(server, "/api/project", {"files": FILES, "label": "again"})
    assert get(server, "/api/graph")["source"]["jobs"]["count"] == 1


def test_cost_page_is_empty_without_a_project_and_real_with_one(server):
    empty = get(server, "/api/cost")
    assert empty["empty"] is True and empty["needs"] == "project" and "totals" not in empty

    post(server, "/api/project", {"files": FILES, "label": "demo"})
    no_jobs = get(server, "/api/cost")
    assert no_jobs["has_jobs"] is False and no_jobs["totals"] is None and no_jobs["nodes"] == []
    assert "preview" not in no_jobs and no_jobs["rules"]

    post(server, "/api/jobs", {"filename": "jobs.json", "text": json.dumps(HISTORY)})
    cost = get(server, "/api/cost")
    assert cost["has_jobs"] is True and cost["unit"] == "bytes_billed"
    assert cost["totals"]["measured"] == 2 ** 40
    priced = get(server, "/api/cost?rate=6.25")
    assert priced["unit"] == "currency" and priced["totals"]["measured"] == 6.25
    for bad in ("/api/cost?rate=abc", "/api/cost?rate=-1"):
        with pytest.raises(HTTPError) as error:
            get(server, bad)
        assert error.value.code == 400


def test_changes_page_needs_git_and_a_base_branch(server):
    assert get(server, "/api/changes")["empty"] is True

    post(server, "/api/project", {"files": FILES, "label": "demo"})
    page = get(server, "/api/changes")
    assert page["can_compare"] is False and page["report"] is None and "ci" not in page
    assert [s["name"] for s in page["sources"]] == ["Dataform project", "Job history"]
    with pytest.raises(HTTPError) as error:
        post(server, "/api/changes/compare", {"base": "main"})
    assert error.value.code == 400 and "git" in json.load(error.value)["error"]


def test_changes_compare_builds_the_report_from_two_branches(server, monkeypatch):
    from kumosql import git_repo

    base = {**FILES, "definitions/stg_orders.sqlx": 'config { type: "table" }\nSELECT id, amount AS amt FROM `proj.raw.orders`'}
    branches = {"main": base, "feature": FILES}

    def fetch(url, branch=None, refresh=False):
        name = branch or "feature"
        return {"repository": "demo", "branch": name, "commit": "abc1234", "files": branches[name]}

    monkeypatch.setattr(git_repo, "fetch_project", fetch)
    monkeypatch.setattr("kumosql.live_insights.fetch_project", fetch, raising=False)
    post(server, "/api/github/load", {"url": "https://example.com/demo.git"})
    page = get(server, "/api/changes")
    assert page["can_compare"] is True and page["remote_branch"] == "feature"

    result = post(server, "/api/changes/compare", {"base": "main"})
    report = result["report"]
    assert report["base"] == "main @ abc1234" and report["head"] == "feature @ abc1234"
    assert [c["model"] for c in report["changes"]] == ["stg_orders"]
    assert report["changes"][0]["kind"] == "modified"
    assert result["evidence_coverage"]["changed"] == 1
    assert result["ci"]["check_name"] == "KumoSQL change report"
    assert get(server, "/api/changes")["report"] == report
    # Loading another project drops the comparison made for the old one.
    post(server, "/api/project", {"files": FILES})
    assert get(server, "/api/changes")["report"] is None


def test_information_schema_job_history_matches_graph_tables(server):
    # bq exports of INFORMATION_SCHEMA.JOBS name tables as {project_id, dataset_id, table_id}.
    files = {**FILES, "workflow_settings.yaml": "defaultProject: proj\ndefaultDataset: stg\n"}
    post(server, "/api/project", {"files": files, "label": "demo"})
    history = [{**HISTORY[0], "destination_table": {"project_id": "proj", "dataset_id": "marts", "table_id": "fct"}}]
    post(server, "/api/jobs", {"filename": "jobs.json", "text": json.dumps(history)})
    graph = get(server, "/api/graph")
    observed = [edge for edge in graph["edges"] if edge["observed_count"]]
    assert [(edge["from"], edge["to"]) for edge in observed] == [("proj.stg.stg_orders", "proj.marts.fct")]
    assert not [gap for gap in graph["gaps"] if gap["asset"] == "job history"]


def test_job_history_survives_a_restart_until_removed(server):
    post(server, "/api/project", {"files": FILES, "label": "demo"})
    post(server, "/api/jobs", {"filename": "jobs.json", "text": json.dumps(HISTORY)})
    # A restart: the in-memory history is gone, the saved copy is read back.
    live_graph._JOBS.update(records=(), label="", restored_from=None)
    assert get(server, "/api/graph")["source"]["jobs"] == {"label": "jobs.json", "count": 1}
    post(server, "/api/jobs/clear", {})
    live_graph._JOBS.update(records=(), label="", restored_from=None)
    assert get(server, "/api/graph")["source"]["jobs"] is None
