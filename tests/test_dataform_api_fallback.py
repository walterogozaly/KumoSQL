"""The Dataform API fallback for computed declarations: what it reads, and what the load says about it either way."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from kumosql import bigquery_catalog as catalog, live_graph, state, workflow_configs as wf
from kumosql.pipeline import load_sqlx_project

SPEC = importlib.util.spec_from_file_location("make_dataform_fixture", Path(__file__).parent.parent / "tools" / "make_dataform_fixture.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)

URL = "https://github.com/example-org/example-repo"
SECRET = "very-private-project"  # a name from the user's project must never appear in a diagnostic
REPO = f"projects/{SECRET}/locations/us-central1/repositories/main"
RESULT = f"{REPO}/compilationResults/c1"
COMPUTED = [("raw_computed", "computed_declared_a"), ("raw_computed", "computed_declared_b")]
DYNAMIC = {
    "workflow_settings.yaml": "defaultProject: proj\ndefaultDataset: analytics\n",
    "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
    "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(catalog, "_disk_loaded", False)
    catalog._memory.clear()
    monkeypatch.setattr(wf, "_token", None)
    yield
    catalog._memory.clear()


def select(projects):
    state.set_section("bigquery", {"projects": projects})


def diagnostic(pipeline, code):
    found = [d for d in pipeline.diagnostics if d.code == code]
    assert len(found) == 1, [d.code for d in pipeline.diagnostics]
    return found[0].message


def write(root):
    root.mkdir(parents=True, exist_ok=True)
    for name, text in DYNAMIC.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def load(root, compiled):
    return load_sqlx_project(write(root), compiled_targets=compiled)


# ---------------------------------------------------------------- why the compilation could not be read


def reason_of(monkeypatch, **setup):
    for name, value in setup.items():
        monkeypatch.setattr(wf, name, value)
    with pytest.raises(wf.CompiledGraphUnavailable) as caught:
        wf.compiled_targets(URL)
    return caught.value


def test_no_selected_project_is_skipped_before_any_request(monkeypatch):
    def forbidden(*args):
        raise AssertionError("no request without a project to search")

    error = reason_of(monkeypatch, _get=forbidden)
    assert (error.reason, error.attempted) == ("no_projects", False)


def test_missing_credentials_are_skipped_with_the_credential_message(monkeypatch):
    select([SECRET])
    monkeypatch.delenv("BQ_ACCESS_TOKEN", raising=False)

    def refuse(scope):
        raise RuntimeError("no BigQuery credentials: set BQ_ACCESS_TOKEN")

    error = reason_of(monkeypatch, access_token=refuse)
    assert (error.reason, error.attempted) == ("no_credentials", False)
    assert "BQ_ACCESS_TOKEN" in str(error)


def test_a_repository_that_does_not_match_names_what_was_searched(monkeypatch):
    select([SECRET, "another"])
    error = reason_of(monkeypatch, _bearer=lambda: "t", _list_all=lambda url, key: [
        {"name": f"{REPO}", "gitRemoteSettings": {"url": "https://github.com/example-org/other"}}])
    assert (error.reason, error.attempted) == ("no_repository", True)
    assert "2 searched project" in str(error) and "us-central1" in str(error) and SECRET not in str(error)


def test_an_api_error_gives_the_status_and_never_dataforms_own_message(monkeypatch):
    select([SECRET])

    def denied(url, key):
        raise wf.WorkflowConfigError(f"Dataform returned HTTP 403: Permission denied on projects/{SECRET}", 403)

    error = reason_of(monkeypatch, _bearer=lambda: "t", _list_all=denied)
    assert error.reason == "api_error" and "HTTP 403" in str(error) and SECRET not in str(error)


def test_a_repository_without_a_compilation_says_so(monkeypatch):
    select([SECRET])

    def listing(url, key):
        if key == "repositories":
            return [{"name": REPO, "gitRemoteSettings": {"url": URL + ".git"}}]
        return []

    monkeypatch.setattr(wf, "_get", lambda url: {})
    error = reason_of(monkeypatch, _bearer=lambda: "t", _list_all=listing)
    assert error.reason == "no_compilation"


# ---------------------------------------------------------------- what the load says


def test_a_load_with_no_repository_says_the_fallback_was_not_used(tmp_path):
    pl = load_sqlx_project(write(tmp_path))
    assert "not used" in diagnostic(pl, "compiled_graph_not_requested")


def test_a_skipped_fallback_says_why(tmp_path):
    def skipped():
        raise wf.CompiledGraphUnavailable("no_credentials", "no BigQuery credentials", attempted=False)

    message = diagnostic(load(tmp_path, skipped), "compiled_graph_unavailable")
    assert "was skipped (no_credentials): no BigQuery credentials" in message and "stay unresolved" in message


def test_a_failed_fallback_says_it_was_tried(tmp_path):
    def failed():
        raise wf.CompiledGraphUnavailable("api_error", "Dataform returned HTTP 503 for 1 of 1 searched project(s) in us-central1")

    assert "was tried and failed (api_error)" in diagnostic(load(tmp_path, failed), "compiled_graph_unavailable")


def test_an_unexpected_error_still_gets_a_diagnostic_without_its_text(tmp_path):
    def boom():
        raise ValueError(SECRET)

    message = diagnostic(load(tmp_path, boom), "compiled_graph_unavailable")
    assert "was tried and failed (ValueError)" in message and SECRET not in message


def test_an_empty_compilation_settles_nothing(tmp_path):
    pl = load(tmp_path, lambda: [])
    assert "empty_compilation" in diagnostic(pl, "compiled_graph_unavailable")
    assert {d.code for d in pl.diagnostics} >= {"unsupported_ref"}


def test_a_successful_fallback_counts_what_it_read_and_never_names_it(tmp_path):
    pl = load(tmp_path, lambda: [(("lake", "raw", "orders"), True), (("proj", "analytics", "m"), False)])
    message = diagnostic(pl, "compiled_graph_read")
    assert "read 2 compiled actions (1 declarations)" in message and "orders" not in message
    assert not [d for d in pl.diagnostics if d.code == "compiled_graph_unavailable"]


# ---------------------------------------------------------------- end to end against a local Dataform API


class Api(BaseHTTPRequestHandler):
    requests: list = []
    location = "us-central1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        Api.requests.append(path)
        auth = self.headers.get("Authorization")
        if auth != "Bearer test-token":
            return self.send(401, {"error": {"message": "no"}})
        if path.startswith(f"/v1beta1/projects/{SECRET}/locations/") and path.endswith("/repositories"):
            if f"/locations/{Api.location}/" not in path:
                return self.send(200, {})  # a location without repositories lists none
            return self.send(200, {"repositories": [{"name": REPO, "gitRemoteSettings": {"url": "git@github.com:Example-Org/example-repo.git"}}]})
        if path == f"/v1beta1/{REPO}/releaseConfigs":
            return self.send(200, {"releaseConfigs": [{"releaseCompilationResult": RESULT}]})
        if path == f"/v1beta1/{RESULT}:query":
            actions = [{"target": {"database": "proj", "schema": schema, "name": name}, "declaration": {}} for schema, name in COMPUTED]
            actions.append({"target": {"database": "proj", "schema": "marts", "name": "report"}, "relation": {}})
            return self.send(200, {"compilationResultActions": actions})
        self.send(404, {"error": {"message": "missing"}})

    def send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def api(monkeypatch):
    Api.requests = []
    Api.location = "us-central1"
    server = HTTPServer(("127.0.0.1", 0), Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(wf, "_API", f"http://127.0.0.1:{server.server_port}/v1beta1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("BQ_ACCESS_TOKEN", "test-token")
    yield Api
    server.shutdown()
    server.server_close()


def synthetic_project(tmp_path):
    root = tmp_path / "fx"
    fixture.generate(root, models=30, seed=3)
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_text(encoding="utf-8", errors="replace")
            for p in root.rglob("*") if p.is_file() and (p.suffix in (".sqlx", ".sql", ".js") or p.name in ("workflow_settings.yaml", "dataform.json"))}


def test_computed_declarations_resolve_through_the_api_and_the_load_says_so(tmp_path, api):
    select([SECRET])
    pl = live_graph.pipeline_from_files(synthetic_project(tmp_path), URL)
    keys = set(pl.sources)
    assert {f"proj.{schema}.{name}" for schema, name in COMPUTED} <= keys
    assert "read 3 compiled actions (2 declarations)" in diagnostic(pl, "compiled_graph_read")
    assert api.requests[-1] == f"/v1beta1/{RESULT}:query"


def test_the_same_project_without_a_matching_repository_says_where_it_looked(tmp_path, api):
    select([SECRET])
    api.location = "europe-west1"  # the repository lives in another region than the one searched
    pl = live_graph.pipeline_from_files(synthetic_project(tmp_path), URL)
    assert not any("computed_declared" in key for key in pl.sources)
    message = diagnostic(pl, "compiled_graph_unavailable")
    assert "no_repository" in message and "us-central1" in message
    assert SECRET not in message


def test_the_same_project_with_a_rejected_token_reports_the_status(tmp_path, api, monkeypatch):
    select([SECRET])
    monkeypatch.setenv("BQ_ACCESS_TOKEN", "stale")
    message = diagnostic(live_graph.pipeline_from_files(synthetic_project(tmp_path), URL), "compiled_graph_unavailable")
    assert "was tried and failed (api_error)" in message and "HTTP 401" in message


def test_the_same_project_with_no_selected_project_is_skipped(tmp_path, api):
    pl = live_graph.pipeline_from_files(synthetic_project(tmp_path), URL)
    assert "was skipped (no_projects)" in diagnostic(pl, "compiled_graph_unavailable")
    assert api.requests == []
