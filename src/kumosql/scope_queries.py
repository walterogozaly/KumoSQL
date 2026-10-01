"""The scope condition "is returned by this SQL query".

A scope condition ``{"field": "user_email", "op": "in_query", "query": "SELECT ..."}`` is
true when the record's field value is among the values the query returns, so a team can be
kept in a BigQuery table and the scope follows it.

Running a query costs money, so it is guarded and cached:

* the query is dry-run first and refused when its estimate is over the byte cap;
* the real run carries ``maximumBytesBilled`` (BigQuery refuses it past the cap) and a label;
* it runs in the billing project chosen in Settings, never one guessed from the query;
* the result (one column of values) is kept for the query cache lifetime (Settings → BigQuery
  projects, default 48 hours), in memory and
  in ``scope-query-cache.json`` in the KumoSQL data directory, and is reused until it
  expires. An expired result is re-run when next used; if the re-run fails the older copy
  is used and flagged stale. ``refresh`` bypasses the timer.

The billing project and the cache lifetime are the BigQuery settings (``bigquery_catalog``);
the byte cap is this module's own setting, in the ``scope_queries`` section of ``state.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from . import state

SECTION = "scope_queries"
#: BigQuery bills at least 10 MB per query, so a smaller cap could never run anything.
MIN_BYTES_BILLED = 10 * 1000 * 1000
DEFAULT_MAX_BYTES_BILLED = 1024**3
MAX_BYTES_BILLED = 1024**4
MAX_VALUES = 200_000
MAX_QUERY_CHARS = 20_000
_API = "https://bigquery.googleapis.com/bigquery/v2"
_POLL_SECONDS = 120


class QueryError(ValueError):
    """A scope query could not be checked or run; the message says what to change."""


@dataclass(frozen=True)
class Settings:
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED

    def to_json(self) -> dict:
        return {"max_bytes_billed": self.max_bytes_billed}


def parse_settings(data: object) -> Settings:
    """Validate JSON-shaped settings, raising ``ValueError`` when malformed."""

    if not isinstance(data, dict):
        raise ValueError("query settings must be an object")
    cap = data.get("max_bytes_billed", DEFAULT_MAX_BYTES_BILLED)
    if isinstance(cap, bool) or not isinstance(cap, int) or not MIN_BYTES_BILLED <= cap <= MAX_BYTES_BILLED:
        raise ValueError(
            f"max_bytes_billed must be a whole number of bytes from {MIN_BYTES_BILLED} (10 MB) to {MAX_BYTES_BILLED}"
        )
    return Settings(cap)


def get_settings() -> Settings:
    try:
        return parse_settings(state.get_section(SECTION, {}) or {})
    except ValueError:
        return Settings()


def save_settings(data: object) -> Settings:
    settings = parse_settings(data)
    state.set_section(SECTION, settings.to_json())
    return settings


def billing_project() -> str:
    """The billing project chosen in Settings → BigQuery projects (empty when none is chosen)."""

    from . import bigquery_catalog

    return bigquery_catalog.billing_project()


def cache_seconds() -> float:
    """How long a result is reused: the query cache lifetime in Settings → BigQuery projects."""

    from . import bigquery_catalog

    return bigquery_catalog.query_cache_seconds()


# ------------------------------------------------------------------- running


@dataclass
class QueryRun:
    """What one execution produced."""

    values: list[str]
    column: str
    estimated_bytes: int | None = None
    bytes_billed: int | None = None
    cache_hit: bool = False


Post = Callable[[str, dict, bytes], tuple[int, dict]]
Get = Callable[[str, dict], tuple[int, dict]]


def _post(url: str, headers: dict, body: bytes) -> tuple[int, dict]:
    from .dryrun import _urllib_transport

    return _urllib_transport(url, headers, body)


def _get(url: str, headers: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": payload.decode("utf-8", "replace")}}


def _api_error(status: int, payload: dict) -> QueryError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return QueryError(error.get("message") or f"BigQuery returned HTTP {status}")


def _need_project(project: str | None) -> str:
    if project:
        return project
    from . import bigquery_catalog

    try:
        return bigquery_catalog.require_billing_project()
    except bigquery_catalog.BillingProjectRequired as exc:
        raise QueryError(str(exc)) from exc


def _pick_column(names: list[str], column: str | None) -> int:
    if not names:
        raise QueryError("the query returns no columns")
    if not column:
        return 0
    lowered = [name.casefold() for name in names]
    if column.casefold() not in lowered:
        raise QueryError(f"the query has no column {column!r}; it returns {', '.join(names)}")
    return lowered.index(column.casefold())


def dry_run(sql: str, column: str | None = None, *, project: str | None = None, max_bytes: int | None = None) -> dict:
    """Plan ``sql`` without running it (free): its columns and estimated bytes, checked against the cap."""

    from . import dryrun

    project = _need_project(project)
    cap = max_bytes if max_bytes is not None else get_settings().max_bytes_billed
    result = dryrun.dry_run(sql, project)
    if not result.ok:
        raise QueryError(f"BigQuery rejected the query: {result.error_message}")
    names = [item.name for item in result.schema]
    index = _pick_column(names, column)
    estimate = result.total_bytes_processed
    if estimate is not None and estimate > cap:
        raise QueryError(
            f"the query would process about {_size(estimate)}, over the {_size(cap)} cap. "
            "Narrow the query or raise the cap in Settings → Scopes."
        )
    return {"columns": names, "column": names[index], "estimated_bytes": estimate, "max_bytes_billed": cap}


def _bq_runner(sql: str, column: str | None, project: str, max_bytes: int) -> QueryRun:
    """Dry-run, then run ``sql`` in ``project`` with a byte cap; return the chosen column's values."""

    from .dryrun import access_token

    plan = dry_run(sql, column, project=project, max_bytes=max_bytes)
    headers = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"}
    body = {
        "query": sql,
        "useLegacySql": False,
        "useQueryCache": True,
        "maximumBytesBilled": str(max_bytes),
        "maxResults": 10000,
        "timeoutMs": 30000,
        "labels": {"kumosql": "scope_query"},
    }
    base = f"{_API}/projects/{quote(project, safe='')}"
    status, payload = _post(f"{base}/queries", headers, json.dumps(body).encode("utf-8"))
    if status >= 400 or "error" in payload:
        raise _api_error(status, payload)
    job = payload.get("jobReference", {})
    deadline = time.time() + _POLL_SECONDS
    while not payload.get("jobComplete", True):
        if time.time() > deadline:
            raise QueryError("the query did not finish in time; try again or make it cheaper")
        params = {"timeoutMs": "30000", **({"location": job["location"]} if job.get("location") else {})}
        status, payload = _get(f"{base}/queries/{quote(job.get('jobId', ''), safe='')}?{urlencode(params)}", headers)
        if status >= 400 or "error" in payload:
            raise _api_error(status, payload)
    names = [item.get("name", "") for item in payload.get("schema", {}).get("fields", [])] or plan["columns"]
    index = _pick_column(names, column)
    values: list[str] = []
    while True:
        for row in payload.get("rows", []):
            cell = row.get("f", [])[index].get("v") if len(row.get("f", [])) > index else None
            if cell is not None:
                values.append(str(cell))
            if len(values) > MAX_VALUES:
                raise QueryError(f"the query returns more than {MAX_VALUES:,} values; narrow it")
        token = payload.get("pageToken")
        if not token:
            break
        params = {"pageToken": token, "maxResults": "10000", **({"location": job["location"]} if job.get("location") else {})}
        status, payload = _get(f"{base}/queries/{quote(job.get('jobId', ''), safe='')}?{urlencode(params)}", headers)
        if status >= 400 or "error" in payload:
            raise _api_error(status, payload)
    billed = payload.get("totalBytesBilled")
    return QueryRun(
        values=values,
        column=names[index],
        estimated_bytes=plan["estimated_bytes"],
        bytes_billed=int(billed) if billed is not None else None,
        cache_hit=bool(payload.get("cacheHit")),
    )


#: The function that executes a query; tests replace it.
RUNNER: Callable[[str, str | None, str, int], QueryRun] = _bq_runner


def _size(count: int | float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


# --------------------------------------------------------------------- cache


@dataclass
class Result:
    values: frozenset[str]
    column: str
    fetched_at: float
    expires_at: float
    estimated_bytes: int | None = None
    bytes_billed: int | None = None
    stale: bool = False
    error: str | None = None
    _folded: frozenset[str] = field(default=frozenset(), repr=False)

    def to_json(self, sample: int = 5) -> dict:
        return {
            "count": len(self.values), "column": self.column, "sample": sorted(self.values)[:sample],
            "fetched_at": self.fetched_at, "expires_at": self.expires_at, "stale": self.stale, "error": self.error,
            "estimated_bytes": self.estimated_bytes, "bytes_billed": self.bytes_billed,
        }


_lock = threading.Lock()
_memory: dict[str, dict] = {}
_disk_loaded = False


def cache_key(sql: str, column: str | None) -> str:
    return hashlib.sha256(f"{sql.strip()}\x1f{(column or '').strip().casefold()}".encode("utf-8")).hexdigest()[:24]


def _cache_path() -> Path:
    return state.data_dir() / "scope-query-cache.json"


def _load_disk() -> None:
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    try:
        stored = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(stored, dict):
        for key, entry in stored.items():
            if isinstance(entry, dict) and isinstance(entry.get("values"), list) and "at" in entry:
                _memory.setdefault(key, entry)


def _save_disk() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".scope-query-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(_memory, out)
        os.replace(temp, path)
    except OSError:
        pass  # the cache is an optimization; a failed save never fails a request


def clear_cache() -> None:
    global _disk_loaded
    with _lock:
        _memory.clear()
        _disk_loaded = True
        try:
            _cache_path().unlink()
        except OSError:
            pass


def _result(entry: dict, now: float, ttl: float, stale: bool = False, error: str | None = None) -> Result:
    values = frozenset(str(v) for v in entry["values"])
    return Result(
        values, entry.get("column", ""), entry["at"], entry["at"] + ttl,
        entry.get("estimated"), entry.get("billed"), stale, error,
        frozenset(v.casefold() for v in values),
    )


def peek(sql: str, column: str | None = None) -> Result | None:
    """The saved result for a query, fresh or not, without running anything."""

    with _lock:
        _load_disk()
        entry = _memory.get(cache_key(sql, column))
    if not entry:
        return None
    now = time.time()
    ttl = cache_seconds()
    return _result(entry, now, ttl, stale=now - entry["at"] >= ttl)


def result_for(sql: str, column: str | None = None, *, refresh: bool = False) -> Result:
    """The values ``sql`` returns, from the cache while it is fresh, else by running it.

    ``refresh`` re-runs it now. When a re-run fails and an older copy exists, that copy is
    returned with ``stale`` set and the failure in ``error``.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise QueryError("the query is empty")
    key = cache_key(sql, column)
    settings = get_settings()
    ttl = cache_seconds()
    with _lock:
        _load_disk()
        entry = _memory.get(key)
    now = time.time()
    if entry and not refresh and now - entry["at"] < ttl:
        return _result(entry, now, ttl)
    try:
        run = RUNNER(sql, column or None, _need_project(None), settings.max_bytes_billed)
    except QueryError as exc:
        if entry:
            return _result(entry, now, ttl, stale=True, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - credentials, network and library errors read the same to the user
        message = f"could not run the query: {exc}"
        if entry:
            return _result(entry, now, ttl, stale=True, error=message)
        raise QueryError(message) from exc
    fresh = {
        "at": time.time(), "values": sorted(set(run.values)), "column": run.column,
        "estimated": run.estimated_bytes, "billed": run.bytes_billed, "sql": sql.strip()[:200],
    }
    with _lock:
        _memory[key] = fresh
        _save_disk()
    return _result(fresh, now, ttl)


def values_for(node: dict, case_sensitive: bool = False) -> frozenset[str]:
    """The values for a ``in_query`` condition, case-folded unless ``case_sensitive``."""

    result = result_for(node["query"], node.get("column"))
    return result.values if case_sensitive else result._folded


def cached_queries() -> list[dict]:
    """What is cached, newest first, for the Settings page."""

    ttl = cache_seconds()
    now = time.time()
    with _lock:
        _load_disk()
        items = [
            {"key": key, "sql": entry.get("sql", ""), "column": entry.get("column", ""), "count": len(entry["values"]),
             "fetched_at": entry["at"], "expires_at": entry["at"] + ttl, "expired": now - entry["at"] >= ttl,
             "bytes_billed": entry.get("billed")}
            for key, entry in _memory.items()
        ]
    return sorted(items, key=lambda item: -item["fetched_at"])


def query_nodes(rule: dict) -> list[dict]:
    """The ``in_query`` conditions of a (scope-expanded) rule."""

    found: list[dict] = []

    def walk(node: dict) -> None:
        if node.get("op") == "in_query":
            found.append(node)
        elif "not" in node:
            walk(node["not"])
        else:
            for child in node.get("all") or node.get("any") or ():
                walk(child)

    walk(rule)
    return found
