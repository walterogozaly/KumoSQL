"""The local data folder setting: validated, saved, used for clones, required to connect a repository."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kumosql import git_repo, repositories, storage
from kumosql.ui import UIHandler


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUMOSQL_GIT_CACHE", raising=False)


def test_nothing_is_configured_at_first():
    info = storage.describe()
    assert info["configured"] is False and info["folder"] is None and info["suggested"].endswith("KumoSQL")


def test_save_creates_the_folder_and_clones_go_there(tmp_path):
    folder = tmp_path / "work" / "KumoSQL"
    result = storage.save(str(folder))
    assert result["configured"] and result["folder"] == str(folder.resolve())
    assert git_repo.cache_dir() == folder.resolve() / "git-cache"
    assert not [p for p in folder.iterdir() if p.name.startswith(".kumosql-probe")]  # the check cleans up


def test_environment_variables_and_home_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("KQ_BASE", str(tmp_path))
    assert storage.validate("$KQ_BASE/data") == (tmp_path / "data").resolve()


@pytest.mark.parametrize("value", ["", "   ", None, 5, "relative/folder"])
def test_unusable_values_are_rejected(value):
    with pytest.raises(storage.StorageError):
        storage.validate(value)


def test_a_file_is_not_a_folder(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(storage.StorageError, match="Could not create"):
        storage.validate(str(tmp_path / "file" / "sub"))


def test_changing_the_folder_reports_the_previous_one(tmp_path):
    storage.save(str(tmp_path / "a"))
    result = storage.save(str(tmp_path / "b"))
    assert result["previous"] == str((tmp_path / "a").resolve())


def test_connecting_a_repository_needs_a_folder_first(tmp_path):
    with pytest.raises(repositories.RepositoryError, match="local data folder"):
        repositories.replace([{"url": "https://github.com/o/r.git"}])
    storage.save(str(tmp_path / "work"))
    assert repositories.replace([{"url": "https://github.com/o/r.git"}])["repositories"]
    # Existing entries can still be removed or kept without it.
    assert repositories.replace([])["repositories"] == []


def test_environment_override_counts_as_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "pinned"))
    assert storage.describe()["configured"] and storage.describe()["override"]
    assert repositories.replace([{"url": "https://github.com/o/r.git"}])["repositories"]


def test_http_endpoints(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        def call(path, payload=None):
            request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                return json.load(response)

        assert call("/api/storage")["configured"] is False
        assert call("/api/storage", {"folder": str(tmp_path / "w")})["configured"] is True
        with pytest.raises(HTTPError) as error:
            call("/api/storage", {"folder": "relative"})
        assert error.value.code == 400 and "full path" in error.value.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
