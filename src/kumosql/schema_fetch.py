"""Column names and types for tables the analysis meets but the project does not define.

``SELECT *`` over a table the project does not declare cannot be expanded, and every column
read through it is lost. Before a project is analysed, the tables it reads that no model or
declared source accounts for are looked up: first in the saved BigQuery catalog, then (only when
the caller has opted in) from BigQuery table metadata, which is free. The lookup is off by default,
so a library call never reaches the network on its own; ``KUMOSQL_SCHEMA_FETCH=1``, the Settings
checkbox or ``python -m kumosql pipeline-report --fetch-schema`` turn it on. Each table is fetched once per
analysis and kept in the catalog cache. A table the user cannot read, or one that is gone,
stays unknown: nothing is guessed. A wildcard table (``prefix_*``) takes the union of the
columns of the tables it matches, or stays unknown when too many match.

Table and column names never reach the log; only counts do.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import sqlglot

from . import bigquery_catalog, console, state, timing

ENV = "KUMOSQL_SCHEMA_FETCH"
MAX_TABLES = 2000  # per analysis; the rest stay unknown
MAX_WILDCARD_MATCHES = 50
WORKERS = 8
MAX_SECONDS = 60.0  # per analysis; tables not answered by then stay unknown
_DENIED_SECONDS = 3600.0
_LOCK = threading.Lock()
_DENIED: dict[tuple[str, str, str], float] = {}  # tables BigQuery refused or does not have, for this process
_NO_CREDENTIALS: list[float] = []  # when credentials last failed
_CREDENTIAL_RETRY_SECONDS = 60.0

_TYPES = {
    "INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT",
    "BIGNUMERIC": "BIGNUMERIC", "TIMESTAMP": "TIMESTAMP",
}


def enabled() -> bool:
    """Whether unknown tables are looked up in BigQuery: the environment, then Settings. Off unless someone opted in."""

    raw = os.environ.get(ENV)
    if raw is not None:
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    saved = state.get_section("schema_fetch", {}) or {}
    value = saved.get("enabled") if isinstance(saved, dict) else None
    return value if isinstance(value, bool) else False


def settings() -> dict:
    return {"enabled": enabled()}


def save_settings(enabled: object = None) -> dict:
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        state.set_section("schema_fetch", {"enabled": enabled})
    return settings()


def cache_tag() -> str:
    """Part of the saved-analysis key: results computed with and without fetching differ."""

    return "fetch" if enabled() else "nofetch"


def _column_type(field: dict) -> str:
    kind = str(field.get("type") or "").upper()
    kind = _TYPES.get(kind, kind)
    if str(field.get("mode") or "").upper() == "REPEATED":
        kind = f"ARRAY<{kind}>" if kind and kind != "STRUCT" else "ARRAY"
    if not kind:
        return "UNKNOWN"
    try:
        sqlglot.exp.DataType.build(kind, dialect="bigquery")
    except Exception:  # noqa: BLE001 - a type sqlglot cannot read is still a known column
        return "UNKNOWN"
    return kind


def columns_of(metadata: dict) -> dict[str, str]:
    """``{column: type}`` from a catalog table entry, in table order; empty when it has no schema."""

    found: dict[str, str] = {}
    for field in metadata.get("schema") or []:
        if isinstance(field, dict) and field.get("name"):
            found.setdefault(str(field["name"]), _column_type(field))
    return found


def _parts(name: str, default_project: str) -> tuple[str, str, str] | None:
    parts = [part.strip("`") for part in re.split(r"\.(?![^`]*`)", name)]
    if len(parts) == 2 and default_project:
        parts = [default_project, *parts]
    if len(parts) != 3 or not all(parts) or "${" in name:
        return None
    return parts[0], parts[1], parts[2]


def _key(project: str, dataset: str, table: str) -> str:
    return f"table\x1f{project}\x1f{dataset}\x1f{table}"


def _credentials_ok() -> bool:
    with _LOCK:
        if _NO_CREDENTIALS and time.time() - _NO_CREDENTIALS[0] < _CREDENTIAL_RETRY_SECONDS:
            return False
    try:
        bigquery_catalog._token_cached()
    except Exception:  # noqa: BLE001 - no credentials means nothing is fetched
        with _LOCK:
            _NO_CREDENTIALS[:] = [time.time()]
        return False
    return True


def _denied(triple: tuple[str, str, str]) -> bool:
    with _LOCK:
        at = _DENIED.get(triple)
    return at is not None and time.time() - at < _DENIED_SECONDS


def _fetch_one(triple: tuple[str, str, str]) -> tuple[dict[str, str] | None, str | None]:
    """``(columns, None)``, or ``(None, reason)``: ``denied``, ``not_found``, ``error`` or ``no_columns``."""

    if _denied(triple):
        return None, "denied"
    project, dataset, table = triple
    try:
        entry = bigquery_catalog.cached(_key(*triple), lambda: bigquery_catalog.get_table(project, dataset, table))
    except bigquery_catalog.CatalogError as exc:
        if exc.status in (403, 404):
            with _LOCK:
                _DENIED[triple] = time.time()
            return None, "denied" if exc.status == 403 else "not_found"
        return None, "error"
    except Exception:  # noqa: BLE001 - one table failing must not stop the others
        return None, "error"
    columns = columns_of(entry["data"])
    return (columns, None) if columns else (None, "no_columns")


def _wildcard(triple: tuple[str, str, str]) -> tuple[dict[str, str] | None, str | None]:
    project, dataset, table = triple
    prefix = table[:-1]
    try:
        listing = bigquery_catalog.cached(
            bigquery_catalog.tables_key(project, dataset),
            lambda: bigquery_catalog.list_tables(project, dataset),
        )["data"]
    except bigquery_catalog.CatalogError as exc:
        return None, "denied" if exc.status == 403 else "not_found" if exc.status == 404 else "error"
    except Exception:  # noqa: BLE001
        return None, "error"
    matches = [item["id"] for item in listing if str(item.get("id", "")).startswith(prefix)]
    if not matches:
        return None, "not_found"
    if len(matches) > MAX_WILDCARD_MATCHES:
        return None, "wildcard_too_many"
    merged: dict[str, str] = {}
    for match in matches:
        columns, _ = _fetch_one((project, dataset, match))
        if columns is None:
            return None, "wildcard_unreadable"  # one table of the set is unreadable: the union would be a guess
        for column, kind in columns.items():
            merged.setdefault(column, kind)
    return (merged, None) if merged else (None, "no_columns")


def saved_schema() -> dict[str, dict[str, str]]:
    """``{"project.dataset.table": {column: type}}`` for every table in the saved catalog; calls nothing."""

    return {
        f"{project}.{dataset}.{table}": columns
        for project, dataset, table, data in bigquery_catalog.saved_tables()
        if (columns := columns_of(data))
    }


def resolve(names: Iterable[str], default_project: str = "", fetch: bool | None = None) -> tuple[dict[str, dict[str, str]], dict]:
    """Schemas for the table names as spelled in the SQL, plus a statistics dict.

    The saved catalog answers first. With ``fetch`` (default: the Settings toggle) the rest are
    requested from BigQuery, each distinct table once, in parallel. The statistics hold counts only:
    ``asked``, ``found``, ``from_catalog``, ``unknown``, and what the fetch did: ``fetch`` (whether it was
    on), ``fetched`` (tables whose columns came from BigQuery), ``failed`` (tables BigQuery was asked
    about and could not describe), ``failed_by_reason`` and ``skipped_by_reason`` (tables never asked about:
    ``fetch_off``, ``no_credentials``, ``no_project``, ``over_limit``, ``out_of_time``).
    """

    names = sorted({n for n in names if n})
    started = time.time()
    refreshes_before = bigquery_catalog.token_refreshes
    saved = saved_schema()
    answers: dict[str, dict[str, str]] = {}
    pending: dict[tuple[str, str, str], list[str]] = {}
    failed_by_reason: dict[str, int] = {}
    skipped_by_reason: dict[str, int] = {}

    def skip(reason: str, count: int = 1) -> None:
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + count

    for name in names:
        triple = _parts(name, default_project)
        if triple is None:
            skip("no_project")  # not a project.dataset.table name, and the project has no default to complete it
            continue
        full = ".".join(triple)
        if full in saved:
            answers[name] = saved[full]
        else:
            pending.setdefault(triple, []).append(name)
    from_catalog = len(answers)
    wanted = enabled() if fetch is None else fetch
    fetched = failed = 0
    if pending and not wanted:
        skip("fetch_off", sum(len(v) for v in pending.values()))
    elif pending and not _credentials_ok():
        skip("no_credentials", sum(len(v) for v in pending.values()))
    elif pending:
        todo = list(pending)[:MAX_TABLES]
        if len(pending) > len(todo):
            skip("over_limit", sum(len(pending[t]) for t in list(pending)[MAX_TABLES:]))
        deadline = time.monotonic() + MAX_SECONDS

        def look_up(triple):
            if time.monotonic() > deadline:
                return None, "out_of_time"  # the table stays unknown rather than holding the analysis up
            return _wildcard(triple) if triple[2].endswith("*") else _fetch_one(triple)

        with timing.stage("schema lookup", tables=len(todo)), bigquery_catalog.batched_saves():
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                fetched_columns = list(pool.map(look_up, todo))
        for triple, (columns, reason) in zip(todo, fetched_columns):
            if columns:
                fetched += 1
                for name in pending[triple]:
                    answers[name] = columns
            elif reason == "out_of_time":
                skip(reason, len(pending[triple]))
            else:
                failed += 1
                failed_by_reason[reason or "error"] = failed_by_reason.get(reason or "error", 0) + 1
    stats = {
        "asked": len(names),
        "found": len(answers),
        "from_catalog": from_catalog,
        "unknown": len(names) - len(answers),
        "fetch": bool(wanted),
        "fetched": fetched,
        "failed": failed,
        "failed_by_reason": dict(sorted(failed_by_reason.items())),
        "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
    }
    if names:
        console.say(
            f"schema lookup: {stats['found']} of {stats['asked']} tables not in the project have known columns "
            f"({stats['from_catalog']} from the saved catalog, {fetched} fetched, {failed} failed; "
            f"{time.time() - started:.1f}s, {bigquery_catalog.token_refreshes - refreshes_before} access token refreshes)",
            console=False,
        )
    return answers, stats


_HINTS = {
    "fetch_off": "fetching is off (pass --fetch-schema, or turn it on in Settings)",
    "no_credentials": "no BigQuery credentials were found, so nothing was fetched",
    "no_project": "the table name has no project and the project has no default one",
    "over_limit": f"more than {MAX_TABLES} tables, so the rest were not asked about",
    "out_of_time": f"the {MAX_SECONDS:.0f}s limit passed before the table was asked about",
    "denied": "the account may not read the table's metadata (HTTP 403)",
    "not_found": "the table was not found (HTTP 404)",
    "error": "BigQuery could not be reached or returned an error",
    "no_columns": "the table has no schema",
    "wildcard_too_many": f"a wildcard matches more than {MAX_WILDCARD_MATCHES} tables",
    "wildcard_unreadable": "a table matched by a wildcard could not be read",
}


def summary(stats: dict) -> str:
    """One line for a person: what the lookup did, with the reasons tables stayed unknown (counts only)."""

    if not stats or not stats.get("asked"):
        return "schema lookup: every table the project reads has known columns (or none is read from outside the project)"
    parts = [f"{stats['found']} of {stats['asked']} tables without known columns now have them"]
    if stats.get("from_catalog"):
        parts.append(f"{stats['from_catalog']} from the saved catalog")
    asked = stats.get("fetched", 0) + stats.get("failed", 0)
    if stats.get("fetch") and asked:
        parts.append(f"fetched {stats.get('fetched', 0)} of {asked} tables, {stats.get('failed', 0)} failed")
    reasons = {**stats.get("failed_by_reason", {}), **stats.get("skipped_by_reason", {})}
    text = "schema lookup: " + "; ".join(parts)
    if reasons:
        text += ". Unknown because: " + "; ".join(f"{n} table(s): {_HINTS.get(r, r)}" for r, n in sorted(reasons.items()))
    return text
