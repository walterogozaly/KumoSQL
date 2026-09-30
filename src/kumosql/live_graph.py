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
from typing import Iterable
import os
import shutil
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


def set_project(pipeline: Pipeline, label: str, observed_reads: Iterable[object] = ()) -> None:
    """Make ``pipeline`` the project the graph page shows.

    ``observed_reads`` are job-history records; when given they feed both the
    graph and the impact view.
    """

    global _LOADED
    with _LOCK:
        _LOADED = {"pipeline": pipeline, "label": label, "observed_reads": tuple(observed_reads)}


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
    created = tempfile.mkdtemp(prefix="kumosql-project-")
    directory = created
    if os.name == "nt":
        # Extended-length prefix: repository paths can exceed Windows' 260-character limit.
        directory = "\\\\?\\" + str(Path(created).resolve())
    try:
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
    finally:
        shutil.rmtree(directory, ignore_errors=True)
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


def _plan(scope_name: str | None, observed_reads: Iterable[object]):
    """The saved scope named ``scope_name`` and where it applies; ``ValueError`` for an unknown name."""

    from . import scopes as scope_store

    if not scope_name:
        return None
    chosen = scope_store.get_scope(scope_name)
    if chosen is None:
        raise ValueError(f"no saved scope named {scope_name!r}")
    return scope_store.plan_scope(chosen, observed_reads)


def graph_payload(
    pipeline: Pipeline, label: str = "", observed_reads: Iterable[object] = (), scope_name: str | None = None
) -> dict:
    """The ``/api/graph`` payload for a real pipeline. No ``preview`` flag.

    With ``scope_name`` (a saved scope) the graph is limited to the models it
    matches and the job history it matches; ``scope`` in the payload says which.
    """

    observed_reads = list(observed_reads)
    plan = _plan(scope_name, observed_reads)
    report = pipeline.report(
        observed_reads=observed_reads,
        scope=plan.models if plan else None,
        observed_scope=plan.jobs if plan else None,
    )
    graph = report.get("graph") or {"nodes": [], "edges": []}
    completeness = report["completeness"]
    lineage = pipeline.lineage_report()
    if plan and plan.models is not None:
        keep = pipeline.scope_keys(plan.models)
        lineage = [row for row in lineage if row["node"] in keep]

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
        "scope": plan.to_json() if plan else None,
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


def sample_scope_note(scope_name: str | None) -> dict | None:
    """What a page with sample data says about the chosen scope: it does not apply."""

    if not scope_name:
        return None
    return {"name": scope_name, "rule": None, "applied_to": [],
            "note": "Sample data is not filtered by scopes. Load a project to apply this scope."}


def graph_or_preview(preview, scope_name: str | None = None) -> dict:
    """Real graph when a project is loaded, else the labeled sample data."""

    current = loaded()
    if current is None:
        payload = preview()
        payload["source"] = {"kind": "sample", "label": "Sample data"}
        payload["scope"] = sample_scope_note(scope_name)
        return payload
    return graph_payload(current["pipeline"], current["label"], current.get("observed_reads", ()), scope_name)


_PAGE_CHANGES = {"drop": "drop_column", "rename": "rename_column", "expression": "change_expression"}


def impact_payload(preview_impact, node: str, column: str, change: str, scope_name: str | None = None) -> dict:
    """The ``/api/impact`` payload: the server's blast radius of one column change.

    ``change`` is ``drop``, ``rename`` or ``expression``. A loaded project is
    assessed by ``Pipeline.assess_change`` together with its job history; with
    nothing loaded, ``preview_impact`` answers from the labeled sample data.
    """

    kind = _PAGE_CHANGES.get(change)
    if kind is None:
        raise ValueError("change must be drop, rename or expression")
    current = loaded()
    if current is None:
        return preview_impact(node, column, kind)
    reads = list(current.get("observed_reads", ()))
    plan = _plan(scope_name, reads)
    if plan and plan.jobs is not None:
        from .scopes import job_record

        reads = [row for row in reads if plan.jobs.matches(job_record(row))]
    result = current["pipeline"].assess_change(
        kind, node, column, scope=plan.models if plan else None, observed_reads=reads
    )
    return {**result.to_json(), "source": {"kind": "project", "label": current["label"]},
            "scope_plan": plan.to_json() if plan else None}


def overlaps_payload(preview_overlaps, node: str, scope: str | None = None) -> dict:
    """The ``/api/overlaps`` payload: tables that already provide the same attributes as ``node``.

    A loaded project is compared with ``OverlapChecker`` (optionally limited to
    the saved scope named ``scope``); with nothing loaded, ``preview_overlaps``
    answers from the labeled sample data. A comparison that fails is returned
    as ``status: "unavailable"``, never as an error, so the page keeps working.
    Raises ``ValueError`` for an unknown scope or a node that is not a model.
    """

    from . import scopes as scope_store
    from .overlap_report import OverlapChecker

    current = loaded()
    if current is None:
        return preview_overlaps(node)
    plan = _plan(scope, current.get("observed_reads", ()))
    chosen = plan.models if plan else None
    pipeline = current["pipeline"]
    if node not in pipeline.models:
        raise ValueError("node is not a model in the loaded project")
    section = OverlapChecker(pipeline, scope=chosen).section(node)
    return {**section, "node": node, "scope": scope or None, "scope_plan": plan.to_json() if plan else None,
            "source": {"kind": "project", "label": current["label"]}}
