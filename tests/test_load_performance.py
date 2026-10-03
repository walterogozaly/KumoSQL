"""Graph first, slow analysis later: the graph never waits for the duplicate searches."""

import threading
import time

import pytest

from kumosql import console, live_graph, live_insights, timing
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


def test_stages_are_timed():
    live_graph.load_files(FILES, "demo")
    wait_done()
    live_graph.graph_or_empty()
    names = {item["stage"] for item in timing.recent()}
    assert {"analyse", "graph report", "graph payload"} <= names
    assert "graph payload:" in console.log_path().read_text(encoding="utf-8")


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
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_stage_start_and_progress_lines_show_a_long_stage_is_alive():
    with timing.stage("demo stage", models=2431):
        progress = timing.Progress("demo loop", 3, every=0.0, slow=0.0)
        for label in ("alpha", "beta", "gamma"):
            progress.step(label)
        progress.finish()
    err = console.log_path().read_text(encoding="utf-8")
    assert "demo stage: started (models 2431)" in err
    assert "demo stage > demo loop: started (items 3)" in err
    assert "demo stage > demo loop: finished" in err and "done 3" in err
    assert "demo loop: slow item model#" in err
    assert "alpha" not in err and "beta" not in err
    assert timing.current_progress() == []


def test_a_loop_left_open_by_an_error_does_not_tangle_the_log():
    try:
        with timing.stage("outer stage"):
            timing.Progress("broken loop", 2).step("alpha")
            raise ValueError("boom")
    except ValueError:
        pass
    assert timing.current_progress() == []
    with timing.stage("next stage"):
        pass
    assert console._stack() == []


def test_analysis_logs_each_stage_before_it_finishes(capsys):
    live_graph.pipeline_from_files(FILES)
    err = capsys.readouterr().err
    for line in ("analyse: started", "read models: started", "order models: started", "trace columns: started"):
        assert line in err


def test_a_slow_model_keeps_its_edges_but_skips_column_tracing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "0.000000001")
    pipeline = live_graph.pipeline_from_files(FILES)
    codes = {entry["code"]: entry for entry in pipeline.report(include_duplicates=False)["diagnostics"]}
    assert "lineage_skipped" in codes
    completeness = pipeline.completeness()
    assert completeness["complete"] is False and completeness["views"]["lineage"] is False
    assert pipeline.upstream["b"] == {"a"}  # the graph edge is unaffected


def test_no_budget_means_no_skipping(monkeypatch):
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "0")
    monkeypatch.setenv("KUMOSQL_LINEAGE_SECONDS", "0")
    pipeline = live_graph.pipeline_from_files({**FILES, "definitions/z.sqlx": 'config { type: "table" }\nselect 1 as z'})
    assert all(e["code"] != "lineage_skipped" for e in pipeline.report(include_duplicates=False)["diagnostics"])


def test_analysis_leaves_the_garbage_collector_as_it_found_it():
    import gc

    before = gc.get_threshold()
    frozen = gc.get_freeze_count()
    live_graph.pipeline_from_files(FILES)
    assert gc.get_threshold() == before
    assert gc.get_freeze_count() == frozen


def test_untrimmed_lineage_gives_the_same_columns_as_trimmed(monkeypatch):
    import kumosql.pipeline as pl

    files = {
        "definitions/a.sqlx": 'config { type: "table" }\nselect id, name from `p.raw.t`',
        "definitions/b.sqlx": 'config { type: "table" }\nwith s as (select id, upper(name) as n from ${ref("a")})\nselect id, n as shown, 1 as k from s',
        "definitions/c.sqlx": 'config { type: "table" }\nselect id from ${ref("a")} union all select id from ${ref("b")}',
    }

    def lineage_of(trim):
        original = pl.lineage
        monkeypatch.setattr(pl, "lineage", lambda *a, **k: original(*a, **{**k, "trim_selects": trim}))
        try:
            analysis = live_graph.pipeline_from_files(files)._analyse()
        finally:
            monkeypatch.setattr(pl, "lineage", original)
            live_graph._PROJECT_CACHE.clear()
        return {str(k): sorted(map(str, v)) for k, v in analysis.lineage.items()}

    assert lineage_of(True) == lineage_of(False)


def test_default_limits_are_generous_and_settings_adjust_them(monkeypatch, tmp_path):
    from kumosql import lineage_limits

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.delenv("KUMOSQL_LINEAGE_MODEL_SECONDS", raising=False)
    monkeypatch.delenv("KUMOSQL_LINEAGE_SECONDS", raising=False)
    assert lineage_limits.effective("model_seconds") >= 60
    lineage_limits.save_settings(model_seconds=0.000000001)
    assert lineage_limits.effective("model_seconds") == 1e-09
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "5")
    assert lineage_limits.effective("model_seconds") == 5  # the environment wins
    with pytest.raises(ValueError):
        lineage_limits.save_settings(model_seconds=-1)


def test_a_model_over_its_limit_is_traced_at_table_level(monkeypatch):
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "0.000000001")
    pipeline = live_graph.pipeline_from_files(FILES)
    analysis = pipeline._analyse()
    skipped = [r for r in analysis.records.values() if r.reason == "lineage_skipped"]
    assert skipped and all(r.sources for r in skipped)
    assert not any(e["code"] in ("lineage_error", "qualify_error") for e in pipeline.report(include_duplicates=False)["diagnostics"])


def test_saved_analysis_is_not_reused_under_other_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "5")
    first = live_graph._snapshot_file("k")
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "50")
    assert live_graph._snapshot_file("k") != first


def test_limits_report_the_value_in_force_and_which_are_locked(monkeypatch, tmp_path):
    from kumosql import lineage_limits

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.delenv("KUMOSQL_LINEAGE_MODEL_SECONDS", raising=False)
    monkeypatch.delenv("KUMOSQL_LINEAGE_SECONDS", raising=False)
    lineage_limits.save_settings(model_seconds=30)
    assert lineage_limits.status()["model_seconds"] == 30 and lineage_limits.status()["locked"] == []
    monkeypatch.setenv("KUMOSQL_LINEAGE_MODEL_SECONDS", "5")
    status = lineage_limits.status()
    assert status["model_seconds"] == 5 and status["locked"] == ["model_seconds"]


def test_a_generated_project_analyses_within_its_time_budget(tmp_path, monkeypatch):
    """A guard against silent slowdowns in parsing and lineage. 500 generated models (one is deliberately wide) take about seven
    seconds here; the budget leaves four times that for slow runners."""

    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "make_large_project", Path(__file__).resolve().parents[1] / "tools" / "make_large_project.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(tmp_path / "p", 500, 7, True)
    files = {p.relative_to(tmp_path / "p").as_posix(): p.read_text(errors="ignore") for p in (tmp_path / "p").rglob("*") if p.is_file()}
    start = time.perf_counter()
    pipeline = live_graph.pipeline_from_files(files)
    pipeline.report(include_duplicates=False)
    assert time.perf_counter() - start < 30


def test_two_loops_of_one_name_keep_their_own_progress():
    """Two analyses can run a loop of the same name at once (the UI's background job and another load)."""

    first, second = timing.Progress("same loop", 2), timing.Progress("same loop", 3)
    first.step("alpha")
    second.step("beta")
    second.step("gamma")
    assert sorted((p["name"], p["done"], p["total"]) for p in timing.current_progress()) == [("same loop", 0, 2), ("same loop", 1, 3)]
    first.finish()
    second.step("delta")  # the other loop ending took its entry with it, and this step raised KeyError
    second.finish()
    assert timing.current_progress() == []
