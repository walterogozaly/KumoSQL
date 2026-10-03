"""Data sources: saved queries whose columns are scope fields and whose names are "applies to" domains."""

import time

import pytest

from kumosql import data_sources, scope_queries, scopes, state, tags
from kumosql.ui import UIHandler


@pytest.fixture(autouse=True)
def billing(monkeypatch):
    state.set_section("bigquery", {"billingProject": "bill-proj"})
    calls = []

    def runner(source, project, max_bytes):
        calls.append((source.query, project, max_bytes))
        return data_sources.Run(
            ["full_name", "owner", "email"],
            [["p.d.orders", "ana", "ana@co.com"], ["p.d.orders", "bo", None], ["p.d.users", "cy", "cy@co.com"]],
            estimated_bytes=10, bytes_billed=0,
        )

    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", runner)
    return calls


def make(**overrides):
    item = {"name": "Owners", "type": "bigquery_sql", "query": "SELECT * FROM p.d.INTERNAL_METADATA", **overrides}
    return data_sources.save_sources([item])[0]


def test_sources_are_validated_and_given_stable_ids():
    source = make()
    assert source.id == "owners" and source.domain == "source:owners"
    assert data_sources.get_source("owners").query.startswith("SELECT")
    for bad in ({"name": ""}, {"query": " "}, {"type": "csv"}, {"cache_hours": -1}, {"cache_hours": "x"}):
        with pytest.raises(ValueError):
            data_sources.save_sources([{"name": "n", "query": "SELECT 1", **bad}])
    with pytest.raises(ValueError, match="unique"):
        data_sources.save_sources([{"name": "A", "query": "SELECT 1"}, {"name": "a", "query": "SELECT 2"}])
    # Editing keeps the id; the name may change.
    renamed = data_sources.save_sources([{**source.to_json(), "name": "Table owners"}])[0]
    assert renamed.id == "owners"


def test_populate_runs_in_the_billing_project_then_reuses_the_rows_until_they_expire(billing):
    source = make()
    table = data_sources.populate(source)
    assert billing == [(source.query, "bill-proj", scope_queries.get_settings().max_bytes_billed)]
    assert table.columns == ["full_name", "owner", "email"] and len(table.rows) == 3
    data_sources.populate(source)
    assert len(billing) == 1  # fresh: no second run
    data_sources.populate(source, refresh=True)
    assert len(billing) == 2
    assert data_sources.peek(source).rows == table.rows  # kept in the data folder


def test_cache_lifetime_defaults_to_the_global_one_and_can_be_set_per_source(billing, monkeypatch):
    default = make()
    assert data_sources.cache_seconds(default) == scope_queries.cache_seconds()
    short = make(cache_hours=0)
    assert data_sources.cache_seconds(short) == 0
    data_sources.populate(short)
    data_sources.populate(short)
    assert len(billing) == 2  # a 0 hour lifetime never reuses


def test_an_edited_query_is_run_again_and_a_failed_run_keeps_the_old_rows(billing, monkeypatch):
    source = make()
    data_sources.populate(source)
    edited = make(query="SELECT * FROM p.d.OTHER")
    assert data_sources.peek(edited) is None
    data_sources.populate(edited)
    assert len(billing) == 2

    def broken(source, project, max_bytes):
        raise scope_queries.QueryError("over the byte cap")

    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", broken)
    table = data_sources.populate(edited, refresh=True)
    assert table.stale and table.error == "over the byte cap" and len(table.rows) == 3
    fresh = make(name="Other", query="SELECT 2")
    with pytest.raises(data_sources.DataSourceError, match="over the byte cap"):
        data_sources.populate(fresh)


def test_populate_needs_a_billing_project(monkeypatch):
    state.set_section("bigquery", {})
    with pytest.raises(data_sources.DataSourceError, match="billing project"):
        data_sources.populate(make())


def test_each_source_is_an_applies_to_domain_and_its_columns_are_fields():
    source = make()
    data_sources.populate(source)
    assert scopes.all_domains()["source:owners"] == "Owners"
    scope = scopes.Scope("Mine", rule={"field": "owner", "op": "eq", "value": "ana"}, applies_to=("source:owners",))
    scopes.save_scope(scope)
    assert scopes.get_scope("Mine").applies_to == ("source:owners",)
    # Inferred from the columns when not given.
    scopes.save_scope(scopes.Scope("Inferred", rule={"field": "email", "op": "not_null"}))
    assert scopes.get_scope("Inferred").applies_to == ("source:owners",)
    assert scope.matches(data_sources.records(source)[0]) and not scope.matches(data_sources.records(source)[2])
    names = {info["name"]: info for info in UIHandler._scope_fields()["fields"]}
    assert names["owner"]["source"] == "Owners" and "ana" in names["owner"]["examples"]
    assert {"key": "source:owners", "label": "Owners"} in UIHandler._scope_fields()["domains"]
    with pytest.raises(ValueError, match="unknown data domain"):
        scopes.parse_scope({"name": "x", "rule": {"field": "a", "op": "eq", "value": "1"}, "applies_to": ["source:nope"]})


def test_a_scope_for_a_source_does_not_break_the_pages_that_cannot_use_it():
    data_sources.populate(make())
    scopes.save_scope(scopes.Scope("Mine", rule={"field": "owner", "op": "eq", "value": "ana"}, applies_to=("source:owners",)))
    plan = scopes.plan_scope(scopes.get_scope("Mine"), [])
    assert plan.models is None and plan.jobs is None and "Owners" in plan.note


def test_deleting_a_source_drops_it_from_scopes_but_never_leaves_one_empty():
    data_sources.populate(make())
    scopes.save_scope(scopes.Scope("Both", rule={"field": "owner", "op": "eq", "value": "ana"}, applies_to=("jobs", "source:owners")))
    scopes.save_scope(scopes.Scope("Only", rule={"field": "owner", "op": "eq", "value": "ana"}, applies_to=("source:owners",)))
    with pytest.raises(ValueError, match="'Only'"):
        data_sources.save_sources([])
    scopes.delete_scope("Only")
    data_sources.save_sources([])
    assert scopes.get_scope("Both").applies_to == ("jobs",)
    assert not data_sources.list_sources() and data_sources.peek(make()) is None


def test_tag_rules_can_use_columns_of_a_source_joined_on_full_name():
    data_sources.populate(make())
    assert data_sources.tag_fields() == ["owner", "email"]
    objects = {
        "p.d.orders": {"full_name": "p.d.orders", "name": "orders"},
        "p.d.users": {"full_name": "p.d.users", "name": "users"},
        "p.d.other": {"full_name": "p.d.other", "name": "other"},
    }
    rule = {"tag": "Ana", "rule": {"field": "owner", "op": "eq", "value": "ana"}}
    assert tags.evaluate_rule(rule, objects) == ["p.d.orders"]
    many = {"tag": "Bo", "rule": {"field": "owner", "op": "in", "value": ["bo"]}}
    assert tags.evaluate_rule(many, objects) == ["p.d.orders"]  # an object with several rows matches any
    tags.save_rules([rule])


def test_a_source_column_is_not_a_tag_field_without_full_name(monkeypatch):
    monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql",
                        lambda s, p, m: data_sources.Run(["owner"], [["ana"]]))
    data_sources.populate(make())
    with pytest.raises(ValueError, match="no field 'owner'"):
        tags.save_rules([{"tag": "T", "rule": {"field": "owner", "op": "eq", "value": "ana"}}])


def test_the_bigquery_runner_returns_every_column(monkeypatch):
    source = make()
    monkeypatch.setattr(scope_queries, "dry_run", lambda sql, column, project, max_bytes: {"columns": ["a", "b"], "estimated_bytes": 5})
    monkeypatch.setattr("kumosql.dryrun.access_token", lambda: "t")
    sent = []

    def post(url, headers, body):
        sent.append(body)
        return 200, {
            "jobComplete": True, "jobReference": {"jobId": "j", "location": "US"},
            "schema": {"fields": [{"name": "a"}, {"name": "b"}]},
            "rows": [{"f": [{"v": "1"}, {"v": None}]}, {"f": [{"v": "2"}, {"v": ["x"]}]}], "totalBytesBilled": "10485760",
        }

    monkeypatch.setattr(scope_queries, "_post", post)
    run = data_sources._bq_runner(source, "bill-proj", 10**9)
    assert run.columns == ["a", "b"] and run.rows == [["1", None], ["2", '["x"]']] and run.bytes_billed == 10485760
    assert b"maximumBytesBilled" in sent[0]


FILES = {
    "dataform.json": '{"defaultSchema": "d", "defaultDatabase": "p"}',
    "definitions/orders.sqlx": 'config { type: "table" }\nSELECT id FROM `p.raw.src`',
    "definitions/users.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("orders")}',
    "definitions/other.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("orders")}',
}


def test_a_scope_over_source_columns_filters_dataform_models_by_their_target_table():
    from kumosql import live_graph

    live_graph.clear_project()
    live_graph.load_files(FILES, "demo")
    pipeline = live_graph.loaded()["pipeline"]
    data_sources.populate(make())  # full_name p.d.orders / p.d.orders / p.d.users, owners ana, bo, cy
    scopes.save_scope(scopes.Scope("Ana", rule={"field": "owner", "op": "eq", "value": "ana"}, applies_to=("models", "source:owners")))
    keys = pipeline.scope_keys(scopes.get_scope("Ana"))
    assert keys == {"p.d.orders"}
    plan = scopes.plan_scope(scopes.get_scope("Ana"), [])
    assert plan.models is not None and plan.note is None
    record = pipeline.model_record("p.d.users")
    assert record["full_name"] == "p.d.users" and record["email"] == "cy@co.com"
    graph = live_graph.graph_payload(pipeline, "demo", (), "Ana")
    assert graph["scope"]["applied_to"] == ["models"]
    ids = {n["id"] for n in graph["nodes"]}
    assert "p.d.orders" in ids and "p.d.users" not in ids and "p.d.other" not in ids
