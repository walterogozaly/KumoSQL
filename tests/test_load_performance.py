"""Graph first, slow analysis later: the graph never waits for the duplicate searches."""

import threading
import time

import pytest

from kumosql import live_graph, live_insights, timing
from kumosql.pipeline import Pipeline

FILES = {
    "definitions/a.sqlx": 'config { type: "table" }\nselect id, name from `p.raw.t`',
    "definitions/b.sqlx": 'config { type: "table" }\nselect id from ${ref("a")}',
}


@pytest.fixture(autouse=True)
def clean():
    live_graph.clear_project()
    live_graph._PROJECT_CACHE.clear()
    live_graph._ANALYSIS.clear()
    yield
    live_graph.clear_project()
    live_graph._PROJECT_CACHE.clear()
    live_graph._ANALYSIS.clear()


def wait_done():
    for _ in range(200):
        if live_graph.analysis_status()["state"] != "running":
            return
        time.sleep(0.05)
    raise AssertionError("background analysis did not finish")


def test_graph_report_can_skip_duplicate_searches(tmp_path, monkeypatch):
    live_graph.load_files(FILES, "demo")
    pipeline = live_graph.loaded()["pipeline"]
    wait_done()
    fresh = live_graph.pipeline_from_files({**FILES, "definitions/c.sqlx": 'config { type: "table" }\nselect 1 as x'})
    calls = []
    monkeypatch.setattr(Pipeline, "duplicate_selects", lambda self, **kw: calls.append("dup") or [])
    monkeypatch.setattr(Pipeline, "near_duplicate_selects", lambda self, **kw: calls.append("near") or [])
    report = fresh.report(include_duplicates=False)
    assert calls == [] and report["duplicates"] == [] and report["near_duplicates"] == []
    assert pipeline is not fresh


def test_duplicate_searches_run_once_per_project():
    live_graph.load_files(FILES, "demo")
    current = live_graph.loaded()["pipeline"]
    wait_done()
    assert current.has_remembered("duplicates") and current.has_remembered("near_duplicates")
    first = current.duplicate_selects(min_nodes=12)
    assert current.duplicate_selects(min_nodes=12) is first


def test_same_content_reuses_the_analysed_project():
    first = live_graph.pipeline_from_files(FILES)
    assert live_graph.pipeline_from_files(dict(FILES)) is first
    other = live_graph.pipeline_from_files({**FILES, "definitions/b.sqlx": 'config { type: "table" }\nselect 2 as id'})
    assert other is not first


def test_pages_report_progress_while_analysis_runs():
    live_graph.load_files(FILES, "demo")
    current = live_graph.loaded()
    wait_done()
    live_graph._ANALYSIS[id(current["pipeline"])].update(state="running", stage="similar queries")
    live_graph._ANALYSIS[id(current["pipeline"])]["event"].clear()
    payload = live_insights.cost_payload()
    assert payload["pending"]["stage"] == "similar queries"
    assert live_insights.changes_payload()["pending"]["state"] == "running"
    assert "pending" not in live_graph.graph_or_empty()


def test_stages_are_timed(capsys):
    live_graph.load_files(FILES, "demo")
    wait_done()
    live_graph.graph_or_empty()
    names = {item["stage"] for item in timing.recent()}
    assert {"analyse", "graph report", "graph payload"} <= names
    assert "[kumosql] graph payload:" in capsys.readouterr().err


def test_analysis_is_saved_by_content_and_reused_after_a_restart(monkeypatch):
    live_graph.load_files(FILES, "demo")
    wait_done()
    # A restart forgets everything in memory; the saved copy is found by content.
    live_graph._PROJECT_CACHE.clear()
    live_graph._ANALYSIS.clear()
    live_graph.clear_project()
    from kumosql import repeated_work

    monkeypatch.setattr(repeated_work, "repeated_work_report", lambda *a, **k: pytest.fail("recomputed"))
    live_graph.load_files(FILES, "demo")
    wait_done()
    assert live_graph.analysis_status()["state"] == "done"
    assert "opportunities" in live_graph.cached_result(live_graph.loaded()["pipeline"], "repeated_work", lambda: pytest.fail("x"))
    assert any(item["stage"] == "analysis cache hit" for item in timing.recent())


def test_changed_content_is_not_served_from_the_saved_analysis():
    live_graph.load_files(FILES, "demo")
    wait_done()
    first = live_graph.cached_result(live_graph.loaded()["pipeline"], "repeated_work", lambda: {})
    live_graph.load_files({**FILES, "definitions/c.sqlx": 'config { type: "table" }\nselect 1 as x'}, "demo")
    wait_done()
    second = live_graph.loaded()["pipeline"]
    assert second.content_key != live_graph.pipeline_from_files(FILES).content_key
    assert second.has_cached("repeated_work") and first is not None


def test_status_lists_what_is_running():
    with live_graph.activity("Parsing project"):
        status = live_graph.server_status()
    assert status["busy"][0]["label"] == "Parsing project"
    assert live_graph.server_status()["busy"] == []


def test_other_requests_stay_fast_while_analysis_burns_cpu(ui_server_url):
    import threading
    from urllib.request import urlopen

    stop = threading.Event()

    def burn():
        while not stop.is_set():
            sum(i * i for i in range(20000))

    worker = threading.Thread(target=burn, daemon=True)
    worker.start()
    try:
        worst = 0.0
        for path in ("/api/settings", "/api/status", "/favicon.svg"):
            for _ in range(5):
                start = time.perf_counter()
                urlopen(f"{ui_server_url}{path}").read()
                worst = max(worst, time.perf_counter() - start)
    finally:
        stop.set()
        worker.join(timeout=2)
    assert worst < 0.5


@pytest.fixture
def ui_server_url():
    from http.server import ThreadingHTTPServer  # noqa: F401
    from kumosql.ui import UIHandler, UIServer

    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
