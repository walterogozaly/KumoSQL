from types import SimpleNamespace

import pytest

from kumosql import prover_context
from kumosql.googlesql_types import infer_pipeline
from kumosql.pipeline_loading import _config_assertions
from kumosql.pipeline_types import Model, Target
from kumosql.prover_schema import DECLARED_FACTS_NOTE, from_bigquery, from_pipeline
from kumosql.smt_equivalence import SmtStatus

SELF_JOIN = "SELECT a.id, b.v FROM p.d.t a JOIN p.d.t b ON a.id = b.id"
PLAIN = "SELECT id, v FROM p.d.t"


def _table(name, fields, primary=None):
    data = {"schema": [{"name": n, "mode": m} for n, m in fields]}
    if primary:
        data["constraints"] = {"primaryKey": {"columns": primary}}
    return ("p", "d", name, data)


def test_assertions_are_read_from_a_config_block():
    config = """
    type: "table",
    assertions: {
      nonNull: ["id", 'v'],
      uniqueKey: ["id"],
      uniqueKeys: [["a", "b"], ["c"]],
    }
    """
    non_null, keys = _config_assertions(config)
    assert non_null == ("id", "v")
    assert keys == (("id",), ("a", "b"), ("c",))
    assert _config_assertions('type: "view"') == ((), ())


def test_bigquery_required_and_primary_key_become_facts():
    schema = from_bigquery([_table("t", [("id", "NULLABLE"), ("v", "REQUIRED")], primary=["id"])])
    facts = schema.constraints["p.d.t"]
    assert facts.not_null == {"id", "v"}
    assert facts.keys == (("id",),)
    assert schema.columns["t"] == ["id", "v"]  # every spelling is registered
    assert schema.notes == (DECLARED_FACTS_NOTE,)


def test_bare_name_shared_by_two_tables_is_dropped():
    schema = from_bigquery([
        ("p", "d1", "t", {"schema": [{"name": "a", "mode": "REQUIRED"}]}),
        ("p", "d2", "t", {"schema": [{"name": "b", "mode": "REQUIRED"}]}),
    ])
    assert "t" not in schema.columns
    assert schema.columns["d1.t"] == ["a"]


def test_tables_without_facts_add_no_note():
    schema = from_bigquery([_table("t", [("id", "NULLABLE")])])
    assert schema.notes == ()


def test_dataform_models_contribute_columns_and_assertions():
    model = Model(
        Target("p", "d", "t"), "table", "SELECT id, v FROM src", None, (), (), (), ("id",), (("id",),)
    )
    star = Model(Target("p", "d", "s"), "table", "SELECT * FROM src")
    schema = from_pipeline(SimpleNamespace(models={"p.d.t": model, "p.d.s": star}, source_schema={}))
    assert schema.columns["d.t"] == ["id", "v"]
    assert schema.constraints["t"].keys == (("id",),)
    assert "s" not in schema.columns  # a star hides the columns


def test_pipeline_type_inference_flows_through_upstream_models():
    source = {"id": "INT64", "payload": "STRUCT<id INT64>", "labels": "ARRAY<STRING>"}
    first = Model(
        Target("p", "d", "first"),
        "table",
        "SELECT id + 1 AS next_id, payload AS obj, labels FROM p.d.source",
    )
    second = Model(
        Target("p", "d", "second"),
        "view",
        "SELECT next_id + obj.id AS total, ARRAY_LENGTH(labels) AS label_count FROM p.d.first",
    )
    pipeline = SimpleNamespace(
        models={"p.d.first": first, "p.d.second": second},
        source_schema={"p.d.source": source},
        topological_order=lambda: ["p.d.first", "p.d.second"],
        cyclic_models=lambda: [],
    )

    inferred = infer_pipeline(pipeline)
    assert [(c.name, c.type.sql() if c.type else None) for c in inferred["p.d.first"].columns] == [
        ("next_id", "INT64"),
        ("obj", "STRUCT<id INT64>"),
        ("labels", "ARRAY<STRING>"),
    ]
    assert [(c.name, c.type.sql() if c.type else None) for c in inferred["p.d.second"].columns] == [
        ("total", "INT64"),
        ("label_count", "INT64"),
    ]

    schema = from_pipeline(pipeline)
    assert schema.types["p.d.first"] == {"next_id": "INT64", "obj": "STRUCT<id INT64>", "labels": "ARRAY<STRING>"}
    assert schema.types["p.d.second"] == {"total": "INT64", "label_count": "INT64"}


def test_pipeline_type_inference_does_not_propagate_skipped_models():
    skipped = Model(Target("p", "d", "skipped"), "table", "SELECT id FROM p.d.source", disabled=True)
    downstream = Model(Target("p", "d", "downstream"), "view", "SELECT * FROM p.d.skipped")
    pipeline = SimpleNamespace(
        models={"p.d.skipped": skipped, "p.d.downstream": downstream},
        source_schema={"p.d.source": {"id": "INT64"}},
        topological_order=lambda: ["p.d.skipped", "p.d.downstream"],
        cyclic_models=lambda: [],
    )

    inferred = infer_pipeline(pipeline)
    assert "p.d.skipped" not in inferred
    assert inferred["p.d.downstream"].columns is None


def test_pipeline_type_inference_uses_saved_nested_bigquery_fields():
    model = Model(
        Target("p", "d", "summary"),
        "view",
        "SELECT ARRAY_LENGTH(tags) AS n, payload.id AS id FROM p.d.source",
    )
    pipeline = SimpleNamespace(
        models={"p.d.summary": model},
        source_schema={},
        topological_order=lambda: ["p.d.summary"],
        cyclic_models=lambda: [],
    )
    metadata = {
        "schema": [
            {"name": "tags", "type": "STRING", "mode": "REPEATED"},
            {"name": "payload", "type": "RECORD", "mode": "NULLABLE", "fields": [{"name": "id", "type": "INT64"}]},
        ]
    }

    schema = from_pipeline(pipeline, [("p", "d", "source", metadata)])
    assert schema.types["p.d.summary"] == {"n": "INT64", "id": "INT64"}


def test_current_schema_cache_invalidates_when_saved_catalog_schema_is_replaced(monkeypatch):
    from kumosql import bigquery_catalog, live_graph

    pipeline = SimpleNamespace(models={}, source_schema={})
    tables = [("p", "d", "t", {"schema": [{"name": "id", "type": "INT64", "mode": "NULLABLE"}]})]
    monkeypatch.setattr(prover_context, "_CACHE", {})
    monkeypatch.setattr(live_graph, "loaded", lambda: {"pipeline": pipeline})
    monkeypatch.setattr(bigquery_catalog, "saved_tables", lambda: tables)

    assert prover_context.current_schema().types["p.d.t"]["id"] == "BIGINT"
    tables[:] = [("p", "d", "t", {"schema": [{"name": "id", "type": "STRING", "mode": "NULLABLE"}]})]
    assert "id" not in prover_context.current_schema().types.get("p.d.t", {})


def test_declared_key_lets_the_solver_prove_a_self_join_away():
    keyed = from_bigquery([_table("t", [("id", "REQUIRED"), ("v", "NULLABLE")], primary=["id"])])
    plain = from_bigquery([_table("t", [("id", "NULLABLE"), ("v", "NULLABLE")])])

    proven = prover_context.prove(SELF_JOIN, PLAIN, schema=keyed)
    assert proven.status is SmtStatus.PROVEN_EQUIVALENT
    assert DECLARED_FACTS_NOTE in proven.assumptions
    assert prover_context.prove(SELF_JOIN, PLAIN, schema=plain).status is not SmtStatus.PROVEN_EQUIVALENT


def test_settings_default_validate_and_persist(monkeypatch):
    store = {}
    monkeypatch.setattr(prover_context.state, "get_section", lambda name, default=None: store.get(name, default))
    monkeypatch.setattr(prover_context.state, "set_section", lambda name, value: store.__setitem__(name, value))
    assert prover_context.settings() == {"enabled": True, "timeout_ms": 5000, "bounded_rows": 3}
    assert prover_context.save_settings(enabled=False, timeout_ms=2000) == {"enabled": False, "timeout_ms": 2000, "bounded_rows": 3}
    assert prover_context.settings() == {"enabled": False, "timeout_ms": 2000, "bounded_rows": 3}
    assert prover_context.save_settings(bounded_rows=0)["bounded_rows"] == 0
    for bad in ({"enabled": "yes"}, {"timeout_ms": 5}, {"timeout_ms": True}):
        with pytest.raises(ValueError):
            prover_context.save_settings(**bad)


def test_bigquery_foreign_keys_reach_the_prover():
    from kumosql.prover_schema import from_bigquery

    schema = from_bigquery(
        [
            ("p", "d", "customers", {"schema": [{"name": "id", "mode": "REQUIRED"}], "constraints": {"primaryKey": {"columns": ["id"]}}}),
            (
                "p",
                "d",
                "orders",
                {
                    "schema": [{"name": "id", "mode": "REQUIRED"}, {"name": "customer_id", "mode": "REQUIRED"}],
                    "constraints": {
                        "primaryKey": {"columns": ["id"]},
                        "foreignKeys": [
                            {
                                "referencedTable": {"projectId": "p", "datasetId": "d", "tableId": "customers"},
                                "columnReferences": [{"referencingColumn": "customer_id", "referencedColumn": "id"}],
                            }
                        ],
                    },
                },
            ),
        ]
    )
    assert schema.constraints["d.orders"].foreign_keys == ((("customer_id",), "p.d.customers", ("id",)),)

    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    result = prove_equivalent_algebraic(
        "SELECT o.id FROM d.orders AS o JOIN d.customers AS c ON o.customer_id = c.id",
        "SELECT id FROM d.orders",
        schema=schema.columns,
        constraints=schema.constraints,
    )
    assert result.proven, result.reason
