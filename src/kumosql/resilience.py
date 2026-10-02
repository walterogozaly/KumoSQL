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
        "lineage_skipped",
        "unexpanded_star",
        "section_failed",
        "cycle",
        "unknown_reads",
        "unresolved_template",
        "skipped_statements",
        "unparsed_operation",
        "ambiguous_reference",
    }
)

# Views that a gap makes unreliable. A result in one of these views is only
# complete when no blocking gap lists it.
VIEWS = ("graph", "lineage", "impact", "dead_columns")

_EFFECTS = {
    "cycle": ("graph", "impact"),
    "external_tables": ("lineage",),
    "unresolved_template": ("graph", "lineage", "impact", "dead_columns"),
    "lineage_skipped": ("lineage", "impact", "dead_columns"),
}

# Kinds shared with the graph page's gaps table; other codes keep their own name.
_GAP_KINDS = {
    "read_error": "inaccessible",
    "unreadable_directory": "inaccessible",
    "settings_unreadable": "inaccessible",
    "invalid_entry": "inaccessible",
    "parse_error": "parse_error",
    "no_query": "parse_error",
    "sqlx_parse_error": "parse_error",
    "unsupported_ref": "parse_error",
    "qualify_error": "parse_error",
    "lineage_error": "parse_error",
    "lineage_skipped": "lineage_skipped",
    "ambiguous_reference": "unmatched_reference",
    "external_tables": "unmatched_reference",
    "unknown_reads": "unattributed_reads",
    "unresolved_template": "unmatched_reference",
}


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


def _extended_text(text: str) -> str:
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def extended_path(path: str | Path) -> Path:
    """``path`` made absolute, with Windows' extended-length prefix so paths over 260 characters work.

    No administrator setting is needed. Elsewhere the path is returned unchanged. Applied to a folder
    root, every path derived from it (``/``, ``rglob``, ``os.walk``) keeps the prefix.
    """

    path = Path(path)
    if os.name != "nt":
        return path
    return Path(_extended_text(os.path.abspath(path)))


def read_text_or_reason(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, None)`` or ``(None, reason)``; never raises for I/O errors."""

    try:
        return path.read_text(encoding="utf-8-sig"), None  # a byte-order mark would hide a leading config block
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
        "severity": "error" if code in ASSET_FAILURE_CODES else "info",
        "effect": list(_EFFECTS.get(code, VIEWS)),
    }


def summarize(entries: list[dict]) -> dict:
    incomplete = [e for e in entries if e.get("analysis_incomplete")]
    return {
        "count": len(entries),
        "assets_not_analyzed": len({e["asset"] for e in incomplete}),
        "analysis_incomplete": bool(incomplete),
    }


def build_completeness(entries: list[dict], extra_gaps: Iterable[dict] = ()) -> dict:
    """Summarize what the analysis could not see, and which views that affects.

    ``entries`` are :func:`diagnostic_entry` dicts. ``extra_gaps`` are gaps from
    other sources (job history); each needs ``asset``, ``kind``, ``message``.
    ``complete`` is false when any blocking gap exists. ``views`` says, per view,
    whether its results can be treated as complete. Informational gaps (tables
    outside the pipeline) are listed but do not block.
    """

    gaps: list[dict] = []
    for entry in entries:
        blocking = bool(entry.get("analysis_incomplete"))
        if not blocking and entry["code"] != "external_tables":
            continue
        gaps.append(
            {
                "asset": entry["asset"],
                "kind": _GAP_KINDS.get(entry["code"], entry["code"]),
                "code": entry["code"],
                "message": entry["message"],
                "blocking": blocking,
                "effect": entry.get("effect", list(VIEWS)),
                "origin": "static",
            }
        )
    for gap in extra_gaps:
        gaps.append({"code": gap["kind"], "blocking": True, "effect": list(VIEWS), "origin": "observed", **gap})
    blocking_gaps = [g for g in gaps if g["blocking"]]
    by_code: dict[str, int] = {}
    for gap in gaps:
        by_code[gap["code"]] = by_code.get(gap["code"], 0) + 1
    return {
        "complete": not blocking_gaps,
        "assets_not_analyzed": len({g["asset"] for g in blocking_gaps if g["origin"] == "static"}),
        "views": {view: not any(view in g["effect"] for g in blocking_gaps) for view in VIEWS},
        "by_code": dict(sorted(by_code.items())),
        "gaps": gaps,
    }
