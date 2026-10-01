"""Read-only BigQuery project, dataset, table, and schema browsing."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .dryrun import access_token
from .state import data_dir, get_section, set_section

_API = "https://bigquery.googleapis.com/bigquery/v2"


class CatalogError(RuntimeError):
    """A BigQuery catalog request failed with a user-facing message."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# Catalog listings and schemas are metadata calls: free, but each is a network
# round trip and the first also mints an OAuth token. Answers are kept in memory
# and in a small file so reopening the page or revisiting an item is instant.
DEFAULT_TTL_SECONDS = 3600
_TOKEN_SECONDS = 1800
_lock = threading.Lock()
_memory: dict[str, dict] = {}
_token: tuple[str, float] | None = None
_disk_loaded = False


def ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("KUMOSQL_CATALOG_TTL", DEFAULT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _cache_path() -> Path:
    return data_dir() / "catalog-cache.json"


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
            if isinstance(entry, dict) and "at" in entry and "data" in entry:
                _memory.setdefault(key, entry)


def _save_disk() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".catalog-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(_memory, out)
        os.replace(temp, path)
    except OSError:
        pass  # the cache is an optimization; never fail a request over it


def clear_cache() -> None:
    global _disk_loaded
    with _lock:
        _memory.clear()
        _disk_loaded = True
        try:
            _cache_path().unlink()
        except OSError:
            pass


def cached(key: str, fetch, refresh: bool = False) -> dict:
    """Return ``{"data", "fetchedAt", "cached", "stale"}`` for ``key``.

    A fresh entry is served without calling BigQuery. ``refresh`` bypasses it.
    If BigQuery fails and an older copy exists, that copy is returned marked
    stale instead of an error.
    """
    with _lock:
        _load_disk()
        entry = _memory.get(key)
    now = time.time()
    if entry and not refresh and now - entry["at"] < ttl_seconds():
        return {"data": entry["data"], "fetchedAt": entry["at"], "cached": True, "stale": False}
    try:
        data = fetch()
    except (CatalogError, RuntimeError):
        if entry and not refresh:
            return {"data": entry["data"], "fetchedAt": entry["at"], "cached": True, "stale": True}
        raise
    with _lock:
        _memory[key] = {"at": now, "data": data}
        _save_disk()
    return {"data": data, "fetchedAt": now, "cached": False, "stale": False}


def forget_table(project: str, dataset: str, table: str) -> None:
    """Drop ``table`` from the cached table list of its dataset (it turned out to be inaccessible)."""
    key = "\x1f".join(("tables", project, dataset))
    with _lock:
        entry = _memory.get(key)
        if entry:
            entry["data"] = [item for item in entry["data"] if item.get("id") != table]
            _save_disk()


def _token_cached() -> str:
    global _token
    if os.environ.get("BQ_ACCESS_TOKEN"):
        return access_token()
    if _token and time.time() < _token[1]:
        return _token[0]
    value = access_token()
    _token = (value, time.time() + _TOKEN_SECONDS)
    return value


def _get(path: str, params: dict[str, str] | None = None) -> dict:
    token = _token_cached()
    url = f"{_API}/{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            payload = {}
        error = payload.get("error", {})
        message = error.get("message") or f"BigQuery returned HTTP {exc.code}"
        raise CatalogError(message, exc.code) from exc
    except URLError as exc:
        raise CatalogError(f"Could not connect to BigQuery: {exc.reason}") from exc


def _list(path: str, key: str, params: dict[str, str] | None = None) -> list[dict]:
    items: list[dict] = []
    query = {"maxResults": "1000", **(params or {})}
    while True:
        payload = _get(path, query)
        items.extend(payload.get(key, []))
        token = payload.get("nextPageToken")
        if not token:
            return items
        query["pageToken"] = token


def can_browse(project: str) -> bool:
    """Whether the credentials may list datasets in ``project``.

    Only a definite denial (403 or 404, which includes BigQuery being disabled
    there) hides a project; a network or server error keeps it.
    """
    try:
        _get(f"projects/{quote(project, safe='')}/datasets", {"maxResults": "1", "all": "true"})
    except CatalogError as exc:
        return exc.status not in (403, 404)
    return True


def list_projects() -> list[dict[str, str]]:
    """List every project the credentials hold some role on.

    This can be long (it is not the console's starred list) and is not probed
    for BigQuery access, so it is only requested while choosing projects.
    ``can_browse`` checks the ones actually chosen.
    """
    return [
        {
            "id": item.get("id", ""),
            "name": item.get("friendlyName") or item.get("name") or item.get("id", ""),
        }
        for item in _list("projects", "projects")
    ]


_PROJECT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9.:_-]{0,99}$")
MAX_SELECTED = 100


def selected_projects() -> list[str]:
    """The project ids chosen for the BigQuery tab (empty until the user picks some)."""
    stored = get_section("bigquery", {})
    items = stored.get("projects", []) if isinstance(stored, dict) else []
    return [item for item in items if isinstance(item, str) and _PROJECT_ID.match(item)]


def select_projects(projects: list) -> list[str]:
    """Validate and save the chosen projects; refuses ids BigQuery denies access to."""
    if not isinstance(projects, list) or not all(isinstance(item, str) for item in projects):
        raise ValueError("projects must be a list of project ids")
    ids = list(dict.fromkeys(item.strip() for item in projects if item.strip()))
    if len(ids) > MAX_SELECTED:
        raise ValueError(f"choose at most {MAX_SELECTED} projects")
    bad = [item for item in ids if not _PROJECT_ID.match(item)]
    if bad:
        raise ValueError(f"not a project id: {bad[0]}")
    previous = set(selected_projects())
    new = [item for item in ids if item not in previous]
    if new:
        with ThreadPoolExecutor(max_workers=min(8, len(new))) as pool:
            allowed = list(pool.map(can_browse, new))
        denied = [item for item, ok in zip(new, allowed) if not ok]
        if denied:
            raise ValueError(f"No access to BigQuery in {', '.join(denied)}")
    set_section("bigquery", {"projects": ids})
    return ids


def _can_list_tables(project: str, dataset: str) -> bool:
    """Whether the credentials may list tables in ``dataset`` (same rule as projects)."""
    try:
        _get(
            f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}/tables",
            {"maxResults": "1"},
        )
    except CatalogError as exc:
        return exc.status not in (403, 404)
    return True


def list_datasets(project: str) -> list[dict[str, str]]:
    """List datasets the credentials can browse.

    Names starting with an underscore are hidden anonymous datasets that hold
    cached query results (the BigQuery console hides them too); datasets whose
    table listing BigQuery denies are dropped.
    """
    datasets = [
        {"id": item.get("datasetReference", {}).get("datasetId", ""),
         "location": item.get("location", "")}
        for item in _list(f"projects/{quote(project, safe='')}/datasets", "datasets", {"all": "true"})
    ]
    datasets = [item for item in datasets if not item["id"].startswith("_")]
    if not datasets:
        return datasets
    with ThreadPoolExecutor(max_workers=min(8, len(datasets))) as pool:
        allowed = list(pool.map(lambda item: _can_list_tables(project, item["id"]), datasets))
    return [item for item, ok in zip(datasets, allowed) if ok]


def list_tables(project: str, dataset: str) -> list[dict[str, str]]:
    return [
        {"id": item.get("tableReference", {}).get("tableId", ""),
         "type": item.get("type", "TABLE")}
        for item in _list(
            f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}/tables", "tables"
        )
    ]


def get_table(project: str, dataset: str, table: str) -> dict:
    """Return table metadata with its BigQuery schema."""
    payload = _get(
        f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}/tables/{quote(table, safe='')}"
    )
    reference = payload.get("tableReference", {})
    return {
        "id": reference.get("tableId", table),
        "type": payload.get("type", "TABLE"),
        "numRows": payload.get("numRows"),
        "schema": payload.get("schema", {}).get("fields", []),
    }
