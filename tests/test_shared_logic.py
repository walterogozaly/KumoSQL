from kumosql import Pipeline, Target, load_compiled_graph
from kumosql.pipeline import Model
from dataclasses import replace

from kumosql.shared_logic import propose_shared_logic, proposals_json, refactor_sql, verify_proposal

BASE = (
    "SELECT o.id, o.customer_id, o.amount, o.created_at "
    "FROM `proj.raw.orders` AS o JOIN `proj.raw.customers` AS c ON o.customer_id = c.id "
    "WHERE o.amount > 0"
)

DECLARATIONS = [
    {"target": {"database": "proj", "schema": "raw", "name": "orders"}},
    {"target": {"database": "proj", "schema": "raw", "name": "customers"}},
]


def table(name, query, kind="table"):
    return {
        "target": {"database": "proj", "schema": "mart", "name": name},
        "type": kind,
        "query": query,
        "fileName": f"definitions/{name}.sqlx",
    }


def exact_graph(extra=()):
    return {
        "tables": [
            table("a", f"WITH base AS ({BASE}) SELECT id FROM base"),
            table("b", f"WITH base AS ({BASE}) SELECT amount FROM base"),
            table("down_a", "SELECT id FROM `proj.mart.a`"),
            table("down_down", "SELECT id FROM `proj.mart.down_a`"),
            table("unrelated", "SELECT 1 AS x"),
            *extra,
        ],
        "declarations": DECLARATIONS,
    }


def by_node(proposal):
    return {c.node: c for c in proposal.consumers}


def test_exact_duplicate_lists_edit_sites_and_transitive_readers():
    (proposal,) = propose_shared_logic(load_compiled_graph(exact_graph()), verify=False)

    assert proposal.origin == "exact_duplicate"
    assert set(proposal.sites) == {("proj.mart.a", "cte:base"), ("proj.mart.b", "cte:base")}
    consumers = by_node(proposal)
    assert set(consumers) == {"proj.mart.a", "proj.mart.b", "proj.mart.down_a", "proj.mart.down_down"}
    assert consumers["proj.mart.a"].role == "edit_site"
    assert consumers["proj.mart.down_down"].role == "downstream"
    assert consumers["proj.mart.down_down"].via == ("proj.mart.a",)
    assert proposal.consumers_complete
    assert proposal.ready is False


def test_json_shape_never_invents_cost_or_readiness():
    (proposal,) = propose_shared_logic(load_compiled_graph(exact_graph()), verify=False)
    data = proposals_json([proposal])["proposals"][0]

    assert data["kind"] == "shared_logic"
    assert data["cost_rationale"] == "unknown"
    assert data["ready"] is False
    assert all(c["label"] == "unknown" for c in data["consumers"])
    assert {"id", "title", "consumers", "shared_sql"} <= set(data)


def test_unparseable_model_marks_consumer_list_incomplete():
    graph = exact_graph([table("broken", "SELECT FROM WHERE ((")])
    (proposal,) = propose_shared_logic(load_compiled_graph(graph))

    assert not proposal.consumers_complete
    assert any("broken" in reason for reason in proposal.incomplete_reasons)
    assert proposals_json([proposal])["proposals"][0]["consumers_complete"] is False


def test_observed_reader_outside_the_project_is_listed():
    pipeline = load_compiled_graph(exact_graph())
    observed = [
        {
            "job_id": "j1",
            "creation_time": "2026-09-01T00:00:00Z",
            "destination": "proj.reporting.dash",
            "referenced_tables": ["proj.mart.a"],
        }
    ]
    (proposal,) = propose_shared_logic(pipeline, observed)

    assert "proj.reporting.dash" in by_node(proposal)
    assert proposal.observed_reads_included


def test_near_duplicate_cluster_with_extra_filter_gets_proposal_with_residuals():
    graph = {
        "tables": [
            table("a", f"WITH base AS ({BASE}) SELECT id FROM base"),
            table("b", f"WITH us AS ({BASE} AND c.country = 'US') SELECT id FROM us"),
            table("reader", "SELECT id FROM `proj.mart.b`"),
        ],
        "declarations": DECLARATIONS,
    }
    (proposal,) = propose_shared_logic(load_compiled_graph(graph))

    assert proposal.origin != "exact_duplicate"
    assert set(by_node(proposal)) == {"proj.mart.a", "proj.mart.b", "proj.mart.reader"}
    assert proposal.residual_filters == {("proj.mart.b", "cte:us"): ("country = 'US'",)}


def test_no_repeated_logic_gives_no_proposals():
    pipeline = Pipeline({"p.d.x": Model(Target("p", "d", "x"), "table", "SELECT 1 AS a")})
    assert propose_shared_logic(pipeline) == []


def test_reader_cycle_terminates():
    graph = exact_graph(
        [
            table("c1", "SELECT id FROM `proj.mart.c2`"),
            table("c2", "SELECT id FROM `proj.mart.c1` UNION ALL SELECT id FROM `proj.mart.a`"),
        ]
    )
    (proposal,) = propose_shared_logic(load_compiled_graph(graph))
    assert {"proj.mart.c1", "proj.mart.c2"} <= set(by_node(proposal))


def test_exact_duplicate_refactor_is_proven_and_ready():
    (proposal,) = propose_shared_logic(load_compiled_graph(exact_graph()))

    assert proposal.ready
    data = proposals_json([proposal])["proposals"][0]
    assert data["ready"] is True
    assert {c["node"]: c["label"] for c in data["consumers"]} == {
        "proj.mart.a": "proven",
        "proj.mart.b": "proven",
        "proj.mart.down_a": "unchanged",
        "proj.mart.down_down": "unchanged",
    }


def test_incomplete_reader_list_keeps_a_proven_refactor_not_ready():
    graph = exact_graph([table("broken", "SELECT FROM WHERE ((")])
    (proposal,) = propose_shared_logic(load_compiled_graph(graph))

    assert not proposal.ready
    assert any("incomplete" in reason for reason in proposal.unready_reasons)


def test_copies_that_read_a_cte_the_model_defines_are_not_ready():
    def model(name, base_filter):
        return table(
            name,
            "WITH base AS (SELECT o.id, o.amount, o.status FROM `proj.raw.orders` AS o "
            f"WHERE {base_filter}), agg AS (SELECT b.id, b.amount FROM base AS b "
            "WHERE b.amount > 5 AND b.status = 'paid' AND b.id > 0) SELECT id FROM agg",
        )

    graph = {"tables": [model("a", "o.amount > 0"), model("b", "o.amount > 0")], "declarations": DECLARATIONS}
    proposals = propose_shared_logic(load_compiled_graph(graph), min_nodes=8)

    assert proposals
    assert not any(p.ready for p in proposals if any("agg" in s[1] for s in p.sites))


def test_near_duplicate_refactor_applies_residual_filters_and_is_verified():
    graph = {
        "tables": [
            table("a", f"WITH base AS ({BASE}) SELECT id, amount FROM base"),
            table("b", f"WITH us AS ({BASE.replace('o.', 'x.').replace('AS o', 'AS x')} AND c.country = 'US') SELECT id FROM us"),
        ],
        "declarations": DECLARATIONS,
    }
    pipeline = load_compiled_graph(graph)
    proposals = propose_shared_logic(pipeline)
    (proposal,) = [p for p in proposals if p.origin == "extra_filters"]

    after = refactor_sql(proposal, pipeline.models["proj.mart.b"].sql, ("proj.mart.b", "cte:us"))

    assert after is not None and "country = 'US'" in after and "_shared" in after
    assert proposal.ready


def test_a_refactor_that_drops_a_filter_is_never_verified():
    pipeline = load_compiled_graph(exact_graph())
    (proposal,) = propose_shared_logic(pipeline, verify=False)
    broken = replace(proposal, shared_sql=proposal.shared_sql.replace("o.amount > 0", "o.amount > 1"))

    result = verify_proposal(pipeline, broken)
    assert not result.ready
    assert result.verification["proj.mart.a"] != "proven"


LEFT_BASE = (
    "SELECT o.id, o.customer_id, o.amount, o.status, c.country FROM `proj.raw.orders` AS o "
    "LEFT JOIN `proj.raw.customers` AS c ON o.customer_id = c.id WHERE o.amount BETWEEN 10 AND 23 AND o.amount > 1"
)


def test_extra_filter_over_a_left_join_is_proven_by_merging_the_view_back():
    graph = {
        "tables": [
            table("a", f"WITH x AS ({LEFT_BASE}) SELECT id FROM x"),
            table("b", f"WITH y AS ({LEFT_BASE} AND o.status <> 'void') SELECT id FROM y"),
        ],
        "declarations": DECLARATIONS,
    }
    pipeline = load_compiled_graph(graph)
    (proposal,) = [p for p in propose_shared_logic(pipeline) if p.origin == "extra_filters"]

    assert proposal.ready
    broken = replace(proposal, residual_filters={k: ("status = 'open'",) for k in proposal.residual_filters})
    assert not verify_proposal(pipeline, broken).ready


# A scalar subquery can read a column of the query around it; extracted on its own it is not a runnable table.
SCHEMA = {"proj.raw.t": {"x": "INT64"}, "proj.raw.v": {"x": "INT64"}, "proj.raw.u": {"k": "INT64"}}


def scalar_graph(subquery):
    query = "SELECT s.x, (" + subquery + ") AS n FROM `proj.raw.%s` AS s"
    declarations = [{"target": {"database": "proj", "schema": "raw", "name": n}} for n in ("t", "v", "u")]
    return {"tables": [table("a", query % "t"), table("b", query % "v")], "declarations": declarations}


def scalar_proposals(subquery, schema=SCHEMA):
    pipeline = load_compiled_graph(scalar_graph(subquery), source_schema=schema)
    return [p for p in propose_shared_logic(pipeline) if any("subquery" in s[1] for s in p.sites)]


def test_correlated_scalar_subquery_is_not_a_ready_shared_table():
    proposals = scalar_proposals("SELECT COUNT(*) AS n FROM `proj.raw.u` WHERE k = x AND k >= 0")
    assert proposals
    for proposal in proposals:
        assert not proposal.ready
        assert "proven" not in proposal.verification.values()
        assert any("reads x from the query around it" in r for r in proposal.unready_reasons)


def test_correlated_scalar_subquery_without_schemas_is_not_ready():
    proposals = scalar_proposals("SELECT COUNT(*) AS n FROM `proj.raw.u` WHERE k = x AND k >= 0", schema={})
    assert proposals and not any(p.ready for p in proposals)


def test_closed_scalar_subquery_with_known_columns_stays_ready():
    proposals = scalar_proposals("SELECT COUNT(*) AS n FROM `proj.raw.u` WHERE k >= 0")
    assert proposals and all(p.ready for p in proposals)
