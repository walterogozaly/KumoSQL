import pytest

from kumosql import bigquery_catalog as catalog


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    monkeypatch.setattr(catalog, "_disk_loaded", False)
    catalog._memory.clear()
    yield
    catalog._memory.clear()


def test_second_call_uses_cache_and_refresh_bypasses():
    calls = []

    def fetch():
        calls.append(1)
        return ["a"]

    first = catalog.cached("k", fetch)
    second = catalog.cached("k", fetch)
    assert (first["cached"], second["cached"], len(calls)) == (False, True, 1)
    assert catalog.cached("k", fetch, refresh=True)["cached"] is False
    assert len(calls) == 2


def test_cache_survives_restart_and_serves_stale_on_error(monkeypatch):
    catalog.cached("k", lambda: ["a"])
    catalog._memory.clear()
    monkeypatch.setattr(catalog, "_disk_loaded", False)
    monkeypatch.setenv("KUMOSQL_CATALOG_TTL", "0")

    def boom():
        raise catalog.CatalogError("offline")

    result = catalog.cached("k", boom)
    assert result["data"] == ["a"] and result["stale"] is True
    with pytest.raises(catalog.CatalogError):
        catalog.cached("k", boom, refresh=True)


def test_list_projects_hides_projects_without_access(monkeypatch):
    def fake_get(path, params=None):
        if path == "projects":
            return {"projects": [{"id": "ok"}, {"id": "denied"}, {"id": "flaky"}]}
        if "denied" in path:
            raise catalog.CatalogError("no", 403)
        if "flaky" in path:
            raise catalog.CatalogError("down", 503)
        return {}

    monkeypatch.setattr(catalog, "_get", fake_get)
    assert [p["id"] for p in catalog.list_projects()] == ["ok", "flaky"]


def test_list_datasets_hides_anonymous_and_denied(monkeypatch):
    def ds(name):
        return {"datasetReference": {"datasetId": name}, "location": "US"}

    def fake_get(path, params=None):
        if path.endswith("/datasets"):
            return {"datasets": [ds("sales"), ds("_abc123"), ds("locked")]}
        if "locked" in path:
            raise catalog.CatalogError("bigquery.tables.list denied", 403)
        return {}

    monkeypatch.setattr(catalog, "_get", fake_get)
    assert [d["id"] for d in catalog.list_datasets("p")] == ["sales"]
