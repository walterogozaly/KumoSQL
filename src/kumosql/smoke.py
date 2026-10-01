"""End-to-end smoke test: ``python -m kumosql smoke --repo URL``.

Starts the real server in a scratch folder, connects a Dataform repository through the API the way
Settings does, waits for the graph and the analysis, opens every page in a real browser, and fails
on any console error, failed or erroring request, unresponsive server, or ``ERROR`` line in
``ui.log``. It is the release gate for anything touching loading, git, the graph or logging.

It reproduces a locked-down work laptop: SSH is blocked (a failing ``GIT_SSH_COMMAND``), the
local data folder is outside AppData, and nothing needs administrator rights or a launcher on
``PATH``. The browser is driven with Playwright (``python -m pip install --user playwright``); it
uses Microsoft Edge or Chrome when installed, so no browser download is needed.

Exit status 0 means every step passed. A JSON report and screenshots are written to the work folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PAGES = [("/", "Workspace"), ("/graph", "Query graph"), ("/cost", "Cost"), ("/changes", "Change reports"), ("/browse", "BigQuery")]
SETTINGS_SECTIONS_MIN = 3  # Settings must offer at least this many sections
SLOW_REQUEST_SECONDS = 10.0  # a trivial request taking longer than this means the server is hung
# Console noise that does not come from KumoSQL: none is ignored by default.
IGNORED_CONSOLE = re.compile(r"$^")


class Report:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, name: str, ok: bool, detail: str = "", seconds: float = 0.0) -> bool:
        self.steps.append({"step": name, "ok": ok, "detail": detail, "seconds": round(seconds, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {name} ({seconds:.1f}s){': ' + detail if detail else ''}", flush=True)
        return ok

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.steps)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(base: str, path: str, payload: object = None, timeout: float = 120.0) -> tuple[int, object]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


class Watchdog(threading.Thread):
    """Pings a trivial endpoint every second; records the slowest answer and every failure."""

    def __init__(self, base: str) -> None:
        super().__init__(daemon=True)
        self.base, self.stop_event = base, threading.Event()
        self.slowest, self.failures = 0.0, []

    def run(self) -> None:
        while not self.stop_event.wait(1.0):
            started = time.monotonic()
            for attempt in (1, 2):
                try:
                    status, _ = call(self.base, "/api/version", timeout=SLOW_REQUEST_SECONDS * 3)
                    if status != 200:
                        self.failures.append(f"/api/version answered {status}")
                    break
                except Exception as exc:  # noqa: BLE001
                    # Windows sometimes resets a fresh local connection (WinError 10054) while the server
                    # is busy; that is not a hang. Only a second reset in a row counts.
                    reset = isinstance(exc, ConnectionResetError) or isinstance(getattr(exc, "reason", None), ConnectionResetError)
                    if reset and attempt == 1:
                        continue
                    self.failures.append(f"/api/version failed{' twice' if reset else ''}: {exc}")
                    break
            self.slowest = max(self.slowest, time.monotonic() - started)


def ssh_blocked_command() -> str:
    return f'"{Path(sys.executable).as_posix()}" -c "import sys; sys.stderr.write(\'KUMO_SSH_BLOCKED\'); sys.exit(255)"'


def check_ssh_blocked(env: dict, report: Report) -> None:
    started = time.monotonic()
    done = subprocess.run(["git", "ls-remote", "ssh://git@example.org/octocat/Hello-World.git"], env=env, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)
    blocked = done.returncode != 0 and "KUMO_SSH_BLOCKED" in done.stderr
    report.step("SSH is blocked for the test", blocked, "git's SSH command is replaced by one that fails" if blocked else "GIT_SSH_COMMAND was not used: " + done.stderr[-200:], time.monotonic() - started)


def collect_server_errors(data_dir: Path, process_output: list[str]) -> list[str]:
    lines = []
    log = data_dir / "ui.log"
    if log.exists():
        lines += [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if " ERROR " in line or "Traceback" in line]
    lines += [line for line in process_output if "Traceback" in line or "ERROR" in line]
    return lines


def pick_browser(playwright, wanted: str, path: str | None = None):
    if path:
        return playwright.chromium.launch(executable_path=path, headless=True), "custom"
    order = [wanted] if wanted != "auto" else ["msedge", "chrome", "chromium"]
    errors = []
    for name in order:
        try:
            return playwright.chromium.launch(channel=None if name == "chromium" else name, headless=True), name
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {str(exc).splitlines()[0]}")
    raise RuntimeError("no browser could be started (" + "; ".join(errors) + ")")


def run_browser(base: str, work: Path, report: Report, browser_name: str, browser_path: str | None, page_timeout: float, graph_nodes: int) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report.step("browser available", False, "Playwright is not installed. Run: python -m pip install --user playwright")
        return
    problems: list[str] = []
    screens = work / "screens"
    screens.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser, used = pick_browser(playwright, browser_name, browser_path)
        except RuntimeError as exc:
            report.step("browser available", False, str(exc))
            return
        report.step("browser available", True, used)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        current = {"name": "start"}

        def note(kind: str, text: str) -> None:
            problems.append(f"[{current['name']}] {kind}: {text[:400]}")

        page.on("console", lambda m: m.type == "error" and not IGNORED_CONSOLE.search(m.text) and note("console error", m.text + " " + str(m.location.get("url", ""))))
        page.on("pageerror", lambda e: note("page error", str(e)))
        page.on("requestfailed", lambda r: note("request failed", f"{r.method} {r.url} {r.failure}") if "net::ERR_ABORTED" not in str(r.failure) else None)
        page.on("response", lambda r: note("http %d" % r.status, f"{r.request.method} {r.url}") if r.status >= 400 else None)
        page.set_default_timeout(page_timeout * 1000)

        def visit(label: str, action) -> None:
            before = len(problems)
            current["name"] = label
            started = time.monotonic()
            detail = ""
            ok = True
            try:
                detail = action() or ""
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            new = problems[before:]
            try:
                page.screenshot(path=str(screens / (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") + ".png")))
            except Exception:  # noqa: BLE001
                pass
            if new:
                ok, detail = False, (detail + " | " if detail else "") + " | ".join(new[:4]) + (f" (+{len(new) - 4} more)" if len(new) > 4 else "")
            report.step(f"page {label}", ok, detail, time.monotonic() - started)

        def open_page(path: str, title: str):
            def go() -> str:
                page.goto(base + path, wait_until="load")
                page.wait_for_function("() => { const t = document.getElementById('page-title'); return !t || (t.textContent || '').trim() !== 'Loading…'; }")
                page.wait_for_load_state("networkidle")
                if page.locator("#load-error:visible").count():
                    raise RuntimeError("page shows an error: " + page.locator("#load-error").inner_text()[:300])
                if not page.locator("#rail").count() and not page.locator(".rail").count():
                    raise RuntimeError("sidebar did not render")
                return ""
            visit(f"{title} ({path})", go)

        for path, title in PAGES:
            open_page(path, title)

        def graph_views() -> str:
            page.goto(base + "/graph", wait_until="load")
            page.wait_for_function("() => !document.getElementById('load-error') || document.getElementById('load-error').hidden")
            page.wait_for_load_state("networkidle")
            for view in ("simple", "explorer"):
                button = page.locator(f'button[data-view="{view}"]')
                if button.count():
                    button.click()
                    page.wait_for_timeout(1500)
            for mode in ("readers", "impact", "lineage", "overlap"):
                button = page.locator(f'button[data-mode="{mode}"]')
                if button.count():
                    button.click()
                    page.wait_for_timeout(500)
            canvas = page.locator(".graph-canvas, .lv-host canvas").count()
            if not canvas:
                raise RuntimeError("no graph canvas rendered")
            return f"graph canvas rendered ({graph_nodes} nodes served)"
        visit("Query graph views and modes", graph_views)

        def settings() -> str:
            page.goto(base + "/", wait_until="load")
            page.locator("#settings-button").click()
            page.locator("dialog.settings-panel[open]").wait_for()
            sections = page.locator(".sp-nav button")
            count = sections.count()
            if count < SETTINGS_SECTIONS_MIN:
                raise RuntimeError(f"Settings shows only {count} sections")
            for index in range(count):
                sections.nth(index).click()
                page.wait_for_timeout(400)
                if page.locator(".sp-status.is-error, .sp-error").count():
                    raise RuntimeError("Settings section " + str(index) + " shows an error: " + page.locator(".sp-status.is-error, .sp-error").first.inner_text()[:200])
            return f"{count} Settings sections opened"
        visit("Settings sections", settings)
        browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kumosql smoke", description="End-to-end smoke test of the server, a repository and every page")
    parser.add_argument("--repo", default=os.environ.get("KUMOSQL_SMOKE_REPO"), help="Dataform repository to connect (https URL, or a local path); or set KUMOSQL_SMOKE_REPO")
    parser.add_argument("--branch", help="Branch to load (default: the remote's default branch)")
    parser.add_argument("--work-dir", type=Path, help="Scratch folder for the data folder, clones, screenshots and the report (default: a new folder under the current directory)")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait for the repository to load and the analysis to finish (default: 900)")
    parser.add_argument("--page-timeout", type=float, default=90.0, help="Seconds each page may take (default: 90)")
    parser.add_argument("--min-nodes", type=int, default=100, help="Fail if the graph has fewer nodes than this (default: 100)")
    parser.add_argument("--browser", default="auto", choices=["auto", "msedge", "chrome", "chromium"], help="Browser to drive (default: Edge, then Chrome, then bundled Chromium)")
    parser.add_argument("--browser-path", default=os.environ.get("KUMOSQL_SMOKE_BROWSER_PATH"), help="Path to a Chromium-based browser executable (overrides --browser)")
    parser.add_argument("--forbid-dataset", default="fx_scratch", help="Fail if the graph has nodes in this dataset (the fixture's default dataset, where wrongly resolved refs land; default: fx_scratch; empty to disable)")
    parser.add_argument("--allow-ssh", action="store_true", help="Do not block SSH (default: blocked, like a work laptop)")
    parser.add_argument("--no-second-start", action="store_true", help="Skip restarting the server to time how fast the saved project reappears")
    parser.add_argument("--second-start-limit", type=float, default=20.0, help="Seconds the saved project may take to appear after a restart (default: 20)")
    parser.add_argument("--no-browser", action="store_true", help="Skip the browser pages (API checks only)")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required (or set KUMOSQL_SMOKE_REPO)")
    work = (args.work_dir or Path.cwd() / f"kumosql-smoke-{time.strftime('%Y%m%d-%H%M%S')}").resolve()
    work.mkdir(parents=True, exist_ok=True)
    home, data_dir = work / "home", work / "data"
    report = Report()
    env = dict(os.environ, KUMOSQL_HOME=str(home), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env.pop("KUMOSQL_GIT_CACHE", None)
    if not args.allow_ssh:
        env["GIT_SSH_COMMAND"] = ssh_blocked_command()
        check_ssh_blocked(env, report)
    in_appdata = "appdata" in str(data_dir).lower()
    report.step("local data folder is outside AppData", not in_appdata, str(data_dir))

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    output: list[str] = []
    process = subprocess.Popen([sys.executable, "-m", "kumosql", "ui", "--port", str(port), "--no-browser"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=lambda: output.extend(line.rstrip() for line in process.stdout), daemon=True).start()
    watchdog = Watchdog(base)

    def session() -> None:
        graph_nodes = 0
        started = time.monotonic()
        while time.monotonic() - started < 60:
            try:
                if call(base, "/api/version", timeout=5)[0] == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            report.step("server starts", False, "no answer after 60 seconds; output: " + " | ".join(output[-5:]), time.monotonic() - started)
            return
        report.step("server starts", True, f"port {port}", time.monotonic() - started)
        watchdog.start()

        started = time.monotonic()
        status, body = call(base, "/api/storage", {"folder": str(data_dir)})
        report.step("choose local data folder", status == 200, "" if status == 200 else str(body), time.monotonic() - started)

        started = time.monotonic()
        status, body = call(base, "/api/repositories", {"repositories": [{"url": args.repo, "branch": args.branch}]})
        repo_id = (body.get("active") if isinstance(body, dict) else None)
        report.step("connect repository", status == 200 and bool(repo_id), "" if status == 200 else str(body), time.monotonic() - started)
        if not repo_id:
            return

        started = time.monotonic()
        try:
            status, body = call(base, "/api/repositories/activate", {"id": repo_id}, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            status, body = 0, f"{type(exc).__name__}: {exc} (hang over {args.timeout:.0f}s?)"
        report.step("load repository", status == 200, (body.get("label", "") + f", {body.get('files')} files" if status == 200 else str(body)[:600]), time.monotonic() - started)
        if status != 200:
            return

        started = time.monotonic()
        state = {}
        while time.monotonic() - started < args.timeout:
            status, body = call(base, "/api/status", timeout=30)
            state = (body.get("analysis") or {}) if isinstance(body, dict) else {}
            if state.get("state") in ("done", "failed", "idle"):
                break
            time.sleep(2)
        report.step("analysis finishes", state.get("state") in ("done", "idle"), f"state {state.get('state')} after {state.get('elapsed')}s at stage {state.get('stage')!r}", time.monotonic() - started)

        for path in ("/api/graph", "/api/cost", "/api/changes", "/api/repositories", "/api/settings", "/api/storage", "/api/workflow-configs", "/api/status"):
            started = time.monotonic()
            try:
                status, body = call(base, path, timeout=args.timeout)
            except Exception as exc:  # noqa: BLE001
                status, body = 0, f"{type(exc).__name__}: {exc}"
            ok, detail = status == 200, ""
            if status != 200:
                detail = str(body)[:300]
            elif path == "/api/graph":
                graph_nodes = len(body.get("nodes", [])) if isinstance(body, dict) else 0
                ok = graph_nodes >= args.min_nodes and not (isinstance(body, dict) and body.get("pending"))
                gaps = body.get("gaps", []) if isinstance(body, dict) else []
                ambiguous = [g for g in gaps if "ambiguous mapping" in str(g.get("message", "")).lower()]
                phantom = [n for n in body.get("nodes", []) if args.forbid_dataset and f".{args.forbid_dataset}." in f".{n.get('id', '')}."] if isinstance(body, dict) else []
                kinds: dict = {}
                for g in gaps:
                    kinds[g.get("kind")] = kinds.get(g.get("kind"), 0) + 1
                if ambiguous or phantom:
                    ok = False
                detail = f"{graph_nodes} nodes, {len(body.get('edges', []))} edges, gaps {kinds or 'none'}" + (f"; {len(ambiguous)} 'Ambiguous mapping' gaps (first: {ambiguous[0]['message'][:120]})" if ambiguous else "") + (f"; {len(phantom)} phantom nodes in {args.forbid_dataset} (first: {phantom[0].get('id')})" if phantom else "") + ("" if ok else f" (expected at least {args.min_nodes}, or analysis still pending)")
            elif path == "/api/repositories":
                errors = [item.get("error") for item in body.get("repositories", []) if item.get("error")]
                ok, detail = not errors, "; ".join(errors)[:300]
            report.step(f"GET {path}", ok, detail, time.monotonic() - started)

        if args.no_browser:
            report.step("browser pages", True, "skipped (--no-browser)")
        else:
            run_browser(base, work, report, args.browser, args.browser_path, args.page_timeout, graph_nodes)
    try:
        session()
    except Exception as exc:  # noqa: BLE001
        report.step('smoke script ran to the end', False, f'{type(exc).__name__}: {exc}')
    if report.ok and not args.no_second_start:
        watchdog.stop_event.set()  # the restart itself makes the server unreachable for a moment
        process = second_start(report, process, env, args.min_nodes, args.second_start_limit, output)
    return finish(report, work, process, watchdog, data_dir, output)


def second_start(report: Report, first: subprocess.Popen, env: dict, min_nodes: int, limit: float, output: list[str]) -> subprocess.Popen:
    """Restart the server on the same data folder: the saved project must show up without waiting for git or the parser."""

    first.terminate()
    try:
        first.wait(10)
    except subprocess.TimeoutExpired:
        first.kill()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-m", "kumosql", "ui", "--port", str(port), "--no-browser"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=lambda: output.extend(line.rstrip() for line in process.stdout), daemon=True).start()
    nodes, detail = 0, ""
    while time.monotonic() - started < max(limit * 3, 60):
        try:
            status, body = call(base, "/api/graph", timeout=30)
            nodes = len(body.get("nodes", [])) if status == 200 and isinstance(body, dict) else 0
            if nodes >= min_nodes and not body.get("pending"):
                break
        except OSError:
            pass
        time.sleep(0.5)
    seconds = time.monotonic() - started
    report.step("second start shows the saved project quickly", nodes >= min_nodes and seconds <= limit,
                f"{nodes} nodes after {seconds:.1f}s (limit {limit:.0f}s)", seconds)
    return process


def finish(report: Report, work: Path, process: subprocess.Popen, watchdog: Watchdog, data_dir: Path, output: list[str]) -> int:
    watchdog.stop_event.set()
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
    time.sleep(0.3)
    report.step("server stayed responsive", not watchdog.failures and watchdog.slowest < SLOW_REQUEST_SECONDS,
                f"slowest trivial request {watchdog.slowest:.1f}s" + (f"; {watchdog.failures[:3]}" if watchdog.failures else ""))
    errors = collect_server_errors(data_dir, output)
    report.step("no errors in ui.log or server output", not errors, " | ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
    (work / "smoke-report.json").write_text(json.dumps({"ok": report.ok, "steps": report.steps, "server_output_tail": output[-60:]}, indent=2), encoding="utf-8")
    print(("\nSMOKE TEST PASSED" if report.ok else "\nSMOKE TEST FAILED") + f". Report and screenshots: {work}", flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
