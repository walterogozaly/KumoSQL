"""The independent checks of parenthesization and redundant DISTINCT (``proof_syntax``).

Fault injection corrupts the rule and the prover's normalizer the same way: matching normalization alone
would approve the wrong rewrite, and the checker must refuse it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import sqlglot
from sqlglot import exp

from kumosql import apply_rule, apply_rules, cleanup, equivalence, prove_equivalent
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.proof_syntax import (
    DISTINCT_ASSUMPTIONS,
    DISTINCT_FAMILY,
    PAREN_ASSUMPTIONS,
    PAREN_FAMILY,
    check_syntax_transition,
)
from kumosql.proof_steps import RewriteStep
from kumosql.rewrite import INDEPENDENT_CHECK, VerificationStatus


def check(family: str, assumptions: tuple[str, ...], before: str, after: str):
    step = RewriteStep("test", family, 0, before, after, assumptions)
    parse = lambda s: sqlglot.parse_one(s, read="bigquery")
    return check_syntax_transition(step, parse(before), parse(after))


def parens(before: str, after: str):
    return check(PAREN_FAMILY, PAREN_ASSUMPTIONS, before, after)


def distinct(before: str, after: str):
    return check(DISTINCT_FAMILY, DISTINCT_ASSUMPTIONS, before, after)


def independent(result) -> list:
    return [record for record in result.verification.checks if record.kind == INDEPENDENT_CHECK]


@pytest.mark.parametrize("before,after", [
    ("SELECT a FROM t WHERE (a = 1) AND (b = 2)", "SELECT a FROM t WHERE a = 1 AND b = 2"),
    ("SELECT a FROM t WHERE ((a))", "SELECT a FROM t WHERE a"),
    ("SELECT a FROM t WHERE (a AND b) AND c", "SELECT a FROM t WHERE a AND b AND c"),
    ("SELECT (a) FROM t", "SELECT a FROM t"),  # output names follow the expression, not its parentheses
    ("SELECT (a + 1) * 2 FROM t", "SELECT (a + 1) * 2 FROM t"),
    ("SELECT a FROM t WHERE x = (1 + 2)", "SELECT a FROM t WHERE x = 1 + 2"),
    ("SELECT a FROM t WHERE a AND (b AND c)", "SELECT a FROM t WHERE (a AND b) AND c"),  # AND is associative
    ("SELECT a FROM t WHERE a AND (b AND (c AND d))", "SELECT a FROM t WHERE a AND b AND c AND d"),
    ("SELECT a FROM t WHERE a OR (b OR c)", "SELECT a FROM t WHERE a OR b OR c"),
    ("SELECT a FROM t WHERE a OR (b AND c)", "SELECT a FROM t WHERE a OR b AND c"),  # AND binds tighter
])
def test_valid_parenthesis_steps_are_accepted(before, after):
    result = parens(before, after)
    assert result.accepted, result.reason


@pytest.mark.parametrize("before,after", [
    ("SELECT a FROM t WHERE (a OR b) AND c", "SELECT a FROM t WHERE a OR b AND c"),  # changes grouping
    ("SELECT (a + 1) * 2 FROM t", "SELECT a + 1 * 2 FROM t"),
    ("SELECT (t).x FROM t", "SELECT t.x FROM t"),  # a field of the row, not a column of the table
    ("SELECT x - (-1) FROM t", "SELECT x --1 FROM t"),  # the text becomes a comment
    ("SELECT NOT (a AND b) FROM t", "SELECT NOT a AND b FROM t"),
    ("SELECT a FROM t WHERE x IN (1, 2)", "SELECT a FROM t WHERE x IN (1)"),
    ("SELECT a FROM t WHERE (x)", "SELECT a FROM u WHERE x"),
    ("SELECT a FROM t WHERE a AND (b OR c)", "SELECT a FROM t WHERE a AND b OR c"),
    ("SELECT a FROM t WHERE a AND (b AND c)", "SELECT a FROM t WHERE a AND (c AND b)"),  # operand order is kept
    ("SELECT a FROM t WHERE a AND (b AND c)", "SELECT a FROM t WHERE a AND b"),  # an operand dropped
    ("SELECT a + (b + c) FROM t", "SELECT (a + b) + c FROM t"),  # floating point and overflow: only AND and OR are associative
    ("SELECT a FROM t WHERE a AND (b OR (c AND d))", "SELECT a FROM t WHERE a AND b OR c AND d"),
])
def test_unsound_parenthesis_steps_are_refused(before, after):
    assert not parens(before, after).accepted


@pytest.mark.parametrize("before,after", [
    ("SELECT DISTINCT a, b, COUNT(*) FROM t GROUP BY a, b", "SELECT a, b, COUNT(*) FROM t GROUP BY a, b"),
    ("SELECT DISTINCT a AS x, SUM(b) FROM t GROUP BY a", "SELECT a AS x, SUM(b) FROM t GROUP BY a"),
    ("SELECT DISTINCT t.a, MAX(b) FROM t GROUP BY t.a HAVING MAX(b) > 1", "SELECT t.a, MAX(b) FROM t GROUP BY t.a HAVING MAX(b) > 1"),
    (
        "SELECT x FROM (SELECT DISTINCT a AS x, COUNT(*) FROM t GROUP BY a)",
        "SELECT x FROM (SELECT a AS x, COUNT(*) FROM t GROUP BY a)",
    ),
])
def test_valid_distinct_steps_are_accepted(before, after):
    result = distinct(before, after)
    assert result.accepted, result.reason


@pytest.mark.parametrize("before,after,reason", [
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a, b", "SELECT a, COUNT(*) FROM t GROUP BY a, b", "not projected"),
    ("SELECT DISTINCT a + 1, COUNT(*) FROM t GROUP BY a", "SELECT a + 1, COUNT(*) FROM t GROUP BY a", "not projected"),
    ("SELECT DISTINCT a FROM t", "SELECT a FROM t", "without a GROUP BY"),
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY ROLLUP(a)", "SELECT a, COUNT(*) FROM t GROUP BY ROLLUP(a)", "not redundant"),
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY 1", "SELECT a, COUNT(*) FROM t GROUP BY 1", "plain column"),
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY ALL", "SELECT a, COUNT(*) FROM t GROUP BY ALL", "not redundant"),
    # an alias spelled like a key but projecting another column: GROUP BY reads the alias
    ("SELECT DISTINCT b AS a, COUNT(*) FROM t GROUP BY a", "SELECT b AS a, COUNT(*) FROM t GROUP BY a", "alias"),
    ("SELECT DISTINCT ON (a) a, COUNT(*) FROM t GROUP BY a", "SELECT a, COUNT(*) FROM t GROUP BY a", ""),
    # DISTINCT cleared and something else changed
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a", "SELECT a, COUNT(*) FROM t WHERE a > 1 GROUP BY a", "other than clearing DISTINCT"),
    # added, or nothing removed
    ("SELECT a, COUNT(*) FROM t GROUP BY a", "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a", "added"),
    ("SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a", "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a", "no DISTINCT was removed"),
])
def test_unsound_distinct_steps_are_refused(before, after, reason):
    result = distinct(before, after)
    assert not result.accepted
    assert reason in result.reason


def test_wrong_family_or_assumptions_are_refused():
    before, after = "SELECT (a) FROM t", "SELECT a FROM t"
    record = RewriteStep("test", PAREN_FAMILY, 0, before, after, PAREN_ASSUMPTIONS)
    parse = lambda s: sqlglot.parse_one(s, read="bigquery")
    assert check_syntax_transition(record, parse(before), parse(after)).accepted
    assert not check_syntax_transition(replace(record, family="unregistered"), parse(before), parse(after)).accepted
    assert not check_syntax_transition(replace(record, assumptions=DISTINCT_ASSUMPTIONS), parse(before), parse(after)).accepted
    assert not check_syntax_transition(replace(record, assumptions=()), parse(before), parse(after)).accepted


def test_the_check_reads_the_text_not_the_callers_tree():
    # A tree can be edited into something that prints differently from the tree itself.
    before = sqlglot.parse_one("SELECT (t).x FROM t", read="bigquery")
    after = before.copy()
    for paren in list(after.find_all(exp.Paren)):
        paren.replace(paren.this)
    step = RewriteStep("test", PAREN_FAMILY, 0, before.sql(dialect="bigquery"), after.sql(dialect="bigquery"), PAREN_ASSUMPTIONS)
    assert not check_syntax_transition(step, before, after).accepted


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    from kumosql import proof_syntax

    def broken(*args, **kwargs):
        raise RuntimeError("parser unavailable")

    monkeypatch.setattr(proof_syntax, "_reparse", broken)
    result = parens("SELECT (a) FROM t", "SELECT a FROM t")
    assert not result.accepted and "unavailable" in result.reason


# --- the rules and the prover's normalizer -------------------------------------------------------------

def test_rule_results_record_the_independent_checks():
    result = apply_rule("remove_redundant_parentheses", "SELECT a FROM t WHERE (a = 1) AND (b OR c)")
    assert result.verification.status is VerificationStatus.PROVEN
    assert [record.outcome for record in independent(result)] == ["passed"]
    result = apply_rule("remove_redundant_distinct", "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a")
    assert result.verification.status is VerificationStatus.PROVEN
    assert [record.outcome for record in independent(result)] == ["passed"]


def test_a_rule_and_the_prover_sharing_a_parenthesis_bug_cannot_certify_it(monkeypatch):
    # Both production algorithms now treat every parenthesis as meaningless, including the one in (t).x.
    monkeypatch.setattr(cleanup, "_redundant", lambda paren: True)
    monkeypatch.setattr(equivalence, "_paren_is_semantic", lambda paren: False)
    source = "SELECT (t).x FROM t"
    result = apply_rule("remove_redundant_parentheses", source)
    assert result.rule_success and result.sql != source
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]
    proof = prove_equivalent(source, "SELECT t.x FROM t")
    assert not proof.proven
    assert proof.proof_checks and not proof.proof_checks[-1].accepted


def test_a_rule_and_the_prover_sharing_a_distinct_bug_cannot_certify_it(monkeypatch):
    # Both production algorithms now call every DISTINCT redundant.
    from kumosql import cost_rules

    monkeypatch.setattr(cost_rules, "distinct_is_redundant", lambda select: True)
    monkeypatch.setattr(equivalence, "distinct_is_redundant", lambda select: True)
    source = "SELECT DISTINCT a FROM t"
    result = apply_rule("remove_redundant_distinct", source)
    assert result.rule_success and result.sql != source
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]
    assert not prove_equivalent(source, "SELECT a FROM t").proven


class _DropsDistinct(RewriteRule):
    name = "remove_redundant_distinct"
    summary = "Fault injection: drops DISTINCT without going through the shared driver"

    def apply(self, sql):
        return RuleOutput("SELECT a FROM t", 1, 1, 1, 0, ())


def test_an_override_cannot_opt_out_of_the_distinct_check():
    result = apply_rule(
        "remove_redundant_distinct", "SELECT DISTINCT a FROM t",
        overrides={"remove_redundant_distinct": _DropsDistinct()},
    )
    assert not result.success
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_later_step_cannot_rescue_a_refused_distinct_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("SELECT DISTINCT a FROM t", 1, 1, 1, 0, ())

    result = apply_rules(
        ["remove_redundant_distinct", "restore"], "SELECT DISTINCT a FROM t",
        overrides={"remove_redundant_distinct": _DropsDistinct(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason
