"""The Reduce page: ``GET /api/reduce``, ``POST /api/reduce/run`` and ``GET /api/reduce/status``.

The run reduces the project loaded in the app (:func:`kumosql.project_reduction.reduce_project`) in the
background. The loaded project's files are written to a temporary folder that is deleted afterwards; nothing is
ever written into the user's project.
"""

from __future__ import annotations

import threading
import time
from typing import Mapping

from .project_reduction import ReductionError, reduce_project

TABLE_TYPES = ("view", "table")
DEFAULT_SECONDS = 120.0
MAX_SECONDS = 600.0

_JOB: dict = {"state": "idle"}
_JOB_LOCK = threading.Lock()
_THREADS: list[threading.Thread] = []


def _loaded_files():
    from . import live_graph

    current = live_graph.loaded()
    if not current:
        raise ReductionError("load a project first")
    files = getattr(current["pipeline"], "source_files", None)
    if not files:
        raise ReductionError("reload the project to edit its files")
    return current, files


def reduction_payload() -> dict:
    """``GET /api/reduce``: the actions of the loaded project and the choices the page offers."""

    from . import live_graph

    current = live_graph.loaded()
    base = {"table_types": list(TABLE_TYPES), "max_seconds": DEFAULT_SECONDS}
    if not current:
        return {**base, "loaded": False, "actions": []}
    pipeline = current["pipeline"]
    actions = [
        {"key": key, "name": model.target.name, "schema": model.target.schema, "kind": model.kind,
         "path": model.path or "", "tags": list(model.tags), "disabled": model.disabled}
        for key, model in pipeline.models.items()
    ]
    actions.sort(key=lambda a: (a["schema"], a["name"], a["key"]))
    return {**base, "loaded": True, "label": current["label"],
            "files_available": bool(getattr(pipeline, "source_files", None)), "actions": actions}


def _flag(body: Mapping, name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise ReductionError(f"{name} must be true or false")
    return value


def _options(body: object, known: set[str]) -> dict:
    if not isinstance(body, dict):
        raise ReductionError("request must be a JSON object")
    keep = body.get("keep")
    if not isinstance(keep, list) or not keep or not all(isinstance(key, str) for key in keep):
        raise ReductionError("choose at least one output to keep")
    unknown = [key for key in keep if key not in known]
    if unknown:
        raise ReductionError(f"not an action of the loaded project: {unknown[0]}")
    table_type = body.get("table_type", "view")
    if table_type not in TABLE_TYPES:
        raise ReductionError("table_type must be view or table")
    seconds = body.get("max_seconds", DEFAULT_SECONDS)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= MAX_SECONDS:
        raise ReductionError(f"max_seconds must be a number from 1 to {int(MAX_SECONDS)}")
    return {
        "keep": list(dict.fromkeys(keep)),
        "drop_only": _flag(body, "drop_only"),
        "keep_assertions": _flag(body, "keep_assertions"),
        "strict": _flag(body, "strict"),
        "table_type": table_type,
        "max_seconds": float(seconds),
    }


def job_status() -> dict:
    """``GET /api/reduce/status``: idle, running (with the latest progress line), done or error."""

    with _JOB_LOCK:
        data = dict(_JOB)
    if data["state"] == "running":
        data["elapsed"] = round(time.time() - data["started"], 1)
    return data


def _update(**values) -> None:
    with _JOB_LOCK:
        _JOB.update(values)


def _work(files: dict[str, str], options: dict, timeout_ms: int) -> None:
    from .live_graph import _Checkout, _write_files

    try:
        with _Checkout() as directory:
            _write_files(files, directory)
            result = reduce_project(
                directory, options["keep"], keep_assertions=options["keep_assertions"], strict=options["strict"],
                rewrite=not options["drop_only"], new_table_type=options["table_type"],
                max_seconds=options["max_seconds"], timeout_ms=timeout_ms,
                progress=lambda line: _update(line=line),
            )
            payload = result.to_json()
        _update(state="done", result=payload)
    except Exception as error:  # noqa: BLE001 - reported to the page, never raised into the server
        _update(state="error", error=str(error) or type(error).__name__)


def run_payload(body: object) -> dict:
    """``POST /api/reduce/run`` ``{keep, drop_only?, keep_assertions?, strict?, table_type?, max_seconds?}``:
    start reducing the loaded project in the background and return the job status."""

    from . import prover_context

    current, files = _loaded_files()
    options = _options(body, set(current["pipeline"].models))
    config = prover_context.settings()
    if not config["enabled"]:
        raise ReductionError("the solver is turned off in Settings")
    with _JOB_LOCK:
        if _JOB["state"] == "running":
            raise ReductionError("a reduction is already running")
        _JOB.clear()
        _JOB.update(state="running", started=time.time(), line="", keep=options["keep"])
    thread = threading.Thread(target=_work, args=(dict(files), options, config["timeout_ms"]), name="reduce-project", daemon=True)
    _THREADS[:] = [thread]
    thread.start()
    return job_status()


def wait(timeout: float = 120.0) -> dict:
    """Block until the latest job finishes (for tests and scripts); returns the final status."""

    if _THREADS:
        _THREADS[-1].join(timeout)
    return job_status()
