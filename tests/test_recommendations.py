from kumosql import ObservedRead, Pipeline, Target, build_query_graph
from kumosql.pipeline import Model
from kumosql.recommendations import (
    RecommendationInput,
    RepeatLocation,
    build_recommendation,
)
from kumosql.rewrite import Verification, VerificationStatus


def t(name):
    return Target("P", "D", name)


def make_pipeline():
    a, b, c = t("a"), t("b"), t("c")
    return Pipeline(
        {
            a.key: Model(a, "table", "SELECT 1 AS id"),
            b.key: Model(b, "view", f"SELECT id FROM `{a.key}`", declared_dependencies=(a,)),
            c.key: Model(c, "view", f"SELECT id FROM `{b.key}`", declared_dependencies=(b,)),
        }
    ), a, b, c


def full_spec(a):
    return RecommendationInput(
        title="Shared step",
        repeats=(RepeatLocation(a.key, "cte:x"),),
        proposed_change="Move the step upstream.",
        rule="some_rule",
        required_label=VerificationStatus.PROVEN,
        verification_plan=("prove each consumer",),
    )


def test_all_four_parts_and_transitive_consumers():
    p, a, b, c = make_pipeline()
    rec = build_recommendation(
        full_spec(a),
        graph=build_query_graph(p),
        verification=Verification(VerificationStatus.UNPROVEN, "not run"),
    )
    j = rec.to_json()
    assert j["consumers"] == sorted([b.key, c.key])
    assert j["proposed_change"] == "Move the step upstream."
    assert j["rule"] == "some_rule"
    assert j["verification"] == {
        "required": "proven", "plan": ["prove each consumer"], "plan_known": True,
    }
    assert j["evidence"]["label"] == "unproven"
    assert j["repeats"] == [{"node": a.key, "where": "cte:x"}]
    # static graph only: never claims completeness
    assert j["consumers_completeness"] == "partial"
    assert any("observed" in g for g in j["consumer_gaps"])
    text = rec.to_text()
    for heading in ("Where the work repeats", "Who relies on it", "Proposed change",
                    "How it would be verified"):
        assert heading in text


def test_missing_inputs_render_unknown():
    rec = build_recommendation(RecommendationInput())
    j = rec.to_json()
    assert j["repeats"] == "unknown"
    assert j["consumers"] == "unknown"
    assert j["consumers_completeness"] == "unknown"
    assert j["proposed_change"] == "unknown"
    assert j["rule"] == "unknown"
    assert j["verification"]["required"] == "unknown"
    assert j["verification"]["plan"] == []
    assert j["evidence"]["label"] == "unknown"
    assert rec.to_text().count("unknown") >= 6


def test_repeat_node_not_in_graph_is_unknown_not_empty():
    p, a, b, c = make_pipeline()
    spec = RecommendationInput(repeats=(RepeatLocation("nope"),))
    j = build_recommendation(spec, graph=build_query_graph(p)).to_json()
    assert j["consumers"] == "unknown"
    assert "repeat node not in graph: nope" in j["consumer_gaps"]
    assert j["repeats"][0]["where"] == "unknown"


def test_unresolved_observation_is_listed_as_gap():
    p, a, b, c = make_pipeline()
    graph = build_query_graph(
        p,
        [ObservedRead(job_id="j", creation_time="2025-01-01T00:00:00Z",
                      destination=c.key, referenced_tables=("other.missing.tbl",))],
    )
    j = build_recommendation(full_spec(a), graph=graph).to_json()
    assert any(g.startswith("unresolved_observation") for g in j["consumer_gaps"])


def test_no_label_stronger_than_supplied():
    p, a, *_ = make_pipeline()
    j = build_recommendation(full_spec(a), graph=build_query_graph(p)).to_json()
    assert j["evidence"]["label"] == "unknown"
