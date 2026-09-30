from kumosql import Pipeline, Target, load_compiled_graph
from kumosql.pipeline import Model
from kumosql.shared_logic import propose_shared_logic, proposals_json

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
    (proposal,) = propose_shared_logic(load_compiled_graph(exact_graph()))

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
    (proposal,) = propose_shared_logic(load_compiled_graph(exact_graph()))
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
