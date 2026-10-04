"""The independent CTE checker (``proof_ctes``): scope-resolved binders, compared by expansion.

Several tests corrupt a rule and the prover's normalizer the same way: matching normalization alone
would then approve a wrong rewrite, and the independent checker must refuse it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import sqlglot

from kumosql import apply_rule, apply_rules, equivalence, prove_equivalent
from kumosql import cleanup
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.proof_ctes import CTE_ASSUMPTIONS, CTE_FAMILY, MAX_EXPANDED_NODES, check_cte_transition
from kumosql.proof_steps import RewriteStep
from kumosql.rewrite import INDEPENDENT_CHECK, VerificationStatus


def parse(sql: str):
    return sqlglot.parse_one(sql, read="bigquery")


def check(before: str, after: str):
    step = RewriteStep("test", CTE_FAMILY, 0, before, after, CTE_ASSUMPTIONS)
    return check_cte_transition(step, parse(before), parse(after))


def independent(result) -> list:
    return [record for record in result.verification.checks if record.kind == INDEPENDENT_CHECK]


@pytest.mark.parametrize("before,after", [
    # inline, with and without an alias
    ("WITH a AS (SELECT 1 x) SELECT * FROM a AS q", "SELECT * FROM (SELECT 1 x) AS q"),
    ("WITH a AS (SELECT 1 x) SELECT * FROM a", "SELECT * FROM (SELECT 1 x) AS a"),
    ("WITH a AS (SELECT 1 x) SELECT * FROM A JOIN t ON TRUE", "SELECT * FROM (SELECT 1 x) AS A JOIN t ON TRUE"),
    # a name is quoted or not, the relation is the same
    ("WITH a AS (SELECT 1 x) SELECT * FROM `a`", "SELECT * FROM (SELECT 1 x) AS a"),
    # drop an unused CTE, and one that only another unused CTE reads
    ("WITH a AS (SELECT 1 x), b AS (SELECT 2) SELECT * FROM a", "WITH a AS (SELECT 1 x) SELECT * FROM a"),
    ("WITH a AS (SELECT * FROM t), b AS (SELECT * FROM a) SELECT 1", "SELECT 1"),
    # merge identical CTEs, keeping the second one's range variable name
    (
        "WITH a AS (SELECT x FROM t), b AS (SELECT x FROM t) SELECT * FROM a WHERE x IN (SELECT x FROM b)",
        "WITH a AS (SELECT x FROM t) SELECT * FROM a WHERE x IN (SELECT x FROM a AS b)",
    ),
    # reorder and rename (the prover's canonical names)
    (
        "WITH b AS (SELECT 2 y), a AS (SELECT 1 x) SELECT * FROM a, b",
        "WITH __canonical_cte_001 AS (SELECT 1 x), __canonical_cte_002 AS (SELECT 2 y) "
        "SELECT * FROM __canonical_cte_001 AS a, __canonical_cte_002 AS b",
    ),
    # a name only a physical table has is not a CTE, so the unused CTE can go
    ("WITH a AS (SELECT 1) SELECT * FROM db.a", "SELECT * FROM db.a"),
    # a later CTE is not visible to an earlier one: the `a` inside b is the table, so dropping the unused a is fine
    ("WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b", "WITH b AS (SELECT * FROM a) SELECT * FROM b"),
    # a nested WITH shadows the outer name, so the outer CTE is unused
    (
        "WITH a AS (SELECT 1) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a)",
        "SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a)",
    ),
])
def test_valid_cte_steps_are_accepted(before, after):
    result = check(before, after)
    assert result.accepted, result.reason


@pytest.mark.parametrize("before,after,reason", [
    # a CTE something still reads
    ("WITH a AS (SELECT 1 x) SELECT * FROM a", "SELECT * FROM a", "differ"),
    ("WITH a AS (SELECT 1 x), b AS (SELECT * FROM a) SELECT * FROM b", "WITH b AS (SELECT * FROM a) SELECT * FROM b", "differ"),
    # a reference differing only in case still reads the CTE
    ("WITH a AS (SELECT 1 x) SELECT * FROM A", "SELECT * FROM A", "differ"),
    # the reference was pointed at a different definition
    (
        "WITH a AS (SELECT 1 x), b AS (SELECT 2 x) SELECT * FROM a",
        "WITH a AS (SELECT 1 x), b AS (SELECT 2 x) SELECT * FROM b AS a",
        "differ",
    ),
    # a CTE renamed to a physical table's name captures it
    (
        "WITH c AS (SELECT 1 x) SELECT * FROM c JOIN t ON TRUE",
        "WITH t AS (SELECT 1 x) SELECT * FROM t JOIN t ON TRUE",
        "differ",
    ),
    # an earlier CTE's reference is to a table, never to the later CTE: inlining it changes what is read
    (
        "WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b",
        "WITH b AS (SELECT * FROM (SELECT 1 x) AS a) SELECT * FROM b",
        "differ",
    ),
    # the inner WITH was dropped but its reference now reads the outer CTE
    (
        "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a)",
        "WITH a AS (SELECT 1 x) SELECT * FROM t WHERE x IN (SELECT x FROM a)",
        "differ",
    ),
    # anything else changed alongside
    ("WITH a AS (SELECT 1 x) SELECT * FROM a", "SELECT * FROM (SELECT 1 x) AS a WHERE TRUE", "differ"),
    ("WITH a AS (SELECT 1 x) SELECT * FROM a", "SELECT * FROM (SELECT 2 x) AS a", "differ"),
    # two CTEs with different bodies merged
    (
        "WITH a AS (SELECT x FROM t), b AS (SELECT x FROM u) SELECT * FROM a JOIN b ON TRUE",
        "WITH a AS (SELECT x FROM t) SELECT * FROM a JOIN a AS b ON TRUE",
        "differ",
    ),
])
def test_unsound_cte_steps_are_refused(before, after, reason):
    result = check(before, after)
    assert not result.accepted
    assert reason in result.reason


@pytest.mark.parametrize("before,after,reason", [
    ("WITH RECURSIVE a AS (SELECT 1 x) SELECT * FROM a", "SELECT 1 x", "recursive"),
    ("WITH a AS (SELECT 1 x), A AS (SELECT 2 x) SELECT * FROM a", "SELECT 1 x", "defined twice"),
    ("WITH a AS MATERIALIZED (SELECT 1 x) SELECT * FROM a", "SELECT * FROM (SELECT 1 x) AS a", "MATERIALIZED"),
    ("WITH a AS (SELECT 1 x) SELECT * FROM a FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", "SELECT 1", "carries"),
    # a CTE name read as something other than a FROM or JOIN relation
    ("WITH a AS (SELECT 1 x) INSERT INTO a SELECT 1", "INSERT INTO a SELECT 1", "other than a FROM or JOIN relation"),
    # a table passed to a function by name is a read the checker cannot track
    ("WITH a AS (SELECT 1 x) SELECT f(a)", "SELECT f(a)", "function argument"),
    # reading it a different number of times changes a volatile value
    (
        "WITH a AS (SELECT RAND() r), b AS (SELECT RAND() r) SELECT * FROM a, b",
        "WITH a AS (SELECT RAND() r) SELECT * FROM a, a AS b",
        "RAND",
    ),
    (
        "WITH a AS (SELECT CURRENT_TIMESTAMP() t) SELECT * FROM a AS x, a AS y",
        "SELECT * FROM (SELECT CURRENT_TIMESTAMP() t) AS x, (SELECT CURRENT_TIMESTAMP() t) AS y",
        "CURRENT",
    ),
])
def test_unclear_cases_are_refused_not_guessed(before, after, reason):
    result = check(before, after)
    assert not result.accepted
    assert reason in result.reason


def test_a_volatile_cte_read_once_or_never_may_be_inlined_or_dropped():
    assert check("WITH a AS (SELECT RAND() r) SELECT 1", "SELECT 1").accepted
    assert check("WITH a AS (SELECT RAND() r) SELECT * FROM a", "SELECT * FROM (SELECT RAND() r) AS a").accepted


def test_expansion_is_bounded():
    chain = "WITH c0 AS (SELECT 1 x)" + "".join(
        f", c{i} AS (SELECT * FROM c{i - 1} AS l, c{i - 1} AS r)" for i in range(1, 30)
    )
    result = check(f"{chain} SELECT * FROM c29", "SELECT 1")
    assert not result.accepted
    assert str(MAX_EXPANDED_NODES) in result.reason


def test_wrong_family_or_assumptions_are_refused():
    before, after = "WITH a AS (SELECT 1) SELECT 2", "SELECT 2"
    record = RewriteStep("test", CTE_FAMILY, 0, before, after, CTE_ASSUMPTIONS)
    assert check_cte_transition(record, parse(before), parse(after)).accepted
    assert not check_cte_transition(replace(record, family="unregistered"), parse(before), parse(after)).accepted
    for assumptions in ((), CTE_ASSUMPTIONS[:-1], CTE_ASSUMPTIONS + ("x_is_not_null",)):
        assert not check_cte_transition(replace(record, assumptions=assumptions), parse(before), parse(after)).accepted


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    from kumosql import proof_ctes

    def broken(*args, **kwargs):
        raise RuntimeError("expansion unavailable")

    monkeypatch.setattr(proof_ctes, "_expand", broken)
    result = check("WITH a AS (SELECT 1) SELECT 2", "SELECT 2")
    assert not result.accepted and "unavailable" in result.reason


def test_the_inputs_are_not_changed():
    before = parse("WITH a AS (SELECT 1 x) SELECT * FROM a")
    after = parse("SELECT * FROM (SELECT 1 x) AS a")
    sql = before.sql(), after.sql()
    step = RewriteStep("test", CTE_FAMILY, 0, sql[0], sql[1], CTE_ASSUMPTIONS)
    assert check_cte_transition(step, before, after).accepted
    assert (before.sql(), after.sql()) == sql


# --- the three rules and the prover's normalizer ---------------------------------------------------------

CORPUS = [
    "WITH a AS (SELECT 1 x), b AS (SELECT 2) SELECT * FROM a",
    "WITH a AS (SELECT * FROM t), b AS (SELECT * FROM a) SELECT 1",
    "WITH a AS (SELECT 1 x) SELECT * FROM a AS q",
    "WITH a AS (SELECT 1 x) SELECT * FROM a JOIN t ON a.x = t.x",
    "WITH a AS (SELECT x FROM t), b AS (SELECT x FROM t) SELECT * FROM a WHERE x IN (SELECT x FROM b)",
    "WITH a AS (SELECT x FROM t) SELECT * FROM a WHERE x IN (SELECT x FROM a AS p, a AS q)",
    "WITH a AS (SELECT x FROM t), b AS (SELECT * FROM a) SELECT * FROM b",
    "WITH a AS (SELECT x FROM t), b AS (SELECT x FROM t), c AS (SELECT * FROM b) SELECT * FROM a JOIN c USING (x)",
    "WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b",
    "WITH a AS (SELECT 1) SELECT * FROM t WHERE x IN (WITH a AS (SELECT 2 x) SELECT x FROM a)",
    "WITH A AS (SELECT 1 x) SELECT * FROM a",
]


@pytest.mark.parametrize("rule", ["remove_unused_ctes", "inline_single_use_ctes", "deduplicate_ctes"])
def test_every_change_a_rule_makes_on_the_corpus_is_accepted(rule):
    changed = 0
    for sql in CORPUS:
        result = apply_rule(rule, sql)
        records = independent(result)
        if result.sql == sql:
            assert not records
            continue
        changed += 1
        assert records and all(record.outcome == "passed" for record in records), (sql, result.sql, records)
        assert result.verification.status is VerificationStatus.PROVEN, (sql, result.sql)
    assert changed


def test_cte_rule_results_carry_a_check_per_changed_statement():
    result = apply_rule("remove_unused_ctes", "WITH a AS (SELECT 1) SELECT 2; WITH b AS (SELECT 1) SELECT 3")
    assert [check.step.statement_index for check in result.verification.proof_checks] == [0, 1]
    assert all(check.step.family == CTE_FAMILY and check.accepted for check in result.verification.proof_checks)


def test_a_rule_and_the_prover_sharing_a_reference_bug_cannot_certify_it(monkeypatch):
    # Both production reference finders overlook every reference, so each treats every CTE as unused.
    monkeypatch.setattr(cleanup, "_references", lambda query, name: [])
    monkeypatch.setattr(equivalence, "is_cte_reference_candidate", lambda table: False)
    source = "WITH a AS (SELECT x FROM t) SELECT * FROM a"
    result = apply_rule("remove_unused_ctes", source)
    assert result.rule_success and result.sql == "SELECT * FROM a"
    assert not result.success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]
    assert not prove_equivalent(source, "SELECT * FROM a").proven


def test_inlining_leaves_a_forward_reference_alone():
    # `a` inside b is a table, because a is defined after b. Inlining b first would move that read into
    # the main query, where `a` is the CTE; the independent check found the rule doing exactly that.
    source = "WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b"
    result = apply_rule("inline_single_use_ctes", source)
    assert result.sql == source


def test_a_broken_prover_normalizer_cannot_certify_a_direct_proof(monkeypatch):
    monkeypatch.setattr(equivalence, "_merge_duplicate_ctes", lambda query: query.set("with_", None) or query.set("with", None) or True)
    result = prove_equivalent("WITH a AS (SELECT x FROM t) SELECT * FROM a", "SELECT * FROM a")
    assert not result.proven
    assert result.proof_checks and not result.proof_checks[-1].accepted


def test_direct_proofs_record_their_accepted_cte_normalization():
    result = prove_equivalent(
        "WITH a AS (SELECT x FROM t), b AS (SELECT x FROM t) SELECT * FROM a JOIN b ON TRUE",
        "WITH c AS (SELECT x FROM t) SELECT * FROM c AS a JOIN c AS b ON TRUE",
    )
    assert result.proven, result.reason
    assert any(check.step.family == CTE_FAMILY and check.accepted for check in result.proof_checks)


class _DropsUsedCte(RewriteRule):
    name = "remove_unused_ctes"
    summary = "Fault injection: drops a CTE that is read, without going through the shared driver"

    def apply(self, sql):
        return RuleOutput("SELECT * FROM a", 1, 1, 1, 0, ())


def test_an_override_cannot_opt_out_of_the_cte_check():
    result = apply_rule(
        "remove_unused_ctes", "WITH a AS (SELECT 1 x) SELECT * FROM a",
        overrides={"remove_unused_ctes": _DropsUsedCte()},
    )
    assert not result.success
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_a_later_step_cannot_rescue_a_refused_cte_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("WITH a AS (SELECT 1 x) SELECT * FROM a", 1, 1, 1, 0, ())

    result = apply_rules(
        ["remove_unused_ctes", "restore"], "WITH a AS (SELECT 1 x) SELECT * FROM a",
        overrides={"remove_unused_ctes": _DropsUsedCte(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason


def test_sqlx_sections_are_checked_too():
    source = 'config {type: "table"}\nWITH a AS (SELECT 1 x), b AS (SELECT 2 y) SELECT * FROM a CROSS JOIN ${ref("t")}'
    result = apply_rule("remove_unused_ctes", source)
    assert result.success, result.verification
    assert all(check.accepted and check.step.section_index == 1 for check in result.verification.proof_checks)


PIVOT_SQL = "SELECT * FROM {src} PIVOT (SUM(q) FOR k IN ('a', 'b'))"


def test_a_pivot_on_a_cte_reference_moves_onto_the_inlined_subquery():
    before = "WITH s AS (SELECT k, q FROM t) " + PIVOT_SQL.format(src="s")
    after = PIVOT_SQL.format(src="(SELECT k, q FROM t) AS s")
    assert check(before, after).accepted


def test_dropping_or_changing_a_pivot_is_refused():
    before = "WITH s AS (SELECT k, q FROM t) " + PIVOT_SQL.format(src="s")
    assert not check(before, "SELECT * FROM (SELECT k, q FROM t) AS s").accepted
    assert not check(before, PIVOT_SQL.format(src="(SELECT k, q FROM t) AS s").replace("'b'", "'c'")).accepted


@pytest.mark.parametrize("sql", [
    "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t",
    "SELECT * FROM (SELECT 1 AS id, 10 AS q1, 20 AS q2) UNPIVOT (v FOR q IN (q1, q2))",
    PIVOT_SQL.format(src="(SELECT k, q FROM t)"),
])
def test_the_prover_still_proves_a_query_equal_to_itself(sql):
    from kumosql.equivalence import prove_equivalent

    assert prove_equivalent(sql, sql).proven
