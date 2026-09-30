"""Keep failures local: one unreadable asset becomes a diagnostic, not a lost report.

Messages built here describe *what kind* of failure happened. They never echo
file contents, decoder byte dumps or exception text that may embed either.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

# Diagnostic codes that mean "this asset was not (fully) analyzed", as opposed
# to informational notes such as tables outside the pipeline.
ASSET_FAILURE_CODES = frozenset(
    {
        "read_error",
        "unreadable_directory",
        "settings_unreadable",
        "invalid_entry",
        "duplicate_model",
        "sqlx_parse_error",
        "unsupported_ref",
        "parse_error",
        "no_query",
        "qualify_error",
        "lineage_error",
        "unexpanded_star",
        "section_failed",
    }
)


class PipelineLoadError(ValueError):
    """The whole input is unusable (missing root, invalid graph JSON)."""


def describe_os_error(exc: BaseException) -> str:
    """A content-free description of an I/O or decoding failure."""

    if isinstance(exc, UnicodeError):
        return "file is not valid UTF-8 text"
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "file not found or a broken link"
    if isinstance(exc, IsADirectoryError):
        return "path is a directory, not a file"
    if isinstance(exc, OSError):
        return "file could not be read"
    return f"unexpected {type(exc).__name__}"


def read_text_or_reason(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, None)`` or ``(None, reason)``; never raises for I/O errors."""

    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError) as exc:
        return None, describe_os_error(exc)


def find_assets(
    root: Path, suffixes: Iterable[str], on_error: Callable[[Path, str], None]
) -> list[Path]:
    """Sorted files under ``root`` with one of ``suffixes``.

    Unlistable directories are reported through ``on_error`` and skipped.
    """

    wanted = tuple(suffixes)
    found: list[Path] = []

    def walk_error(exc: OSError) -> None:
        on_error(Path(exc.filename) if exc.filename else root, describe_os_error(exc))

    for directory, _dirs, files in os.walk(root, onerror=walk_error):
        found.extend(Path(directory) / name for name in files if name.endswith(wanted))
    return sorted(found)


def parse_json_or_raise(path: Path, what: str) -> object:
    """Parse a whole-input JSON file; failures raise one content-free error."""

    text, reason = read_text_or_reason(path)
    if text is None:
        raise PipelineLoadError(f"{what} could not be read: {reason}")
    try:
        return json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        where = f" (line {line})" if line else ""
        raise PipelineLoadError(f"{what} is not valid JSON{where}") from None


def guarded(default: T, action: Callable[[], T]) -> tuple[T, str | None]:
    """Run ``action``; on any exception return ``default`` and the exception class name."""

    try:
        return action(), None
    except Exception as exc:  # noqa: BLE001 - isolation boundary by design
        return default, type(exc).__name__


def diagnostic_entry(model: str, code: str, message: str) -> dict:
    """JSON shape for the UI: ``asset`` and ``message`` plus the stable ``model``/``code`` keys."""

    return {
        "asset": model,
        "model": model,
        "code": code,
        "message": message,
        "analysis_incomplete": code in ASSET_FAILURE_CODES,
    }


def summarize(entries: list[dict]) -> dict:
    incomplete = [e for e in entries if e.get("analysis_incomplete")]
    return {
        "count": len(entries),
        "assets_not_analyzed": len({e["asset"] for e in incomplete}),
        "analysis_incomplete": bool(incomplete),
    }
