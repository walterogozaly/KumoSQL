"""Exercise the installed UI's HTTP contract with real rewrite rules."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlglot import exp

from kumosql import (
    PipelineResult,
    RewriteResult,
    RewriteRule,
    Verification,
    VerificationCheck,
    VerificationStatus,
    engine,
)
from kumosql.ui import UIHandler


@pytest.fixture
def ui_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post_json(base_url, payload):
    request = Request(
        f"{base_url}/api/transform",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        return json.load(response)


def test_ui_serves_assets_and_registered_rules(ui_server):
    for path, marker in (
        ("/", b"Original SQL"),
        ("/assets/style.css", b".workspace"),
        ("/assets/app.js", b"/api/transform"),
        ("/assets/shell.js", b"kumosql-sidebar"),
        ("/assets/shell.css", b".rail"),
        ("/favicon.svg", b"<svg"),
        ("/assets/settings.js", b"/api/settings/format"),
    ):
        with urlopen(ui_server + path) as response:
            assert marker in response.read()
    with urlopen(ui_server + "/api/rules") as response:
        rules = json.load(response)
    assert any(rule["name"] == "lift_subqueries" for rule in rules)


def test_ui_transforms_sql_and_exposes_verification(ui_server):
    result = post_json(ui_server, {
        "sql": "SELECT id FROM (SELECT id FROM t WHERE 1 = 1) AS x",
        "rules": ["lift_subqueries", "remove_trivial_predicates"],
    })
    assert result["success"]
    assert result["verification"]["status"] == "proven"
    assert isinstance(result["verification"]["checks"], list)
    assert all(isinstance(step["verification"], dict) for step in result["steps"])
    assert all("checks" in step["verification"] for step in result["steps"])
    assert "WITH" in result["sql"]
    assert "1 = 1" not in result["sql"]
    assert [step["rule"] for step in result["steps"]] == [
        "lift_subqueries", "remove_trivial_predicates"
    ]


def test_ui_rejects_unknown_rules(ui_server):
    with pytest.raises(HTTPError) as error:
        post_json(ui_server, {"sql": "SELECT 1", "rules": ["not_a_rule"]})
    assert error.value.code == 400
    assert "unknown" in json.load(error.value)["error"]


@pytest.mark.parametrize(
    ("sql", "diagnostic_code"),
    [
        ("SELECT * FROM", "parse_error"),
        (
            "WITH __lifted_subquery_001 AS (SELECT * FROM __lifted_subquery_002) "
            "SELECT * FROM __lifted_subquery_001",
            "cte_dependency_error",
        ),
    ],
)
def test_ui_withholds_rule_failure_output_and_exposes_diagnostic(
    ui_server, sql, diagnostic_code
):
    result = post_json(ui_server, {"sql": sql, "rules": ["lift_subqueries"]})

    assert not result["success"]
    assert not result["rule_success"]
    assert result["sql"] == ""
    assert result["verification"]["status"] == "failed"
    assert any(
        check["kind"] == "rewrite" and check["outcome"] == "failed"
        for check in result["verification"]["checks"]
    )
    assert any(
        item["code"] == diagnostic_code
        for step in result["steps"]
        for item in step["diagnostics"]
    )


class _DropWhereRule(RewriteRule):
    name = "test_drop_where_for_ui"
    summary = "Test that unproven output remains available for review"

    def rewrite_statement(self, statement, index):
        changed = 0
        for select in statement.find_all(exp.Select):
            if select.args.get("where") is not None:
                select.set("where", None)
                changed += 1
        return changed, []


class _DropSqlxWhereRule(RewriteRule):
    name = "test_drop_sqlx_where_for_ui"
    summary = "Test that SQLX restoration failures are diagnostics in the UI"

    def rewrite_statement(self, statement, index):
        where = statement.args.get("where")
        if where is None:
            return 0, []
        statement.set("where", None)
        return 1, []


def test_ui_keeps_unproven_candidate_separate_from_rule_failure(ui_server, monkeypatch):
    monkeypatch.setitem(engine._REGISTRY, _DropWhereRule.name, _DropWhereRule())
    result = post_json(
        ui_server,
        {
            "sql": "SELECT value FROM source WHERE value > 1",
            "rules": [_DropWhereRule.name],
        },
    )

    assert result["rule_success"]
    assert not result["success"]
    assert result["verification"]["status"] == "unproven"
    assert result["sql"]


def test_ui_serializes_planner_checked_and_its_supporting_checks(ui_server, monkeypatch):
    source = "SELECT 1 AS value"
    candidate = "SELECT 2 AS value"
    verification = Verification(
        VerificationStatus.PLANNER_CHECKED,
        "the planner check passed, but equivalence could not be proven",
        checks=(
            VerificationCheck("equivalence_proof", "not_proven", "No proof was found."),
            VerificationCheck(
                "planner",
                "passed",
                "Both queries planned and schemas match; results were not compared.",
                (
                    ("scope", "end_to_end"),
                    ("schema_matches", True),
                    ("schema_differences", ()),
                    ("estimated_bytes_delta", 120),
                    ("results_compared", False),
                ),
            ),
        ),
    )
    step = RewriteResult(
        "format_sql", source, candidate, 1, (), verification, rule_success=True
    )
    monkeypatch.setattr(
        "kumosql.ui.apply_rules",
        lambda names, sql, overrides=None: PipelineResult(sql, candidate, (step,), verification),
    )

    result = post_json(ui_server, {"sql": source, "rules": ["format_sql"]})

    assert result["verification"]["status"] == "planner_checked"
    assert result["steps"][0]["verification"]["status"] == "planner_checked"
    assert result["verification"]["checks"] == [
        {"kind": "equivalence_proof", "outcome": "not_proven", "detail": "No proof was found."},
        {
            "kind": "planner",
            "outcome": "passed",
            "detail": "Both queries planned and schemas match; results were not compared.",
            "evidence": {
                "scope": "end_to_end",
                "schema_matches": True,
                "schema_differences": [],
                "estimated_bytes_delta": 120,
                "results_compared": False,
            },
        },
    ]
    assert result["steps"][0]["verification"]["checks"] == result["verification"]["checks"]
    assert not result["success"]
    assert result["rule_success"]
    assert result["sql"] == candidate


def test_ui_withholds_sqlx_restoration_failure_and_exposes_diagnostic(ui_server, monkeypatch):
    monkeypatch.setitem(engine._REGISTRY, _DropSqlxWhereRule.name, _DropSqlxWhereRule())
    result = post_json(
        ui_server,
        {
            "sql": (
                'config { type: "table" }\n'
                'SELECT id FROM source WHERE ${when(incremental(), "id > 0", "TRUE")}'
            ),
            "rules": [_DropSqlxWhereRule.name],
        },
    )

    assert not result["success"]
    assert not result["rule_success"]
    assert result["sql"] == ""
    assert any(
        item["code"] == "sqlx_restore_error"
        for step in result["steps"]
        for item in step["diagnostics"]
    )


def put_json(base_url, section, payload):
    request = Request(
        f"{base_url}/api/settings/{section}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urlopen(request) as response:
        return json.load(response)


def get_settings(base_url):
    with urlopen(base_url + "/api/settings") as response:
        return json.load(response)


def test_ui_settings_persist_across_server_restarts(ui_server):
    assert get_settings(ui_server)["ui"] == {}
    put_json(ui_server, "ui", {"theme": "dark", "enabled": ["format_sql"]})
    scopes = [{"name": "My Team", "rule": {"field": "author", "op": "in", "value": ["ana@co.com"]}}]
    assert put_json(ui_server, "scopes", scopes) == scopes
    legacy = [{"name": "Old", "fields": {"author": ["ana@co.com"]}}]
    assert put_json(ui_server, "scopes", legacy)[0]["rule"]["op"] == "in"
    assert put_json(ui_server, "scopes", scopes) == scopes
    saved = put_json(ui_server, "format", {"keyword_case": "lower"})
    assert saved["keyword_case"] == "lower"

    settings = get_settings(ui_server)  # state lives on disk, not in the server object
    assert settings["ui"]["theme"] == "dark"
    assert settings["scopes"] == scopes
    assert settings["format"]["keyword_case"] == "lower"


def test_ui_rejects_invalid_settings(ui_server):
    for section, payload in (
        ("scopes", [{"name": "x"}]),
        ("scopes", [{"name": "a", "fields": {"f": ["v"]}}, {"name": "A", "fields": {"f": ["v"]}}]),
        ("format", {"max_line_length": 1}),
        ("ui", []),
    ):
        with pytest.raises(HTTPError) as error:
            put_json(ui_server, section, payload)
        assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        put_json(ui_server, "other", {})
    assert error.value.code == 404


def test_ui_formats_with_request_preferences_and_reports_complexity(ui_server):
    sql = "select a,b from t join u on t.id=u.id"
    result = post_json(ui_server, {
        "sql": sql, "rules": ["format_sql"], "format": {"keyword_case": "lower"},
    })
    assert result["success"] and result["sql"].startswith("select")
    assert result["complexity"]["before"]["metrics"]["joins"] == 1
    assert result["complexity"]["after"]["band"] == "low"
    assert any(rule["name"] == "format_sql" for rule in json.load(urlopen(ui_server + "/api/rules")))


def get_json(base_url, path):
    with urlopen(base_url + path) as response:
        return json.load(response)


def test_ui_serves_roadmap_views(ui_server):
    for path, marker in (
        ("/graph", b"insights.js"),
        ("/cost?x=1", b"insights.js"),
        ("/changes", b"insights.js"),
        ("/assets/insights.js", b"/api/graph"),
        ("/assets/insights.css", b".graph-canvas"),
        ("/assets/evidence.js", b"planner_checked"),
    ):
        with urlopen(ui_server + path) as response:
            assert marker in response.read()


@pytest.mark.parametrize("path", ["/api/graph", "/api/cost", "/api/changes?refresh=1"])
def test_ui_insight_endpoints_serve_an_empty_state_without_a_project(ui_server, path):
    payload = get_json(ui_server, path)
    assert payload["empty"] is True and payload["needs"] == "project"
    assert payload["message"] and payload["source"]["kind"] == "none"
    assert "preview" not in payload


def test_ui_pages_carry_no_sample_data_banner(ui_server):
    for path in ("/graph", "/cost", "/changes", "/assets/insights.js"):
        with urlopen(ui_server + path) as response:
            page = response.read().decode()
        assert "Preview with sample data" not in page and "Roadmap items" not in page
        assert "sample data" not in page.lower()


def test_ui_lists_scope_fields_including_saved_scopes(ui_server):
    put_json(ui_server, "scopes", [{"name": "Mine", "rule": {"field": "submitter", "op": "eq", "value": "ana"}}])
    payload = get_json(ui_server, "/api/scope-fields")
    assert any(f["name"] == "submitter" and f["source"] == "saved" for f in payload["fields"])
    assert {"in", "regex", "is_null"} <= {o["op"] for o in payload["operators"]}

def test_ui_lists_sqlfluff_rules_for_the_settings_panel(ui_server):
    with urlopen(ui_server + "/api/sqlfluff/rules") as response:
        rules = json.load(response)
    by_code = {rule["code"]: rule for rule in rules}
    assert by_code["LT01"]["category"] == "layout"
    assert by_code["LT01"]["description"]
    assert by_code["CP01"]["fixable"] is True
    assert "capitalisation" in by_code["CP01"]["groups"]
    # Rules for other dialects are left out; KumoSQL formats BigQuery.
    assert not any(rule["category"] in ("tsql", "postgres", "oracle") for rule in rules)


def test_pages_ship_the_scope_picker_and_settings_section(ui_server):
    for path in ("/graph", "/cost", "/changes"):
        with urlopen(ui_server + path) as response:
            page = response.read().decode()
        assert 'id="scope-picker"' in page and "/assets/scopes.js" in page
    with urlopen(ui_server + "/assets/scopes.js") as response:
        script = response.read().decode()
    assert "KumoScopes" in script and "renderManager" in script
    with urlopen(ui_server + "/assets/settings.js") as response:
        assert 'id: "scopes"' in response.read().decode()


@pytest.mark.parametrize("path", ["/assets/lineage-view.js", "/assets/vendor/cytoscape.min.js"])
def test_graph_explorer_assets_are_served(ui_server, path):
    with urlopen(f"{ui_server}{path}") as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/javascript")
        assert len(response.read()) > 1000


def test_version_is_reported():
    from kumosql import version

    data = version.info()
    assert data["version"] and "commit" in data
    assert version.describe().startswith(data["version"])


def test_ui_solver_settings_round_trip_and_validate(ui_server):
    def call(method, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(ui_server + "/api/prover", data=data, method=method, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    assert call("GET")["enabled"] is True
    saved = call("PUT", {"enabled": False, "timeout_ms": 2000})
    assert (saved["enabled"], saved["timeout_ms"]) == (False, 2000)
    assert call("GET")["enabled"] is False
    for bad in ({"enabled": "yes"}, {"timeout_ms": 1}):
        with pytest.raises(HTTPError) as error:
            call("PUT", bad)
        assert error.value.code == 400
