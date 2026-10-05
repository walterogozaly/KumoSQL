"""The Reduce page: the payload functions, the HTTP routes and the static assets."""

import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request

import pytest

pytest.importorskip("z3")

from kumosql import live_graph, prover_context, reduction_app
from kumosql.project_reduction import ReductionError
from kumosql.ui import ASSETS, UIHandler, UIServer
from test_project_reduction import SHOP
from ui_http import urlopen

STATIC = Path(__file__).resolve().parents[1] / "src" / "kumosql" / "static"
RPT_REVENUE = "shop.an.rpt_revenue"
RPT_EVENTS = "shop.an.rpt_events"


@pytest.fixture(autouse=True)
def fresh_job(monkeypatch):
    monkeypatch.setattr(reduction_app, "_JOB", {"state": "idle"})
    monkeypatch.setattr(reduction_app, "_THREADS", [])


@pytest.fixture
def loaded(monkeypatch):
    files = dict(SHOP)
    pipeline = live_graph.pipeline_from_files(files)
    pipeline.source_files = files
    monkeypatch.setattr(live_graph, "loaded", lambda: {"pipeline": pipeline, "label": "shop"})
    return pipeline, files


@pytest.fixture
def ui_server():
    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post(base, body, path="/api/reduce/run"):
    request = Request(base + path, method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request) as response:
        return json.load(response)


def get(base, path):
    with urlopen(base + path) as response:
        return json.load(response)


def finished(base, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get(base, "/api/reduce/status")
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the reduction did not finish")


# ------------------------------------------------------------------ payloads


def test_nothing_loaded(monkeypatch):
    monkeypatch.setattr(live_graph, "loaded", lambda: None)
    assert reduction_app.reduction_payload()["loaded"] is False
    assert reduction_app.reduction_payload()["actions"] == []
    with pytest.raises(ReductionError, match="load a project"):
        reduction_app.run_payload({"keep": [RPT_REVENUE]})


def test_actions_of_the_loaded_project(loaded):
    pipeline, _ = loaded
    data = reduction_app.reduction_payload()
    assert data["loaded"] and data["files_available"] and data["label"] == "shop"
    assert {a["key"] for a in data["actions"]} == set(pipeline.models)
    assert data["table_types"] == ["view", "table"]
    revenue = next(a for a in data["actions"] if a["key"] == RPT_REVENUE)
    assert revenue["name"] == "rpt_revenue" and revenue["kind"] == "table" and revenue["tags"] == ["reports"]
    assert revenue["path"] == "definitions/reports/rpt_revenue.sqlx"
    # declarations are not actions
    assert not any(a["kind"] == "declaration" for a in data["actions"])
    json.dumps(data)


def test_source_files_are_needed(loaded):
    pipeline, _ = loaded
    pipeline.source_files = None
    assert reduction_app.reduction_payload()["files_available"] is False
    with pytest.raises(ReductionError, match="reload the project"):
        reduction_app.run_payload({"keep": [RPT_REVENUE]})


@pytest.mark.parametrize("body,message", [
    ([], "JSON object"),
    ({}, "at least one"),
    ({"keep": []}, "at least one"),
    ({"keep": "rpt_revenue"}, "at least one"),
    ({"keep": [1]}, "at least one"),
    ({"keep": ["nope"]}, "not an action"),
    ({"keep": ["shop.raw.orders"]}, "not an action"),
    ({"keep": [RPT_REVENUE], "strict": "yes"}, "strict"),
    ({"keep": [RPT_REVENUE], "drop_only": 1}, "drop_only"),
    ({"keep": [RPT_REVENUE], "keep_assertions": None}, "keep_assertions"),
    ({"keep": [RPT_REVENUE], "table_type": "incremental"}, "table_type"),
    ({"keep": [RPT_REVENUE], "max_seconds": 0}, "max_seconds"),
    ({"keep": [RPT_REVENUE], "max_seconds": 100000}, "max_seconds"),
    ({"keep": [RPT_REVENUE], "max_seconds": True}, "max_seconds"),
    ({"keep": [RPT_REVENUE], "max_seconds": "5"}, "max_seconds"),
])
def test_bad_input_is_refused_before_any_work(loaded, body, message):
    with pytest.raises(ReductionError, match=message):
        reduction_app.run_payload(body)
    assert reduction_app.job_status() == {"state": "idle"}


def test_solver_off_refuses(loaded, monkeypatch):
    monkeypatch.setattr(prover_context, "settings", lambda: {"enabled": False, "timeout_ms": 1000, "bounded_rows": 1})
    with pytest.raises(ReductionError, match="solver is turned off"):
        reduction_app.run_payload({"keep": [RPT_REVENUE]})
    assert reduction_app.job_status()["state"] == "idle"


# ------------------------------------------------------------------ routes


def test_run_returns_the_proved_patch_and_leaves_the_project_alone(ui_server, loaded):
    _, files = loaded
    before = dict(files)
    data = get(ui_server, "/api/reduce")
    assert RPT_REVENUE in {a["key"] for a in data["actions"]}
    assert get(ui_server, "/api/reduce/status") == {"state": "idle"}

    start = post(ui_server, {"keep": [RPT_REVENUE], "drop_only": True, "table_type": "table", "max_seconds": 60})
    assert start["state"] in ("running", "done") and start["keep"] == [RPT_REVENUE]
    job = finished(ui_server)
    assert job["state"] == "done", job
    result = job["result"]
    assert result["verified"] and result["verdict"] in ("proven", "proven_with_assumptions", "unchanged")
    assert {r["model"] for r in result["removed"]} >= {RPT_EVENTS}
    assert result["changed"] == [] and result["added"] == []  # drop_only rewrites nothing
    assert result["actions"]["after"] < result["actions"]["before"]
    assert result["score"]["after"] < result["score"]["before"]
    assert "deleted file mode" in result["diff"]
    assert [c["model"] for c in result["checks"]] == [RPT_REVENUE] and result["checks"][0]["role"] == "kept"
    assert files == before and live_graph.loaded()["pipeline"].source_files == before


def test_full_reduction_runs_with_every_option(ui_server, loaded):
    post(ui_server, {"keep": [RPT_REVENUE], "keep_assertions": True, "strict": True})
    job = finished(ui_server)
    assert job["state"] == "done", job
    assert job["result"]["verified"]
    assert job["result"]["keep"] == [RPT_REVENUE]


def test_one_reduction_runs_at_a_time(ui_server, loaded, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def slow(root, keep, **options):
        entered.set()
        release.wait(30)
        raise ReductionError("stopped by the test")

    monkeypatch.setattr(reduction_app, "reduce_project", slow)
    assert post(ui_server, {"keep": [RPT_REVENUE]})["state"] == "running"
    assert entered.wait(10)
    with pytest.raises(HTTPError) as error:
        post(ui_server, {"keep": [RPT_REVENUE]})
    assert error.value.code == 400 and "already running" in json.load(error.value)["error"]
    release.set()
    job = finished(ui_server)
    assert job["state"] == "error" and job["error"] == "stopped by the test"


def test_bad_requests_are_400_and_a_failure_is_reported_in_the_status(ui_server, loaded):
    for body in ({"keep": ["nope"]}, {"keep": []}, {"keep": [RPT_REVENUE], "strict": "x"}):
        with pytest.raises(HTTPError) as error:
            post(ui_server, body)
        assert error.value.code == 400
        assert json.load(error.value)["error"]
    assert get(ui_server, "/api/reduce/status") == {"state": "idle"}


def test_the_reduction_runs_in_a_temporary_folder_that_is_removed(ui_server, loaded, monkeypatch):
    seen = {}
    real = reduction_app.reduce_project

    def spy(root, keep, **options):
        seen["root"] = Path(root)
        seen["files"] = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
        seen["options"] = options
        return real(root, keep, **options)

    monkeypatch.setattr(reduction_app, "reduce_project", spy)
    post(ui_server, {"keep": [RPT_REVENUE], "drop_only": True, "max_seconds": 30})
    assert finished(ui_server)["state"] == "done"
    assert "definitions/reports/rpt_revenue.sqlx" in seen["files"]
    assert seen["options"]["rewrite"] is False and seen["options"]["max_seconds"] == 30.0
    assert seen["options"]["new_table_type"] == "view"
    assert not seen["root"].exists()


# ------------------------------------------------------------------ static assets


def test_page_and_assets_are_served(ui_server):
    for route in ("/reduce", "/assets/reduce.js", "/assets/reduce.css", "/assets/patch-view.js"):
        assert route in ASSETS
        name = ASSETS[route][0]
        assert (STATIC / name).is_file()
    with urlopen(ui_server + "/reduce") as response:
        page = response.read().decode()
    assert "/assets/reduce.js" in page and "/assets/patch-view.js" in page and "__KUMOSQL_SESSION_TOKEN__" not in page
    with urlopen(ui_server + "/assets/reduce.js") as response:
        script = response.read().decode()
    for route in ("/api/reduce", "/api/reduce/run", "/api/reduce/status"):
        assert route in script
    with urlopen(ui_server + "/assets/patch-view.js") as response:
        assert b"window.KumoPatch" in response.read()


def test_both_patch_pages_load_the_shared_patch_view():
    for page in ("shared-models.html", "reduce.html"):
        text = (STATIC / page).read_text()
        assert text.index("/assets/patch-view.js") < text.index(f"/assets/{page.replace('.html', '.js')}")
    assert "window.KumoPatch" in (STATIC / "shared-models.js").read_text()
    assert "window.KumoPatch" in (STATIC / "reduce.js").read_text()


def test_navigation_lists_the_page_and_the_page_has_no_help_text():
    assert 'href: "/reduce", label: "Reduce"' in (STATIC / "shell.js").read_text()
    html = (STATIC / "reduce.html").read_text()
    assert "<p" not in html and "@media" not in (STATIC / "reduce.css").read_text()


def test_package_data_covers_the_new_assets():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "static/*.html" in pyproject and "static/*.js" in pyproject and "static/*.css" in pyproject
