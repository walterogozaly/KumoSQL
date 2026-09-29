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
