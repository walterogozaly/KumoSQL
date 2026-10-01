import json
import threading
import urllib.request

from kumosql import console, ui


def test_log_goes_to_file_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    console.log("hello")
    assert "hello" in (tmp_path / "ui.log").read_text(encoding="utf-8")
    monkeypatch.setattr(console, "MAX_LOG_BYTES", 10)
    console.log("second")
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
