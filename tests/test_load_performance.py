"""Graph first, slow analysis later: the graph never waits for the duplicate searches."""

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
