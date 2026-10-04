"""The rule-level differential harness (tools/rule_fuzz.py): it sees a rewrite that changes results, it ignores a
LIMIT that cuts among ties, and a seeded generated run finds no unsound rewrite."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402

from sqlglot import exp  # noqa: E402

SCHEMA = {"t": [["id", "INT64"], ["x", "INT64"]], "u": [["k", "INT64"]]}
KEYED = {"t": {"keys": [["id"]], "not_null": ["id"]}}


def case(sql, schema=None):
    return {"sql": sql, "dialect": "bigquery", "schema": schema or SCHEMA, "constraints": KEYED, "options": {}, "source": "test"}


def test_every_rule_normalize_calls_is_found():
    names = fuzz.rule_names()
    assert len(names) > 60
    assert names["_prune_derived"] == "algebraic_equivalence"
    assert names["distinct_rules"] == "distinct_rules"


def test_a_rewrite_that_changes_results_is_reported(monkeypatch):
    from kumosql import algebraic_equivalence as ae

    original = ae._fold_trivia

    def wrong(tree, *args, **kwargs):
        tree = original(tree, *args, **kwargs)
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string and literal.this == "1":
                literal.set("this", "2")
        return tree

    monkeypatch.setitem(ae.__dict__, "_fold_trivia", wrong)
    result = fuzz.check_case(case("SELECT t.id FROM t WHERE t.x = 1"), only={"_fold_trivia"})
    assert [f["status"] for f in result["fires"]] == ["differs"]


def test_a_sound_rewrite_checks_equal():
    result = fuzz.check_case(case("SELECT DISTINCT t.id FROM t"))
    fires = [f for f in result["fires"] if f["status"] != "unchecked"]
    assert fires and all(f["status"] == "equal" for f in fires)


def test_a_limit_that_cuts_among_ties_is_not_evidence():
    oracle = fuzz.Oracle(
        case("SELECT 1"),
        [{"name": "ties", "tables": {"t": [[1, 5], [2, 5], [3, 5]], "u": []}}],
    )
    try:
        verdict = oracle.compare("SELECT x FROM t ORDER BY x LIMIT 1", "SELECT id FROM t ORDER BY x LIMIT 1")
    finally:
        oracle.close()
    assert verdict["status"] != "differs"


def test_a_limit_with_a_total_order_still_differs():
    oracle = fuzz.Oracle(case("SELECT 1"), [{"name": "plain", "tables": {"t": [[1, 5], [2, 6]], "u": []}}])
    try:
        verdict = oracle.compare("SELECT id FROM t ORDER BY id LIMIT 1", "SELECT id FROM t ORDER BY id DESC LIMIT 1")
    finally:
        oracle.close()
    assert verdict["status"] == "differs"


def test_seeded_generated_run_finds_no_unsound_rewrite():
    bugs = []
    for generated in fuzz.gen_cases(seed=5, count=24):
        result = fuzz.check_case(generated, seed=5)
        bugs += [(generated["sql"], f["rule"]) for f in result["fires"] if f["status"] == "differs"]
    assert not bugs
