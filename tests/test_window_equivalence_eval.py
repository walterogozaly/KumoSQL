"""Window equivalence eval (issue #503): case file shape, the stable split, label evidence on DuckDB and prover floors.

The first floors are the baseline measured before any window rule; the rule changes of the workstream raise them. ``FLOORS`` only
ever goes up, and a wrong verdict fails the test whatever the floors say.
"""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "window_equivalence_bench.py"
sys.path.insert(0, str(_path.parent))
_spec = importlib.util.spec_from_file_location("window_equivalence_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["window_equivalence_bench"] = bench
_spec.loader.exec_module(bench)

HAND = [c for c in bench.load_cases(("derived", "idiom"))]
MINED = [c for c in bench.load_cases(("slt",))]
EXECUTABLE = [c for c in HAND + MINED if c.executable]

# Equivalent pairs proved and non-equivalent pairs refuted (minimums), measured 2026-10-05 on the baseline checkout (hand-a 4 and 20, hand-b 2 and
# 17, slt 58 and 18); the refuters work to a time budget, so the floors leave room for a slow machine. Groups spread the work over test workers.
FLOORS = {
    "hand-a": {"equivalent_proven": 4, "not_equivalent_refuted": 17},
    "hand-b": {"equivalent_proven": 2, "not_equivalent_refuted": 14},
    "slt": {"equivalent_proven": 55, "not_equivalent_refuted": 16},
}


def _group(name: str) -> list:
    if name == "slt":
        return MINED
    first = [c for c in HAND if c.source == "derived" or c.family in ("latest-row", "sessions")]
    return first if name == "hand-a" else [c for c in HAND if c not in first]


def test_the_case_files_have_the_expected_shape():
    cases = HAND + MINED
    assert len({c.id for c in cases}) == len(cases)
    assert {c.label for c in cases} == {"equivalent", "not_equivalent"}
    assert {c.source for c in cases} == {"derived", "idiom", "slt"}
    fixtures = bench.load_fixtures()
    assert all(isinstance(c.fixture, dict) or c.fixture in fixtures for c in cases)
    assert all(c.why and c.origin and c.licence for c in cases)
    # every family named in the issue has equivalent and non-equivalent cases, some of them tie-dependent
    idioms = [c for c in cases if c.source == "idiom"]
    for family in ("latest-row", "sessions", "running", "islands"):
        labels = {c.label for c in idioms if c.family == family}
        assert labels == {"equivalent", "not_equivalent"}, family
    assert any(c.tie_dependent and c.label == "not_equivalent" for c in idioms)
    assert any(c.tie_dependent and c.label == "equivalent" for c in idioms)
    # a mined case keeps where it came from
    assert all(c.detail.get("file") and c.detail.get("revision") and c.detail.get("sql") for c in MINED)
    assert {c.origin for c in MINED} <= {"slt-rule", "slt-wrap", "slt-mutant"}


def test_the_split_is_a_stable_hash_of_the_case_id():
    # pinned: a change to the hash or its salt would move cases between dev and held-out
    assert [bench.held_out(i) for i in ("islands-wrong-sign", "latest-row-number-vs-rank-ties", "running-sum-partition-dropped", "derived-pushdown-partition-predicate")] == [
        True, False, False, True,
    ]
    cases = HAND + MINED
    held = [c for c in cases if c.held_out]
    assert 0.18 <= len(held) / len(cases) <= 0.32
    assert {c.id for c in bench.split_of(cases, "dev")}.isdisjoint(c.id for c in bench.split_of(cases, "held-out"))
    # adding a case moves no other case
    assert [c.id for c in held] == [c.id for c in cases if bench.held_out(c.id)]


def test_the_corpus_index_holds_development_pairs_only():
    index = bench.corpus_index()
    assert len({e["id"] for e in index}) == len(index)
    leetcode = [e for e in index if e["suite"] == "leetcode"]
    assert leetcode and all(e["index"] % 24 == 0 for e in leetcode)  # the development sample of the VeriEQL LeetCode eval
    spark = [e for e in index if e["suite"] == "sqlsolver-spark"]
    assert [(e["index"], e["label"]) for e in spark] == [(50, "not_equivalent")]
    assert all(e["label"] in ("unlabelled", "not_equivalent") for e in index)
    # the sqllogictest suite of SQLite has no window query, so nothing is mined from it
    assert all(c.detail["suite"] == "duckdb-slt" for c in MINED)


@pytest.mark.parametrize("case", EXECUTABLE, ids=lambda c: c.id)
def test_the_label_holds_on_duckdb(case):
    evidence = bench.label_evidence(case, bench.load_fixtures(), databases=4)
    assert evidence["ok"], evidence["note"]


def test_the_label_check_can_fail():
    case = next(c for c in HAND if c.label == "equivalent" and c.executable and c.tie_dependent is False and c.id.startswith("latest-row-number-vs-rank-total"))
    wrong = bench.Case(**{**case.__dict__, "label": "not_equivalent", "witness": {"events": [[1, 1, 1, 1], [2, 1, 1, 1]]}})
    assert not bench.label_evidence(wrong, bench.load_fixtures())["ok"]


def test_every_executable_non_equivalent_case_has_a_witness():
    for case in EXECUTABLE:
        if case.label == "not_equivalent" and case.source != "slt":
            assert case.witness, case.id  # a mined case's witness is its source's own rows


@pytest.fixture(scope="module")
def decided():
    cache = {}

    def get(name: str):
        if name not in cache:
            fixtures = bench.load_fixtures()
            cache[name] = [bench.decide(c, fixtures) for c in _group(name)]
        return cache[name]

    return get


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_nothing_is_wrong_and_the_floors_hold(decided, name):
    rows = decided(name)
    assert [r["id"] for r in rows if r["wrong"]] == []
    summary = bench.summary(rows)
    assert summary["not_equivalent_proven"] == 0  # a tie trap is never proved
    assert summary["tie_not_equivalent_proven"] == 0
    assert summary["equivalent_refuted"] == 0
    for key, floor in FLOORS[name].items():
        assert summary[key] >= floor, (name, key, summary[key], floor)


def test_the_held_out_quarter_has_no_wrong_answer(decided):
    for name in sorted(FLOORS):
        assert not any(r["wrong"] for r in decided(name) if r["held_out"])


def test_sqlsolver_spark_pair_50_is_never_proved():
    entry = next(e for e in bench.corpus_index() if e["id"] == "sqlsolver-spark-50")
    row = bench.decide_corpus(entry)
    assert row["outcome"] != "proven" and not row["wrong"]


def test_a_sample_of_corpus_window_pairs_has_no_wrong_answer():
    index = [e for e in bench.corpus_index() if e["suite"] != "sqlsolver-spark"][::9]
    try:
        cases = bench.verieql_cases()
    except OSError as error:  # no network and nothing cached
        pytest.skip(str(error))
    rows = [bench.decide_corpus(e, cases) for e in index]
    assert [r["id"] for r in rows if r["wrong"]] == []


def test_the_results_file_matches_the_case_files():
    path = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "window-equivalence.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    cases = HAND + MINED
    assert row["size"] == len(cases) + len(bench.corpus_index())
    assert sum(row["coverage"].values()) == row["size"]
    assert row["score"].endswith(" 0 wrong")
