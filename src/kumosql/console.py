"""Keep the server from freezing when someone clicks in its console window.

On Windows a click in a console starts a text selection (QuickEdit), and the
console then blocks every write to stdout or stderr until the selection is
cleared. The server used to write one request-log line per request, so a single
stray click made the browser hang until Ctrl+C or Esc. Two defences:

* :func:`disable_quick_edit` turns QuickEdit off for the console on start-up
  (no administrator rights needed).
* Request and error logging go to ``ui.log`` in the data directory
  (:func:`log`), never to the console, so nothing the server does can block on it.
"""

from __future__ import annotations

import contextlib
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import state

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


def log_path() -> Path:
    return state.data_dir() / LOG_NAME


def log(message: str) -> None:
    """Append one line to the UI log; never raises and never touches the console."""

    try:
        path = log_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


# ---------- what the console shows ----------
#
# Console output goes through a queue and one writer thread, so a console that
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
            print(line, flush=True)
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


def say(message: str, console: bool = True) -> None:
    """One status line: to ``ui.log`` always, to the console unless ``console`` is false."""

    log(message)
    if console:
        _show(f"{datetime.now():%H:%M:%S} {message}")


def error(message: str, exc: BaseException | None = None, trace: bool = True) -> None:
    """One clear line, then the traceback (for unexpected failures)."""

    say(f"ERROR {message}")
    if exc is not None and trace and exc.__traceback__ is not None:
        for line in "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip().splitlines():
            say("  " + line)


@contextlib.contextmanager
def task(name: str):
    """Log a background task's start and its end with the time taken; failures are re-raised."""

    say(f"{name}: started")
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        say(f"{name}: failed after {time.monotonic() - started:.1f}s: {exc}")
        raise
    say(f"{name}: finished in {time.monotonic() - started:.1f}s")


def banner(url: str) -> list[str]:
    """What to know when someone sends a screenshot of this window."""

    from . import git_repo, storage, version

    lines = [f"KumoSQL {version.describe()}", f"Open {url}   (Ctrl+C stops it)"]
    python = sys.executable or "unknown"
    lines.append(f"Python {sys.version.split()[0]} at {python}" + ("   <- Microsoft Store build" if git_repo.is_store_python() else ""))
    try:
        found = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        lines.append(f"git: {found.stdout.strip() or found.stderr.strip() or 'no output'}")
    except FileNotFoundError:
        lines.append("git: NOT FOUND on PATH (needed to load repositories)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"git: could not run ({exc})")
    folder = storage.saved_folder()
    lines.append(f"Local data folder: {folder if folder else 'not chosen yet (Settings > Local data folder)'}")
    lines.append(f"Settings file: {state.state_path()}")
    lines.append(f"Log file: {log_path()}" + ("" if VERBOSE else "   (start with --verbose to show every request here)"))
    return lines
