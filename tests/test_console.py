import json
import threading
import urllib.request

from kumosql import console, ui


def test_log_goes_to_file_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    console.log("hello", summarized=True)
    assert "hello" in (tmp_path / "ui.log").read_text(encoding="utf-8")
    monkeypatch.setattr(console, "MAX_LOG_BYTES", 10)
    console.log("second", summarized=True)
    assert (tmp_path / "ui.log.1").exists()


def test_quick_edit_is_a_noop_off_windows():
    import sys
    if sys.platform != "win32":
        assert console.disable_quick_edit() is False


def test_requests_are_logged_not_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    server = ui.UIServer(("127.0.0.1", 0), ui.UIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/version").read()
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert "GET /api/version" not in captured.err + captured.out
    assert "GET /api/version" in (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert server.daemon_threads


def test_task_logs_duration_and_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    with console.task("thing"):
        pass
    try:
        with console.task("broken"):
            raise ValueError("nope")
    except ValueError:
        pass
    text = (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert "thing: started" in text and "thing: finished in" in text
    assert "broken: failed (ValueError)" in text and "nope" not in text


def test_error_is_one_line_then_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        console.error("it broke", exc)
    lines = (tmp_path / "ui.log").read_text(encoding="utf-8").splitlines()
    assert "ERROR [KS-INTERNAL] it broke" in lines[0] and any("RuntimeError: [KS-INTERNAL]" in line for line in lines[1:])
    assert "boom" not in "\n".join(lines)


def test_banner_names_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    text = "\n".join(console.banner("http://127.0.0.1:1/"))
    for expected in ("KumoSQL", "Python", "git:", "Local data folder", "Settings file", "Log file", "--verbose"):
        assert expected in text


def test_slow_git_is_reported_and_every_git_call_logged(tmp_path, monkeypatch):
    import sys
    from kumosql import git_repo

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(git_repo, "_SLOW_SECONDS", 0.2)
    if sys.platform != "win32":
        import os, stat
        fake = tmp_path / "bin" / "git"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nsleep 2\necho ok\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
        git_repo._git(["fetch"])
        text = (tmp_path / "ui.log").read_text(encoding="utf-8")
        assert "git fetch: still running after" in text and "slower than expected" in text and "git fetch: finished in" in text and "exit 0" in text
