"""Run cleanup rewrites on synthetic data and require identical results.

This is an independent check on the cleanup rules: the prover must prove each
rewrite, and DuckDB must also return the same rows (NULLs included) on every
seed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from kumosql import VerificationStatus, apply_rules
from kumosql.result_equivalence import assert_result_equivalent


SCHEMA = {
    "p.d.customers": {"id": "INT64", "name": "STRING", "region": "STRING", "active": "BOOL"},
    "p.d.orders": {"order_id": "INT64", "customer_id": "INT64", "amount": "FLOAT64"},
}

RULES = [
    "lift_subqueries",
    "remove_trivial_predicates",
    "remove_redundant_parentheses",
    "deduplicate_ctes",
    "remove_unused_ctes",
]

CORPUS = {
    "where_one_equals_one": "SELECT id FROM `p.d.customers` WHERE 1 = 1 AND (active)",
    "nullable_or_false": "SELECT id FROM `p.d.customers` WHERE (active OR FALSE) AND TRUE",
    "not_folded": "SELECT id FROM `p.d.customers` WHERE NOT (2 < 1) AND ((region = 'a') OR (name = 'b'))",
    "join_on_trivial": """
SELECT c.id, o.amount
FROM `p.d.customers` AS c
LEFT JOIN `p.d.orders` AS o ON (o.customer_id = c.id) AND 1 = 1
WHERE TRUE""",
    "having_with_group": """
SELECT customer_id, COUNT(*) AS n
FROM `p.d.orders`
WHERE 1 = 1
GROUP BY customer_id
HAVING (COUNT(*) > 1) AND TRUE""",
    "duplicate_and_unused_ctes": """
WITH unused AS (SELECT 1 AS z),
a AS (SELECT id, region FROM `p.d.customers` WHERE 1 = 1),
b AS (SELECT id, region FROM `p.d.customers` WHERE 1 = 1),
x AS (SELECT a1.id FROM a AS a1),
y AS (SELECT b1.id FROM b AS b1)
SELECT x.id FROM x JOIN y ON x.id = y.id""",
    "subquery_cleanup": """
SELECT s.id
FROM (SELECT id FROM `p.d.customers` WHERE (TRUE) AND (region IS NOT NULL)) AS s
WHERE 1 = 1""",
}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_cleanup_rewrites_return_identical_results(name):
    source = CORPUS[name]
    result = apply_rules(RULES, source)

    assert result.sql != source
    assert result.verification.status is VerificationStatus.PROVEN, result.verification.details
    assert_result_equivalent(source, result.sql, SCHEMA, seeds=range(8))
