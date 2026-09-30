"""Persistent local state shared by the UI, the CLI and the Python API.

State is one JSON file in the platform's per-user data directory (or under
``$KUMOSQL_HOME`` when set), so UI preferences, saved scopes and formatting
preferences survive restarts and are shared by every entry point.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading

STATE_FILENAME = "state.json"
_LOCK = threading.Lock()


def data_dir() -> Path:
    """The directory KumoSQL keeps its local state in."""

    override = os.environ.get("KUMOSQL_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "kumosql"


def state_path() -> Path:
    return data_dir() / STATE_FILENAME


def load_state() -> dict:
    """Read all saved state; a missing or unreadable file yields empty state."""

    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def get_section(name: str, default=None):
    """Return one top-level section of the saved state."""

    with _LOCK:
        return load_state().get(name, default)


def set_section(name: str, value) -> None:
    """Replace one top-level section, leaving the others untouched."""

    with _LOCK:
        state = load_state()
        state[name] = value
        _write(state)
