"""The proof safeguard: an independent checker for predicate cleanup, and dynamic SQLX fragments.

Several tests corrupt a rule and the prover's own normalizer the same way: matching normalization
alone would then approve a wrong rewrite, and the independent checker must refuse it.
"""

from __future__ import annotations

from dataclasses import replace
import random

import pytest
from sqlglot import exp

from kumosql import apply_rule, apply_rules, prove_equivalent
from kumosql import cleanup, equivalence, proof_steps
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.proof_steps import PREDICATE_FAMILY, RewriteStep, check_predicate_step
from kumosql.rewrite import INDEPENDENT_CHECK, VerificationStatus


def step(before: str, after: str) -> RewriteStep:
    return RewriteStep("remove_trivial_predicates", PREDICATE_FAMILY, 0, before, after)


def independent(result) -> list:
    return [check for check in result.verification.checks if check.kind == INDEPENDENT_CHECK]


@pytest.mark.parametrize("before,after", [
    ("x AND TRUE", "x"),
    ("FALSE OR x", "x"),
    ("NOT (2 < 1) AND x", "x"),
    ("1.5 = 1.5 AND x", "x"),
    ("1e309 = 1e309 AND x", "x"),  # BigQuery reads 1e309 as infinity, which equals itself
    ("9223372036854775807 > 9007199254740993 AND x", "x"),
    ("'a' = 'a' AND x", "x"),
    ("NULL AND TRUE", "NULL"),
    ("(x OR FALSE) AND (y AND TRUE)", "x AND y"),
    ("NOT (x AND TRUE)", "NOT x"),
    ("x AND y", "y AND x"),
])
def test_valid_predicate_identities_are_accepted(before, after):
    check = check_predicate_step(step(f"SELECT id FROM t WHERE {before}", f"SELECT id FROM t WHERE {after}"))
    assert check.accepted, check.reason


@pytest.mark.parametrize("before,after,reason", [
    ("WHERE x = x", "", "removed"),  # x = x is NULL when x is NULL
    ("WHERE x AND FALSE", "WHERE FALSE", "removed"),  # x could raise an error
    ("WHERE x OR TRUE", "", "removed"),
    ("WHERE x AND x", "WHERE x", "removed"),
    ("WHERE x AND TRUE", "WHERE NOT x", "disagree"),
    ("WHERE NULL", "WHERE FALSE", "disagree"),
    ("WHERE x OR 9007199254740993 = 9007199254740992.0", "WHERE x", "removed"),  # INT64 vs FLOAT64 coercion
    ("WHERE 9223372036854775808 = 9223372036854775808", "", "removed"),  # not an INT64
    ("WHERE 007 = 7", "", "removed"),
    ("WHERE 'a\\\\b' = 'a\\\\b'", "", "removed"),
    ("WHERE 'a' < 'b'", "", "removed"),
    ("HAVING TRUE", "", "HAVING"),
    ("QUALIFY TRUE", "", "QUALIFY"),
])
def test_unsound_predicate_steps_are_refused(before, after, reason):
    check = check_predicate_step(step(f"SELECT x FROM t {before}", f"SELECT x FROM t {after}"))
    assert not check.accepted
    assert reason in check.reason


def test_qualify_true_can_go_when_the_query_has_a_window():
    check = check_predicate_step(step(
        "SELECT x, ROW_NUMBER() OVER (ORDER BY x) AS n FROM t QUALIFY TRUE",
        "SELECT x, ROW_NUMBER() OVER (ORDER BY x) AS n FROM t",
    ))
    assert check.accepted, check.reason


def test_join_on_true_is_kept():
    check = check_predicate_step(step("SELECT x FROM t JOIN u ON TRUE", "SELECT x FROM t CROSS JOIN u"))
    assert not check.accepted


def test_counterexample_keeps_null_distinct_from_false():
    check = check_predicate_step(step("SELECT x FROM t WHERE x", "SELECT x FROM t WHERE NOT x"))
    assert not check.accepted
    assert check.counterexample == (("x", False),)
    check = check_predicate_step(step("SELECT x FROM t WHERE x IS NULL OR x", "SELECT x FROM t WHERE x OR x IS NULL"))
    assert check.accepted


def test_each_occurrence_of_a_volatile_call_is_its_own_value():
    # Two RAND() > 0.5 calls are two values, so swapping which one is negated changes the predicate.
    check = check_predicate_step(step(
        "SELECT x FROM t WHERE RAND() > 0.5 OR NOT RAND() > 0.5",
        "SELECT x FROM t WHERE NOT RAND() > 0.5 OR RAND() > 0.5",
    ))
    assert not check.accepted
    assert "disagree" in check.reason


@pytest.mark.parametrize("before,after", [
    ("SELECT x FROM t WHERE TRUE", "SELECT y FROM t"),
    ("SELECT x FROM t WHERE TRUE", "SELECT x FROM u"),
    ("SELECT x FROM t WHERE TRUE ORDER BY x", "SELECT x FROM t"),
    ("SELECT x FROM t WHERE TRUE GROUP BY x", "SELECT x FROM t"),
    ("DELETE FROM t WHERE TRUE", "DELETE FROM t"),
    ("SELECT x FROM t WHERE TRUE AND x", "SELECT x FROM t WHERE y"),
])
def test_changes_outside_predicates_cannot_hide_behind_a_valid_identity(before, after):
    assert not check_predicate_step(step(before, after)).accepted


def test_a_change_inside_a_subquery_predicate_is_checked_one_level_down():
    assert check_predicate_step(step(
        "SELECT x FROM t WHERE x IN (SELECT y FROM u WHERE TRUE AND y > 1) AND TRUE",
        "SELECT x FROM t WHERE x IN (SELECT y FROM u WHERE y > 1)",
    )).accepted
    check = check_predicate_step(step(
        "SELECT x FROM t WHERE x IN (SELECT y FROM u WHERE y > 1)",
        "SELECT x FROM t WHERE x IN (SELECT y FROM u WHERE y > 2)",
    ))
    assert not check.accepted


def test_wrong_family_or_assumptions_are_refused():
    record = step("SELECT x FROM t WHERE TRUE", "SELECT x FROM t")
    assert check_predicate_step(record).accepted
    assert not check_predicate_step(replace(record, family="unregistered")).accepted
    for assumptions in ((), record.assumptions[:-1], record.assumptions + ("x_is_not_null",)):
        assert not check_predicate_step(replace(record, assumptions=assumptions)).accepted


def test_too_many_atoms_is_unproven_not_sampled():
    predicate = " AND ".join(f"x{i}" for i in range(proof_steps.MAX_PREDICATE_ATOMS + 1))
    check = check_predicate_step(step(f"SELECT id FROM t WHERE {predicate} AND TRUE", f"SELECT id FROM t WHERE {predicate}"))
    assert not check.accepted
    assert "exhaustive check is not run" in check.reason


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(proof_steps.sqlite3, "connect", unavailable)
    check = check_predicate_step(step("SELECT x FROM t WHERE x AND TRUE", "SELECT x FROM t WHERE x"))
    assert not check.accepted
    assert "unavailable" in check.reason


def test_rule_results_carry_one_check_per_changed_statement_and_section():
    source = 'config {type: "table"}\nSELECT x FROM ${ref("t")} WHERE TRUE; SELECT y FROM ${ref("u")} WHERE y OR FALSE'
    result = apply_rule("remove_trivial_predicates", source)
    assert result.success, result.verification
    checks = result.verification.proof_checks
    assert [check.step.statement_index for check in checks] == [0, 1]
    assert all(check.accepted and check.step.section_index == 1 for check in checks)
    assert [record.outcome for record in independent(result)] == ["passed", "passed"]


def test_rule_and_normalizer_sharing_a_bug_cannot_certify_it(monkeypatch):
    # Both production algorithms drop the predicate; matching normalization alone would prove this.
    monkeypatch.setattr(cleanup, "simplify_predicate", lambda node: (exp.true(), 1))
    monkeypatch.setattr(equivalence, "_normalize_predicate", lambda node: exp.true())
    result = apply_rule("remove_trivial_predicates", "SELECT x FROM t WHERE x = x")
    assert result.rule_success
    assert not result.success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]
    assert not prove_equivalent("SELECT x FROM t WHERE x = x", "SELECT x FROM t").proven


def test_a_broken_normalizer_cannot_certify_a_direct_proof(monkeypatch):
    monkeypatch.setattr(equivalence, "_normalize_predicate", lambda node: exp.true())
    result = prove_equivalent("SELECT x FROM t WHERE x AND TRUE", "SELECT x FROM t")
    assert not result.proven
    assert result.proof_checks and not result.proof_checks[-1].accepted


def test_a_normalizer_that_edits_outside_predicates_is_caught(monkeypatch):
    def corrupt(query):
        for select in query.find_all(exp.Select):
            select.set("expressions", [exp.column("y")])

    monkeypatch.setattr(equivalence, "_normalize_predicates", corrupt)
    result = prove_equivalent("SELECT x FROM t WHERE TRUE", "SELECT y FROM t WHERE TRUE")
    assert not result.proven
    assert "outside a predicate" in result.diagnostics[0]


def test_direct_proofs_record_their_accepted_normalization():
    result = prove_equivalent("SELECT x FROM t WHERE x AND TRUE", "SELECT x FROM t WHERE x")
    assert result.proven
    assert [check.accepted for check in result.proof_checks] == [True]
    assert prove_equivalent("SELECT x FROM t WHERE x", "SELECT x FROM t WHERE x").proof_checks == ()


class _DropsWhere(RewriteRule):
    name = "remove_trivial_predicates"
    summary = "Fault injection: drops the whole WHERE clause without going through the shared driver"

    def apply(self, sql):
        return RuleOutput("SELECT x FROM t", 1, 1, 1, 0, ())


def test_an_override_cannot_opt_out_of_the_independent_check():
    result = apply_rule(
        "remove_trivial_predicates", "SELECT x FROM t WHERE x",
        overrides={"remove_trivial_predicates": _DropsWhere()},
    )
    assert not result.success
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_later_step_cannot_rescue_a_refused_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("SELECT x FROM t WHERE x", 1, 1, 1, 0, ())

    result = apply_rules(
        ["remove_trivial_predicates", "restore"], "SELECT x FROM t WHERE x",
        overrides={"remove_trivial_predicates": _DropsWhere(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert not result.success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason


def test_a_safeguarded_step_the_prover_cannot_parse_is_not_rescued_either():
    class Garbage(RewriteRule):
        name = "remove_trivial_predicates"
        summary = "Fault injection: output that does not parse"

        def apply(self, sql):
            return RuleOutput("SELECT x FROM t WHERE", 1, 1, 1, 0, ())

    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("SELECT x FROM t WHERE x", 1, 1, 1, 0, ())

    result = apply_rules(
        ["remove_trivial_predicates", "restore"], "SELECT x FROM t WHERE x",
        overrides={"remove_trivial_predicates": Garbage(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert not result.success


def test_every_change_the_rule_makes_on_a_random_corpus_is_accepted():
    rng = random.Random(20261002)
    atoms = ["x", "y > 1", "z IS NULL", "NULL", "TRUE", "FALSE", "1 = 1", "2 < 1", "'a' = 'a'"]

    def predicate(depth):
        if depth == 0 or rng.random() < 0.35:
            return rng.choice(atoms)
        if rng.random() < 0.2:
            return f"NOT ({predicate(depth - 1)})"
        return f"({predicate(depth - 1)}) {rng.choice(['AND', 'OR'])} ({predicate(depth - 1)})"

    changed = 0
    for _ in range(100):
        source = f"SELECT x FROM t WHERE {predicate(3)}"
        result = apply_rule("remove_trivial_predicates", source)
        if result.changes:
            changed += 1
            assert [record.outcome for record in independent(result)] == ["passed"], source
            assert result.success, (source, result.verification)
    assert changed >= 50


def test_ui_exposes_the_independent_check():
    from kumosql.ui import transform

    payload = transform("SELECT x FROM t WHERE x AND TRUE", ["remove_trivial_predicates"])
    checks = [check for check in payload["steps"][0]["verification"]["checks"] if check["kind"] == INDEPENDENT_CHECK]
    assert checks and checks[0]["outcome"] == "passed"
    assert checks[0]["evidence"]["assumptions"] == list(proof_steps.PREDICATE_ASSUMPTIONS)
    assert checks[0]["evidence"]["cases_checked"] == 3


# Dataform expressions other than ref(), resolve() and self() can expand to any SQL.

@pytest.mark.parametrize("rule,source", [
    # ${"x OR y"} without parentheses binds as z AND x OR y.
    ("remove_redundant_parentheses", 'SELECT x FROM t WHERE z AND (${"x OR y"})'),
    # Not incremental: WHERE with nothing after it; incremental: WHERE AND b > 1.
    ("remove_trivial_predicates", 'SELECT x FROM t WHERE TRUE ${when(incremental(), "AND b > 1")}'),
    # The expression reads c, so c is not unused.
    ("remove_unused_ctes", 'WITH c AS (SELECT 1 AS x) SELECT * FROM t WHERE ${"x IN (SELECT x FROM c)"}'),
    ("inline_single_use_ctes", 'WITH c AS (SELECT 1 AS x), d AS (SELECT x FROM c) SELECT * FROM d WHERE ${"x IN (SELECT x FROM c)"}'),
])
def test_a_changed_statement_with_a_dynamic_sqlx_expression_is_unproven(rule, source):
    result = apply_rule(rule, source)
    assert result.changes
    assert not result.success
    assert any("compile the SQLX" in detail for detail in result.verification.details)


@pytest.mark.parametrize("rule,source", [
    ("remove_trivial_predicates", 'SELECT x FROM ${ref("t")} WHERE TRUE AND y = 1'),
    ("remove_trivial_predicates", "SELECT x FROM ${ref('s', 't')} WHERE d = '${constants.START}' AND TRUE"),
    ("remove_trivial_predicates", 'SELECT x FROM ${self()} WHERE TRUE AND y = 1'),
    ("inline_single_use_ctes", 'WITH c AS (SELECT * FROM ${resolve("t")}) SELECT * FROM c'),
    # The dynamic expression is in a statement the rule left alone.
    ("remove_trivial_predicates", 'SELECT x FROM t WHERE TRUE AND y = 1;\nSELECT ${when(incremental(), "z")} FROM u'),
])
def test_relation_references_and_string_contents_stay_provable(rule, source):
    result = apply_rule(rule, source)
    assert result.changes
    assert result.success, result.verification


@pytest.mark.parametrize("call", ["CURRENT_DATETIME()", "SESSION_USER()"])
def test_clock_and_user_calls_are_volatile_values(call):
    # Left alone they are fine; merging two CTEs that each call one changes how many calls run.
    assert prove_equivalent(f"SELECT {call} AS v FROM t WHERE TRUE", f"SELECT {call} AS v FROM t").proven
    result = prove_equivalent(
        f"WITH a AS (SELECT {call} AS v), b AS (SELECT {call} AS v) SELECT a.v, b.v AS w FROM a, b",
        f"WITH a AS (SELECT {call} AS v) SELECT a.v, b.v AS w FROM a, a AS b",
    )
    assert not result.proven
