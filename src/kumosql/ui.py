"""Local browser UI for the registered SQL rewrite rules."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import threading
import webbrowser

from .rewrite import apply_rules, available_rules


MAX_REQUEST_BYTES = 5 * 1024 * 1024
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def transform(sql: str, names: list[str]) -> dict:
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

    result = apply_rules(names, sql)
    return {
        "sql": result.sql,
        "success": result.success,
        "verification": {
            "status": result.verification.status.value,
            "reason": result.verification.reason,
            "details": list(result.verification.details),
        },
        "steps": [
            {
                "rule": step.rule,
                "changes": step.changes,
                "success": step.success,
                "rule_success": step.rule_success,
                "verification": step.verification.status.value,
                "diagnostics": [
                    {"code": item.code, "message": item.message, "statement_index": item.statement_index}
                    for item in step.diagnostics
                ],
                "details": list(step.verification.details),
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

    def do_GET(self) -> None:
        if self.path == "/api/rules":
            self._json(200, [
                {"name": name, "summary": rule.summary}
                for name, rule in available_rules().items()
            ])
            return
        asset = ASSETS.get(self.path)
        if asset is None:
            self._json(404, {"error": "not found"})
            return
        filename, content_type = asset
        body = files("kumosql").joinpath("static", filename).read_bytes()
        self._send(200, body, content_type)

    def do_POST(self) -> None:
        if self.path != "/api/transform":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "send application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length is required"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(413, {"error": "SQL input is too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            result = transform(payload.get("sql"), payload.get("rules"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)


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
