from kumosql.proposal_readiness import (
    UNRESOLVED_CONSUMERS,
    ConsumerResult,
    assess_proposal,
    verify_consumer,
    verify_consumers,
)


def _proposal(labels, **extra):
    return {"id": "p", "consumers": [{"node": f"m.c{i}"} for i in range(len(labels))], **extra}


def _results(labels):
    return {f"m.c{i}": label for i, label in enumerate(labels)}


def _labels(out):
    return [c["label"] for c in out["consumers"]]


def test_ready_when_all_proven_or_unchanged():
    labels = ["proven", "unchanged", "proven"]
    out = assess_proposal(_proposal(labels), _results(labels))
    assert out["ready"] is True and out["not_ready_reasons"] == []


def test_planner_checked_unproven_failed_block_readiness():
    for bad in ("planner_checked", "unproven", "failed"):
        labels = ["proven", bad]
        out = assess_proposal(_proposal(labels), _results(labels))
        assert out["ready"] is False
        assert _labels(out) == ["proven", bad]


def test_missing_result_is_unknown():
    out = assess_proposal(_proposal(["a", "b"]), {"m.c0": "proven"})
    assert _labels(out) == ["proven", "unknown"]
    assert out["ready"] is False
    assert "m.c1" in out["not_ready_reasons"][0]


def test_missing_or_empty_consumer_list_is_unknown():
    for proposal in ({"id": "p"}, {"id": "p", "consumers": None}, {"id": "p", "consumers": []}):
        out = assess_proposal(proposal, {})
        assert out["ready"] is False
        assert out["consumers"] == [{"node": UNRESOLVED_CONSUMERS, "label": "unknown"}]


def test_incomplete_consumer_list_never_ready():
    labels = ["proven", "proven"]
    out = assess_proposal(_proposal(labels, consumers_complete=False), _results(labels))
    assert out["ready"] is False
    assert _labels(out) == ["proven", "proven", "unknown"]
    assert "consumers_complete" not in out


def test_unrecognized_label_is_unknown():
    out = assess_proposal(_proposal(["x"]), {"m.c0": "great"})
    assert _labels(out) == ["unknown"] and out["ready"] is False


def test_embedded_labels_and_result_objects():
    proposal = {"id": "p", "consumers": [{"node": "a", "label": "proven"}, "b"]}
    out = assess_proposal(proposal, {"b": ConsumerResult("b", "unchanged")})
    assert out["ready"] is True


def test_verify_consumer_labels():
    assert verify_consumer("SELECT a FROM t", "SELECT a FROM t").label == "unchanged"
    assert verify_consumer("SELECT a FROM t", "select a from t").label in ("proven", "unchanged")
    assert verify_consumer("SELECT a FROM t", "SELECT b FROM t").label == "unproven"
    assert verify_consumer("SELECT a FROM t", None).label == "unknown"
    assert verify_consumer("", "SELECT 1").label == "unknown"


def test_verify_consumers_missing_query_is_unknown_and_blocks_ready():
    proposal = {"id": "p", "consumers": [{"node": "a"}, {"node": "b"}]}
    results = verify_consumers(proposal, {"a": ("SELECT 1 AS x", "SELECT 1 AS x")})
    assert results["a"].label == "unchanged" and results["b"].label == "unknown"
    assert assess_proposal(proposal, results)["ready"] is False


def test_ready_agrees_with_the_gate():
    for labels, ready in ((["proven", "proven", "planner_checked", "unknown"], False),
                          (["proven", "unchanged", "proven"], True)):
        proposal = {"id": "p", "consumers": [{"node": f"n{i}", "label": label} for i, label in enumerate(labels)]}
        assert assess_proposal(proposal)["ready"] is ready
