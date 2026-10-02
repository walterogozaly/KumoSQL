"""Catalogs: rule-defined sets of what a team owns, beyond the loaded Dataform repository."""

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from kumosql import bigquery_catalog as bq
from kumosql import catalogs, live_graph, scopes, state, tags
from kumosql.scopes import Scope

FILES = {
    "dataform.json": '{"defaultDataset": "stg", "defaultProject": "p1"}',
    "definitions/orders.sqlx": 'config { type: "table", schema: "stg", database: "p1" }\nSELECT id FROM `p1.raw.events`',
    "definitions/report.sqlx": 'config { type: "view", schema: "stg", database: "p1" }\nSELECT id FROM ${ref("orders")}',
}
TABLES = {
    "tables\x1fp1\x1fworkbooks": [{"id": "adhoc", "type": "TABLE"}, {"id": "fn", "type": "UDF"}],
    "tables\x1fp1\x1fother": [{"id": "theirs", "type": "TABLE"}],
}


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    live_graph.clear_project()
    state.set_section("bigquery", {"projects": ["p1"]})

    def peek(key):
        if key == "datasets\x1fbrowsable\x1fp1":
            return {"data": [{"id": "workbooks"}, {"id": "other"}]}
        return {"data": TABLES[key]} if key in TABLES else None

    monkeypatch.setattr(bq, "peek", peek)
    live_graph.load_files(FILES, "test")
    yield
    live_graph.clear_project()


def pipeline():
    return live_graph.loaded()["pipeline"]


def team(**extra):
    return {"name": "My team", "rule": {"field": "dataset", "op": "in", "value": ["workbooks"]}, **extra}


def test_default_catalog_is_the_repository_models_not_its_declarations():
    keys = catalogs.members(catalogs.DEFAULT)
    assert keys == {"p1.stg.orders", "p1.stg.report"}
    assert catalogs.active_names() == [catalogs.DEFAULT]


def test_catalog_can_own_bigquery_objects_written_outside_dataform():
    catalogs.save_catalogs([team()])
    assert catalogs.members("My team") == {"p1.workbooks.adhoc", "p1.workbooks.fn"}
    row = {c["name"]: c for c in catalogs.snapshot()["catalogs"]}["My team"]
    assert row["matched"] == 2 and row["active"] is False and not row["builtin"]


def test_catalog_rules_may_use_scopes_and_tags_but_not_catalogs():
    scopes.save_scope(Scope("Workbooks", rule={"field": "dataset", "op": "eq", "value": "workbooks"}))
    tags.change_manual(["p1.other.theirs"], add=["Mine"])
    catalogs.save_catalogs([
        {"name": "By scope", "rule": {"scope": "Workbooks"}},
        {"name": "By tag", "rule": {"field": "tag", "op": "in", "value": ["Mine"]}},
    ])
    assert catalogs.members("By scope") == {"p1.workbooks.adhoc", "p1.workbooks.fn"}
    assert catalogs.members("By tag") == {"p1.other.theirs"}
    with pytest.raises(ValueError, match="catalog"):
        catalogs.save_catalogs([{"name": "Loop", "rule": {"field": "catalog", "op": "eq", "value": "x"}}])
    with pytest.raises(ValueError, match="catalog"):
        tags.save_rules([{"tag": "T", "rule": {"field": "catalog", "op": "eq", "value": "x"}}])


def test_names_must_be_unique_and_the_builtin_cannot_be_redefined():
    with pytest.raises(ValueError, match="built in"):
        catalogs.save_catalogs([team(name=catalogs.DEFAULT)])
    with pytest.raises(ValueError, match="unique"):
        catalogs.save_catalogs([team(), team(name="my TEAM")])


def test_active_catalogs_are_kept_and_fall_back_to_the_default():
    catalogs.save_catalogs([team()])
    assert catalogs.set_active(["Dataform repository", "my team"]) == ["Dataform repository", "My team"]
    owned = catalogs.owned(pipeline())
    assert "p1.workbooks.adhoc" in owned and "p1.stg.orders" in owned and "p1.other.theirs" not in owned
    catalogs.save_catalogs([])  # deleting the catalog drops it from the active list
    assert catalogs.active_names() == [catalogs.DEFAULT]
    with pytest.raises(ValueError):
        catalogs.set_active(["nope"])
    with pytest.raises(ValueError):
        catalogs.set_active([])


def test_models_match_a_scope_on_the_catalog_field():
    catalogs.save_catalogs([{"name": "Reports", "rule": {"field": "name", "op": "eq", "value": "report"}}])
    scope = Scope("In reports", rule={"field": "catalog", "op": "in", "value": ["Reports"]})
    assert pipeline().scope_keys(scope) == {"p1.stg.report"}
    default = Scope("Repo", rule={"field": "catalog", "op": "in", "value": [catalogs.DEFAULT]})
    assert pipeline().scope_keys(default) == set(pipeline().models)


def test_owned_model_keys_are_none_when_every_model_is_owned():
    assert catalogs.model_keys(pipeline()) is None
    catalogs.save_catalogs([{"name": "Reports", "rule": {"field": "name", "op": "eq", "value": "report"}}])
    catalogs.set_active(["Reports"])
    assert catalogs.model_keys(pipeline()) == {"p1.stg.report"}


def test_impact_flags_readers_outside_the_active_catalogs_instead_of_dropping_them():
    catalogs.save_catalogs([{"name": "Reports", "rule": {"field": "name", "op": "eq", "value": "report"}}])
    catalogs.set_active(["Reports"])
    impact = pipeline().assess_change("drop_column", "p1.stg.orders", "id", owned=catalogs.owned(pipeline()))
    found = {a.model: a.owned for a in impact.affected}
    assert found == {"p1.stg.report": True}
    full = pipeline().assess_change("drop_table", "p1.raw.events", owned=catalogs.owned(pipeline()))
    assert {a.model: a.owned for a in full.affected} == {"p1.stg.orders": False, "p1.stg.report": True}
    assert full.not_owned == 1 and full.catalogs == ["Reports"]
    assert full.to_json()["affected"][0]["owned"] is False
    assert pipeline().assess_change("drop_table", "p1.raw.events").not_owned == 0


def test_graph_nodes_say_whether_the_active_catalogs_own_them():
    payload = live_graph.graph_payload(pipeline())
    assert payload["catalogs"] == [catalogs.DEFAULT]
    owned = {node["id"]: node["owned"] for node in payload["nodes"]}
    assert owned["p1.stg.orders"] is True and owned["p1.raw.events"] is False


def test_preview_counts_what_a_rule_would_own():
    result = catalogs.preview(team())
    assert result["matched"] == 2 and result["examples"] == ["p1.workbooks.adhoc", "p1.workbooks.fn"]


@pytest.fixture
def server():
    from kumosql.ui import UIHandler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def call(base, method, path, payload=None):
    request = Request(base + path, method=method, headers={"Content-Type": "application/json"},
                      data=None if payload is None else json.dumps(payload).encode())
    try:
        with urlopen(request) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def test_http_round_trip(server):
    assert call(server, "PUT", "/api/settings/catalogs", [team()])[0] == 200
    status, preview = call(server, "POST", "/api/catalogs/preview", team())
    assert status == 200 and preview["matched"] == 2
    assert call(server, "POST", "/api/catalogs/active", {"active": ["My team"]}) == (200, {"active": ["My team"]})
    status, snap = call(server, "GET", "/api/catalogs")
    assert status == 200 and snap["active"] == ["My team"]
    assert [c["name"] for c in snap["catalogs"]] == [catalogs.DEFAULT, "My team"]
    status, settings = call(server, "GET", "/api/settings")
    assert [c["name"] for c in settings["catalogs"]] == ["My team"]
    assert call(server, "POST", "/api/catalogs/active", {"active": ["nope"]})[0] == 400
    assert call(server, "PUT", "/api/settings/catalogs", [team(name=catalogs.DEFAULT)])[0] == 400
