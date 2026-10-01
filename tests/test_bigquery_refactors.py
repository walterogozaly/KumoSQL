"""Everyday BigQuery refactors the algebraic prover should prove, and near misses it must not."""

import pytest

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, TableConstraints

SCHEMA = {
    "orders": ["order_id", "customer_id", "amount", "status", "created_at", "region"],
    "customers": ["customer_id", "name", "country"],
}
CONSTRAINTS = {
    "orders": TableConstraints(not_null=frozenset({"order_id", "customer_id", "amount"}), keys=(("order_id",),)),
    "customers": TableConstraints(not_null=frozenset({"customer_id"}), keys=(("customer_id",),)),
}

PROVEN = {
    "cte inlined": (
        "WITH o AS (SELECT * FROM orders WHERE status = 'paid') SELECT customer_id, SUM(amount) AS total FROM o GROUP BY customer_id",
        "SELECT customer_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY customer_id",
    ),
    "countif": (
        "SELECT customer_id, COUNTIF(status = 'paid') AS n FROM orders GROUP BY customer_id",
        "SELECT customer_id, COUNT(CASE WHEN status = 'paid' THEN 1 END) AS n FROM orders GROUP BY customer_id",
    ),
    "ifnull and coalesce": (
        "SELECT IFNULL(region, 'none') AS r FROM orders",
        "SELECT COALESCE(region, 'none') AS r FROM orders",
    ),
    "distinct and group by": (
        "SELECT DISTINCT customer_id FROM orders",
        "SELECT customer_id FROM orders GROUP BY customer_id",
    ),
    "join order": (
        "SELECT o.order_id, c.name FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
        "SELECT o.order_id, c.name FROM customers c JOIN orders o ON c.customer_id = o.customer_id",
    ),
    "filter pushed below a USING join": (
        "SELECT o.order_id FROM orders o JOIN customers c USING (customer_id) WHERE c.country = 'US'",
        "SELECT o.order_id FROM orders o JOIN (SELECT * FROM customers WHERE country = 'US') c ON o.customer_id = c.customer_id",
    ),
    "qualify and a row_number subquery": (
        "SELECT * EXCEPT (rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) AS rn FROM orders) WHERE rn = 1",
        "SELECT * FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) = 1",
    ),
    "in and exists": (
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers WHERE country = 'US')",
        "SELECT order_id FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = o.customer_id AND c.country = 'US')",
    ),
    "safe_divide": (
        "SELECT SAFE_DIVIDE(amount, customer_id) AS h FROM orders",
        "SELECT IF(customer_id = 0, NULL, amount / customer_id) AS h FROM orders",
    ),
    "case and if": (
        "SELECT CASE WHEN amount > 10 THEN 'big' ELSE 'small' END AS size FROM orders",
        "SELECT IF(amount > 10, 'big', 'small') AS size FROM orders",
    ),
    "select except": (
        "SELECT * EXCEPT (region) FROM orders",
        "SELECT order_id, customer_id, amount, status, created_at FROM orders",
    ),
    "left join null filter and not exists": (
        "SELECT c.customer_id FROM customers c LEFT JOIN orders o ON o.customer_id = c.customer_id WHERE o.order_id IS NULL",
        "SELECT customer_id FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)",
    ),
    "group by ordinal": (
        "SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m, COUNT(*) AS n FROM orders GROUP BY m",
        "SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m, COUNT(*) AS n FROM orders GROUP BY 1",
    ),
    "between and not in": (
        "SELECT order_id FROM orders WHERE amount BETWEEN 1 AND 5 AND status NOT IN ('a', 'b')",
        "SELECT order_id FROM orders WHERE amount >= 1 AND amount <= 5 AND status <> 'a' AND status <> 'b'",
    ),
    "sum case and filter": (
        "SELECT customer_id, SUM(CASE WHEN status = 'paid' THEN amount END) AS s FROM orders GROUP BY customer_id",
        "SELECT customer_id, SUM(amount) FILTER (WHERE status = 'paid') AS s FROM orders GROUP BY customer_id",
    ),
    "window through a cte": (
        "SELECT order_id, SUM(amount) OVER (PARTITION BY customer_id ORDER BY created_at) AS run FROM orders",
        "WITH base AS (SELECT * FROM orders) SELECT order_id, SUM(amount) OVER (PARTITION BY customer_id ORDER BY created_at) AS run FROM base",
    ),
    "select-list scalar aggregate and left join": (
        "SELECT c.customer_id, (SELECT SUM(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) AS t FROM customers c",
        "SELECT c.customer_id, g.s AS t FROM customers c LEFT JOIN (SELECT customer_id, SUM(amount) AS s FROM orders GROUP BY customer_id) g ON c.customer_id = g.customer_id",
    ),
    "sum of grouped sums": (
        "WITH t AS (SELECT customer_id, SUM(amount) AS s FROM orders GROUP BY customer_id) SELECT SUM(s) AS total FROM t",
        "SELECT SUM(amount) AS total FROM orders",
    ),
    "in over a union all": (
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers UNION ALL SELECT order_id FROM orders)",
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers) OR customer_id IN (SELECT order_id FROM orders)",
    ),
    "min and count with a null guard": (
        "SELECT MIN(region) AS m, COUNT(*) AS n FROM orders WHERE region IS NOT NULL",
        "SELECT MIN(region) AS m, COUNT(region) AS n FROM orders",
    ),
    "trim and lower commute, || is concat": (
        "SELECT LOWER(TRIM(status)) || region AS x FROM orders",
        "SELECT CONCAT(TRIM(LOWER(status)), region) AS x FROM orders",
    ),
    "limit in a derived table": (
        "SELECT order_id FROM (SELECT order_id, amount FROM orders ORDER BY amount, order_id LIMIT 5)",
        "SELECT order_id FROM orders ORDER BY amount, order_id LIMIT 5",
    ),
}

NOT_PROVEN = {
    "limit in a derived table, ordered the other way": (
        "SELECT order_id FROM (SELECT order_id, amount FROM orders ORDER BY amount, order_id LIMIT 5)",
        "SELECT order_id FROM orders ORDER BY amount DESC, order_id LIMIT 5",
    ),
    "inner join is not a left join to the grouped table": (
        "SELECT c.customer_id, (SELECT SUM(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) AS t FROM customers c",
        "SELECT c.customer_id, g.s AS t FROM customers c JOIN (SELECT customer_id, SUM(amount) AS s FROM orders GROUP BY customer_id) g ON c.customer_id = g.customer_id",
    ),
    "sum of grouped counts reads NULL for no rows": (
        "SELECT SUM(n) AS total FROM (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id)",
        "SELECT COUNT(*) AS total FROM orders",
    ),
    "null guard under a group by drops groups": (
        "SELECT customer_id, COUNT(*) AS n FROM orders WHERE region IS NOT NULL GROUP BY customer_id",
        "SELECT customer_id, COUNT(region) AS n FROM orders GROUP BY customer_id",
    ),
    "in over a union is not in over one branch": (
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers UNION ALL SELECT order_id FROM orders)",
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers)",
    ),
    "boundary changed": ("SELECT order_id FROM orders WHERE amount > 5", "SELECT order_id FROM orders WHERE amount >= 5"),
    "safe_divide is not plain division": (
        "SELECT SAFE_DIVIDE(amount, customer_id) AS h FROM orders",
        "SELECT amount / customer_id AS h FROM orders",
    ),
    "join on another column": (
        "SELECT o.order_id FROM orders o JOIN customers c USING (customer_id)",
        "SELECT o.order_id FROM orders o JOIN customers c ON o.order_id = c.customer_id",
    ),
    "except drops a different column": (
        "SELECT * EXCEPT (region) FROM orders",
        "SELECT order_id, customer_id, amount, region, created_at FROM orders",
    ),
    "window ordered the other way": (
        "SELECT order_id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at) AS n FROM orders",
        "SELECT order_id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) AS n FROM orders",
    ),
}


@pytest.mark.parametrize("left, right", PROVEN.values(), ids=PROVEN.keys())
def test_everyday_refactors_are_proven(left, right):
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=CONSTRAINTS, compare_names=False, dialect="bigquery")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


@pytest.mark.parametrize("left, right", NOT_PROVEN.values(), ids=NOT_PROVEN.keys())
def test_near_misses_are_not_proven(left, right):
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=CONSTRAINTS, compare_names=False, dialect="bigquery")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
