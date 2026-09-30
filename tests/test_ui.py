"""Exercise the installed UI's HTTP contract with real rewrite rules."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

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
        ("/favicon.svg", b"<svg"),
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
    scopes = [{"name": "My Team", "fields": {"author": ["ana@co.com"]}}]
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
