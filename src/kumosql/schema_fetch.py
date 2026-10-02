"""Column names and types for tables the analysis meets but the project does not define.

``SELECT *`` over a table the project does not declare cannot be expanded, and every column
read through it is lost. Before a project is analysed, the tables it reads that no model or
declared source accounts for are looked up: first in the saved BigQuery catalog, then (when
the setting is on) from BigQuery table metadata, which is free. Each table is fetched once per
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

from . import bigquery_catalog, console, state

ENV = "KUMOSQL_SCHEMA_FETCH"
MAX_TABLES = 2000  # per analysis; the rest stay unknown
MAX_WILDCARD_MATCHES = 50
WORKERS = 8
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
    """Whether unknown tables are looked up in BigQuery: the environment, then Settings (on by default)."""

    raw = os.environ.get(ENV)
    if raw is not None:
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    saved = state.get_section("schema_fetch", {}) or {}
    value = saved.get("enabled") if isinstance(saved, dict) else None
    return value if isinstance(value, bool) else True


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


def _fetch_one(triple: tuple[str, str, str]) -> dict[str, str] | None:
    if _denied(triple):
        return None
    project, dataset, table = triple
    try:
        entry = bigquery_catalog.cached(_key(*triple), lambda: bigquery_catalog.get_table(project, dataset, table))
    except bigquery_catalog.CatalogError as exc:
        if exc.status in (403, 404):
            with _LOCK:
                _DENIED[triple] = time.time()
        return None
    except Exception:  # noqa: BLE001 - one table failing must not stop the others
        return None
    return columns_of(entry["data"]) or None


def _wildcard(triple: tuple[str, str, str]) -> dict[str, str] | None:
    project, dataset, table = triple
    prefix = table[:-1]
    try:
        listing = bigquery_catalog.cached(
            bigquery_catalog.tables_key(project, dataset),
            lambda: bigquery_catalog.list_tables(project, dataset),
        )["data"]
    except Exception:  # noqa: BLE001
        return None
    matches = [item["id"] for item in listing if str(item.get("id", "")).startswith(prefix)]
    if not matches or len(matches) > MAX_WILDCARD_MATCHES:
        return None
    merged: dict[str, str] = {}
    for match in matches:
        columns = _fetch_one((project, dataset, match))
        if columns is None:
            return None  # one table of the set is unreadable: the union would be a guess
        for column, kind in columns.items():
            merged.setdefault(column, kind)
    return merged or None


def saved_schema() -> dict[str, dict[str, str]]:
    """``{"project.dataset.table": {column: type}}`` for every table in the saved catalog; calls nothing."""

    return {
        f"{project}.{dataset}.{table}": columns
        for project, dataset, table, data in bigquery_catalog.saved_tables()
        if (columns := columns_of(data))
    }


def resolve(names: Iterable[str], default_project: str = "", fetch: bool | None = None) -> tuple[dict[str, dict[str, str]], dict]:
    """Schemas for the table names as spelled in the SQL, plus ``{"asked", "found", "unknown"}`` counts.

    The saved catalog answers first. With ``fetch`` (default: the Settings toggle) the rest are
    requested from BigQuery, each distinct table once, in parallel.
    """

    names = sorted({n for n in names if n})
    saved = saved_schema()
    answers: dict[str, dict[str, str]] = {}
    pending: dict[tuple[str, str, str], list[str]] = {}
    for name in names:
        triple = _parts(name, default_project)
        if triple is None:
            continue
        full = ".".join(triple)
        if full in saved:
            answers[name] = saved[full]
        else:
            pending.setdefault(triple, []).append(name)
    from_catalog = len(answers)
    if (enabled() if fetch is None else fetch) and pending and _credentials_ok():
        todo = list(pending)[:MAX_TABLES]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            fetched = list(pool.map(lambda t: _wildcard(t) if t[2].endswith("*") else _fetch_one(t), todo))
        for triple, columns in zip(todo, fetched):
            if columns:
                for name in pending[triple]:
                    answers[name] = columns
    stats = {"asked": len(names), "found": len(answers), "from_catalog": from_catalog, "unknown": len(names) - len(answers)}
    if names:
        console.say(
            f"schema lookup: {stats['found']} of {stats['asked']} tables not in the project have known columns "
            f"({stats['from_catalog']} from the saved catalog)",
            console=False,
        )
    return answers, stats
