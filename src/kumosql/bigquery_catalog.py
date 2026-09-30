"""Read-only BigQuery project, dataset, table, and schema browsing."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .dryrun import access_token

_API = "https://bigquery.googleapis.com/bigquery/v2"


class CatalogError(RuntimeError):
    """A BigQuery catalog request failed with a user-facing message."""


def _get(path: str, params: dict[str, str] | None = None) -> dict:
    token = access_token()
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
        raise CatalogError(message) from exc
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


def list_projects() -> list[dict[str, str]]:
    """List projects visible to the active Google credentials."""
    return [
        {
            "id": item.get("id", ""),
            "name": item.get("friendlyName") or item.get("name") or item.get("id", ""),
        }
        for item in _list("projects", "projects")
    ]


def list_datasets(project: str) -> list[dict[str, str]]:
    return [
        {"id": item.get("datasetReference", {}).get("datasetId", ""),
         "location": item.get("location", "")}
        for item in _list(f"projects/{quote(project, safe='')}/datasets", "datasets", {"all": "true"})
    ]


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
