import os
import stat
import sys
import threading

import pytest

from kumosql import git_repo, repositories, storage


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake git")
def test_git_never_waits_on_the_console(home, monkeypatch):
    """A git that reads stdin (a prompt) must get end-of-input, not the server's console."""

    bin_dir = home / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text('#!/bin/sh\ncat >/dev/null\necho "$GCM_INTERACTIVE|$GIT_TERMINAL_PROMPT|[$GIT_ASKPASS]|[$SSH_ASKPASS]"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    r, w = os.pipe()  # a stdin that never ends, like an idle console
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        done = []
        thread = threading.Thread(target=lambda: done.append(git_repo._git(["status"])), daemon=True)
        thread.start()
        thread.join(10)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(w)
        os.close(r)
    assert done, "git blocked reading the console"
    assert done[0].strip() == "never|0|[]|[]"


def test_settings_do_not_wait_for_a_running_load(home, monkeypatch):
    storage.save(str(home / "data"))
    saved = repositories.replace([{"url": "https://example.com/team/dataform.git", "branch": ""}])
    repo_id = saved["repositories"][0]["id"]
    started, release = threading.Event(), threading.Event()

    def slow_load(url, branch, refresh):
        started.set()
        assert release.wait(20)
        return {"loaded": True, "label": "dataform (main @ abc)", "files": 3}

    monkeypatch.setattr(repositories, "load_into_graph", slow_load)
    thread = repositories.autoload(background=True)
    assert started.wait(10)
    answers = []
    reader = threading.Thread(target=lambda: answers.append(repositories.listing()), daemon=True)
    reader.start()
    reader.join(5)
    try:
        assert answers, "listing waited for the git load"
        assert answers[0]["repositories"][0]["loading"] is True
    finally:
        release.set()
        thread.join(10)
    final = repositories.listing()["repositories"][0]
    assert "loading" not in final and final["files"] == 3 and final["id"] == repo_id
