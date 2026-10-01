"""An SSH remote on a machine without working SSH loads over https; clones keep the saved URL."""

import pytest

from kumosql import git_repo, repositories, state, storage
from kumosql.git_repo import GitRepoError, https_equivalent


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))


@pytest.mark.parametrize("remote, expected", [
    ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
    ("git@github.com:owner/repo", "https://github.com/owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo.git"),
    ("ssh://git@host.example:2222/team/x.git", "https://host.example/team/x.git"),
    ("https://github.com/owner/repo.git", None),
    ("/srv/git/repo.git", None),
    ("C:\\repos\\x", None),
    ("file:///srv/repo.git", None),
])
def test_https_equivalent(remote, expected):
    assert https_equivalent(remote) == expected


def fake_fetch(monkeypatch, ssh_error="Permission denied (publickey).\nfatal: Could not read from remote repository."):
    calls = []

    def fetch(remote, wanted, refresh):
        calls.append(remote)
        if not remote.startswith("https://"):
            raise GitRepoError(ssh_error)
        return {"repository": "owner/repo", "branch": "main", "commit": "abc", "files": {"definitions/a.sqlx": "select 1"}}

    monkeypatch.setattr(git_repo, "_fetch", fetch)
    return calls


def test_saved_ssh_entry_loads_over_https_when_ssh_fails(monkeypatch):
    calls = fake_fetch(monkeypatch)
    result = git_repo.fetch_project("git@github.com:owner/repo.git")
    assert calls == ["git@github.com:owner/repo.git", "https://github.com/owner/repo.git"]
    assert "https://github.com/owner/repo.git" in result["note"] and "SSH did not work" in result["note"]
    calls.clear()  # remembered: no more SSH attempts on this machine
    again = git_repo.fetch_project("git@github.com:owner/repo.git", refresh=True)
    assert calls == ["https://github.com/owner/repo.git"] and again["note"]


def test_https_is_never_turned_into_ssh(monkeypatch):
    calls = []

    def fetch(remote, wanted, refresh):
        calls.append(remote)
        raise GitRepoError("Authentication failed")

    monkeypatch.setattr(git_repo, "_fetch", fetch)
    with pytest.raises(GitRepoError):
        git_repo.fetch_project("https://github.com/owner/repo.git")
    assert calls == ["https://github.com/owner/repo.git"]


def test_a_wrong_repository_is_not_retried_over_https(monkeypatch):
    calls = []

    def fetch(remote, wanted, refresh):
        calls.append(remote)
        raise GitRepoError("This repository does not appear to contain a Dataform project")

    monkeypatch.setattr(git_repo, "_fetch", fetch)
    with pytest.raises(GitRepoError):
        git_repo.fetch_project("git@github.com:owner/repo.git")
    assert len(calls) == 1


def test_when_both_fail_the_error_names_both_urls(monkeypatch):
    def fetch(remote, wanted, refresh):
        raise GitRepoError("fatal: Could not read from remote repository." if remote.startswith("git@")
                           else "Authentication failed for 'https://github.com/owner/repo.git'")

    monkeypatch.setattr(git_repo, "_fetch", fetch)
    with pytest.raises(GitRepoError) as caught:
        git_repo.fetch_project("git@github.com:owner/repo.git")
    text = str(caught.value)
    assert "Could not read from remote repository" in text and "Also tried over https (https://github.com/owner/repo.git)" in text


def test_the_saved_entry_records_the_note(monkeypatch, tmp_path):
    fake_fetch(monkeypatch)
    storage.save(str(tmp_path / "data"))
    saved = repositories.replace([{"url": "git@github.com:owner/repo.git", "branch": ""}])
    repo_id = saved["repositories"][0]["id"]
    repositories.load(repo_id)
    entry = repositories.listing()["repositories"][0]
    assert "https://github.com/owner/repo.git" in entry["note"] and not entry.get("error")


def test_errors_show_the_exact_remote(tmp_path):
    with pytest.raises(GitRepoError) as caught:
        git_repo.sync(str(tmp_path / "missing.git"))
    assert str(tmp_path / "missing.git") in str(caught.value)


def test_a_cached_clone_is_pointed_back_at_the_saved_url(tmp_path):
    from tests.test_git_repo import commit, run

    origin = tmp_path / "origin.git"
    run("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    run("clone", str(origin), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    commit(work, {"workflow_settings.yaml": "defaultProject: p\n"}, "init")
    checkout = git_repo.sync(str(origin))
    run("remote", "set-url", "origin", "git@github.com:someone/else.git", cwd=checkout)
    git_repo.sync(str(origin))
    import subprocess

    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=checkout, capture_output=True, text=True).stdout.strip()
    assert url == str(origin)
