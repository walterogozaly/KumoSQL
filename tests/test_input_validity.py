"""Inputs BigQuery would reject earn no proof, and an input that only parsed in recovery earns no trust.

The refusal cases below were each rejected by a BigQuery dry run (project kumosql, 0 bytes) with the message
quoted beside them. The controls are valid and must keep passing.
"""

from __future__ import annotations

import pytest
import sqlglot

from kumosql import prove_equivalent
from kumosql.input_validity import invalid_input_reason
from kumosql.rewrite import VerificationStatus, apply_rule, apply_rules

REJECTED = {
    # "The HAVING clause requires GROUP BY or aggregation to be present"
    "having without group": "SELECT region, c FROM (SELECT 'a' AS region, 1 AS c) AS t HAVING c > 10",
    "having over a real table": "SELECT 1 AS a FROM t HAVING TRUE",
    # "Name missing not found inside q"
    "qualified missing column": "SELECT q.missing FROM (SELECT 1 AS x) AS q",
    "qualified missing CTE column": "WITH r AS (SELECT 1 AS region) SELECT r.customer_id FROM r",
    # "Unrecognized name: customer_id"
    "bare missing column": "WITH r AS (SELECT 1 AS region) SELECT * FROM (SELECT customer_id, region FROM r) AS x",
    "bare missing column of a derived table": "SELECT missing FROM (SELECT 1 AS x) AS q",
}

VALID = {
    "having with group": "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1",
    "having with aggregate": "SELECT COUNT(*) FROM t HAVING COUNT(*) > 1",
    "having over a select alias": "SELECT COUNT(*) AS c FROM (SELECT 1 AS x) AS q GROUP BY x HAVING c > 0",
    "cte column": "WITH a AS (SELECT x FROM t) SELECT a.x FROM a",
    "star source": "SELECT q.x FROM (SELECT * FROM t) AS q",
    "struct field": "SELECT x, s.f FROM (SELECT 1 AS x, STRUCT(1 AS f) AS s) AS q",
    "select alias in order by": "SELECT x AS y FROM (SELECT 1 AS x) AS q ORDER BY y",
    "correlated subquery": "SELECT x FROM (SELECT 1 AS x) AS q WHERE EXISTS (SELECT 1 FROM t WHERE t.a = x)",
    "real table next to a derived table": "SELECT x, y FROM (SELECT 1 AS x) AS q JOIN t ON TRUE",
    "unnest source": "SELECT x, e FROM (SELECT 1 AS x) AS q, UNNEST([1]) AS e",
    "whole-row reference": "SELECT q FROM (SELECT 1 AS x) AS q",
    "union outputs": "SELECT x FROM (SELECT 1 AS x UNION ALL SELECT 2) AS q",
    "unpivot adds columns": "SELECT * FROM (SELECT id, a, b FROM t) UNPIVOT (v FOR k IN (a, b)) ORDER BY id, k",
    "pivot adds columns": "SELECT * FROM (SELECT id, a, b FROM t) PIVOT (SUM(b) FOR a IN ('x', 'y')) ORDER BY id, x",
    "case differs": "SELECT Q.X FROM (SELECT 1 AS x) AS q",
}


def reason(sql: str) -> str | None:
    return invalid_input_reason(sqlglot.parse_one(sql, read="bigquery"))


@pytest.mark.parametrize("name", REJECTED)
def test_rejected_inputs_are_found(name):
    assert reason(REJECTED[name]), name


@pytest.mark.parametrize("name", VALID)
def test_valid_inputs_pass(name):
    assert reason(VALID[name]) is None, name


@pytest.mark.parametrize("name", REJECTED)
def test_structural_prover_refuses_rejected_inputs(name):
    sql = REJECTED[name]
    # The same query on both sides is the easiest proof there is; it is refused for the input, not the change.
    proof = prove_equivalent(sql, sql.replace("SELECT", "select", 1))
    assert not proof.proven, name
    assert "would not run on BigQuery" in " ".join(proof.diagnostics)


def test_rewrite_of_a_rejected_input_is_not_proven():
    fixture_q09 = (
        "WITH regions AS (SELECT region FROM `demo.sales.regions`), "
        "enriched AS (SELECT * FROM (SELECT customer_id, region FROM regions) AS r) SELECT * FROM enriched"
    )
    fixture_q21 = (
        "SELECT region, order_count FROM (SELECT region, COUNT(*) AS order_count "
        "FROM `demo.sales.orders` GROUP BY region) AS totals HAVING order_count > 10"
    )
    for sql in (fixture_q09, fixture_q21):
        result = apply_rule("lift_subqueries", sql)
        assert result.changes and result.verification.status is VerificationStatus.UNPROVEN
        assert "would not run on BigQuery" in " ".join(result.verification.details)
        assert not result.success

    control = apply_rule("lift_subqueries", "SELECT q.x FROM (SELECT 1 AS x) AS q")
    assert control.verification.status is VerificationStatus.PROVEN


RECOVERED_UNCHANGED = [
    "SELECT a FROM t WHERE 1 =",
    "SELECT a FROM t WHERE a = 1 GARBAGE GARBAGE ,,",
    "SELECT * FROM t QUALIFY",
]


@pytest.mark.parametrize("sql", RECOVERED_UNCHANGED)
def test_unchanged_output_of_a_recovered_parse_is_not_trusted(sql):
    for rule in ("lift_subqueries", "remove_trivial_predicates"):
        result = apply_rule(rule, sql)
        assert result.sql == sql
        assert result.verification.status is VerificationStatus.UNPROVEN, (rule, sql)
        assert not result.success
        assert any(check.kind == "recovered_parse" for check in result.verification.checks)

    pipeline = apply_rules(["lift_subqueries", "remove_trivial_predicates"], sql)
    assert pipeline.verification.status is VerificationStatus.UNPROVEN


def test_unchanged_output_of_a_strict_parse_is_still_trusted():
    result = apply_rule("lift_subqueries", "SELECT a FROM t WHERE a = 1")

    assert result.verification.status is VerificationStatus.UNCHANGED
    assert result.success
