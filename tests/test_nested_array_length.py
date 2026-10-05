"""ARRAY_LENGTH facts for stored ARRAY columns (``kumosql.nested_array_length``).

A stored array is never NULL and has no NULL elements, so ``ARRAY_LENGTH`` of it is never NULL and equals the row count of its
UNNEST. The rule applies only to a column the schema types as an ARRAY of a base table. Each decline below is a place where
the fact is false (computed arrays, struct fields, null-extended join sides) or not known (no column types).
"""

from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.nested_array_length import ASSUMPTION  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

TYPES = {
    "orders": {"order_id": "INT64", "customer_id": "INT64", "tags": "ARRAY<STRING>", "ship": "STRUCT<city STRING, marks ARRAY<STRING>>", "lines": "ARRAY<STRUCT<sku STRING, qty INT64>>"},
    "customers": {"customer_id": "INT64", "name": "STRING", "emails": "ARRAY<STRING>"},
}
SCHEMA = {table: list(columns) for table, columns in TYPES.items()}


def prove(left: str, right: str, **kwargs):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=kwargs.pop("types", TYPES), dialect="bigquery", timeout_ms=20000, **kwargs)


def proven(left: str, right: str) -> bool:
    return prove(left, right).status is SmtStatus.PROVEN_EQUIVALENT


def norm(sql: str, types=TYPES) -> tuple[str, set[str]]:
    assumptions: set[str] = set()
    return normalize(sql, schema=SCHEMA, dialect="bigquery", types=types, _assumptions=assumptions), assumptions


# ---------------------------------------------------------------- the rule fires


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) >= 1"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) != 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) <> 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE 0 < ARRAY_LENGTH(tags)"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE NOT ARRAY_LENGTH(tags) = 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE NOT (ARRAY_LENGTH(tags) <= 0)"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(tags)) > 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags))"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) < 1"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) <= 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(tags)) = 0"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE NOT ARRAY_LENGTH(tags) > 0"),
        ("SELECT order_id FROM orders WHERE tags IS NOT NULL", "SELECT order_id FROM orders"),
        ("SELECT order_id FROM orders WHERE tags IS NULL", "SELECT order_id FROM orders WHERE FALSE"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) IS NOT NULL", "SELECT order_id FROM orders"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) >= 0", "SELECT order_id FROM orders"),
        ("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) < 0", "SELECT order_id FROM orders WHERE FALSE"),
        ("SELECT order_id, ARRAY_LENGTH(tags) AS n FROM orders", "SELECT order_id, (SELECT COUNT(*) FROM UNNEST(tags)) AS n FROM orders"),
        ("SELECT order_id, ARRAY_LENGTH(tags) AS n FROM orders", "SELECT order_id, IFNULL(ARRAY_LENGTH(tags), 0) AS n FROM orders"),
        ("SELECT order_id, ARRAY_LENGTH(tags) AS n FROM orders", "SELECT order_id, COALESCE(ARRAY_LENGTH(tags), -1) AS n FROM orders"),
        (
            "SELECT o.order_id FROM orders AS o WHERE ARRAY_LENGTH(o.tags) > 0",
            "SELECT p.order_id FROM orders AS p WHERE EXISTS (SELECT 1 FROM UNNEST(p.tags) AS t)",
        ),
        (
            "SELECT o.order_id FROM orders AS o WHERE EXISTS (SELECT t FROM UNNEST(o.tags) AS t)",
            "SELECT o.order_id FROM orders AS o WHERE ARRAY_LENGTH(o.tags) >= 1",
        ),
        (
            "SELECT o.order_id, o.tags IS NOT NULL AS has_tags FROM orders AS o",
            "SELECT o.order_id, TRUE AS has_tags FROM orders AS o",
        ),
        (
            "SELECT c.name FROM customers AS c JOIN orders AS o ON o.customer_id = c.customer_id WHERE ARRAY_LENGTH(o.tags) > 0",
            "SELECT c.name FROM customers AS c JOIN orders AS o ON o.customer_id = c.customer_id WHERE EXISTS (SELECT 1 FROM UNNEST(o.tags))",
        ),
        (
            "SELECT order_id FROM orders WHERE ARRAY_LENGTH(lines) > 0",
            "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(lines))",
        ),
    ],
)
def test_stored_array_forms_are_proven_equal(left, right):
    result = prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert ASSUMPTION in result.assumptions or left == right


def test_assumption_is_recorded_only_when_the_rule_fires():
    _, used = norm("SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))")
    assert ASSUMPTION in used
    _, unused = norm("SELECT order_id FROM orders WHERE order_id > 1")
    assert ASSUMPTION not in unused


def test_normal_form_is_array_length_compared_with_zero():
    text, _ = norm("SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))")
    assert "ARRAY_LENGTH(tags) > 0" in text and "EXISTS" not in text
    text, _ = norm("SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags))")
    assert "ARRAY_LENGTH(tags) = 0" in text


def test_correlated_count_in_a_subquery_resolves_through_the_outer_scope():
    left = "SELECT c.name FROM customers AS c WHERE EXISTS (SELECT 1 FROM orders AS o WHERE (SELECT COUNT(*) FROM UNNEST(o.tags)) > 0 AND o.customer_id = c.customer_id)"
    right = "SELECT c.name FROM customers AS c WHERE EXISTS (SELECT 1 FROM orders AS o WHERE ARRAY_LENGTH(o.tags) > 0 AND o.customer_id = c.customer_id)"
    assert proven(left, right)


# ---------------------------------------------------------------- the rule must not fire


def test_length_above_one_is_not_exists():
    left = "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 1"
    right = "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))"
    assert not proven(left, right)
    refuted = prove(left, right, search_counterexample=True)
    assert refuted.status is SmtStatus.NOT_EQUIVALENT


@pytest.mark.parametrize("op", ["= 1", ">= 2", "> 1", "<= 1", "< 2", "!= 1"])
def test_other_constants_are_not_zero_tests(op):
    assert not proven(f"SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) {op}", "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))")
    assert not proven(f"SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) {op}", "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags))")


def test_empty_is_not_the_opposite_of_length_above_one():
    assert not proven(
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0", "SELECT order_id FROM orders WHERE NOT ARRAY_LENGTH(tags) > 1"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id FROM orders WHERE ARRAY_CONCAT(tags, ['x']) IS NOT NULL",
        "SELECT order_id FROM orders WHERE ARRAY_CONCAT(tags, CAST(NULL AS ARRAY<STRING>)) IS NOT NULL",
        "SELECT order_id FROM orders WHERE ARRAY(SELECT t FROM UNNEST(tags) AS t WHERE t = 'a') IS NOT NULL",
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(ARRAY_CONCAT(tags, ['x'])) IS NOT NULL",
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(ARRAY(SELECT t FROM UNNEST(tags) AS t WHERE t = 'a')) > 0",
        "SELECT order_id, IFNULL(ARRAY_LENGTH(ARRAY_CONCAT(tags, ['x'])), 0) AS n FROM orders",
        "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(ARRAY_CONCAT(tags, ['x']))) > 0",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(ARRAY_CONCAT(tags, ['x'])))",
    ],
)
def test_computed_arrays_are_left_alone(sql):
    text, used = norm(sql)
    assert ASSUMPTION not in used
    assert text == normalize(sql, schema=SCHEMA, dialect="bigquery", types={}, _assumptions=set())


def test_computed_array_is_not_proven_never_null():
    assert not proven(
        "SELECT order_id FROM orders WHERE ARRAY_CONCAT(tags, CAST(NULL AS ARRAY<STRING>)) IS NOT NULL", "SELECT order_id FROM orders"
    )
    assert not proven("SELECT order_id FROM orders WHERE ARRAY_LENGTH(ARRAY_CONCAT(tags, CAST(NULL AS ARRAY<STRING>))) = 0", "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(ARRAY_CONCAT(tags, CAST(NULL AS ARRAY<STRING>))))")


def test_struct_fields_are_left_alone():
    # the struct can be NULL, which makes its array field NULL
    for sql in (
        "SELECT order_id FROM orders WHERE ship.marks IS NOT NULL",
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(ship.marks) > 0",
        "SELECT order_id FROM orders AS o WHERE ARRAY_LENGTH(o.ship.marks) = 0",
        "SELECT order_id FROM orders AS o WHERE EXISTS (SELECT 1 FROM UNNEST(o.ship.marks))",
    ):
        assert ASSUMPTION not in norm(sql)[1], sql
    assert not proven("SELECT order_id FROM orders WHERE ship.marks IS NOT NULL", "SELECT order_id FROM orders")


def test_the_null_extended_side_of_an_outer_join_is_left_alone():
    # an unmatched row has a NULL array there: ARRAY_LENGTH is NULL, UNNEST has no rows
    left = "SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id WHERE o.tags IS NULL"
    assert ASSUMPTION not in norm(left)[1]
    assert not proven(left, "SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id WHERE FALSE")
    assert not proven(
        "SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id WHERE ARRAY_LENGTH(o.tags) IS NULL",
        "SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id WHERE FALSE",
    )
    left = "SELECT c.name, ARRAY_LENGTH(o.tags) AS n FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id"
    right = "SELECT c.name, (SELECT COUNT(*) FROM UNNEST(o.tags)) AS n FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id"
    assert not proven(left, right)
    assert ASSUMPTION not in norm(right)[1]
    # also a table preceding a RIGHT or FULL join
    assert ASSUMPTION not in norm("SELECT c.name FROM orders AS o RIGHT JOIN customers AS c ON o.customer_id = c.customer_id WHERE ARRAY_LENGTH(o.tags) = 0")[1]
    assert ASSUMPTION not in norm("SELECT c.name FROM orders AS o FULL JOIN customers AS c ON o.customer_id = c.customer_id WHERE o.tags IS NULL")[1]


def test_the_preserved_side_of_an_outer_join_is_still_stored():
    sql = "SELECT c.name FROM orders AS o LEFT JOIN customers AS c ON o.customer_id = c.customer_id WHERE o.tags IS NOT NULL"
    assert ASSUMPTION in norm(sql)[1]


def test_without_array_column_types_nothing_changes():
    sql = "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0"
    other = "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))"
    for types in (None, {}, {"orders": {"order_id": "INT64", "tags": "STRING"}}, {"orders": {"order_id": "INT64"}}):
        assert ASSUMPTION not in norm(sql, types)[1]
        assert (
            prove_equivalent_algebraic(sql, other, schema=SCHEMA, types=types, dialect="bigquery", timeout_ms=20000).status
            is not SmtStatus.PROVEN_EQUIVALENT
        )


def test_a_non_array_column_is_not_a_stored_array():
    assert ASSUMPTION not in norm("SELECT order_id FROM orders WHERE customer_id IS NOT NULL")[1]
    assert not proven("SELECT order_id FROM orders WHERE customer_id IS NOT NULL", "SELECT order_id FROM orders")


def test_a_derived_table_or_cte_is_not_a_base_table():
    # the derived table may compute the array
    derived = "SELECT d.order_id FROM (SELECT order_id, ARRAY_CONCAT(tags, CAST(NULL AS ARRAY<STRING>)) AS tags FROM orders) AS d WHERE d.tags IS NOT NULL"
    assert ASSUMPTION not in norm(derived)[1]
    cte = "WITH orders AS (SELECT order_id, CAST(NULL AS ARRAY<STRING>) AS tags FROM customers_dim) SELECT order_id FROM orders WHERE tags IS NOT NULL"
    assert ASSUMPTION not in norm(cte)[1]


def test_unknown_or_ambiguous_unqualified_names_are_left_alone():
    # `tags` is a stored array of orders, but an UNNEST of structs next to it could expose a field of that name
    sql = "SELECT o.order_id FROM orders AS o, UNNEST(o.lines) AS l WHERE tags IS NOT NULL"
    assert ASSUMPTION not in norm(sql)[1]
    # `customer_id` is in both tables: not an array, and ambiguous anyway; `tags` only in orders
    sql = "SELECT c.name FROM customers AS c JOIN orders AS o ON o.customer_id = c.customer_id WHERE tags IS NOT NULL"
    assert ASSUMPTION in norm(sql)[1]
    two = {**TYPES, "other": {"tags": "ARRAY<STRING>", "k": "INT64"}}
    sql = "SELECT o.order_id FROM orders AS o JOIN other AS r ON r.k = o.order_id WHERE tags IS NOT NULL"
    assert ASSUMPTION not in norm(sql, two)[1]


def test_an_output_alias_that_hides_the_column_is_left_alone():
    sql = "SELECT order_id, customer_id AS tags FROM orders WHERE tags IS NOT NULL"
    assert ASSUMPTION not in norm(sql)[1]


def test_filtered_unnest_is_not_a_plain_count():
    for sql in (
        "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(tags) AS t WHERE t <> 'x') > 0",
        "SELECT order_id FROM orders WHERE (SELECT COUNT(DISTINCT t) FROM UNNEST(tags) AS t) > 0",
        "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(tags) AS t WITH OFFSET) > 0",
        "SELECT order_id FROM orders WHERE (SELECT SUM(LENGTH(t)) FROM UNNEST(tags) AS t) > 0",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'x')",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags) AS t LIMIT 0)",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 / (ARRAY_LENGTH(tags) - 1) FROM UNNEST(tags) AS t)",
    ):
        assert ASSUMPTION not in norm(sql)[1], sql


def test_the_count_distinct_trap_is_not_proven():
    assert not proven(
        "SELECT order_id, ARRAY_LENGTH(tags) AS n FROM orders", "SELECT order_id, (SELECT COUNT(DISTINCT t) FROM UNNEST(tags) AS t) AS n FROM orders"
    )


def test_two_different_arrays_are_not_the_same_length():
    assert not proven("SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0", "SELECT order_id FROM orders WHERE ARRAY_LENGTH(lines) > 0")


# ---------------------------------------------------------------- the rewrites agree on DuckDB with stored arrays

duckdb = pytest.importorskip("duckdb")

STORED_ROWS = [
    (1, 10, []),
    (2, 10, ["a"]),
    (3, 11, ["a", "b"]),
    (4, 12, ["a", "a", "b"]),
    (5, 12, []),
    (6, 13, ["gift"]),
]

PROPERTY_QUERIES = [
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0",
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0",
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) >= 1",
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) <> 0",
    "SELECT order_id FROM orders WHERE NOT ARRAY_LENGTH(tags) = 0",
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) < 1",
    "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 1",
    "SELECT order_id FROM orders WHERE tags IS NOT NULL",
    "SELECT order_id FROM orders WHERE tags IS NULL",
    "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags))",
    "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags))",
    "SELECT order_id FROM orders AS o WHERE EXISTS (SELECT t FROM UNNEST(o.tags) AS t)",
    "SELECT order_id, (SELECT COUNT(*) FROM UNNEST(tags)) AS n FROM orders",
    "SELECT order_id, IFNULL(ARRAY_LENGTH(tags), 0) AS n FROM orders",
    "SELECT order_id, ARRAY_LENGTH(tags) IS NULL AS missing FROM orders",
    "SELECT order_id FROM orders WHERE (SELECT COUNT(*) FROM UNNEST(tags)) = 0",
    "SELECT c.customer_id FROM orders AS o JOIN orders AS c ON c.customer_id = o.customer_id WHERE NOT EXISTS (SELECT 1 FROM UNNEST(o.tags)) AND ARRAY_LENGTH(c.tags) >= 1",
]


@pytest.mark.parametrize("sql", PROPERTY_QUERIES)
def test_normalized_query_returns_the_same_rows_on_stored_arrays(sql):
    from collections import Counter

    from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful

    columns = {"order_id": "INT64", "customer_id": "INT64", "tags": "ARRAY<STRING>"}
    types = {"orders": columns}
    rewritten = normalize(sql, schema={"orders": list(columns)}, dialect="bigquery", types=types, _assumptions=set())
    db = duckdb.connect(":memory:")
    try:
        configure(db)
        db.execute("CREATE TABLE orders(order_id BIGINT, customer_id BIGINT, tags VARCHAR[])")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?)", STORED_ROWS)

        def run(text: str):
            return bigquery_rows(db.execute(faithful(sqlglot.parse_one(text, read="bigquery"), columns).sql(dialect="duckdb")).fetchall())

        assert Counter(run(sql)) == Counter(run(rewritten))
    finally:
        db.close()
