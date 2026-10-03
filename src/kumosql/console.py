"""Keep the server from freezing when someone clicks in its console window.

On Windows a click in a console starts a text selection (QuickEdit), and the
console then blocks every write to stdout or stderr until the selection is
cleared. The server used to write one request-log line per request, so a single
stray click made the browser hang until Ctrl+C or Esc. Two defences:

* :func:`disable_quick_edit` turns QuickEdit off for the console on start-up
  (no administrator rights needed).
* Request and error logging go to ``ui.log`` in the data directory
  (:func:`log`), never to the console, so nothing the server does can block on it.

This is also the one logging module every component uses (start-up, background
stages, git, BigQuery, Dataform, analysis, errors). Everything it writes, to the
console and to ``ui.log``, passes through :mod:`kumosql.redact`, so a paste does
not reveal repository, project, dataset, table, model or file names.

The functions to call:

* ``say(message)`` / ``warn(message)``: one status line.
* ``error(message, exc=None, code=None, hint=None)``: one ``ERROR [code]`` line, a
  ``hint:`` line, and the redacted traceback in ``ui.log`` only.
* ``with task("repo load", repo=url) as t:``: start and end lines with the time
  taken, nested names (``repo load > write cached clone``), ``t.note(files=12)``,
  ``t.progress(done, total)``, a "still running" line every few seconds and a
  warning once ``warn_after`` seconds pass. Keyword names that are a kind of
  private name (``repo``, ``branch``, ``project``, ``dataset``, ``table``, ``model``,
  ``file``) are registered for redaction.
* ``ref(kind, value)`` gives the placeholder for a name (``repo#1``) and
  ``register(kind, values)`` tells the redactor about names before they are logged.
* ``diagnostics()`` is the text behind Settings > Diagnostics > Copy diagnostics.
"""

from __future__ import annotations

import contextlib
import os
import platform
import json
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import redact, state

LOG_NAME = "ui.log"
MAX_LOG_BYTES = 1_000_000
_LOCK = threading.Lock()

_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_STD_INPUT_HANDLE = -10


def disable_quick_edit() -> bool:
    """Clear QuickEdit on the console; True when changed. Never raises, no-op off Windows."""

    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a console (redirected or run by a service)
        if not mode.value & _ENABLE_QUICK_EDIT_MODE:
            return False
        new_mode = (mode.value & ~_ENABLE_QUICK_EDIT_MODE) | _ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


def redactor() -> redact.Redactor:
    return redact.GLOBAL


def set_redaction(enabled: bool) -> None:
    """On by default; ``--no-redact`` turns it off for local debugging."""

    redact.GLOBAL.enabled = bool(enabled)


def ref(kind: str, value: object) -> str:
    """The placeholder for a private name (``repo#1``); the name itself when redaction is off."""

    return redact.GLOBAL.ref(kind, value)


def register(kind: str, values: object) -> None:
    """Tell the redactor about private names (one value or many) before they might be logged."""

    redact.GLOBAL.register(kind, values)


def scrub(text: object) -> str:
    _prepare(log_path())
    return redact.GLOBAL.scrub(text)


def log_path() -> Path:
    return state.data_dir() / LOG_NAME


_PREPARED: list = [None]


def _prepare(path: Path) -> None:
    folder = str(path.parent)
    if _PREPARED[0] != folder:
        _PREPARED[0] = folder
        redact.GLOBAL.set_data_folder(folder)


def log(message: str, level: str = "INFO", *, summarized: bool = False) -> None:
    """Append a producer summary or withheld marker; never raises or touches the console."""

    try:
        path = log_path()
        _prepare(path)
        # Unknown free-form payloads may be data with no recognizable syntax.
        # Only producer summaries belong in a shareable log.
        text = redact.GLOBAL.scrub(message) if summarized else "<unsummarized log entry withheld>"
        text = text.replace("\r", " ").replace("\n", " | ")
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"{datetime.now().isoformat(timespec='seconds')} {redact.GLOBAL.session} {level:<5} {'[summary] ' if summarized else ''}{text}\n")
    except Exception:
        pass


# ---------- what the console shows ----------
#
# Console output goes to stderr (so ``--json`` style output on stdout stays clean) through a queue and one writer thread, so a console that
# is paused or slow can never block a request, a load or a git call. Everything
# shown is also written to ``ui.log``.

VERBOSE = False
_OUT: queue.Queue = queue.Queue(maxsize=2000)
_WRITER: threading.Thread | None = None
_WRITER_LOCK = threading.Lock()


def set_verbose(value: bool) -> None:
    global VERBOSE
    VERBOSE = bool(value)


def _write_forever() -> None:
    while True:
        line = _OUT.get()
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            try:  # a console that cannot show a character (cp1252) must still show the line
                print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
            except Exception:
                pass


def _show(line: str) -> None:
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is None:
            _WRITER = threading.Thread(target=_write_forever, name="kumosql-console", daemon=True)
            _WRITER.start()
    try:
        _OUT.put_nowait(line)
    except queue.Full:
        pass


def say(message: str, console: bool = True, level: str = "INFO") -> None:
    """One status line: to ``ui.log`` always, to the console unless ``console`` is false."""

    log(message, level, summarized=True)
    if console:
        shown = scrub(message)
        _show(f"{datetime.now():%H:%M:%S} {shown}" if level == "INFO" else f"{datetime.now():%H:%M:%S} {level} {shown}")


def warn(message: str, console: bool = True) -> None:
    say(message, console, level="WARN")


# ---------- error codes and hints ----------

_DIAGNOSE = "python -m kumosql.ui --diagnose-repo <repo-url>"
HINTS = {
    "KS-GIT-AUTH": "Sign in to git on this computer (Git Credential Manager, `gh auth login` or an SSH key), then check with: " + _DIAGNOSE,
    "KS-GIT-NET": "The remote could not be reached; SSH is often blocked on work networks, so use the https URL. Check with: " + _DIAGNOSE,
    "KS-GIT-TIMEOUT": "git took too long; a large repository or slow network. Check with: " + _DIAGNOSE,
    "KS-GIT-MISSING": "Install git and make sure `git --version` works in this window, then start again with: python -m kumosql.ui",
    "KS-GIT": "git failed; run " + _DIAGNOSE + " to see every git call (the output is redacted when pasted from Settings > Diagnostics).",
    "KS-IO-PERM": "A file could not be written. Choose a Local data folder in your home folder (Settings > Local data folder), not under AppData or a synced folder.",
    "KS-IO-DISK": "The disk is full or over its quota; free space or choose another Local data folder in Settings.",
    "KS-IO": "A file could not be read or written; choose a Local data folder you control in Settings > Local data folder.",
    "KS-BQ-ACCESS": "BigQuery refused the request; check your sign-in with `gcloud auth application-default login` and the projects chosen in Settings > BigQuery projects.",
    "KS-TIMEOUT": "A step took too long and was stopped; try again, and use Settings > Diagnostics > Copy diagnostics if it repeats.",
    "KS-GRAPH-BUILD": "This is a KumoSQL bug, not a problem with the repository; use Settings > Diagnostics > Copy diagnostics and share the result.",
    "KS-DATAFORM": "Schedules come from Dataform in the projects chosen under Settings > BigQuery projects; check that you can see the repository in Google Cloud and that its git remote matches. The graph itself is not affected.",
    "KS-ANALYSIS": "A background analysis failed; the Cost and Change pages compute what they need themselves. If they also fail, use Settings > Diagnostics > Copy diagnostics.",
    "KS-INTERNAL": "Unexpected failure; use Settings > Diagnostics > Copy diagnostics and share the result.",
}


def classify(exc: BaseException | None, message: str = "") -> str:
    """A short error code for an exception (and its message)."""

    text = f"{message} {exc or ''}".lower()
    if isinstance(exc, subprocess.TimeoutExpired) or isinstance(exc, TimeoutError):
        return "KS-TIMEOUT"
    if isinstance(exc, PermissionError):
        return "KS-IO-PERM"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (28, 122):  # ENOSPC, EDQUOT
        return "KS-IO-DISK"
    if "git was not found" in text or (isinstance(exc, FileNotFoundError) and getattr(exc, "filename", None) == "git"):
        return "KS-GIT-MISSING"
    if "timed out" in text and "git" in text:
        return "KS-GIT-TIMEOUT"
    try:
        from . import git_repo

        if any(marker in text for marker in git_repo._AUTH_FAILURES):
            return "KS-GIT-AUTH"
        if any(marker in text for marker in git_repo._NETWORK_FAILURES):
            return "KS-GIT-NET"
        if isinstance(exc, git_repo.GitRepoError):
            return "KS-GIT"
    except Exception:  # noqa: BLE001
        pass
    if "403" in text or "access denied" in text or "permission denied" in text and "bigquery" in text:
        return "KS-BQ-ACCESS"
    if isinstance(exc, OSError):
        return "KS-IO"
    return "KS-INTERNAL"


def format_traceback(exc: BaseException) -> list[str]:
    """Frames kept (package files by name and line, others by file name only), messages left to redaction."""

    package = str(Path(__file__).resolve().parent)
    lines = []
    chain = []
    current = exc
    while current is not None and len(chain) < 4:
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    for item in reversed(chain):
        for frame in traceback.extract_tb(item.__traceback__):
            name = frame.filename
            shown = f"kumosql/{Path(name).name}" if name.startswith(package) else Path(name).name
            lines.append(f"  at {shown}:{frame.lineno} in {frame.name}")
        position = re.search(r"Line \d+, Col: \d+", str(item))
        lines.append(f"{type(item).__name__}: [{classify(item)}]" + (f" {position.group(0)}" if position else ""))
        if item is not chain[0]:
            lines.append("  (was the cause of)" if item.__cause__ is not None else "  (during handling of the above)")
    return lines


def error(message: str, exc: BaseException | None = None, trace: bool = True, code: str | None = None, hint: str | None = None) -> None:
    """One clear ``ERROR [code]`` line, a ``hint:`` line, and (in ``ui.log`` only) the traceback."""

    code = code or classify(exc, message)
    detail = f" ({type(exc).__name__})" if exc is not None else ""
    say(f"ERROR [{code}] {message}{detail}", level="INFO")
    say(f"  hint: {hint or HINTS.get(code) or HINTS['KS-INTERNAL']}")
    if exc is not None and trace and exc.__traceback__ is not None:
        for line in format_traceback(exc):
            log("  " + line, "TRACE", summarized=True)
        _show(f"{datetime.now():%H:%M:%S}   (traceback with file names and line numbers is in ui.log)")


# ---------- stages ----------

_LOCAL = threading.local()
_ACTIVE: dict[int, "Task"] = {}
_ACTIVE_LOCK = threading.Lock()
_MONITOR: threading.Thread | None = None
HEARTBEAT_SECONDS = 5.0


def _stack() -> list[str]:
    if not hasattr(_LOCAL, "stack"):
        _LOCAL.stack = []
    return _LOCAL.stack


def _monitor_forever() -> None:
    while True:
        time.sleep(0.5)
        with _ACTIVE_LOCK:
            tasks = list(_ACTIVE.values())
        for item in tasks:
            try:
                item._beat()
            except Exception:  # noqa: BLE001
                pass


class Task:
    """A running stage: ``note`` adds counts to its end line, ``progress`` shows how far it got."""

    def __init__(self, path: str, details: dict, quiet: bool, warn_after: float | None, heartbeat_after: float) -> None:
        self.path = path
        self.details = dict(details)
        self.quiet = quiet
        self.warn_after = warn_after
        self.started = time.monotonic()
        self._next_beat = max(heartbeat_after, 0.0)
        self._progress: tuple[int, int | None] | None = None
        self._warned = False

    def note(self, **counts: object) -> None:
        self.details.update(counts)

    def progress(self, done: int, total: int | None = None) -> None:
        self._progress = (done, total)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _summary(self) -> str:
        parts = []
        for key, value in self.details.items():
            if key in redact.KINDS:
                shown = ref(key, value)
            elif key in ("files", "models", "tables", "columns", "items", "done", "total", "exit", "megabytes", "rows", "count") and isinstance(value, (int, float, bool)):
                shown = value
            elif key == "mode" and value in ("fetch latest", "use cached copy", "cached copy if any"):
                shown = value
            else:
                shown = "<value withheld>"
            shown_key = key if key in redact.KINDS or key in ("files", "models", "tables", "columns", "items", "done", "total", "exit", "megabytes", "rows", "count", "mode") else "detail"
            parts.append(f"{shown_key} {shown}")
        return ", ".join(parts)

    def _beat(self) -> None:
        elapsed = self.elapsed()
        if elapsed < self._next_beat:
            return
        self._next_beat = elapsed + HEARTBEAT_SECONDS
        where = ""
        if self._progress:
            done, total = self._progress
            where = f", {done}/{total}" if total else f", {done} so far"
        slow = self.warn_after is not None and elapsed >= self.warn_after
        text = f"{self.path}: still running after {elapsed:.0f}s{where}"
        if slow and not self._warned:
            self._warned = True
            text += " (slower than expected: a large repository, a slow network or waiting for credentials)"
        say(text, level="WARN" if slow else "INFO")


@contextlib.contextmanager
def task(name: str, quiet: bool = False, warn_after: float | None = None, heartbeat_after: float = HEARTBEAT_SECONDS, **details: object):
    """Log a stage's start and end with the time taken; failures are logged once and re-raised.

    Names nest (``repo load > write cached clone``). ``quiet`` keeps a fast, successful stage out of the
    console. Keyword arguments named like a private-name kind are registered for redaction.
    """

    for key, value in details.items():
        if key in redact.KINDS:
            register(key, value)
    stack = _stack()
    stack.append(name)
    current = Task(" > ".join(stack), details, quiet, warn_after, heartbeat_after)
    with _ACTIVE_LOCK:
        _ACTIVE[id(current)] = current
        global _MONITOR
        if _MONITOR is None:
            _MONITOR = threading.Thread(target=_monitor_forever, name="kumosql-heartbeat", daemon=True)
            _MONITOR.start()
    context = current._summary()
    say(f"{current.path}: started" + (f" ({context})" if context else ""), console=not quiet)
    try:
        yield current
    except BaseException as exc:
        seconds = current.elapsed()
        if not getattr(exc, "_kumosql_logged", False):
            try:
                exc._kumosql_logged = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            context = current._summary()
            kind = type(exc).__name__
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                say(f"{current.path}: stopped after {seconds:.1f}s ({kind})")
            else:
                error(f"{current.path}: failed ({kind}) after {seconds:.1f}s" + (f" [{context}]" if context else ""),
                      exc)
        else:
            say(f"{current.path}: failed after {seconds:.1f}s (details above)")
        raise
    else:
        context = current._summary()
        say(f"{current.path}: finished in {current.elapsed():.1f}s" + (f" ({context})" if context else ""),
            console=not quiet or current.elapsed() >= HEARTBEAT_SECONDS)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(id(current), None)
        if stack:
            stack.pop()


# ---------- what to know when someone pastes this window ----------

def python_kind() -> str:
    """Which kind of Python this is: matters because the Store build hides AppData from git."""

    executable = (sys.executable or "").lower()
    if sys.platform == "win32" and "windowsapps" in executable:
        return "Microsoft Store"
    if os.environ.get("CONDA_PREFIX") or "conda" in executable or "miniforge" in executable:
        return "conda"
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "virtualenv"
    return "system"


def _git_version() -> str:
    try:
        found = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return found.stdout.strip() or found.stderr.strip() or "no output"
    except FileNotFoundError:
        return "NOT FOUND on PATH (needed to load repositories)"
    except Exception as exc:  # noqa: BLE001
        return f"could not run ({type(exc).__name__})"


def _package_version(name: str) -> str:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def banner(url: str) -> list[str]:
    """What to know when someone sends a screenshot of this window (names redacted when shown)."""

    from . import storage, version

    redact.GLOBAL.set_data_folder(state.data_dir())
    lines = [f"KumoSQL {version.describe()}  (session {redact.GLOBAL.session}, names {'redacted' if redact.GLOBAL.enabled else 'SHOWN: started with --no-redact'})",
             f"Open {url}   (Ctrl+C stops it)"]
    lines.append(f"Python {sys.version.split()[0]} ({python_kind()} build, {platform.system()} {platform.release()})"
                 + ("   <- hides AppData from git" if python_kind() == "Microsoft Store" else ""))
    lines.append(f"git: {_git_version()}")
    folder = storage.saved_folder()
    lines.append(f"Local data folder: {'<data> (chosen)' if folder else 'default location (Settings > Local data folder)'}")
    lines.append(f"Settings file: <data>/{state.STATE_FILENAME}")
    lines.append(f"Log file: <data>/{LOG_NAME}" + ("" if VERBOSE else "   (start with --verbose to show every request here)"))
    if redact.GLOBAL.enabled:
        lines.append(f"Private name map: <data>/{redact.MAP_NAME} (stays on this computer; look up a placeholder with: python -m kumosql.ui --lookup repo#1)")
    return lines


def announce(url: str) -> None:
    """Write the start-up banner to the console and the log."""

    for line in banner(url):
        say(line)


_SAFE_STRING_KEYS = frozenset(("theme", "mode", "view", "op", "operator", "type", "kind", "state", "status", "combine"))
_SAFE_SETTING_KEYS = frozenset((
    "ui", "theme", "sidebar", "repositories", "items", "active", "id", "url", "branch", "query", "sql", "scopes",
    "name", "mode", "view", "op", "operator", "type", "kind", "state", "status", "combine", "files", "models",
    "tables", "columns", "loading", "last_loaded", "error", "error_at", "stale_reason", "content_key", "actual_branch",
    "projects", "project", "location", "storage", "folder", "vars", "variables", "rows", "results", "parameters", "bindings",
))
_SAFE_ENUMS = frozenset(("dark", "light", "system", "loading", "ready", "failed", "idle", "running", "done", "and", "or"))
_HIDDEN_KEYS = re.compile(r"(?i)token|secret|password|passwd|credential|api[_-]?key|private[_-]?key|authorization|^(?:vars|variables|rows|results|parameters|bindings|values)$")


def _safe_settings(value: object, key: str = "") -> object:
    """Keep known structure, fixed enums and counts; withhold arbitrary keys and values."""

    if _HIDDEN_KEYS.search(key):
        return "<hidden>"
    if isinstance(value, dict):
        return {k if k in _SAFE_SETTING_KEYS else f"<field {index}>": _safe_settings(v, str(k))
                for index, (k, v) in enumerate(value.items(), 1)}
    if isinstance(value, (list, tuple)):
        return [_safe_settings(item, key) for item in value]
    if isinstance(value, str):
        if key in ("query", "sql"):
            return f"<SQL withheld, {len(value)} characters>"
        if key in _SAFE_STRING_KEYS and value in _SAFE_ENUMS:
            return value
        if not value:
            return value
        return "<value withheld>"  # settings may contain SQL literals; never put these into the reverse map
    if value is None or isinstance(value, bool):
        return value
    return value if key in ("files", "models", "tables", "columns") else "<value withheld>"


def diagnostics(max_log_lines: int = 300) -> str:
    """A redacted bundle to paste: versions, OS, Python type, settings and the recent log.

    The text is scrubbed whether or not ``--no-redact`` is on, and never contains the private name map.
    """

    from . import live_graph, storage, version

    redact.GLOBAL.set_data_folder(state.data_dir())
    out = [f"KumoSQL diagnostics (names redacted), session {redact.GLOBAL.session}, {datetime.now().isoformat(timespec='seconds')}",
           "",
           "== Versions ==",
           f"KumoSQL: {version.describe()}",
           f"Python: {sys.version.split()[0]} ({python_kind()} build, {platform.machine()})",
           f"OS: {platform.system()} {platform.release()} ({platform.version()})",
           f"git: {_git_version()}"]
    for package in ("sqlglot", "z3-solver", "sqlfluff", "google-cloud-bigquery"):
        out.append(f"{package}: {_package_version(package)}")
    folder = storage.saved_folder()
    out += ["", "== Storage ==", f"Local data folder: {'chosen' if folder else 'default location'}"]
    try:
        probe = state.data_dir()
        out.append(f"Data folder writable: {os.access(probe, os.W_OK) if probe.exists() else 'does not exist yet'}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"Data folder check failed ({type(exc).__name__})")
    out += ["", "== Settings (names replaced, secrets hidden) =="]
    try:
        out.append(json.dumps(_safe_settings(state.load_state()), indent=1, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        out.append(f"could not read settings ({type(exc).__name__})")
    out += ["", "== Server status =="]
    try:
        out.append(json.dumps(_safe_settings(live_graph.server_status()), indent=1, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        out.append(f"unavailable ({type(exc).__name__})")
    out += ["", f"== Recent log (last {max_log_lines} lines) =="]
    if not redact.GLOBAL.enabled:
        out.append("not included: this session was started with --no-redact, so its log holds real names. "
                   "Start without --no-redact to include the log.")
    else:
        try:
            lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-max_log_lines:]
            # Legacy and free-form log payloads have no safe producer summary. Do not export them.
            safe = [line for line in lines if re.match(r"^\d{4}-\d\d-\d\dT\S+ [0-9a-f]+ [A-Z]+\s+\[summary\] ", line)]
            out.extend(redact.GLOBAL.scrub(line, force=True) for line in safe)
            if len(safe) != len(lines):
                out.append(f"{len(lines) - len(safe)} unsummarized log entries omitted")
        except OSError:
            out.append("no log yet")
    # Each section above already has a safe shape. Scrub lines independently so
    # the safe settings JSON is not mistaken for an arbitrary multi-line dump.
    return "\n".join(redact.GLOBAL.scrub(line, force=True) for line in "\n".join(out).splitlines())
