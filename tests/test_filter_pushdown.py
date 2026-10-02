import pytest

from kumosql import load_compiled_graph
from kumosql.filter_pushdown import find_upstream_filter_proposals


def table(name, query, kind="table", deps=()):
    return {
        "target": {"database": "p", "schema": "d", "name": name},
        "type": kind,
        "query": query,
        "fileName": f"definitions/{name}.sqlx",
        "dependencyTargets": [{"database": "p", "schema": "d", "name": dep} for dep in deps],
    }


def build(*tables, extra=None):
    graph = {
        "tables": list(tables),
        "declarations": [{"target": {"database": "p", "schema": "d", "name": "raw"}}],
    }
    graph.update(extra or {})
    return load_compiled_graph(graph)


STG = table("stg", "SELECT id, amount AS amt, status FROM `p.d.raw`")


def reader(name, where, source="stg"):
    return table(name, f"SELECT id FROM `p.d.{source}` AS s WHERE {where}")


def run(*tables, **kw):
    return find_upstream_filter_proposals(build(*tables, **kw))


def reasons(result):
    return {r.model: r.reason for r in result.refusals}


def test_common_filter_is_proposed_with_full_consumer_set():
    result = run(STG, reader("a", "s.status = 'paid'"), reader("b", "s.status = 'paid' AND s.id > 3"))
    assert [p.predicate for p in result.proposals] == ["status = 'paid'"]
    data = result.proposals[0].to_json()
    assert data["kind"] == "upstream_filter"
    assert data["ready"] is False
    assert data["cost_rationale"].startswith("Unknown")
    assert {c["node"] for c in data["consumers"]} == {"table:p.d.a", "table:p.d.b"}
    assert all(c["label"] == "unproven" for c in data["consumers"])


def test_output_alias_is_mapped_to_source_column():
    # `amt > 0` and `0 < amt` are only recognised as the same filter by the SMT prover.
    pytest.importorskip("z3")
    result = run(STG, reader("a", "s.amt > 0"), reader("b", "0 < s.amt"))
    assert [p.predicate for p in result.proposals] == ["amount > 0"]


def test_absent_or_different_filter_is_refused():
    result = run(STG, reader("a", "s.status = 'paid'"), reader("b", "s.status = 'open'"))
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "no_common_filter"
    result = run(STG, reader("a", "s.status = 'paid'"), table("b", "SELECT id FROM `p.d.stg`"))
    assert not result.proposals


def test_unparsed_consumer_refuses():
    result = run(STG, reader("a", "s.status = 'paid'"), table("b", "SELECT FROM WHERE (", deps=["stg"]))
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "consumer_unparsed"


def test_unknown_observed_consumer_refuses():
    pipeline = build(STG, reader("a", "s.status = 'paid'"))
    result = find_upstream_filter_proposals(
        pipeline, [{"job_id": "j", "destination": "p.other.out", "referenced_tables": ["p.d.stg"]}]
    )
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "unknown_consumer"


def test_an_operation_that_reads_the_model_is_a_consumer_that_cannot_be_checked():
    reads = {"target": {"database": "p", "schema": "d", "name": "op"}, "queries": ["SELECT 1 FROM `p.d.stg`"]}
    result = run(STG, reader("a", "s.status = 'paid'"), extra={"operations": [reads]})
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "consumer_unparsed"


def test_blind_graph_refuses():
    blind = {"target": {"database": "p", "schema": "d", "name": "op"}, "queries": ["EXECUTE IMMEDIATE FORMAT('SELECT 1 FROM %s', 'x' || CAST(1 AS STRING))"]}
    result = run(STG, reader("a", "s.status = 'paid'"), extra={"operations": [blind]})
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "graph_incomplete"


def test_outer_join_nullable_side_is_not_treated_as_filtered():
    left = table(
        "a",
        "SELECT o.id FROM `p.d.raw` AS o LEFT JOIN `p.d.stg` AS s ON o.id = s.id WHERE s.status = 'paid'",
    )
    result = run(STG, left)
    assert not result.proposals
    assert reasons(result)["p.d.stg"] == "unsupported_read"


def test_filter_on_joined_reader_alias_only():
    joined = table(
        "a",
        "SELECT s.id FROM `p.d.stg` AS s JOIN `p.d.raw` AS o ON s.id = o.id WHERE s.status = 'paid' AND o.x = 1",
    )
    result = run(STG, joined)
    assert [p.predicate for p in result.proposals] == ["status = 'paid'"]


def test_aggregating_target_and_computed_column_refuse():
    agg = table("stg", "SELECT status, COUNT(*) AS n FROM `p.d.raw` GROUP BY status")
    assert reasons(run(agg, reader("a", "s.status = 'x'"))) == {"p.d.stg": "unsafe_target"}
    computed = table("stg", "SELECT id, amount * 2 AS amt FROM `p.d.raw`")
    assert reasons(run(computed, reader("a", "s.amt > 1"))) == {"p.d.stg": "unmappable_filter"}


def test_nondeterministic_predicate_is_never_proposed():
    result = run(STG, reader("a", "s.amt > RAND()"), reader("b", "s.amt > RAND()"))
    assert not result.proposals


def test_incremental_target_refuses_and_star_passthrough_maps():
    inc = table("stg", "SELECT * FROM `p.d.raw`", kind="incremental")
    assert reasons(run(inc, reader("a", "s.x = 1")))["p.d.stg"] == "unsupported_target"
    star = table("stg", "SELECT * FROM `p.d.raw`")
    pipeline = build(star, reader("a", "s.x = 1"), reader("b", "s.x = 1"))
    pipeline.source_schema["p.d.raw"] = {"x": "INT64", "id": "INT64"}
    pipeline._analysis = None
    result = find_upstream_filter_proposals(pipeline)
    assert [p.predicate for p in result.proposals] == ["x = 1"]


def test_filter_already_in_target_is_not_proposed():
    stg = table("stg", "SELECT id, status FROM `p.d.raw` WHERE status = 'paid'")
    assert not run(stg, reader("a", "s.status = 'paid'")).proposals
