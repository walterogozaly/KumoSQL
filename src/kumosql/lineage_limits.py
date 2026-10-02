"""Time limits for column-level lineage tracing.

A model that takes longer than its limit keeps its reads, readers and column names but is
traced at table level (every column is treated as built from everything the model reads).
The limits come from the environment (``KUMOSQL_LINEAGE_MODEL_SECONDS`` and
``KUMOSQL_LINEAGE_SECONDS``, ``0`` for none) when set, otherwise from Settings, otherwise
from the defaults here.
"""

from __future__ import annotations

import os

from . import state

DEFAULT_MODEL_SECONDS = 60.0
DEFAULT_TOTAL_SECONDS = 600.0
MAX_SECONDS = 86400.0

_ENV = {"model_seconds": "KUMOSQL_LINEAGE_MODEL_SECONDS", "total_seconds": "KUMOSQL_LINEAGE_SECONDS"}
_DEFAULTS = {"model_seconds": DEFAULT_MODEL_SECONDS, "total_seconds": DEFAULT_TOTAL_SECONDS}


def _valid(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_SECONDS


def settings() -> dict:
    """``{"model_seconds", "total_seconds"}`` as saved in Settings, defaults filled in (``0`` is no limit)."""

    saved = state.get_section("lineage_limits", {}) or {}
    if not isinstance(saved, dict):
        saved = {}
    return {name: float(saved[name]) if _valid(saved.get(name)) else default for name, default in _DEFAULTS.items()}


def overridden() -> dict:
    """``{name: bool}``: whether an environment variable is setting each limit."""

    return {name: _env_value(name) is not None for name in _ENV}


def status() -> dict:
    """The limits in force, and which of them an environment variable fixes."""

    return {
        **{name: effective(name) for name in _ENV},
        "locked": [name for name, fixed in overridden().items() if fixed],
    }


def save_settings(**values: object) -> dict:
    current = settings()
    for name, value in values.items():
        if name not in _DEFAULTS or value is None:
            continue
        if not _valid(value):
            raise ValueError(f"{name} must be a number of seconds from 0 (no limit) to {int(MAX_SECONDS)}")
        current[name] = float(value)
    state.set_section("lineage_limits", current)
    return current


def _env_value(name: str) -> float | None:
    try:
        return max(float(os.environ[_ENV[name]]), 0.0)
    except (KeyError, ValueError):
        return None


def effective(name: str) -> float:
    """The limit in force for ``name``: the environment, then Settings. ``0`` means no limit."""

    fixed = _env_value(name)
    return settings()[name] if fixed is None else fixed


def cache_tag() -> str:
    """Part of the saved-analysis key: a result computed under other limits is not reused."""

    return f"{effective('model_seconds')}|{effective('total_seconds')}"
