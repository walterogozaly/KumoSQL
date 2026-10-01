"""The equivalence solver as the app uses it: settings, the facts it may assume, one entry point.

Rewrite verification asks ``prove`` when the structural prover cannot establish
equivalence. ``prove`` runs the algebraic prover (rewrites plus the SMT prover) with
the columns, NOT NULL columns and keys of the loaded Dataform project and the
saved BigQuery catalog, so a rewrite that depends on a key is provable once
the key is declared. The solver is on by default; ``enabled`` and the time
limit per solver check live in the ``prover`` section of the saved settings.
"""

from __future__ import annotations

import threading

from . import state
from .prover_schema import ProverSchema, from_pipeline
from .smt_equivalence import SmtEquivalenceResult, SmtStatus, z3

DEFAULT_TIMEOUT_MS = 5000
MIN_TIMEOUT_MS = 500
MAX_TIMEOUT_MS = 60000

_LOCK = threading.Lock()
_CACHE: dict = {}


def settings() -> dict:
    """``{"enabled": bool, "timeout_ms": int}`` with defaults filled in."""

    saved = state.get_section("prover", {}) or {}
    enabled = saved.get("enabled", True)
    timeout = saved.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        timeout = DEFAULT_TIMEOUT_MS
    return {
        "enabled": enabled if isinstance(enabled, bool) else True,
        "timeout_ms": min(max(timeout, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS),
    }


def save_settings(enabled: object = None, timeout_ms: object = None) -> dict:
    """Update the solver settings; refuses values of the wrong type or range."""

    current = settings()
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        current["enabled"] = enabled
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not (
            MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS
        ):
            raise ValueError(f"timeout_ms must be a whole number from {MIN_TIMEOUT_MS} to {MAX_TIMEOUT_MS}")
        current["timeout_ms"] = timeout_ms
    state.set_section("prover", current)
    return current


def current_schema() -> ProverSchema:
    """Facts from the loaded project and the saved BigQuery catalog (nothing is fetched)."""

    from . import bigquery_catalog, live_graph

    loaded = live_graph.loaded()
    pipeline = loaded["pipeline"] if loaded else None
    tables = bigquery_catalog.saved_tables()
    signature = (id(pipeline), len(tables))
    with _LOCK:
        if _CACHE.get("signature") == signature:
            return _CACHE["schema"]
    schema = from_pipeline(pipeline, tables) if pipeline is not None else from_pipeline(_Empty(), tables)
    with _LOCK:
        _CACHE.update(signature=signature, schema=schema, pipeline=pipeline)
    return schema


class _Empty:
    models: dict = {}
    source_schema: dict = {}


def status() -> dict:
    """What the solver can use right now, for the status line and Settings."""

    config = settings()
    schema = current_schema() if config["enabled"] else ProverSchema()
    return {
        **config,
        "available": z3 is not None,
        **schema.to_json(),
    }


def prove(old_sql: str, new_sql: str, *, timeout_ms: int | None = None, schema: ProverSchema | None = None) -> SmtEquivalenceResult:
    """Prove two queries return the same rows, using the project's declared facts."""

    from .algebraic_equivalence import prove_equivalent_algebraic

    facts = schema if schema is not None else current_schema()
    result = prove_equivalent_algebraic(
        old_sql,
        new_sql,
        schema=facts.columns or None,
        constraints=facts.constraints or None,
        timeout_ms=timeout_ms if timeout_ms is not None else settings()["timeout_ms"],
    )
    if facts.notes and result.status is SmtStatus.PROVEN_EQUIVALENT:
        extra = tuple(n for n in facts.notes if n not in result.assumptions)
        if extra:
            import dataclasses

            result = dataclasses.replace(result, assumptions=tuple(result.assumptions) + extra)
    return result
