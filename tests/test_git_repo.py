"""Loading a project through the local git CLI, against local bare repositories."""

import subprocess
from pathlib import Path

import pytest

from kumosql import git_repo, live_graph

FILES = {
    "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: d\n",
    "definitions/a.sqlx": "config { type: \"table\" }\nSELECT 1 AS id",
    "definitions/b.sqlx": "config { type: \"table\" }\nSELECT id FROM ${ref(\"a\")}",
    "notes.txt": "ignored",
}


def run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                        "HOME": str(cwd)})


def commit(work: Path, files: dict, message: str):
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    run("add", "-A", cwd=work)
    run("commit", "-m", message, cwd=work)
    run("push", "origin", "HEAD", cwd=work)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    commit(work, FILES, "first")
    run("checkout", "-b", "dev", cwd=work)
    commit(work, {"definitions/c.sqlx": "SELECT 2 AS id"}, "dev")
    run("checkout", "main", cwd=work)
    return bare, work


def test_loads_default_branch_and_filters_files(remote):
    bare, _ = remote
    fetched = git_repo.fetch_project(str(bare))
    assert fetched["branch"] == "main"
    assert set(fetched["files"]) == {"workflow_settings.yaml", "definitions/a.sqlx", "definitions/b.sqlx"}


def test_branch_selection(remote):
    bare, _ = remote
    assert "definitions/c.sqlx" in git_repo.fetch_project(str(bare), "dev")["files"]


def test_cache_is_reused_until_refresh(remote):
    bare, work = remote
    git_repo.fetch_project(str(bare))
    commit(work, {"definitions/d.sqlx": "SELECT 3 AS id"}, "later")
    assert "definitions/d.sqlx" not in git_repo.fetch_project(str(bare))["files"]
    assert "definitions/d.sqlx" in git_repo.fetch_project(str(bare), refresh=True)["files"]


def test_git_errors_are_passed_through(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    with pytest.raises(git_repo.GitRepoError, match="git clone failed: .*(not exist|not found|does not appear)"):
        git_repo.fetch_project(str(tmp_path / "missing.git"))


def test_failed_refresh_keeps_the_cached_copy(remote):
    bare, _ = remote
    git_repo.fetch_project(str(bare))
    bare.rename(bare.with_name("moved.git"))
    with pytest.raises(git_repo.GitRepoError, match="git clone failed"):
        git_repo.fetch_project(str(bare), refresh=True)
    assert git_repo.fetch_project(str(bare))["files"]


@pytest.mark.parametrize("value", ["-oProxyCommand=x", "ext::sh -c id", "", None, "relative/path", "a\nb"])
def test_unsafe_remotes_are_rejected(value):
    with pytest.raises(git_repo.GitRepoError):
        git_repo.parse_remote(value)


@pytest.mark.parametrize("value", [
    "https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git", "/srv/r.git",
])
def test_supported_remote_forms(value):
    assert git_repo.parse_remote(value) == value


def test_bad_branch_rejected():
    with pytest.raises(git_repo.GitRepoError):
        git_repo.parse_branch("--upload-pack=x")


def test_non_dataform_repo_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    bare, work = tmp_path / "r.git", tmp_path / "w"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    commit(work, {"README.md": "hi"}, "x")
    with pytest.raises(git_repo.GitRepoError, match="Dataform"):
        git_repo.fetch_project(str(bare))


def test_load_into_graph(remote):
    bare, _ = remote
    result = git_repo.load_into_graph(str(bare))
    assert result["loaded"] and result["files"] == 3
    assert live_graph.loaded()["label"].startswith("remote (main @ ")
    live_graph.clear_project()


def commit_without_tree(work: Path, files: dict, message: str):
    """Commit through the index only, so the test works where the path is too long to check out."""
    for name, text in files.items():
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, input=text.encode(),
                              capture_output=True, check=True).stdout.decode().strip()
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", cwd=work)
    run("commit", "-m", message, cwd=work)
    run("push", "origin", "HEAD", cwd=work)


def test_very_long_paths_load_without_a_working_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    bare, work = tmp_path / "r.git", tmp_path / "w"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    deep = "definitions/" + "/".join(["very_long_directory_name_" + "x" * 40] * 5) + "/" + "m" * 120 + ".sqlx"
    assert len(deep) > 300
    commit_without_tree(work, {"workflow_settings.yaml": FILES["workflow_settings.yaml"], deep: "SELECT 1 AS id"}, "long")
    fetched = git_repo.fetch_project(str(bare))
    assert deep in fetched["files"]
    cached = next((tmp_path / "cache").iterdir())
    assert not (cached / "definitions").exists()  # nothing was checked out
    # The temporary folder the graph loader writes is cleaned up even for long paths.
    assert git_repo.load_into_graph(str(bare))["loaded"]
    live_graph.clear_project()


def test_symlinks_and_non_ascii_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    bare, work = tmp_path / "r.git", tmp_path / "w"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    link = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, input=b"/etc/passwd",
                          capture_output=True, check=True).stdout.decode().strip()
    run("update-index", "--add", "--cacheinfo", f"120000,{link},definitions/link.sqlx", cwd=work)
    commit_without_tree(work, {"definitions/caf\u00e9.sqlx": "SELECT 1 AS id"}, "x")
    assert set(git_repo.fetch_project(str(bare))["files"]) == {"definitions/caf\u00e9.sqlx"}


@pytest.mark.parametrize("value", ["C:\\repos\\r.git", "c:/repos/r.git", "\\\\server\\share\\r.git"])
def test_windows_paths_are_accepted(value):
    assert git_repo.parse_remote(value) == value


def test_repository_name_handles_every_remote_form(monkeypatch):
    seen = {}
    monkeypatch.setattr(git_repo, "sync", lambda remote, branch, refresh: Path("."))
    monkeypatch.setattr(git_repo, "_git", lambda args, cwd=None: "main")
    monkeypatch.setattr(git_repo, "_tree_blobs", lambda checkout: {"a.sqlx": "x"})
    monkeypatch.setattr(git_repo, "_read_blobs", lambda checkout, blobs: {"a.sqlx": b"SELECT 1"})
    for remote in ["git@github.com:o/repo.git", "https://github.com/o/repo", "C:\\repos\\repo.git", "/srv/repo.git/"]:
        seen[remote] = git_repo.fetch_project(remote)["repository"]
    assert set(seen.values()) == {"repo"}


def test_concurrent_loads_of_a_new_remote_do_not_collide(remote):
    import threading

    bare, _ = remote
    errors, results = [], []

    def load():
        try:
            results.append(git_repo.fetch_project(str(bare)))
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(str(exc))

    threads = [threading.Thread(target=load) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(results) == 6


def test_blank_branch_uses_the_remotes_default_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    bare, work = tmp_path / "r.git", tmp_path / "w"
    run("init", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "trunk", cwd=work)
    commit(work, FILES, "x")
    assert git_repo.fetch_project(str(bare), "")["branch"] == "trunk"


@pytest.mark.parametrize("stderr", [
    "fatal: Authentication failed for 'https://github.com/o/r.git/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "git@github.com: Permission denied (publickey).",
    "remote: Repository not found.",
])
def test_missing_credentials_get_a_sign_in_hint_and_keep_gits_message(monkeypatch, stderr):
    monkeypatch.setattr(git_repo.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, b"", stderr.encode()))
    with pytest.raises(git_repo.GitRepoError) as error:
        git_repo._git(["clone", "x"])
    assert stderr in str(error.value) and "Git Credential Manager" in str(error.value)


def test_other_git_errors_get_no_sign_in_hint(monkeypatch):
    monkeypatch.setattr(git_repo.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, b"", b"fatal: disk full"))
    with pytest.raises(git_repo.GitRepoError) as error:
        git_repo._git(["clone", "x"])
    assert "Credential Manager" not in str(error.value)


def test_git_runs_with_an_existing_directory_even_if_the_servers_is_gone(tmp_path, monkeypatch, remote):
    import os

    bare, _ = remote
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()  # the server was started from a folder that no longer exists
    with pytest.raises(FileNotFoundError):
        os.getcwd()
    assert git_repo.fetch_project(str(bare))["files"]


def test_git_config_that_rewrites_https_to_ssh_is_refused(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    with pytest.raises(git_repo.GitRepoError, match="rewrites https://github.com/o/r.git to git@github.com:o/r.git"):
        git_repo.fetch_project("https://github.com/o/r.git")
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


def test_https_urls_are_used_as_entered(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty"))
    assert git_repo.parse_remote("https://github.com/o/r.git") == "https://github.com/o/r.git"
    git_repo._refuse_rewritten_transport("https://github.com/o/r.git")  # no rewrite configured: fine


@pytest.mark.parametrize("stderr", [
    "ssh: connect to host github.com port 22: Connection timed out\nfatal: Could not read from remote repository.",
    "ssh: connect to host github.com port 22: Network is unreachable",
    "ssh: Could not resolve hostname github.com: Name or service not known",
])
def test_blocked_ssh_suggests_https(monkeypatch, stderr):
    monkeypatch.setattr(git_repo.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, b"", stderr.encode()))
    with pytest.raises(git_repo.GitRepoError) as error:
        git_repo._git(["clone", "x"])
    assert stderr.splitlines()[0] in str(error.value) and "https URL instead" in str(error.value)


def test_diagnose_reports_every_git_call(remote, tmp_path):
    bare, _ = remote
    report = git_repo.diagnose(str(bare))
    assert "KumoSQL:" in report and "git calls, in order:" in report and "git clone" in report
    assert "cwd: " in report and "Result: OK: loaded 3 files" in report
    assert git_repo._TRACE is None  # tracing is switched off afterwards


def test_diagnose_reports_failures_with_the_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    report = git_repo.diagnose(str(tmp_path / "missing.git"))
    assert "Result: FAILED" in report and "GitRepoError" in report and "exit: 128" in report
