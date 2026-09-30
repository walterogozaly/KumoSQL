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


def test_repository_load_builds_the_graph(server, monkeypatch):
    monkeypatch.setattr(
        github_repo, "fetch_project",
        lambda url: {"repository": "org/repo", "branch": "main", "files": FILES},
    )
    result = post(server, "/api/github/load", {"url": "https://github.com/org/repo"})
    assert result == {"loaded": True, "label": "org/repo (main)", "files": 3}
    assert get(server, "/api/graph")["source"]["label"] == "org/repo (main)"


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
