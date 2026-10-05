"""Nested data (ARRAY, STRUCT, UNNEST): the pairs of tests/fixtures/nested_data scored by tools/nested_data_bench.py.

Floors only ever go up. They are the development-split numbers measured on 2026-10-05, before any prover rule
for nested data existed (6/65 proofs and 33/47 refutations over all pairs, 0 wrong). The held-out quarter is
run, because a wrong answer anywhere is a bug, but no floor is set from its numbers. The 112 pairs are split
over four groups so the work spreads over test workers.
"""

from pathlib import Path
import sys

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import nested_data_bench as bench  # noqa: E402

GROUPS = 4
# development pairs proved / refuted per group, measured 2026-10-05: 1/6, 1/8, 2/7 and 0/7 (4 and 28 in all). The
# counterexample search works to a time budget, so a loaded machine can lose one refutation: the refutation floors
# are one below the measurement, the proof floors are the measurement.
FLOORS = {0: (1, 5), 1: (1, 7), 2: (2, 6), 3: (0, 6)}

FIXTURE = bench.load()


def _group(g: int) -> list:
    return [p for i, p in enumerate(FIXTURE.pairs) if i % GROUPS == g]


@pytest.fixture(scope="module", params=range(GROUPS))
def group(request):
    pairs = _group(request.param)
    return request.param, {row["id"]: row for row in bench.run(pairs, FIXTURE.schemas)}


def test_the_fixture_shape():
    pairs = FIXTURE.pairs
    assert len(pairs) == 112
    assert len({p.id for p in pairs}) == len(pairs)
    assert sum(p.equivalent for p in pairs) == 65
    assert sum(not p.equivalent for p in pairs) == 47
    assert {p.label for p in pairs} == {"equivalent", "different"}
    assert sum(p.held_out for p in pairs) == 26
    # every pair names a schema set that has stored databases, and BigQuery's recorded differences point at them
    for pair in pairs:
        stored = FIXTURE.datasets[pair.schema]
        assert all(0 <= i < len(stored) for i in pair.bigquery_differs), pair.id
        if pair.equivalent:
            assert not pair.bigquery_differs, pair.id
        elif not pair.nondeterministic:
            assert pair.bigquery_differs, pair.id


def test_the_labels_agree_with_the_stored_databases():
    # DuckDB, through the BigQuery translation, sees the differences BigQuery returned (see the fixture README)
    assert bench.check_labels(FIXTURE) == []


def test_nothing_is_wrong(group):
    _, rows = group
    assert [i for i, row in rows.items() if row["wrong"]] == []


def test_a_trap_is_never_proved_and_an_equivalent_pair_never_refuted(group):
    _, rows = group
    assert [i for i, r in rows.items() if r["label"] == "different" and r["outcome"] == "proven"] == []
    assert [i for i, r in rows.items() if r["label"] == "equivalent" and r["outcome"] == "refuted"] == []


def test_the_development_floors(group):
    index, rows = group
    dev = [r for r in rows.values() if not r["held_out"]]
    proofs = sum(r["outcome"] == "proven" for r in dev if r["label"] == "equivalent")
    refutations = sum(r["outcome"] == "refuted" for r in dev if r["label"] == "different")
    proof_floor, refutation_floor = FLOORS[index]
    assert proofs >= proof_floor, f"group {index}: {proofs} development proofs, floor {proof_floor}"
    assert refutations >= refutation_floor, f"group {index}: {refutations} development refutations, floor {refutation_floor}"


# Traps the counterexample search refutes by building ARRAY and STRUCT databases: each needs a row shape the
# default databases do not have. The pairs are written for this test, not taken from the fixture.


def _pair(id, left, right, schema, label="different"):
    return bench.Pair(id=id, family="regression", schema=schema, label=label, left=left, right=right)


REGRESSIONS = {
    # a repeated key: the MAX lookup returns one row per event, the LEFT JOIN one row per matching element
    "max-lookup-vs-left-join-on-key": _pair(
        "max-lookup-vs-left-join-on-key",
        "SELECT e.event_name, (SELECT MAX(value.int_value) FROM UNNEST(e.event_params) WHERE key = 'ga_session_id') AS v FROM events AS e",
        "SELECT e.event_name, p.value.int_value AS v FROM events AS e LEFT JOIN UNNEST(e.event_params) AS p ON p.key = 'ga_session_id'",
        "ga4",
    ),
    # an empty array: the subscript gives a row with NULL, the cross join with the offset gives no row
    "safe-offset-vs-unnest-offset-cross-join": _pair(
        "safe-offset-vs-unnest-offset-cross-join",
        "SELECT event_id, contexts_web_page[SAFE_OFFSET(0)].id AS v FROM events",
        "SELECT e.event_id, c.id AS v FROM events AS e, UNNEST(e.contexts_web_page) AS c WITH OFFSET AS o WHERE o = 0",
        "snowplow",
    ),
    # a one-element array: more than one element is not the same as any element
    "length-over-one-vs-exists": _pair(
        "length-over-one-vs-exists",
        "SELECT event_id FROM events WHERE ARRAY_LENGTH(contexts_web_page) > 1",
        "SELECT event_id FROM events AS e WHERE EXISTS (SELECT 1 FROM UNNEST(e.contexts_web_page))",
        "snowplow",
    ),
}


@pytest.mark.parametrize("name", sorted(REGRESSIONS))
def test_the_search_refutes_the_trap(name):
    pair = REGRESSIONS[name]
    outcome, reason = bench.prove(pair, FIXTURE.schemas)
    assert outcome == "refuted", reason
    assert reason == "counterexample search"


def test_an_equivalent_pair_over_an_array_with_no_null_element_is_not_refuted():
    # a stored array holds no NULL element, so NOT 3 IN UNNEST(arr) and NOT EXISTS agree on every database
    pair = _pair(
        "not-in-unnest-vs-not-exists",
        "SELECT customer_id FROM customers WHERE NOT 3 IN UNNEST(scores)",
        "SELECT customer_id FROM customers AS c WHERE NOT EXISTS (SELECT 1 FROM UNNEST(c.scores) AS s WHERE s = 3)",
        "shop",
        label="equivalent",
    )
    outcome, _ = bench.prove(pair, FIXTURE.schemas)
    assert outcome != "refuted"


def test_a_fixture_pair_the_search_refutes_stays_refuted():
    by_id = {p.id: p for p in FIXTURE.pairs}
    assert not by_id["ga4-max-lookup-vs-left-join"].held_out
    assert bench.prove(by_id["ga4-max-lookup-vs-left-join"], FIXTURE.schemas)[0] == "refuted"


def test_the_results_rows_carry_the_split_and_the_honesty_fields():
    rows = [
        {"id": "a", "family": "f", "label": "equivalent", "outcome": "proven", "reason": "", "wrong": False, "held_out": False},
        {"id": "b", "family": "f", "label": "equivalent", "outcome": "unknown", "reason": "", "wrong": False, "held_out": True},
        {"id": "c", "family": "f", "label": "different", "outcome": "refuted", "reason": "", "wrong": False, "held_out": False},
        {"id": "d", "family": "f", "label": "different", "outcome": "unknown", "reason": "", "wrong": False, "held_out": True},
    ]
    results = bench.results_rows(rows, "caveat")
    assert results["nested-data-proof"]["score"] == "1/2, 0 wrong"
    assert results["nested-data-executed"]["score"] == "1/2, 0 wrong"
    assert results["nested-data-proof"]["held_out"] == "0/1 proved, 0 wrong (dev 1/1)"
    assert results["nested-data-executed"]["held_out"] == "0/1 refuted, 0 wrong (dev 1/1)"
