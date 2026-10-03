"""Exercise the local UI boundary with raw requests that cannot add tokens implicitly."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread

import pytest

from kumosql import state
from kumosql.ui import ASSETS, SESSION_HEADER, UIHandler, UIServer


@contextmanager
def running_server():
    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def server():
    with running_server() as instance:
        yield instance


def request(server, method="GET", path="/api/version", headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def bootstrap(server, path="/"):
    status, headers, body = request(server, path=path)
    assert status == 200
    token = re.search(rb'name="kumosql-session-token" content="([^"]+)"', body)[1].decode("ascii")
    return token, headers, body


def assert_hardened(headers):
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
    assert not any(name.lower().startswith("access-control-") for name in headers)


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/assets/session.js"), ("GET", "/api/version"),
    ("PUT", "/api/settings/ui"), ("POST", "/api/project/clear"),
    ("DELETE", "/api/settings/ui"), ("OPTIONS", "/"),
])
def test_foreign_host_is_refused_before_assets_or_api(server, method, path):
    token, _, _ = bootstrap(server)
    status, headers, _ = request(server, method, path, {
        "Host": f"other.invalid:{server.server_port}",
        "Origin": f"http://other.invalid:{server.server_port}",
        SESSION_HEADER: token, "Content-Type": "application/json",
    }, '{}')
    assert status == 403
    assert_hardened(headers)
    assert state.get_section("ui", {}) == {}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_own_host_and_origin_allow_authenticated_write(server, host):
    token, _, _ = bootstrap(server)
    authority = f"{host}:{server.server_port}"
    status, headers, body = request(server, "PUT", "/api/settings/ui", {
        "Host": authority, "Origin": f"http://{authority}", SESSION_HEADER: token,
        "Content-Type": "application/json",
    }, '{"theme":"dark"}')
    assert status == 200
    assert json.loads(body) == {"theme": "dark"}
    assert state.get_section("ui", {}) == {"theme": "dark"}
    assert_hardened(headers)


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/settings/ui"), ("POST", "/api/project/clear"), ("DELETE", "/api/settings/ui"),
])
@pytest.mark.parametrize("origin", ["http://other.invalid:{port}", "null", "https://127.0.0.1:{port}", "http://127.0.0.1:1"])
def test_foreign_origin_is_refused_on_writes(server, method, path, origin):
    token, _, _ = bootstrap(server)
    status, headers, _ = request(server, method, path, {
        "Origin": origin.format(port=server.server_port), SESSION_HEADER: token,
        "Content-Type": "application/json",
    }, '{"theme":"dark"}')
    assert status == 403
    assert state.get_section("ui", {}) == {}
    assert_hardened(headers)


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/version"), ("GET", "/api/settings"), ("GET", "/api/catalog/projects?refresh=1"),
    ("PUT", "/api/settings/ui"), ("POST", "/api/project/clear"), ("DELETE", "/api/settings/ui"),
])
@pytest.mark.parametrize("token", [None, "wrong-session", "", "\u00e9"])
def test_all_api_calls_require_current_token(server, method, path, token):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[SESSION_HEADER] = token
    status, response_headers, _ = request(server, method, path, headers, '{}')
    assert status == 403
    assert state.get_section("ui", {}) == {}
    assert_hardened(response_headers)


@pytest.mark.parametrize("header", ["Host", "Origin", SESSION_HEADER])
def test_duplicate_boundary_headers_are_rejected(server, header):
    token, _, _ = bootstrap(server)
    values = {"Host": f"127.0.0.1:{server.server_port}",
              "Origin": f"http://127.0.0.1:{server.server_port}", SESSION_HEADER: token}
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.putrequest("PUT", "/api/settings/ui", skip_host=True)
        for name, value in values.items():
            connection.putheader(name, value)
        connection.putheader(header, values[header])
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()


def test_missing_host_and_wrong_port_are_rejected(server):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.putrequest("GET", "/", skip_host=True)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
    assert request(server, path="/", headers={"Host": "127.0.0.1:1"})[0] == 403


def test_non_browser_api_calls_work_with_token_without_origin(server):
    token, _, _ = bootstrap(server)
    assert request(server, headers={SESSION_HEADER: token})[0] == 200
    assert request(server, "PUT", "/api/settings/ui", {
        SESSION_HEADER: token, "Content-Type": "application/json",
    }, '{}')[0] == 200


def test_json_only_and_no_cors_are_preserved(server):
    token, _, _ = bootstrap(server)
    status, headers, _ = request(server, "POST", "/api/project/clear", {
        SESSION_HEADER: token, "Content-Type": "text/plain",
    }, '{}')
    assert status == 415
    assert_hardened(headers)
    status, headers, _ = request(server, "OPTIONS", "/api/project/clear", {SESSION_HEADER: token})
    assert status == 501
    assert not any(name.lower().startswith("access-control-") for name in headers)


@pytest.mark.parametrize("path", [path for path, (_, kind) in ASSETS.items() if kind.startswith("text/html")])
def test_every_page_bootstraps_token_before_scripts(server, path):
    token, headers, body = bootstrap(server, path)
    assert token == server.session_token
    assert "__KUMOSQL_SESSION_TOKEN__" not in body.decode()
    assert body.index(b'kumosql-session-token') < body.index(b'/assets/session.js') < body.index(b'/assets/shell.js')
    assert_hardened(headers)


def test_session_token_is_replaced_when_server_restarts(server):
    token, _, _ = bootstrap(server)
    with running_server() as restarted:
        replacement, _, _ = bootstrap(restarted)
        assert token != replacement
        assert len(replacement) >= 43
        assert request(restarted, headers={SESSION_HEADER: token})[0] == 403
        assert request(restarted, headers={SESSION_HEADER: replacement})[0] == 200


def test_smoke_client_uses_page_bootstrap(server):
    from kumosql.smoke import call

    status, body = call(f"http://127.0.0.1:{server.server_port}", "/api/project/clear", {})
    assert status == 200
    assert body == {"loaded": False}
    assert call(f"http://127.0.0.1:{server.server_port}", "/api/version")[0] == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to exercise the browser fetch wrapper")
def test_browser_bootstrap_preserves_requests_and_keeps_token_local():
    script = Path(__file__).resolve().parents[1] / "src/kumosql/static/session.js"
    program = r'''
const assert = require("node:assert/strict");
global.location = new URL("http://127.0.0.1:8765/graph");
global.document = {querySelector: () => ({content: "session-secret"})};
const calls = [];
global.window = {fetch: (...args) => {calls.push(args); return Promise.resolve("response");}};
require(process.argv[1]);
window.fetch("/api/version");
assert.equal(calls[0][1].headers.get("X-KumoSQL-Session"), "session-secret");
const signal = new AbortController().signal;
window.fetch("/api/settings/ui", {method: "PUT", body: "{}", headers: {"Content-Type": "application/json"}, signal});
assert.equal(calls[1][1].headers.get("Content-Type"), "application/json");
assert.equal(calls[1][1].headers.get("X-KumoSQL-Session"), "session-secret");
assert.equal(calls[1][1].method, "PUT");
assert.equal(calls[1][1].body, "{}");
assert.equal(calls[1][1].signal, signal);
const request = new Request(location.origin + "/api/version", {headers: {"Accept": "application/json"}});
window.fetch(request);
assert.equal(calls[2][0], request);
assert.equal(calls[2][1].headers.get("Accept"), "application/json");
assert.equal(calls[2][1].headers.get("X-KumoSQL-Session"), "session-secret");
window.fetch(request, {headers: {"Accept": "text/plain"}});
assert.equal(calls[3][1].headers.get("Accept"), "text/plain");
for (const url of ["http://other.invalid/api/version", "http://127.0.0.1:8766/api/version", "/assets/app.js"]) {
  window.fetch(url);
  assert.equal(calls.at(-1)[1], undefined);
}
'''
    subprocess.run([shutil.which("node"), "-e", program, str(script)], check=True, capture_output=True, text=True)
