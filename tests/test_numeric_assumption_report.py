"""The numeric assumption report: what counts as numeric, how proofs are compared, and a small live collection."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import numeric_assumption_report as report  # noqa: E402

NAN = "FLOAT64 values are never NaN"
ERRORS = "runtime errors (division by zero, overflow, failed casts) are not modeled"
ORDER = "SUM and AVG are treated as independent of row order"
TYPES = "result column types are not compared; confirm schemas with a BigQuery dry run"
EXACT = "+, - and * are exact (no FLOAT64 rounding or INT64 overflow)"
LIMIT = "a LIMIT subquery with the same text returns the same rows each time"


def record(index, proven, labels, left="SELECT 1", right="SELECT 1", name="qed"):
    return {"eval": name, "index": index, "left": left, "right": right, "dialect": "mysql", "proven": proven, "assumptions": list(labels)}


def test_numeric_labels_are_the_ones_about_numbers():
    for label in (NAN, ERRORS, ORDER, EXACT, "x and y in ABS(x - y) are numbers", "values compared have the same numeric type (no INT64 to FLOAT64 conversion)"):
        assert report.is_numeric_label(label), label
    for label in (TYPES, LIMIT, "rows tied on the ORDER BY are cut by LIMIT the same way for equal inputs"):
        assert not report.is_numeric_label(label), label


def test_the_labels_of_the_prover_are_classified_as_the_report_documents():
    from kumosql import smt_equivalence as smt

    numeric = {label for label in (*smt.BASE_ASSUMPTIONS, smt.EXACT_ARITHMETIC_ASSUMPTION, smt.MIXED_NUMERIC_ASSUMPTION, smt.NUMERIC_DIFFERENCE_ASSUMPTION) if report.is_numeric_label(label)}
    assert numeric == {smt.BASE_ASSUMPTIONS[0], ERRORS, ORDER, smt.EXACT_ARITHMETIC_ASSUMPTION, smt.MIXED_NUMERIC_ASSUMPTION, smt.NUMERIC_DIFFERENCE_ASSUMPTION}
    assert not report.is_numeric_label(smt.LIMIT_SOURCE_ASSUMPTION)


@pytest.mark.parametrize(
    "sql, kinds",
    [
        ("SELECT a FROM t", set()),
        ("SELECT a FROM t WHERE b > 5", {"literal"}),
        ("SELECT a FROM t LIMIT 10", {"literal"}),
        ("SELECT a + b FROM t", {"arithmetic"}),
        ("SELECT a FROM t WHERE b = 0.5", {"arithmetic"}),
        ("SELECT ABS(a) FROM t", {"arithmetic"}),
        ("SELECT a FROM t WHERE b = -3", {"literal"}),
        ("SELECT -a FROM t", {"arithmetic"}),
        ("SELECT a FROM t WHERE s = '5'", set()),
        ("SELECT a FROM", set()),
    ],
)
def test_what_touches_numeric_expressions(sql, kinds):
    assert report.query_numeric_kinds([sql]) == kinds


def test_fewer_labels_replaced_labels_and_lost_and_gained_proofs_are_told_apart():
    labels = [NAN, ERRORS, ORDER, TYPES]
    before = [
        record(0, True, labels, "SELECT a + b FROM t", "SELECT b + a FROM t"),  # drops three labels
        record(1, True, labels, "SELECT a FROM t", "SELECT a FROM t"),  # no numeric content, but it carried numeric labels
        record(2, True, labels, "SELECT a + 1 FROM t", "SELECT 1 + a FROM t"),  # replaces one label
        record(3, True, labels, "SELECT a + 1 FROM t", "SELECT 1 + a FROM t"),  # lost
        record(4, False, [], "SELECT a FROM t", "SELECT b FROM t"),  # gained
        record(5, True, [TYPES], "SELECT a FROM t", "SELECT a FROM t"),  # no numeric label and no numeric content
        record(6, True, labels, "SELECT a FROM t", "SELECT a FROM t"),  # text differs between the collections
    ]
    after = [
        record(0, True, [TYPES], "SELECT a + b FROM t", "SELECT b + a FROM t"),
        record(1, True, labels, "SELECT a FROM t", "SELECT a FROM t"),
        record(2, True, [NAN, "runtime errors: the same operations can fail in both queries", ORDER, TYPES], "SELECT a + 1 FROM t", "SELECT 1 + a FROM t"),
        record(3, False, [], "SELECT a + 1 FROM t", "SELECT 1 + a FROM t"),
        record(4, True, labels, "SELECT a FROM t", "SELECT b FROM t"),
        record(5, True, [TYPES], "SELECT a FROM t", "SELECT a FROM t"),
        record(6, True, labels, "SELECT 7 FROM t", "SELECT a FROM t"),
    ]
    out = report.analyse(before, after, {"qed:3": True})
    assert out["pairs"] == 6 and out["mismatched"] == ["qed:6"]
    assert (out["proofs_before"], out["proofs_after"]) == (5, 5)
    stated = out["rules"]["label_or_query"]
    # pairs 0, 1 and 2 are re-proved and touch numbers (they carried numeric labels); 5 carried none and has no arithmetic
    assert stated == {"touching": 3, "fewer": 1, "same": 1, "replaced": 1}
    assert out["rules"]["arithmetic"] == {"touching": 2, "fewer": 1, "replaced": 1}
    assert out["touching_before_including_lost"] == 4
    assert out["dropped"] == {NAN: 1, ERRORS: 1, ORDER: 1}
    assert [row["index"] for row in out["lost"]] == [3] and out["lost"][0]["counterexample"] is True
    assert out["gained"] == ["qed:4"]
    assert out["labels"][NAN] == {"numeric": True, "before": 4, "after": 3}
    assert out["labels"][TYPES]["numeric"] is False
    assert out["per_eval"]["qed"]["touching"] == 3


def test_the_rendering_names_the_numbers_and_the_lost_proofs():
    before = [record(0, True, [NAN, TYPES], "SELECT a + b FROM t", "SELECT b + a FROM t"), record(1, True, [NAN], "SELECT a + 1 FROM t", "SELECT 1 + a FROM t")]
    after = [record(0, True, [TYPES], "SELECT a + b FROM t", "SELECT b + a FROM t"), record(1, False, [], "SELECT a + 1 FROM t", "SELECT 1 + a FROM t")]
    text = report.render(report.analyse(before, after, {"qed:1": False}), "old", "new")
    assert "1 of 1 = 100.0%" in text and "1 of 2 = 50.0%" in text
    assert "NO counterexample found" in text and "qed:1" in text


def test_the_percentage_of_nothing_is_not_a_number():
    assert report.percent(0, 0) == "n/a" and report.percent(1, 4) == "25.0%"


def test_a_collection_records_the_proof_and_its_labels_for_each_eval_loader():
    pytest.importorskip("z3")
    records = report.collect(["rbot", "calcite-mined", "sqlsolver-tpcc", "cosette"], limit=2)
    assert {r["eval"] for r in records} == {"rbot", "calcite-mined", "sqlsolver-tpcc", "cosette"} and len(records) == 8
    for r in records:
        assert r["proven"] == (r["status"] == "PROVEN_EQUIVALENT")
        assert r["left"] and r["right"] and (r["assumptions"] or not r["proven"])


def test_a_shard_keeps_every_nth_pair_and_the_refutation_mode_answers_for_the_pairs_asked_about():
    pytest.importorskip("z3")
    whole = report.collect(["sqlsolver-tpcc"], limit=4)
    shard = report.collect(["sqlsolver-tpcc"], shard=(1, 2), limit=2)
    assert [r["index"] for r in shard] == [1, 3] and [r["index"] for r in whole] == [0, 1, 2, 3]
    answered = report.collect(["sqlsolver-tpcc"], only={"sqlsolver-tpcc": [0]}, refute=True)
    assert [(r["index"], r["counterexample"]) for r in answered] == [(0, False)]


def test_the_numeric_traps_eval_is_collected_as_bigquery_with_a_label_for_its_refutations():
    pytest.importorskip("z3")
    records = report.collect(["numeric-traps"], limit=6)
    assert [r["index"] for r in records] == [0, 1, 2, 3, 4, 5] and {r["dialect"] for r in records} == {"bigquery"}
    assert any(r["proven"] for r in records)
    # there is no executed search for these pairs: the case's label says whether a proof would be false
    answers = report.collect(["numeric-traps"], only={"numeric-traps": [0, 1]}, refute=True)
    assert len(answers) == 2 and all(isinstance(r["counterexample"], bool) for r in answers)


def test_reading_the_pairs_as_bigquery_changes_the_labels_of_a_typed_proof():
    pytest.importorskip("z3")
    kept = report.collect(["sqlsolver-tpcc"], limit=19)
    read = report.collect(["sqlsolver-tpcc"], limit=19, dialect="bigquery")
    assert len(kept) == len(read) == 19
    assert all(any(a.startswith("runtime errors (") for a in r["assumptions"]) for r in kept if r["proven"])
    assert any(r["proven"] and not any(a.startswith("runtime errors (") for a in r["assumptions"]) for r in read)
