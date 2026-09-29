import pytest
from sqlglot import exp

from bq_sql_tools import (
    RewriteRule,
    VerificationStatus,
    apply_rule,
    apply_rules,
    available_rules,
    engine,
    get_rule,
    lift_subqueries,
)


def test_built_in_rules_are_registered():
    rules = available_rules()

    assert {"lift_subqueries", "inline_single_use_ctes"} <= set(rules)
    assert all(rule.summary for rule in rules.values())


def test_unknown_rule_names_the_known_rules():
    with pytest.raises(KeyError, match="lift_subqueries"):
        get_rule("no_such_rule")


def test_lift_rule_matches_public_function_and_is_verified():
    source = "SELECT c.id FROM (SELECT id FROM `p.d.customers`) AS c"

    result = apply_rule("lift_subqueries", source)

    assert result.sql == lift_subqueries(source).sql
    assert result.changes == 1
    assert result.verification.status is VerificationStatus.PROVEN
    assert result.success


def test_unchanged_output_is_reported_as_unchanged():
    source = "SELECT id FROM `p.d.customers`"

    result = apply_rule("lift_subqueries", source)

    assert result.sql == source
    assert result.verification.status is VerificationStatus.UNCHANGED
    assert result.success


class _DropWhereRule(RewriteRule):
    name = "test_drop_where"
    summary = "Deliberately wrong rule used to check verification"

    def rewrite_statement(self, statement, index):
        changed = 0
        for select in list(statement.find_all(exp.Select)):
            if select.args.get("where") is not None:
                select.set("where", None)
                changed += 1
        return changed, []


def test_wrong_rule_output_is_flagged_unproven(monkeypatch):
    monkeypatch.setitem(engine._REGISTRY, _DropWhereRule.name, _DropWhereRule())

    result = apply_rule("test_drop_where", "SELECT id FROM `p.d.t` WHERE id > 1")

    assert result.changes == 1
    assert result.rule_success
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert result.verification.details
    assert not result.success


def test_pipeline_verifies_each_step_and_overall_result():
    source = """WITH o AS (SELECT customer_id FROM `p.d.orders`)
SELECT c.id FROM `p.d.customers` AS c JOIN o ON o.customer_id = c.id"""

    result = apply_rules(["inline_single_use_ctes", "lift_subqueries"], source)

    assert [step.rule for step in result.steps] == ["inline_single_use_ctes", "lift_subqueries"]
    assert all(step.verification.status is VerificationStatus.PROVEN for step in result.steps)
    assert result.verification.status is VerificationStatus.PROVEN
    assert "__lifted_subquery_001" in result.sql
    assert result.success


def test_duplicate_rule_name_is_rejected():
    class Clash(RewriteRule):
        name = "lift_subqueries"
        summary = "clash"

    with pytest.raises(ValueError, match="already registered"):
        engine.register_rule(Clash)
