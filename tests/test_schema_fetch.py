import random

import pytest

from kumosql import ColumnRef, bigquery_catalog, load_compiled_graph, schema_fetch


@pytest.fixture(autouse=True)
def clean_caches():
    bigquery_catalog.clear_cache()
    schema_fetch._DENIED.clear()
    schema_fetch._NO_CREDENTIALS.clear()
    yield
    bigquery_catalog.clear_cache()


def fake_bigquery(monkeypatch, tables, denied=(), listing=None):
    """Stand in for BigQuery: ``tables`` is ``{"p.d.t": [column names]}``; returns the list of fetched tables."""

    calls = []

    def get_table(project, dataset, table):
        full = f"{project}.{dataset}.{table}"
        calls.append(full)
        if full in denied or full not in tables:
            raise bigquery_catalog.CatalogError("no access", 403)
        return {"id": table, "schema": [{"name": c, "type": "INTEGER" if c == "id" else "STRING"} for c in tables[full]]}

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "token")
    monkeypatch.setattr(bigquery_catalog, "get_table", get_table)
    monkeypatch.setattr(
        bigquery_catalog, "list_tables",
        lambda project, dataset: [{"id": t.split(".")[2]} for t in (listing or tables) if t.startswith(f"{project}.{dataset}.")],
    )
    return calls


def project(**queries):
    graph = {"tables": [{"target": {"database": "p", "schema": "m", "name": n}, "query": q} for n, q in queries.items()]}
    return load_compiled_graph(graph)


def codes(pipeline):
    return {(d.model, d.code) for d in pipeline._analyse().diagnostics}


def test_star_over_unknown_table_stays_unknown_when_fetching_is_off():
    pipeline = project(s="SELECT * FROM `p.ext.raw`")
    assert ("p.m.s", "unexpanded_star") in codes(pipeline)


def test_star_over_unknown_table_is_expanded_from_fetched_columns(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id", "name"]})
    pipeline = project(s="SELECT * FROM `p.ext.raw`", t="SELECT id, name FROM `p.m.s`")

    assert ("p.m.s", "unexpanded_star") not in codes(pipeline)
    assert calls == ["p.ext.raw"]
    assert pipeline.explain_lineage()[ColumnRef("p.m.s", "name")].status == "traced"
    assert pipeline.trace_column(ColumnRef("p.m.t", "id")).complete


def test_each_table_is_fetched_once_across_models(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]})
    project(**{f"m{i}": "SELECT * FROM `p.ext.raw`" for i in range(20)})._analyse()
    assert calls == ["p.ext.raw"]


def test_inaccessible_table_stays_unknown(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]}, denied={"p.ext.raw"})
    schema_fetch._DENIED.clear()
    pipeline = project(s="SELECT * FROM `p.ext.raw`")
    assert ("p.m.s", "unexpanded_star") in codes(pipeline)


def test_wildcard_table_uses_union_of_matching_schemas(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.ev_1": ["id", "a"], "p.ext.ev_2": ["id", "b"], "p.ext.other": ["z"]})
    schema, stats = schema_fetch.resolve(["p.ext.ev_*"])
    assert list(schema["p.ext.ev_*"]) == ["id", "a", "b"]
    assert stats["unknown"] == 0


def test_wildcard_with_an_unreadable_member_stays_unknown(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.ev_1": ["id"], "p.ext.ev_2": ["id"]}, denied={"p.ext.ev_2"})
    schema_fetch._DENIED.clear()
    schema, stats = schema_fetch.resolve(["p.ext.ev_*"])
    assert schema == {} and stats["unknown"] == 1


def test_saved_catalog_answers_without_calling_bigquery(monkeypatch):
    calls = fake_bigquery(monkeypatch, {})
    monkeypatch.setattr(bigquery_catalog, "saved_tables", lambda: [("p", "ext", "raw", {"schema": [{"name": "id", "type": "INT64"}]})])
    schema, stats = schema_fetch.resolve(["ext.raw"], default_project="p")
    assert schema == {"ext.raw": {"id": "INT64"}} and calls == [] and stats["from_catalog"] == 1


def test_no_credentials_means_no_lookup(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]})
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: (_ for _ in ()).throw(RuntimeError("no credentials")))
    schema_fetch._NO_CREDENTIALS.clear()
    assert schema_fetch.resolve(["p.ext.raw"])[0] == {} and calls == []


def test_log_holds_counts_not_names(monkeypatch):
    fake_bigquery(monkeypatch, {"p.secret_ds.secret_table": ["secret_col"]})
    messages = []
    monkeypatch.setattr(schema_fetch.console, "say", lambda message, **kw: messages.append(message))
    schema_fetch.resolve(["p.secret_ds.secret_table"])
    assert messages and not any("secret" in m for m in messages)


def test_settings_round_trip(monkeypatch):
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    assert schema_fetch.settings() == {"enabled": True}
    assert schema_fetch.save_settings(False) == {"enabled": False}
    with pytest.raises(ValueError):
        schema_fetch.save_settings("yes")


def test_generated_job_log_star_failures_are_resolved(monkeypatch):
    """A job-log-shaped corpus: many statements over a few hundred outside tables, some unreadable."""

    rng = random.Random(7)
    tables = {f"p.ext{i % 12}.t{i}": ["id", "name", "amount"] for i in range(300)}
    denied = {name for name in tables if rng.random() < 0.1}
    calls = fake_bigquery(monkeypatch, tables, denied=denied)
    schema_fetch._DENIED.clear()
    queries = {}
    for n in range(1200):
        source = rng.choice(list(tables))
        queries[f"j{n}"] = rng.choice(["SELECT * FROM `{t}`", "SELECT * EXCEPT (name) FROM `{t}` WHERE id > 1"]).format(t=source)
    pipeline = project(**queries)

    stars = [(m, c) for m, c in codes(pipeline) if c == "unexpanded_star"]
    readable = {q.split("`")[1] for q in queries.values()} - denied
    assert len(set(calls)) == len(calls) <= 300
    # every statement over a readable table is resolved; every one over an unreadable table stays unknown
    unresolved_models = {m for m, _ in stars}
    assert unresolved_models == {f"p.m.{n}" for n, q in queries.items() if q.split("`")[1] in denied}
    assert len(stars) < 1200 and readable
