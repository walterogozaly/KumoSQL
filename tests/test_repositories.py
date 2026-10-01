"""Connected repositories are a saved setting that reloads on start."""

import json
import subprocess
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kumosql import live_graph, repositories
from kumosql.ui import UIHandler

from test_git_repo import FILES, commit, run


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    live_graph.clear_project()
    yield
    live_graph.clear_project()


@pytest.fixture
def bare(tmp_path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    commit(work, FILES, "first")
    return bare, work


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


def call(base, path, payload=None, method=None):
    request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method=method)
    with urlopen(request) as response:
        return json.load(response)


def test_repositories_persist_and_first_one_is_active(bare):
    bare, _ = bare
    saved = repositories.replace([{"url": str(bare), "branch": ""}])
    assert saved["active"] == saved["repositories"][0]["id"]
    assert repositories.listing() == saved  # read back from the state file


def test_duplicates_are_dropped_and_status_is_kept(bare):
    bare, _ = bare
    first = repositories.replace([{"url": str(bare)}])["repositories"][0]
    repositories.load(first["id"])
    again = repositories.replace([{"url": str(bare)}, {"url": str(bare), "branch": ""}])["repositories"]
    assert len(again) == 1 and again[0]["id"] == first["id"] and again[0]["last_loaded"]


@pytest.mark.parametrize("entries", ["x", [5], [{"url": "-oProxyCommand=x"}], [{"url": "/r.git", "branch": "--x"}]])
def test_invalid_entries_are_rejected(entries):
    with pytest.raises(repositories.RepositoryError):
        repositories.replace(entries)


def test_load_records_status_and_makes_it_the_project(bare):
    bare, _ = bare
    repo = repositories.replace([{"url": str(bare)}])["repositories"][0]
    result = repositories.load(repo["id"])
    assert result["files"] == 3 and live_graph.loaded()["label"].startswith("remote (main @ ")
    saved = repositories.listing()["repositories"][0]
    assert saved["files"] == 3 and "error" not in saved


def test_failed_load_saves_gits_message(tmp_path):
    repo = repositories.replace([{"url": str(tmp_path / "missing.git")}])["repositories"][0]
    with pytest.raises(Exception, match="git clone failed"):
        repositories.load(repo["id"])
    assert "git clone failed" in repositories.listing()["repositories"][0]["error"]


def test_autoload_picks_up_new_commits_and_falls_back_to_the_cached_copy(bare):
    bare, work = bare
    repo = repositories.replace([{"url": str(bare)}])["repositories"][0]
    before = repositories.load(repo["id"])["label"]
    commit(work, {"definitions/d.sqlx": "SELECT 3 AS id"}, "later")
    repositories.autoload(background=False)
    assert live_graph.loaded()["label"] != before  # a newer commit was fetched
    bare.rename(bare.with_name("moved.git"))  # remote becomes unreachable
    live_graph.clear_project()
    repositories.autoload(background=False)
    saved = repositories.listing()["repositories"][0]
    assert live_graph.loaded() is not None and "git" in saved["stale_reason"]


def test_http_endpoints(server, bare):
    bare, _ = bare
    assert call(server, "/api/repositories") == {"repositories": [], "active": None}
    saved = call(server, "/api/repositories", {"repositories": [{"url": str(bare)}]})
    repo_id = saved["repositories"][0]["id"]
    assert call(server, "/api/repositories/activate", {"id": repo_id})["files"] == 3
    assert call(server, "/api/repositories/refresh", {"id": repo_id})["loaded"] is True
    with pytest.raises(HTTPError) as error:
        call(server, "/api/repositories/refresh", {"id": "nope"})
    assert error.value.code == 400


def test_clear_all_removes_repositories_clones_and_caches(tmp_path, monkeypatch):
    from kumosql import git_repo, live_graph, state, storage, workflow_configs

    storage.save(str(tmp_path / "data"))
    saved = repositories.replace([{"url": "https://example.com/a/b.git", "branch": ""},
                                  {"url": "https://example.com/c/d.git", "branch": "dev"}])
    assert len(saved["repositories"]) == 2
    clones = git_repo.cache_dir()
    (clones / ("a" * 20)).mkdir(parents=True)
    (clones / ("a" * 20) / "x").write_text("clone")
    (clones / "keep-me").mkdir()
    state.set_section(git_repo._TRANSPORT_SECTION, {"git@x:y/z": "https://x/y/z"})
    state.set_section("scopes", {"mine": 1})
    live_graph.load_files({"definitions/a.sqlx": "select 1 as a", "workflow_settings.yaml": "defaultProject: p\n"}, "x")
    from kumosql import bigquery_catalog

    key = "dataform-workflows:example.com/a/b:p:us"
    bigquery_catalog.cached(key, lambda: {"configs": []}, refresh=True)
    result = repositories.clear_all()
    assert result["removed"] == 2 and result["clones"] == 1 and result["schedule_lookups"] == 1
    assert repositories.listing() == {"repositories": [], "active": None}
    assert live_graph.loaded() is None
    assert not (clones / ("a" * 20)).exists() and (clones / "keep-me").exists()
    assert bigquery_catalog.peek(key) is None
    assert state.get_section(git_repo._TRANSPORT_SECTION) == {}
    assert state.get_section("scopes") == {"mine": 1}  # unrelated settings stay
    assert repositories.clear_all()["removed"] == 0  # idempotent
