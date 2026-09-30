"""The graph page serves the loaded project and labels sample data (issues #24, #26, #28)."""

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


def test_nothing_loaded_serves_labeled_sample_data(server):
    payload = get(server, "/api/graph")
    assert payload["preview"] is True
    assert payload["source"] == {"kind": "sample", "label": "Sample data"}


def test_loaded_project_replaces_the_sample(server):
    post(server, "/api/project", {"files": FILES, "label": "demo"})
    payload = get(server, "/api/graph")
    assert "preview" not in payload
    assert payload["source"] == {"kind": "project", "label": "demo"}
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


def test_clear_restores_sample(server):
    post(server, "/api/project", {"files": FILES})
    assert post(server, "/api/project/clear", {}) == {"loaded": False}
    assert get(server, "/api/graph")["preview"] is True


@pytest.mark.parametrize("files", [
    {}, {"../evil.sqlx": "SELECT 1"}, {"/abs.sqlx": "SELECT 1"}, {"notes.txt": "x"}, {"a.sqlx": 5},
])
def test_invalid_project_files_are_rejected(server, files):
    with pytest.raises(HTTPError) as error:
        post(server, "/api/project", {"files": files})
    assert error.value.code == 400
    assert get(server, "/api/graph")["preview"] is True


def test_repository_load_builds_the_graph(server, tmp_path, monkeypatch):
    from kumosql import git_repo

    monkeypatch.setattr(
        git_repo, "fetch_project",
        lambda url, branch=None, refresh=False: {
            "repository": "repo", "branch": branch or "main", "commit": "abc1234", "files": FILES},
    )
    result = post(server, "/api/project/git", {"url": "git@example.com:org/repo.git", "branch": "dev"})
    assert result == {"loaded": True, "label": "repo (dev @ abc1234)", "files": 3}
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

    monkeypatch.setattr(ui, "ThreadingHTTPServer", boom)
    with pytest.raises(Stop):
        ui.main(["--project", str(tmp_path), "--no-browser"])
    assert live_graph.loaded()["label"] == str(tmp_path)


def get(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def test_impact_endpoint_serves_labeled_sample_and_validates(server):
    sample = get(server, "/api/impact?node=staging.stg_orders&column=amount_usd&change=drop")
    assert sample["source"]["kind"] == "sample"
    assert {a["model"] for a in sample["affected"]} >= {"marts.fct_orders", "marts.daily_revenue"}
    observed = {o["model"]: o for o in sample["observed"]}
    assert observed["reporting.exec_dashboard"]["effect"] == "may_break"
    assert observed["reporting.exec_dashboard"]["last_seen"] and observed["reporting.exec_dashboard"]["confidence"]
    assert not set(observed) & {a["model"] for a in sample["affected"]}
    assert sample["safe_to_delete"] == "unknown"
    for path in ("/api/impact?node=a&column=b&change=explode", "/api/impact?node=a", "/api/impact"):
        with pytest.raises(HTTPError) as error:
            get(server, path)
        assert error.value.code == 400


def test_impact_endpoint_uses_the_loaded_project_and_its_job_history(server):
    pipeline = live_graph.load_files(FILES, "demo")
    live_graph.set_project(
        pipeline, "demo",
        observed_reads=[{"job_id": "j1", "creation_time": "2026-09-20T06:00:00Z",
                        "destination": "proj.rep.board", "referenced_tables": ["stg_orders"]}],
    )
    result = get(server, "/api/impact?node=stg.stg_orders&column=amt&change=drop")
    assert result["source"] == {"kind": "project", "label": "demo"}
    assert [a["model"] for a in result["affected"]] == ["marts.fct"]
    assert [(o["model"], o["depth"]) for o in result["observed"]] == [("proj.rep.board", 1)]
    graph = get(server, "/api/graph")
    assert any(e["source"] == "observed" for e in graph["edges"])


def test_overlaps_endpoint_serves_labeled_sample_and_validates(server):
    sample = get(server, "/api/overlaps?node=marts.daily_revenue")
    assert sample["source"]["kind"] == "sample" and sample["preview"] is True
    assert [m["kind"] for m in sample["matches"]] == ["same_meaning", "partial"]
    assert "compared" in sample["summary"] and "skipped" in sample["summary"]
    assert "No match" in get(server, "/api/overlaps?node=raw.orders")["summary"]
    with pytest.raises(HTTPError) as error:
        get(server, "/api/overlaps")
    assert error.value.code == 400


def test_overlaps_endpoint_compares_the_loaded_project(server):
    from test_overlap_report import BY_REGION, RENAMED, build

    live_graph.set_project(build({"state_totals": BY_REGION, "revenue": RENAMED}), "demo")
    result = get(server, "/api/overlaps?node=proj.core.revenue")
    assert result["source"] == {"kind": "project", "label": "demo"} and result["status"] == "ok"
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


def test_active_scope_limits_impact_and_labels_sample_pages(server):
    live_graph.set_project(live_graph.load_files(FILES, "demo"), "demo", observed_reads=JOBS)
    _put_scopes(server, [{"name": "Ana", "rule": {"field": "submitter", "op": "eq", "value": "ana@co.com"}}])
    result = get(server, "/api/impact?node=stg.stg_orders&column=amt&change=drop&scope=Ana")
    assert [o["model"] for o in result["observed"]] == ["proj.rep.board"]
    assert result["scope_plan"]["applied_to"] == ["job history"]

    live_graph.clear_project()
    for page in ("/api/graph", "/api/cost", "/api/changes"):
        note = get(server, page + "?scope=Ana")["scope"]
        assert note["applied_to"] == [] and "Sample data" in note["note"]
        assert get(server, page)["scope"] is None
