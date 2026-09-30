import pytest

from kumosql.graph import GraphEdge, GraphResult
from kumosql.opportunities import (
    OpportunityInput,
    downstream_reach,
    frequency_label,
    rank_opportunities,
)


def opp(id, value=None, basis=None, **kw):
    return OpportunityInput(id=id, title=id, savings_value=value, savings_basis=basis, **kw)


def ids(entries):
    return [e["id"] for e in entries]


def test_basis_tiers_are_not_blended():
    out = rank_opportunities([
        opp("ub", 1000.0, "upper_bound"),
        opp("est", 500.0, "estimate"),
        opp("meas", 10.0, "measured"),
    ])
    assert ids(out) == ["meas", "est", "ub"]
    assert [e["rank"] for e in out] == [1, 2, 3]
    assert [e["savings"]["basis"] for e in out] == ["measured", "estimate", "upper_bound"]


def test_within_basis_sorted_by_value_then_runs_reach_id():
    out = rank_opportunities([
        opp("d", 5.0, "estimate", runs=1, downstream_reach=9),
        opp("c", 5.0, "estimate", runs=10, downstream_reach=0),
        opp("b", 5.0, "estimate", runs=10, downstream_reach=3),
        opp("a", 5.0, "estimate", runs=10, downstream_reach=3),
        opp("top", 6.0, "estimate"),
    ])
    assert ids(out) == ["top", "a", "b", "c", "d"]


def test_unpriced_listed_last_with_null_savings():
    out = rank_opportunities([opp("x", downstream_reach=5), opp("y", 1.0, "upper_bound")])
    assert ids(out) == ["y", "x"]
    assert out[1]["savings"] is None
    assert out[1]["frequency"] == "unknown"


def test_empty_and_range_shape():
    assert rank_opportunities([]) == []
    out = rank_opportunities([opp("r", 5.0, "estimate", savings_range=(2.0, 8.0))])
    assert out[0]["savings"] == {"value": 5.0, "basis": "estimate", "range": [2.0, 8.0]}
    assert set(out[0]) >= {"rank", "title", "savings", "measured_cost", "frequency", "downstream_reach"}


@pytest.mark.parametrize("kwargs", [
    dict(savings_value=1.0, savings_basis=None),
    dict(savings_value=1.0, savings_basis="unknown"),
    dict(savings_value=None, savings_basis="measured"),
    dict(savings_value=-1.0, savings_basis="measured"),
    dict(savings_value=9.0, savings_basis="estimate", savings_range=(1.0, 2.0)),
])
def test_invalid_inputs_rejected(kwargs):
    with pytest.raises(ValueError):
        OpportunityInput(id="i", title="t", **kwargs)


def test_frequency_label():
    assert frequency_label(30, 30) == "daily"
    assert frequency_label(4, 28) == "weekly"
    assert frequency_label(1, 30) == "monthly"
    assert frequency_label(None, 30) == "unknown"
    assert frequency_label(2, 365) == "2 runs in 365 days"


class _Id:
    def __init__(self, key):
        self.stable_key = key


def _edge(a, b):
    return GraphEdge(upstream=_Id(a), downstream=_Id(b), declared=True)


def test_downstream_reach_is_transitive_and_cycle_safe():
    graph = GraphResult(
        nodes=(), edges=(_edge("a", "b"), _edge("b", "c"), _edge("a", "c"), _edge("c", "a"))
    )
    assert downstream_reach(graph, "a") == 2
    assert downstream_reach(graph, "z") == 0
