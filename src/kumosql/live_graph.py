"""The project loaded into the UI server and the payload ``/api/graph`` serves.

The query graph page reads one JSON shape (see ``docs/ui-roadmap.md``). This
module builds it from a real :class:`~kumosql.pipeline.Pipeline` and keeps the
currently loaded project in memory. With nothing loaded, ``ui.py`` falls back
to the sample data in ``preview_data``, which is labeled as sample.

Every payload carries the pipeline's completeness block (issue #28): ``gaps``
lists what could not be analyzed and ``coverage.complete`` is false while any
blocking gap exists, so a partial graph is never presented as complete.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import tempfile
import threading

from .pipeline import Pipeline, load_sqlx_project

MAX_FILES = 500
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_CONFIG_FILES = ("workflow_settings.yaml", "workflow_settings.yml", "dataform.json")
_ALLOWED_SUFFIXES = (".sqlx", ".sql")

_LOCK = threading.Lock()
_LOADED: dict | None = None


class ProjectError(ValueError):
    """The supplied project files could not be loaded."""


def set_project(pipeline: Pipeline, label: str) -> None:
    """Make ``pipeline`` the project the graph page shows."""

    global _LOADED
    with _LOCK:
        _LOADED = {"pipeline": pipeline, "label": label}


def clear_project() -> None:
    global _LOADED
    with _LOCK:
        _LOADED = None


def loaded() -> dict | None:
    with _LOCK:
        return _LOADED


def _safe_path(path: object) -> PurePosixPath:
    if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path or "\0" in path:
        raise ProjectError("invalid file path")
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise ProjectError("invalid file path")
    name = posix.name.lower()
    if not (name.endswith(_ALLOWED_SUFFIXES) or name in _CONFIG_FILES):
        raise ProjectError("only .sqlx, .sql and Dataform config files are accepted")
    return posix


def load_files(files: object, label: str) -> Pipeline:
    """Load a project from ``{relative path: text}`` and make it current."""

    if not isinstance(files, dict) or not files:
        raise ProjectError("files must be a non-empty object of path to text")
    if len(files) > MAX_FILES:
        raise ProjectError(f"too many files (limit {MAX_FILES})")
    if not isinstance(label, str) or not label.strip():
        label = "uploaded project"
    total = 0
    with tempfile.TemporaryDirectory(prefix="kumosql-project-") as directory:
        for path, text in files.items():
            relative = _safe_path(path)
            if not isinstance(text, str):
                raise ProjectError("file contents must be text")
            total += len(text.encode("utf-8"))
            if total > MAX_TOTAL_BYTES:
                raise ProjectError("project is too large")
            target = Path(directory, *relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        try:
            pipeline = load_sqlx_project(directory)
            pipeline.completeness()  # analyse now: the folder is deleted on exit
        except Exception as exc:  # loader errors are user-facing
            raise ProjectError(str(exc) or "project could not be loaded") from exc
    set_project(pipeline, label.strip()[:200])
    return pipeline


def _edge_source(edge: dict) -> str:
    if edge["observed"] and (edge["declared"] or edge["parsed"]):
        return "both"
    if edge["declared"]:
        return "declared"
    return "observed" if edge["observed"] else "parsed"


def _split(key: str) -> tuple[str, str]:
    parts = key.split(".")
    return (".".join(parts[-3:-1]) if len(parts) > 1 else ""), parts[-1]


def graph_payload(pipeline: Pipeline, label: str = "") -> dict:
    """The ``/api/graph`` payload for a real pipeline. No ``preview`` flag."""

    report = pipeline.report()
    graph = report.get("graph") or {"nodes": [], "edges": []}
    completeness = report["completeness"]
    lineage = pipeline.lineage_report()

    columns: dict[str, list[str]] = {}

    def add_column(node: str, column: str) -> None:
        seen = columns.setdefault(node, [])
        if column not in seen:
            seen.append(column)

    for key in pipeline.models:
        for column in pipeline.output_columns(key):
            add_column(key, column)
    for row in lineage:
        add_column(row["node"], row["column"])
        for source in row["sources"]:
            add_column(source["node"], source["column"])

    gap_assets = {gap["asset"] for gap in completeness["gaps"] if gap["blocking"]}
    nodes = []
    for item in graph["nodes"]:
        key = item["identity"]["key"]
        dataset, name = _split(key)
        model = pipeline.models.get(key)
        if model is not None:
            kind = "view" if model.kind == "view" else "model"
        elif item["resolved"]:
            kind = "observed"
        else:
            kind = "source"
        entry = {
            "id": key, "dataset": dataset, "name": name, "kind": kind,
            "source": "declared" if model is not None else "parsed",
            "columns": columns.get(key, []),
        }
        if key in gap_assets:
            entry["note"] = "Could not be fully analyzed; its reads are unknown."
        nodes.append(entry)
    known = {node["id"] for node in nodes}
    nodes.extend(
        {"id": key, "dataset": _split(key)[0], "name": _split(key)[1], "kind": "unmatched",
         "source": "declared", "columns": [], "note": "Could not be analyzed."}
        for key in sorted(gap_assets - known) if key in pipeline.models
    )

    edges = [
        {
            "from": edge["upstream"]["key"], "to": edge["downstream"]["key"],
            "source": _edge_source(edge),
            "confidence": edge["confidence"], "last_seen": edge["last_seen"],
            "observed_count": edge["observed_count"],
        }
        for edge in graph["edges"]
    ]
    coverage = dict(report["coverage"] or {})
    coverage.setdefault("complete", completeness["complete"])
    return {
        "source": {"kind": "project", "label": label},
        "window": coverage.get("window") or {"start": None, "end": None},
        "coverage": coverage,
        "nodes": nodes,
        "edges": edges,
        "column_lineage": lineage,
        "gaps": [
            {"asset": gap["asset"], "kind": gap["kind"], "message": gap["message"],
             "blocking": gap["blocking"]}
            for gap in completeness["gaps"]
        ],
        "completeness": {
            "complete": completeness["complete"],
            "views": completeness["views"],
            "assets_not_analyzed": completeness["assets_not_analyzed"],
        },
    }


def graph_or_preview(preview) -> dict:
    """Real graph when a project is loaded, else the labeled sample data."""

    current = loaded()
    if current is None:
        payload = preview()
        payload["source"] = {"kind": "sample", "label": "Sample data"}
        return payload
    return graph_payload(current["pipeline"], current["label"])
