"""Local browser UI for the registered SQL rewrite rules."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import threading
import webbrowser

from . import preview_data
from . import scopes as scope_store
from . import state
from .formatting import FormatSqlRule, complexity, load_preferences, parse_preferences, save_preferences
from .rewrite import apply_rules, available_rules


MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_GITHUB_REQUEST_BYTES = 8 * 1024 * 1024
MAX_UI_STATE_BYTES = 64 * 1024
# Roadmap views. Each returns a JSON payload; preview_data stands in until the
# issues listed in docs/ui-roadmap.md replace these with real implementations.
INSIGHTS = {
    "/api/graph": preview_data.graph,
    "/api/cost": preview_data.cost,
    "/api/changes": preview_data.changes,
}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/browse": ("browse.html", "text/html; charset=utf-8"),
    "/browse/": ("browse.html", "text/html; charset=utf-8"),
    "/settings": ("settings.html", "text/html; charset=utf-8"),
    "/assets/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/assets/evidence.js": ("evidence.js", "text/javascript; charset=utf-8"),
    "/graph": ("insights.html", "text/html; charset=utf-8"),
    "/cost": ("insights.html", "text/html; charset=utf-8"),
    "/changes": ("insights.html", "text/html; charset=utf-8"),
    "/assets/insights.css": ("insights.css", "text/css; charset=utf-8"),
    "/assets/insights.js": ("insights.js", "text/javascript; charset=utf-8"),
    "/assets/browse.css": ("browse.css", "text/css; charset=utf-8"),
    "/assets/browse.js": ("browse.js", "text/javascript; charset=utf-8"),
    "/assets/background.jpg": ("background.jpg", "image/jpeg"),
}


def _complexity(sql: str) -> dict | None:
    try:
        return complexity(sql).to_json()
    except Exception:
        return None


def transform(sql: str, names: list[str], format_preferences: object = None) -> dict:
    """Apply selected rules and return a JSON-ready report."""

    if not isinstance(sql, str):
        raise ValueError("sql must be a string")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("select at least one transformation")
    known = available_rules()
    if len(names) > len(known) or len(set(names)) != len(names) or any(name not in known for name in names):
        raise ValueError("unknown or duplicate transformation")
    if not sql.strip():
        raise ValueError("paste SQL or SQLX to transform")

    overrides = None
    if format_preferences is not None:
        overrides = {"format_sql": FormatSqlRule(parse_preferences(format_preferences))}
    result = apply_rules(names, sql, overrides=overrides)
    rule_success = all(step.rule_success for step in result.steps)
    candidate_sql = result.sql if rule_success else ""
    return {
        "complexity": {
            "before": _complexity(sql),
            "after": _complexity(candidate_sql) if candidate_sql else None,
        },
        "sql": candidate_sql,
        "success": result.success,
        "rule_success": rule_success,
        "verification": result.verification.to_json(),
        "steps": [
            {
                "rule": step.rule,
                "changes": step.changes,
                "success": step.success,
                "rule_success": step.rule_success,
                "verification": step.verification.to_json(),
                "diagnostics": [
                    {"code": item.code, "message": item.message, "statement_index": item.statement_index}
                    for item in step.diagnostics
                ],
            }
            for step in result.steps
        ],
    }


class UIHandler(BaseHTTPRequestHandler):
    """Serve bundled assets and a small same-origin JSON API."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict | list) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self, limit: int = MAX_REQUEST_BYTES):
        """Read a JSON request body; sends the error response and returns None on failure."""

        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "send application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length is required"})
            return None
        if length < 0 or length > limit:
            self._json(413, {"error": "request is too large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return None

    def do_GET(self) -> None:
        if self.path == "/api/settings":
            self._json(200, {
                "ui": state.get_section("ui", {}),
                "format": load_preferences().to_json(),
                "scopes": [scope.to_json() for scope in scope_store.list_scopes()],
            })
            return
        if self.path == "/api/rules":
            self._json(200, [
                {"name": name, "summary": rule.summary}
                for name, rule in available_rules().items()
            ])
            return
        route = self.path.split("?", 1)[0]
        if route in INSIGHTS:
            self._json(200, INSIGHTS[route]())
            return
        if self.path.startswith("/api/catalog/"):
            self._catalog()
            return
        asset = ASSETS.get(route)
        if asset is None:
            self._json(404, {"error": "not found"})
            return
        filename, content_type = asset
        body = files("kumosql").joinpath("static", filename).read_bytes()
        self._send(200, body, content_type)

    def do_PUT(self) -> None:
        section = self.path.removeprefix("/api/settings/")
        if section not in ("ui", "format", "scopes") or section == self.path:
            self._json(404, {"error": "not found"})
            return
        payload = self._read_json(MAX_UI_STATE_BYTES if section == "ui" else MAX_REQUEST_BYTES)
        if payload is None:
            return
        try:
            if section == "ui":
                if not isinstance(payload, dict):
                    raise ValueError("ui settings must be an object")
                state.set_section("ui", payload)
                saved = payload
            elif section == "format":
                prefs = parse_preferences(payload)
                save_preferences(prefs)
                saved = prefs.to_json()
            else:
                if not isinstance(payload, list):
                    raise ValueError("scopes must be a list")
                parsed = [scope_store.parse_scope(item) for item in payload]
                scope_store.save_scopes(parsed)
                saved = [scope.to_json() for scope in parsed]
        except (ValueError, OSError) as exc:
            self._json(400 if isinstance(exc, ValueError) else 500, {"error": str(exc)})
            return
        self._json(200, saved)

    def _catalog(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        from . import bigquery_catalog

        route = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if route == "/api/catalog/projects":
                result = bigquery_catalog.list_projects()
            elif route == "/api/catalog/datasets":
                result = bigquery_catalog.list_datasets(_required(query, "project"))
            elif route == "/api/catalog/tables":
                result = bigquery_catalog.list_tables(
                    _required(query, "project"), _required(query, "dataset")
                )
            elif route == "/api/catalog/table":
                result = bigquery_catalog.get_table(
                    _required(query, "project"), _required(query, "dataset"),
                    _required(query, "table"),
                )
            else:
                self._json(404, {"error": "not found"})
                return
        except (ValueError, bigquery_catalog.CatalogError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, result)

    def do_POST(self) -> None:
        if self.path not in ("/api/transform", "/api/github/connect", "/api/github/file"):
            self._json(404, {"error": "not found"})
            return
        payload = self._read_json(
            MAX_GITHUB_REQUEST_BYTES if self.path.startswith("/api/github/") else MAX_REQUEST_BYTES
        )
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            if self.path == "/api/github/connect":
                from .github_repo import connect

                result = connect(payload.get("url"))
            elif self.path == "/api/github/file":
                from .github_repo import read_file

                result = read_file(payload.get("url"), payload.get("branch"), payload.get("path"))
            else:
                result = transform(payload.get("sql"), payload.get("rules"), payload.get("format"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value or len(value) > 1024:
        raise ValueError(f"{name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local KumoSQL browser UI")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), UIHandler)
    except OSError as exc:
        parser.error(f"could not start local server: {exc}")
    url = f"http://127.0.0.1:{args.port}/"
    print(f"KumoSQL UI: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
