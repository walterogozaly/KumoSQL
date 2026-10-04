"""The independent check of the subquery lifter (``proof_lift``): lifted CTEs must expand back into their subqueries.

The prover lifts both sides of a proof with the lifter the rule uses, so a lifter bug is approved by both. Several
tests corrupt the lifter and the prover's use of it the same way and require the checker to refuse the result.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
import json
from pathlib import Path

import pytest
import sqlglot

from kumosql import apply_rule, apply_rules, equivalence, lift_subqueries as lift, prove_equivalent
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.proof_lift import LIFT_ASSUMPTIONS, LIFT_FAMILY, check_lift_transition
from kumosql.proof_steps import RewriteStep
from kumosql.rewrite import INDEPENDENT_CHECK, INDEPENDENT_CHECK_FAMILIES, VerificationStatus

lift_module = importlib.import_module("kumosql.lift_subqueries")  # the module, not the function of the same name
L1, L2 = "__lifted_subquery_001", "__lifted_subquery_002"


def check(before: str, after: str):
    step = RewriteStep("test", LIFT_FAMILY, 0, before, after, LIFT_ASSUMPTIONS)
    parse = lambda s: sqlglot.parse_one(s, read="bigquery")
    return check_lift_transition(step, parse(before), parse(after))


def independent(result) -> list:
    return [record for record in result.verification.checks if record.kind == INDEPENDENT_CHECK]


@pytest.mark.parametrize("before,after", [
    # aliased and unaliased
    (f"SELECT * FROM (SELECT 1 x) AS s", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s"),
    (f"SELECT * FROM (SELECT 1 x)", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1}"),
    # a join, and a nested subquery (inner first, so each name is defined before it is read)
    (
        "SELECT * FROM (SELECT * FROM (SELECT 1 x) a) b JOIN (SELECT 2 y) c ON TRUE",
        f"WITH {L1} AS (SELECT 1 x), {L2} AS (SELECT * FROM {L1} AS a), __lifted_subquery_003 AS (SELECT 2 y) "
        f"SELECT * FROM {L2} AS b JOIN __lifted_subquery_003 AS c ON TRUE",
    ),
    # from inside an existing CTE: the lifted CTE goes before it
    (
        "WITH a AS (SELECT * FROM (SELECT 1 x) s) SELECT * FROM a",
        f"WITH {L1} AS (SELECT 1 x), a AS (SELECT * FROM {L1} AS s) SELECT * FROM a",
    ),
    # a body that reads an original CTE
    (
        "WITH a AS (SELECT 1 x) SELECT * FROM (SELECT * FROM a) s",
        f"WITH a AS (SELECT 1 x), {L1} AS (SELECT * FROM a) SELECT * FROM {L1} AS s",
    ),
    # from a predicate subquery, a set operation and a table the query also reads
    (
        "SELECT * FROM t WHERE x IN (SELECT y FROM (SELECT 1 y) q)",
        f"WITH {L1} AS (SELECT 1 y) SELECT * FROM t WHERE x IN (SELECT y FROM {L1} AS q)",
    ),
    (
        "SELECT x FROM (SELECT 1 x) a UNION ALL SELECT x FROM (SELECT 2 x) b",
        f"WITH {L1} AS (SELECT 1 x), {L2} AS (SELECT 2 x) SELECT x FROM {L1} AS a UNION ALL SELECT x FROM {L2} AS b",
    ),
    # the PIVOT belongs to the relation, so it moves onto the reference
    (
        "SELECT * FROM (SELECT k, q FROM t) PIVOT (SUM(q) FOR k IN ('a', 'b'))",
        f"WITH {L1} AS (SELECT k, q FROM t) SELECT * FROM {L1} PIVOT (SUM(q) FOR k IN ('a', 'b'))",
    ),
    # a lifted body reading a name a nested WITH does not define is unaffected by the nesting
    (
        "SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a JOIN (SELECT 1 y) s ON TRUE)",
        f"WITH {L1} AS (SELECT 1 y) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a JOIN {L1} AS s ON TRUE)",
    ),
    # a volatile body at the query's own level is read once, like the derived table it was
    ("SELECT * FROM (SELECT RAND() r) s", f"WITH {L1} AS (SELECT RAND() r) SELECT * FROM {L1} AS s"),
    # a quoted alias that needs no quotes
    ("SELECT * FROM (SELECT 1 x) AS `s`", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s"),
])
def test_valid_lifts_are_accepted(before, after):
    result = check(before, after)
    assert result.accepted, result.reason


@pytest.mark.parametrize("before,after,reason", [
    # a physical table already has the generated name: the CTE captures it
    (
        f"SELECT * FROM {L1} JOIN (SELECT 1 x) s ON TRUE",
        f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} JOIN {L1} AS s ON TRUE",
        "no CTE with a new name",
    ),
    (  # and in another case: BigQuery reads CTE names case-insensitively
        f"SELECT * FROM {L1.upper()} JOIN (SELECT 1 x) s ON TRUE",
        f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1.upper()} JOIN {L1} AS s ON TRUE",
        "",
    ),
    # a column of the query is spelled like the generated name
    (
        f"SELECT {L1} FROM (SELECT 1 {L1}) s",
        f"WITH {L1} AS (SELECT 1 {L1}) SELECT {L1} FROM {L1} AS s",
        "",
    ),
    # a name captured: the body read the outer CTE `a`, but a nested WITH defines another `a` around it
    (
        "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM (SELECT x FROM a) q)",
        f"WITH a AS (SELECT 1 x), {L1} AS (SELECT x FROM a) "
        f"SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM {L1} AS q)",
        "differ|not the before",
    ),
    # a body that read a name the nested WITH defines now reads the table of that name
    (
        "SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM (SELECT x FROM a) q)",
        f"WITH {L1} AS (SELECT x FROM a) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM {L1} AS q)",
        "differ|not the before",
    ),
    # scope: the lifted CTE is defined after the CTE that reads it
    (
        "WITH a AS (SELECT * FROM (SELECT 1 x) s) SELECT * FROM a",
        f"WITH a AS (SELECT * FROM {L1} AS s), {L1} AS (SELECT 1 x) SELECT * FROM a",
        "differ|not the before",
    ),
    # scope: the lifted CTE sits in a WITH the reference cannot see
    (
        "SELECT * FROM (SELECT 1 x) s WHERE x IN (SELECT 1)",
        f"SELECT * FROM {L1} AS s WHERE x IN (WITH {L1} AS (SELECT 1 x) SELECT 1)",
        "differ|not the before",
    ),
    # a correlated derived table: the CTE cannot see the query around it
    (
        "SELECT * FROM t, (SELECT u.a FROM u WHERE u.a = t.a) s",
        f"WITH {L1} AS (SELECT u.a FROM u WHERE u.a = t.a) SELECT * FROM t, {L1} AS s",
        "reads t.a",
    ),
    (
        "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM (SELECT * FROM u WHERE u.a = t.a) s)",
        f"WITH {L1} AS (SELECT * FROM u WHERE u.a = t.a) SELECT * FROM t WHERE EXISTS (SELECT 1 FROM {L1} AS s)",
        "reads t.a",
    ),
    # a dropped subquery, a changed body, a changed alias, a lost PIVOT
    (
        "SELECT * FROM t JOIN (SELECT 1 x) s ON TRUE",
        f"WITH {L1} AS (SELECT 1 x) SELECT * FROM t",
        "read 0 times",
    ),
    ("SELECT * FROM (SELECT 1 x) s", f"WITH {L1} AS (SELECT 2 x) SELECT * FROM {L1} AS s", "differ|not the before"),
    ("SELECT * FROM (SELECT x FROM t WHERE x > 1) s", f"WITH {L1} AS (SELECT x FROM t) SELECT * FROM {L1} AS s", "differ|not the before"),
    ("SELECT s.x FROM (SELECT 1 x) s", f"WITH {L1} AS (SELECT 1 x) SELECT s.x FROM {L1} AS q", "differ|not the before"),
    ("SELECT s.x FROM (SELECT 1 x) s", f"WITH {L1} AS (SELECT 1 x) SELECT s.x FROM {L1}", "differ|not the before"),
    (
        "SELECT * FROM (SELECT k, q FROM t) PIVOT (SUM(q) FOR k IN ('a', 'b'))",
        f"WITH {L1} AS (SELECT k, q FROM t) SELECT * FROM {L1}",
        "differ|not the before",
    ),
    # something else changed with the lift
    ("SELECT * FROM (SELECT 1 x) s", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s WHERE TRUE", "differ|not the before"),
    ("SELECT a FROM (SELECT 1 a) s", f"WITH {L1} AS (SELECT 1 a) SELECT DISTINCT a FROM {L1} AS s", "differ|not the before"),
    # an original CTE dropped, renamed or changed in the same step
    (
        "WITH a AS (SELECT 1) SELECT * FROM (SELECT 2 y) s",
        f"WITH {L1} AS (SELECT 2 y) SELECT * FROM {L1} AS s",
        "differ|not the before",
    ),
    (
        "WITH a AS (SELECT 1) SELECT * FROM (SELECT 2 y) s",
        f"WITH b AS (SELECT 1), {L1} AS (SELECT 2 y) SELECT * FROM {L1} AS s",
        "read 0 times",
    ),
    (
        "WITH a AS (SELECT 1) SELECT * FROM (SELECT 2 y) s",
        f"WITH a AS (SELECT 9), {L1} AS (SELECT 2 y) SELECT * FROM {L1} AS s",
        "differ|not the before",
    ),
    # a lifted CTE nobody reads, or one read twice for two subqueries that happened to be equal
    ("SELECT 1", f"WITH {L1} AS (SELECT 1 x) SELECT 1", "read 0 times"),
    (
        "SELECT * FROM (SELECT 1 x) a JOIN (SELECT 1 x) b ON TRUE",
        f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS a JOIN {L1} AS b ON TRUE",
        "read 2 times",
    ),
    (
        "SELECT * FROM (SELECT 1 x) a",
        f"WITH {L1} AS (SELECT 1 x), {L2} AS (SELECT 1 x) SELECT * FROM {L1} AS a",
        "read 0 times",
    ),
    # a volatile body inside an expression subquery is evaluated per outer row, a CTE need not be
    (
        "SELECT (SELECT r FROM (SELECT RAND() r) s) FROM t",
        f"WITH {L1} AS (SELECT RAND() r) SELECT (SELECT r FROM {L1} AS s) FROM t",
        "RAND",
    ),
])
def test_unsound_lifts_are_refused(before, after, reason):
    result = check(before, after)
    assert not result.accepted
    assert any(word in result.reason for word in reason.split("|")), result.reason


@pytest.mark.parametrize("before,after,reason", [
    (
        "WITH RECURSIVE a AS (SELECT 1 x) SELECT * FROM (SELECT 2 y) s",
        f"WITH RECURSIVE a AS (SELECT 1 x), {L1} AS (SELECT 2 y) SELECT * FROM {L1} AS s",
        "recursive",
    ),
    (
        "SELECT * FROM (SELECT 1 x) s",
        f"WITH RECURSIVE {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s",
        "recursive",
    ),
    (
        "SELECT * FROM (SELECT 1 x) s",
        f"WITH {L1} AS MATERIALIZED (SELECT 1 x) SELECT * FROM {L1} AS s",
        "MATERIALIZED",
    ),
    # the generated name read somewhere other than FROM or JOIN
    (
        "SELECT * FROM (SELECT 1 x) s",
        f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s WHERE s.x IN (SELECT x FROM {L1})",
        "read 2 times",
    ),
    ("SELECT 1; SELECT 2", f"WITH {L1} AS (SELECT 1) SELECT 2", "exactly one statement"),
])
def test_unclear_cases_are_refused_not_guessed(before, after, reason):
    step = RewriteStep("test", LIFT_FAMILY, 0, before, after, LIFT_ASSUMPTIONS)
    result = check_lift_transition(step, None, None)
    assert not result.accepted
    assert reason in result.reason, result.reason


def test_unparseable_text_is_refused():
    step = RewriteStep("test", LIFT_FAMILY, 0, "SELECT * FROM (", "WITH", LIFT_ASSUMPTIONS)
    assert not check_lift_transition(step, None, None).accepted


def test_wrong_family_or_assumptions_are_refused():
    before, after = "SELECT * FROM (SELECT 1 x) AS s", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s"
    record = RewriteStep("test", LIFT_FAMILY, 0, before, after, LIFT_ASSUMPTIONS)
    assert check_lift_transition(record, None, None).accepted
    assert not check_lift_transition(replace(record, family="unregistered"), None, None).accepted
    for assumptions in ((), LIFT_ASSUMPTIONS[:-1], LIFT_ASSUMPTIONS + ("x_is_not_null",)):
        assert not check_lift_transition(replace(record, assumptions=assumptions), None, None).accepted


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    from kumosql import proof_lift

    def broken(*args, **kwargs):
        raise RuntimeError("expansion unavailable")

    monkeypatch.setattr(proof_lift, "_expand", broken)
    result = check("SELECT * FROM (SELECT 1 x) AS s", f"WITH {L1} AS (SELECT 1 x) SELECT * FROM {L1} AS s")
    assert not result.accepted and "unavailable" in result.reason


def test_the_checker_reads_the_text_not_the_trees():
    # The trees the caller passes are not consulted: a refusal-worthy text is refused whatever they hold.
    good_tree = sqlglot.parse_one("SELECT 1", read="bigquery")
    step = RewriteStep("test", LIFT_FAMILY, 0, "SELECT * FROM (SELECT 1 x) s", f"WITH {L1} AS (SELECT 2 x) SELECT * FROM {L1} AS s", LIFT_ASSUMPTIONS)
    assert not check_lift_transition(step, good_tree, good_tree).accepted


# --- sqlglot's printing -------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT CAST(a AS INT) AS a FROM t) s",  # the printer writes INT as INT64
    "SELECT * FROM (SELECT a FROM t WHERE b <> 1) s",  # and <> as !=
    "SELECT * FROM (SELECT a FROM t -- comment\n) s",
    "SELECT * FROM (select a from t) s",
    "SELECT * FROM (SELECT a FROM t) AS `s`",
    "SELECT * FROM (SELECT 1 AS a) s CROSS JOIN (SELECT 2 AS b) u",
])
def test_the_lifters_output_is_accepted_after_the_same_print(sql):
    lifted = lift(sql, rewrite_pipe_syntax=True)
    assert lifted.lifted_subqueries
    result = check(sql, lifted.sql)
    assert result.accepted, result.reason


def test_pipe_syntax_translated_by_the_prover_lift_is_accepted():
    sql = "FROM t |> WHERE x > 1 |> SELECT x"
    lifted = lift(sql, rewrite_pipe_syntax=True)
    if lifted.sql != sql:
        assert check(sql, lifted.sql).accepted


# --- the lifter on a corpus ---------------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = [row["sql_text"] for row in json.loads((FIXTURES / "sql_subquery_samples.json").read_text(encoding="utf-8"))]
CORPUS += [
    "SELECT a.id FROM (SELECT id FROM (SELECT id FROM `p.d.t`) AS x) AS a JOIN (SELECT id FROM `p.d.u`) AS b ON a.id = b.id",
    "WITH a AS (SELECT id FROM `p.d.t`) SELECT * FROM (SELECT * FROM a) AS s",
    "SELECT * FROM (SELECT 1 AS a) AS s; SELECT * FROM (SELECT 2 AS b) AS u",
    "DELETE FROM `p.d.t` WHERE TRUE AND id IN (SELECT id FROM (SELECT id FROM `p.d.u`) AS s)",
    "SELECT * FROM t, (SELECT u.a FROM u WHERE u.a = t.a) s",
    "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM (SELECT x FROM a) q)",
    "SELECT * FROM (SELECT 1 x) PIVOT (SUM(x) FOR x IN (1))",
    "SELECT * FROM `p.d.__lifted_subquery_001` AS z JOIN (SELECT 1 x) AS s ON TRUE",
    "SELECT * FROM (SELECT * FROM `p.d.t` TABLESAMPLE SYSTEM (10 PERCENT)) AS s",
    "SELECT * FROM (t)",
    "SELECT * FROM (SELECT 1 AS a UNION ALL SELECT 2) AS s WHERE a IN (SELECT a FROM (SELECT 1 AS a) AS q)",
]
# A slice of a public pairs corpus, for queries nobody wrote for this checker.
CORPUS += [
    json.loads(line)["sql_a"]
    for index, line in enumerate((FIXTURES / "qed" / "qed_calcite_pairs.jsonl").read_text(encoding="utf-8").splitlines())
    if index % 3 == 0
]


@pytest.mark.parametrize("chunk", range(4))
def test_every_change_the_rule_makes_on_the_corpus_is_accepted(chunk):
    changed = 0
    for sql in CORPUS[chunk::4]:
        result = apply_rule("lift_subqueries", sql)
        records = independent(result)
        if result.sql == sql:
            assert not records
            continue
        if any(check.detail.startswith(("strict parse failed", "the query would not run")) for check in result.verification.checks):
            continue  # text BigQuery rejects (recovered by sqlglot, or a type it lacks): nothing is proven or checked
        changed += 1
        assert records and all(record.outcome == "passed" for record in records), (sql, result.sql, [r.detail for r in records])
    assert changed


def test_the_correlated_and_captured_subqueries_of_the_corpus_stay_in_place():
    for sql in (
        "SELECT * FROM t, (SELECT u.a FROM u WHERE u.a = t.a) s",
        "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM (SELECT x FROM a) q)",
    ):
        for pipe in (False, True):
            assert lift(sql, rewrite_pipe_syntax=pipe).lifted_subqueries == 0, sql


@pytest.mark.parametrize("chunk", range(4))
def test_the_provers_own_lift_is_rederived_on_the_corpus(chunk):
    lifted = 0
    for sql in CORPUS[chunk::4]:
        result = prove_equivalent(sql, sql)
        if not result.proof_checks:
            continue
        lifts = [check for check in result.proof_checks if check.step.family == LIFT_FAMILY]
        assert all(check.accepted for check in result.proof_checks), (sql, [c.reason for c in result.proof_checks if not c.accepted])
        lifted += bool(lifts)
    assert lifted


def test_the_prover_records_its_accepted_lift():
    result = prove_equivalent("SELECT * FROM (SELECT x FROM t) AS s", "WITH c AS (SELECT x FROM t) SELECT * FROM c AS s")
    assert result.proven, result.reason
    assert any(check.step.family == LIFT_FAMILY and check.accepted for check in result.proof_checks)


def test_lift_results_carry_a_check_per_changed_statement():
    result = apply_rule("lift_subqueries", "SELECT * FROM (SELECT 1 AS a) AS s; SELECT 2; SELECT * FROM (SELECT 3 AS c) AS u")
    assert [check.step.statement_index for check in result.verification.proof_checks] == [0, 2]
    assert all(check.step.family == LIFT_FAMILY and check.accepted for check in result.verification.proof_checks)
    assert result.verification.status is VerificationStatus.PROVEN


# --- fault injection: the lifter and the prover's use of it corrupted the same way ---------------------------

def _prover_certifies_without_the_check(monkeypatch, left: str, right: str) -> bool:
    """Whether the prover alone (its lift check switched off) calls the pair equal under the current fault."""

    with monkeypatch.context() as patch:
        patch.setattr(equivalence, "_checked_lift", lambda *args: None)
        return prove_equivalent(left, right).proven


def _rule_status_without_the_check(monkeypatch, sql: str):
    """The rule's verification status when the acceptance layer has no independent check for it."""

    with monkeypatch.context() as patch:
        patch.delitem(INDEPENDENT_CHECK_FAMILIES, "lift_subqueries")
        patch.setattr(equivalence, "_checked_lift", lambda *args: None)  # the prover inside the verification checks too
        return apply_rule("lift_subqueries", sql).verification.status


def _refused_by_the_check(rule_result) -> bool:
    return (
        not rule_result.success
        and rule_result.verification.status is VerificationStatus.UNPROVEN
        and [record.outcome for record in independent(rule_result)] == ["failed"]
    )


def test_a_lifter_that_drops_a_filter_cannot_certify_a_wrong_pair(monkeypatch):
    original = lift_module._make_cte

    def drops_where(name, body):
        body = body.copy()
        body.set("where", None)
        return original(name, body)

    monkeypatch.setattr(lift_module, "_make_cte", drops_where)
    left = "SELECT * FROM (SELECT x FROM t WHERE x > 1) AS s"
    right = "SELECT * FROM (SELECT x FROM t WHERE x > 2) AS s"
    # Both sides lose the filter the same way, so the prover alone calls two different queries equal ...
    assert _prover_certifies_without_the_check(monkeypatch, left, right)
    # ... and so does the rule's own verification of its wrong output ...
    assert _rule_status_without_the_check(monkeypatch, left) is VerificationStatus.PROVEN
    # ... but the independent check refuses both.
    result = prove_equivalent(left, right)
    assert not result.proven
    assert result.proof_checks and not result.proof_checks[-1].accepted
    assert _refused_by_the_check(apply_rule("lift_subqueries", left))


def test_a_lifter_that_reuses_a_physical_tables_name_cannot_certify_it(monkeypatch):
    monkeypatch.setattr(lift_module, "_next_name", lambda used, counter: "t")
    left = "SELECT * FROM t JOIN (SELECT x FROM u) AS s ON TRUE"
    assert _rule_status_without_the_check(monkeypatch, left) is VerificationStatus.PROVEN  # `t` now means the CTE
    assert _refused_by_the_check(apply_rule("lift_subqueries", left))
    result = prove_equivalent(left, left)
    assert not result.proven
    assert result.reason == "the independent check of a normalization step refused it"


def test_a_lifter_that_ignores_scope_cannot_certify_a_captured_name(monkeypatch):
    monkeypatch.setattr(lift_module, "_liftable", lambda subquery, query: True)
    left = "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM (SELECT x FROM a) q)"
    # The lifted body now reads the outer `a`, where it read the nested one.
    assert _refused_by_the_check(apply_rule("lift_subqueries", left))
    assert not prove_equivalent(left, left).proven


def test_a_lifter_that_lifts_a_correlated_subquery_cannot_certify_it(monkeypatch):
    monkeypatch.setattr(lift_module, "_liftable", lambda subquery, query: True)
    left = "SELECT * FROM t, (SELECT u.a FROM u WHERE u.a = t.a) s"
    assert _rule_status_without_the_check(monkeypatch, left) is VerificationStatus.PROVEN
    rule = apply_rule("lift_subqueries", left)
    assert rule.sql != left
    assert _refused_by_the_check(rule)
    assert not prove_equivalent(left, left).proven


def test_a_lifter_that_points_a_reference_at_the_wrong_cte_cannot_certify_it(monkeypatch):
    original = lift_module._replace_relation_subquery

    def points_at_the_wrong_cte(subquery, name):
        original(subquery, "__lifted_subquery_002" if name.endswith("001") else "__lifted_subquery_001")

    monkeypatch.setattr(lift_module, "_replace_relation_subquery", points_at_the_wrong_cte)
    left = "SELECT * FROM (SELECT 1 x) a JOIN (SELECT 2 y) b ON TRUE"
    assert _rule_status_without_the_check(monkeypatch, left) is VerificationStatus.PROVEN
    assert _refused_by_the_check(apply_rule("lift_subqueries", left))
    assert not prove_equivalent(left, left).proven


def test_a_corrupted_prover_lift_is_refused_even_when_the_rule_is_correct(monkeypatch):
    real = equivalence.lift_subqueries

    def corrupted(sql, **kwargs):
        result = real(sql, **kwargs)
        return replace(result, sql=result.sql.replace("WHERE", "WHERE TRUE AND"))

    monkeypatch.setattr(equivalence, "lift_subqueries", corrupted)
    result = prove_equivalent("SELECT * FROM (SELECT x FROM t WHERE x > 1) AS s", "SELECT * FROM (SELECT x FROM t WHERE x > 1) AS s")
    assert not result.proven
    assert result.reason == "the independent check of a normalization step refused it"


class _BadLift(RewriteRule):
    name = "lift_subqueries"
    summary = "Fault injection: lifts a subquery and drops its filter, without going through the shared driver"

    def apply(self, sql):
        return RuleOutput(
            f"WITH {L1} AS (SELECT x FROM t) SELECT * FROM {L1} AS s", 1, 1, 1, 0, ()
        )


SOURCE = "SELECT * FROM (SELECT x FROM t WHERE x > 1) AS s"


def test_an_override_cannot_opt_out_of_the_lift_check():
    result = apply_rule("lift_subqueries", SOURCE, overrides={"lift_subqueries": _BadLift()})
    assert not result.success
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_later_step_cannot_rescue_a_refused_lift_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput(SOURCE, 1, 1, 1, 0, ())

    result = apply_rules(
        ["lift_subqueries", "restore"], SOURCE,
        overrides={"lift_subqueries": _BadLift(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason


def test_sqlx_sections_are_checked_too():
    source = 'config {type: "table"}\nSELECT * FROM (SELECT x FROM ${ref("t")}) AS s'
    result = apply_rule("lift_subqueries", source)
    assert result.success, result.verification
    assert result.verification.proof_checks
    assert all(check.accepted and check.step.section_index == 1 for check in result.verification.proof_checks)


# --- the lifter's own repairs ---------------------------------------------------------------------------------

def test_the_lifter_keeps_a_column_list_on_the_alias():
    # BigQuery has no `(...) AS t (a, b)`, but sqlglot reads it, and a lift that drops the names changes the relation.
    tree = sqlglot.parse_one("SELECT * FROM (SELECT 1, 2) AS t (a, b)")
    subquery = tree.find(sqlglot.exp.Subquery)
    lift_module._replace_relation_subquery(subquery, "c")
    assert [column.name for column in tree.find(sqlglot.exp.Table).args["alias"].args["columns"]] == ["a", "b"]


def test_the_lifter_leaves_a_parenthesized_table_and_values_alone():
    for sql in ("SELECT * FROM (t)", "SELECT * FROM (VALUES (1, 2)) AS v"):
        assert lift(sql).lifted_subqueries == 0


def test_the_lifter_skips_a_name_an_alias_already_has():
    # `inline_single_use_ctes` leaves a lifted name behind as an alias; lifting again must not reuse it, or the
    # independent check (a lifted name occurs nowhere else in the statement) would refuse the second lift.
    sql = "SELECT * FROM (SELECT * EXCEPT (st) FROM t) AS __lifted_subquery_001 WHERE a > 5"
    lifted = lift(sql, rewrite_pipe_syntax=True)
    assert "__lifted_subquery_002" in lifted.sql
    assert check(sql, lifted.sql).accepted


def test_lift_then_inline_is_still_proven_step_by_step():
    result = apply_rules(
        ["lift_subqueries", "inline_single_use_ctes"], "SELECT * FROM (SELECT * EXCEPT (st, arr) FROM t) WHERE a > 5 ORDER BY id"
    )
    assert all(step.verification.status is not VerificationStatus.UNPROVEN for step in result.steps), [
        (step.rule, step.verification.reason) for step in result.steps
    ]


def test_a_step_that_lifted_nothing_is_accepted_when_the_printed_texts_agree():
    result = check("SELECT * FROM (SELECT 1 x) s", "SELECT * FROM (SELECT 1 AS x) AS s")
    assert result.accepted and "nothing was lifted" in result.reason
