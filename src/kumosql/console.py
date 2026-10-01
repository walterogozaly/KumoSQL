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

import sys
import threading
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
