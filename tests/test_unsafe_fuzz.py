"""Seeded metamorphic fuzzing (tools/unsafe_fuzz.py, tools/compose_fuzz.py): zero false proofs."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import compose_fuzz  # noqa: E402
import unsafe_fuzz  # noqa: E402

from kumosql import check_idempotence, canonical_rule_order  # noqa: E402
from kumosql.result_equivalence import ResultEquivalenceStatus, check_result_equivalence  # noqa: E402


def _assert_sound(summary):
    c = summary["all"]["correctness"]
    assert c["false_proofs"] == 0
    assert c["bad_counterexamples"] == 0
    assert c["label_errors"] == 0
    assert c["prover_errors"] == 0


def test_unsafe_rewrites_are_never_proved_and_always_found_by_execution():
    outcomes, summary = unsafe_fuzz.run_prover_suite("unsafe", 2, 1)
    _assert_sound(summary)
    different = summary["all"]["different"]
    assert different["cases"] > 25
    # The prover alone refutes few of these; the synthetic-data check must catch every one.
    assert different["found_either_way"] == different["cases"]
    equivalent = summary["all"]["equivalent"]
    assert equivalent["proved"] >= 0.8 * equivalent["cases"]
    assert summary["all"]["synthetic_claims_difference_on_equivalent"] == 0


def test_tlp_norec_and_mutation_pairs_are_sound():
    outcomes, summary = unsafe_fuzz.run_prover_suite("fuzz", 3, 1)
    _assert_sound(summary)
    equivalent = summary["all"]["equivalent"]
    assert equivalent["proved"] >= 0.25 * equivalent["cases"]


@pytest.mark.slow
def test_larger_fuzz_run_is_sound():
    for seed in (11, 12):
        _, summary = unsafe_fuzz.run_prover_suite("fuzz", 40, seed)
        _assert_sound(summary)


def test_rule_chains_preserve_meaning_and_terminate():
    summary = compose_fuzz.run(6, 3)
    assert summary["correctness"] == {
        "behaviour_changes": 0,
        "prover_false_proofs": 0,
        "prover_refuted_a_preserving_step": 0,
        "errors": 0,
    }, summary["failures"]
    assert summary["termination"]["non_terminating"] == 0
    assert summary["determinism"]["nondeterministic"] == 0
    assert summary["idempotence"]["not_idempotent"] == 0, summary["failures"]


@pytest.mark.slow
def test_larger_composition_run():
    summary = compose_fuzz.run(60, 21)
    assert sum(summary["correctness"].values()) == 0, summary["failures"]
    assert summary["idempotence"]["not_idempotent"] == 0, summary["failures"]


def test_mutations_cover_the_unsafe_operators():
    import random

    query = (
        "SELECT DISTINCT x.a AS a FROM t AS x LEFT JOIN u AS y ON x.a = y.a "
        "WHERE x.b < 2 AND NOT (y.c = 1) UNION ALL SELECT COUNT(*) AS a FROM t"
    )
    ops = {op for op, _ in unsafe_fuzz.mutate(query, random.Random(0))}
    assert {"drop-distinct", "left-to-inner", "flip-union-all", "count-star-to-col", "drop-where", "swap-LT", "drop-not"} <= ops


def test_synthetic_check_runs_queries_that_qualify_columns_by_table_name():
    # The check used to fail with "Referenced table not found" because the
    # renamed table kept no alias for ``t.a`` to resolve against.
    schema = unsafe_fuzz.SCHEMA
    result = check_result_equivalence(
        "SELECT t.a AS k FROM t WHERE t.a NOT IN (SELECT u.b FROM u)",
        "SELECT t.a AS k FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)",
        schema,
        seeds=range(8),
        rows_per_table=8,
    )
    assert result.status is ResultEquivalenceStatus.DIFFERENT
    ok = check_result_equivalence("SELECT t.a FROM t JOIN u ON t.a = u.a", "SELECT t.a FROM t JOIN u ON u.a = t.a", schema)
    assert ok.status is ResultEquivalenceStatus.EQUIVALENT


def test_canonical_pipeline_is_idempotent_when_removing_an_unused_cte_leaves_one_reader():
    sql = "WITH a AS (SELECT a FROM t), unused AS (SELECT a FROM a) SELECT a FROM a"
    check = check_idempotence(canonical_rule_order(), sql)
    assert check.idempotent, (check.sql, check.rerun_sql)


def test_committed_case_file_matches_its_seed():
    import json

    path = Path(__file__).parent / "fixtures" / "unsafe_rewrite_cases.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [json.loads(json.dumps(c.__dict__)) for c in unsafe_fuzz.build_cases("unsafe", 3, 1)]


def test_normalizer_keeps_or_condition_parenthesised_when_pushed_into_a_derived_distinct():
    # Pushing `WHERE b = 2 OR a < 2` into the derived DISTINCT printed
    # `WHERE a >= 1 AND b = 2 OR a < 2`, which reads back with the wrong
    # precedence and made the prover return a counterexample that is not one.
    from kumosql.algebraic_equivalence import normalize

    sql = "SELECT x.a AS a, x.b AS b FROM (SELECT DISTINCT x.a AS a, x.b AS b FROM t AS x WHERE x.a >= 1) AS x WHERE x.b = 2 OR x.a < 2"
    out = normalize(sql)
    assert "(x.b = 2 OR x.a < 2)" in out
    oracle = unsafe_fuzz.Oracle(trials=60)
    assert oracle.search(sql, out) is None
    wrong = sql.replace("OR x.a", "AND x.a")
    result = unsafe_fuzz.prove(wrong, sql)
    if result.counterexample is not None:
        assert oracle.replay(result.counterexample.tables, wrong, sql)
