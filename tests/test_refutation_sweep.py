"""The sweep plugin leaves the prover's answers alone, recurses nowhere and skips what it must."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import refutation_sweep as sweep  # noqa: E402
from kumosql import algebraic_equivalence  # noqa: E402
from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus  # noqa: E402

NOT_IN = "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)"
NOT_EXISTS = "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)"
SCHEMA = {"t": ["a"], "u": ["b"]}
TYPES = {"t": {"a": "INT64"}, "u": {"b": "INT64"}}


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def installed(tmp_path):
    made = []

    def install(mode="real", **options):
        s = sweep.Sweep(mode, tmp_path / "out.jsonl", **options)
        s.install()
        made.append(s)
        return s

    yield install
    for s in made:
        s.uninstall()


def test_a_refutable_pair_is_logged_and_the_result_is_unchanged(installed, tmp_path):
    plain = algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    s = installed()
    wrapped = algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    assert (wrapped.status, wrapped.reason) == (plain.status, plain.reason) == (SmtStatus.NOT_PROVEN, plain.reason)
    (record,) = records(tmp_path / "out.jsonl")
    assert record["left"] == NOT_IN and record["types_guessed"] is False and record["prover_status"] == "not_proven"
    assert record["witness"]["t"] and record["left_rows"] != record["right_rows"]
    assert s.counts["(outside a test)" if "(outside a test)" in s.counts else next(iter(s.counts))]["refuted"] == 1


def test_an_equivalent_pair_is_synthesized_but_never_logged(installed, tmp_path):
    s = installed()
    result = algebraic_equivalence.prove_equivalent_algebraic("SELECT a FROM t", "SELECT a FROM t WHERE 1 = 1", schema=SCHEMA, types=TYPES)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert records(tmp_path / "out.jsonl") == []
    assert next(iter(s.counts.values()))["synthesized_proven_equivalent"] == 1


def test_a_pair_without_a_schema_is_skipped(installed, tmp_path):
    s = installed()
    algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS)
    assert records(tmp_path / "out.jsonl") == []
    assert next(iter(s.counts.values()))["skipped_no_schema"] == 1


def test_untyped_pairs_wait_for_guessed_mode_and_are_marked(installed, tmp_path):
    s = installed("real")
    algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA)
    assert records(tmp_path / "out.jsonl") == [] and next(iter(s.counts.values()))["skipped_untyped"] == 1
    s.uninstall()
    s = installed("guessed")
    algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA)
    (record,) = records(tmp_path / "out.jsonl")
    assert record["types_guessed"] is True and record["guessed_columns"] == 2
    assert set(record["types"]) == {"t", "u"}


def test_guessed_mode_leaves_fully_typed_pairs_to_the_real_run(installed, tmp_path):
    s = installed("guessed")
    algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    assert records(tmp_path / "out.jsonl") == [] and next(iter(s.counts.values()))["skipped_typed"] == 1


def test_a_repeated_pair_is_synthesized_once(installed, tmp_path):
    installed()
    for _ in range(3):
        algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    assert len(records(tmp_path / "out.jsonl")) == 1


def test_a_held_out_pair_is_matched_as_a_pair_and_a_single_row_by_either_query():
    held = sweep.HeldOut()
    held.add_row({"sql_a": "SELECT 1  FROM t;", "sql_b": "SELECT 2 FROM t", "name": "x"})
    held.add_row({"before": {"m1": "SELECT 3 FROM t", "m2": "SELECT 4 FROM t"}, "after": {"m1": "SELECT 5 FROM t"}})
    assert held.contains("select 1 from t", "SELECT 2 FROM t") and held.contains("SELECT 2 FROM t", "select 1 from t")
    assert not held.contains("SELECT 1 FROM t", "SELECT 9 FROM t")  # one query of a pair is not the pair
    assert held.contains("SELECT 4 FROM t", "SELECT 9 FROM t")  # a row of three queries is held out query by query


def test_a_held_out_pair_is_neither_run_nor_logged(installed, tmp_path):
    s = installed(held_out=sweep.HeldOut(pairs={frozenset((sweep.normalise(NOT_IN), sweep.normalise(NOT_EXISTS)))}))
    algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    assert records(tmp_path / "out.jsonl") == [] and next(iter(s.counts.values()))["held_out"] == 1


def test_nested_calls_are_not_synthesized(tmp_path, monkeypatch):
    """A call made while another wrapped call runs (the prover calls itself) is never swept."""

    calls = []

    def inner(left, right, **kwargs):
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "stub")

    s = sweep.Sweep("real", tmp_path / "out.jsonl")
    holder = {}

    def outer(left, right, **kwargs):
        calls.append("outer")
        if len(calls) == 1:
            holder["wrapped"](NOT_IN, NOT_EXISTS, **kwargs)  # the nested call, through the wrapper
        return inner(left, right, **kwargs)

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", outer)
    s.install()
    holder["wrapped"] = algebraic_equivalence.prove_equivalent_algebraic
    try:
        result = holder["wrapped"](NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    finally:
        s.uninstall()
    assert result.reason == "stub"
    assert calls == ["outer", "outer"]
    assert len(records(tmp_path / "out.jsonl")) == 1  # the outer call; the nested one is not observed at all
    assert next(iter(s.counts.values()))["calls"] == 1


def test_synthesis_does_not_recurse_into_the_wrapper(tmp_path, monkeypatch):
    from kumosql import refutation_synthesis

    s = sweep.Sweep("real", tmp_path / "out.jsonl")
    seen = []
    real = refutation_synthesis.synthesize

    def spying(*args, **kwargs):
        seen.append(getattr(s.local, "busy", False))
        algebraic_equivalence.prove_equivalent_algebraic("SELECT a FROM t", "SELECT a FROM t", schema=SCHEMA, types=TYPES)
        return real(*args, **kwargs)

    monkeypatch.setattr(refutation_synthesis, "synthesize", spying)
    s.install()
    try:
        algebraic_equivalence.prove_equivalent_algebraic(NOT_IN, NOT_EXISTS, schema=SCHEMA, types=TYPES)
    finally:
        s.uninstall()
    assert seen == [True]  # synthesis ran behind the guard, and the prover call inside it was not swept again
    assert next(iter(s.counts.values()))["calls"] == 1


def test_modules_that_imported_the_prover_by_name_are_rebound(installed):
    import types

    module = types.ModuleType("fake_eval")
    module.prove_equivalent_algebraic = algebraic_equivalence.prove_equivalent_algebraic
    sys.modules["fake_eval"] = module
    try:
        s = installed()
        assert module.prove_equivalent_algebraic is s.wrapper
        s.uninstall()
        assert module.prove_equivalent_algebraic is not s.wrapper
    finally:
        del sys.modules["fake_eval"]
