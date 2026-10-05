"""``x IN UNNEST(arr)`` read as an IN subquery over UNNEST (``kumosql.nested_in_unnest``).

Three groups: the rewrite itself (what it produces and where it declines), proofs and the near misses that must
stay unproven, and a property check that runs the original and the normalized query on DuckDB (through the
BigQuery translation) over synthetic data with empty arrays, NULL scalars and repeated tags.
"""

from __future__ import annotations

from collections import Counter

import pytest
import sqlglot

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.nested_in_unnest import ASSUMPTION, rewrite_in_unnest
from kumosql.smt_equivalence import SmtStatus, TableConstraints

TYPES = {
    "orders": {
        "order_id": "INT64",
        "customer_id": "INT64",
        "tags": "ARRAY<STRING>",
        "lines": "ARRAY<STRUCT<sku STRING, qty INT64>>",
    },
    "customers": {"customer_id": "INT64", "name": "STRING", "scores": "ARRAY<INT64>"},
}
SCHEMA = {table: list(columns) for table, columns in TYPES.items()}


def rewritten(sql: str, *, types=TYPES, not_null=None) -> tuple[str, set[str]]:
    assumptions: set[str] = set()
    tree = rewrite_in_unnest(sqlglot.parse_one(sql, read="bigquery"), types, not_null, assumptions)
    return tree.sql(dialect="bigquery"), assumptions


def prove(left: str, right: str, *, constraints=None, types=TYPES) -> SmtStatus:
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, types=types, constraints=constraints, dialect="bigquery", timeout_ms=10000
    ).status


# ---------------------------------------------------------------- the rewrite


def test_in_unnest_becomes_a_membership_subquery():
    sql, assumptions = rewritten("SELECT order_id FROM orders WHERE customer_id IN UNNEST(ARRAY_CONCAT(tags, tags))")
    assert "IN (SELECT in_unnest_1 FROM UNNEST(ARRAY_CONCAT(tags, tags)) AS in_unnest_1)" in sql
    assert not assumptions


def test_not_in_unnest_keeps_its_not():
    sql, _ = rewritten("SELECT name FROM customers WHERE name NOT IN UNNEST(ARRAY_CONCAT(scores, scores))")
    assert sql.startswith("SELECT name FROM customers WHERE NOT name IN (SELECT") or "NOT" in sql


def test_literal_array_is_an_in_list():
    sql, _ = rewritten("SELECT 1 FROM customers WHERE customer_id IN UNNEST([1, 2, NULL])")
    assert "IN (1, 2, NULL)" in sql and "UNNEST" not in sql


def test_empty_literal_array_is_false():
    sql, _ = rewritten("SELECT 1 FROM customers WHERE customer_id IN UNNEST(ARRAY<INT64>[])")
    assert "WHERE FALSE" in sql


def test_struct_and_nested_literals_keep_the_subquery_reading():
    sql, _ = rewritten("SELECT 1 FROM customers WHERE customer_id IN UNNEST([ARRAY_LENGTH([1]), 2])")
    assert "UNNEST" in sql and "IN (" in sql
    sql, _ = rewritten("SELECT 1 FROM customers WHERE STRUCT(customer_id AS a) IN UNNEST([STRUCT(1 AS a)])")
    assert "UNNEST" in sql


def test_constant_against_stored_array_is_exists_and_records_the_storage_fact():
    sql, assumptions = rewritten("SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)")
    assert "NOT EXISTS(SELECT 1 FROM UNNEST(tags) AS in_unnest_1 WHERE in_unnest_1 = 'sale')" in sql
    assert assumptions == {ASSUMPTION}


@pytest.mark.parametrize(
    "sql, types",
    [
        # a nullable left side: a NULL value against a non-empty array is NULL, not FALSE
        ("SELECT order_id FROM orders WHERE NOT customer_id IN UNNEST(ARRAY_CONCAT(tags, tags))", TYPES),
        ("SELECT name FROM customers WHERE customer_id IN UNNEST(scores)", TYPES),
        # an array that is not a plain column of a stored table (it can be NULL or hold a NULL element)
        ("SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(ARRAY_CONCAT(tags, ['x']))", TYPES),
        ("SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags[SAFE_OFFSET(0)])", TYPES),
        # a column whose table the schema does not type as an array, or does not type at all
        ("SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)", {"orders": {"tags": "STRING"}}),
        ("SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)", {}),
        # an array of structs
        ("SELECT order_id FROM orders WHERE NOT 'a' IN UNNEST(lines)", TYPES),
        # a bare name that more than one source could provide
        ("SELECT o.order_id FROM orders AS o, customers AS c WHERE NOT 1 IN UNNEST(scores) AND o.order_id = 1", {**TYPES, "orders": {**TYPES["orders"], "scores": "ARRAY<INT64>"}}),
        # a derived table the array may come from
        ("SELECT d.order_id FROM (SELECT order_id, tags FROM orders) AS d WHERE NOT 'sale' IN UNNEST(d.tags)", TYPES),
    ],
)
def test_storage_reading_declines_when_its_premises_fail(sql, types):
    text, assumptions = rewritten(sql, types=types)
    assert "EXISTS" not in text and not assumptions
    assert "UNNEST" in text


def test_a_declared_not_null_column_counts_as_a_constant():
    sql = "SELECT name FROM customers WHERE NOT customer_id IN UNNEST(scores)"
    text, assumptions = rewritten(sql, not_null={"customers": {"customer_id"}})
    assert "NOT EXISTS(" in text and assumptions == {ASSUMPTION}


def test_not_null_is_not_trusted_next_to_an_outer_join():
    sql = "SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id WHERE NOT o.order_id IN UNNEST(c.scores)"
    text, assumptions = rewritten(sql, not_null={"orders": {"order_id"}})
    assert "EXISTS" not in text and not assumptions


def test_a_fresh_name_never_captures_a_column():
    sql, _ = rewritten("SELECT 1 FROM customers WHERE in_unnest_1 IN UNNEST(ARRAY_CONCAT(scores, scores))")
    assert sql.count("in_unnest_1") == 3  # the compared column, the element, its alias

    # two tests in one query get two names
    sql, _ = rewritten("SELECT 1 FROM customers WHERE 1 IN UNNEST(ARRAY_CONCAT(scores, scores)) AND 2 IN UNNEST(ARRAY_CONCAT(scores, scores))")
    assert "in_unnest_1" in sql and "in_unnest_2" in sql


# ---------------------------------------------------------------- proofs and near misses


PROVEN = [
    # membership against a stored array: a filter, a value, and its negation
    (
        "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'sale')",
    ),
    (
        "SELECT order_id, 'sale' IN UNNEST(tags) AS hit FROM orders",
        "SELECT order_id, EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'sale') AS hit FROM orders",
    ),
    (
        "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders AS o WHERE NOT EXISTS (SELECT 1 FROM UNNEST(o.tags) AS t WHERE t = 'sale')",
    ),
    (
        "SELECT order_id FROM orders WHERE 'sale' NOT IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'sale')",
    ),
    # a nullable left side in a filter: NULL and FALSE both drop the row
    (
        "SELECT name FROM customers WHERE customer_id IN UNNEST(scores)",
        "SELECT name FROM customers AS c WHERE EXISTS (SELECT 1 FROM UNNEST(c.scores) AS s WHERE s = c.customer_id)",
    ),
    # a literal array is an IN list, NOT IN included
    (
        "SELECT name FROM customers WHERE name IN UNNEST(['a', 'b'])",
        "SELECT name FROM customers WHERE name IN ('a', 'b')",
    ),
    (
        "SELECT name FROM customers WHERE name NOT IN UNNEST(['a', 'b'])",
        "SELECT name FROM customers WHERE name NOT IN ('a', 'b')",
    ),
    (
        "SELECT name, name IN UNNEST(['a', NULL]) AS hit FROM customers",
        "SELECT name, name IN ('a', NULL) AS hit FROM customers",
    ),
    (
        "SELECT name FROM customers WHERE name IN UNNEST(ARRAY<STRING>[])",
        "SELECT name FROM customers WHERE FALSE",
    ),
    # membership is a semi-join: under DISTINCT it is the join over the unnested rows
    (
        "SELECT DISTINCT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT DISTINCT o.order_id FROM orders AS o, UNNEST(o.tags) AS t WHERE t = 'sale'",
    ),
]


@pytest.mark.parametrize("left, right", PROVEN)
def test_proves_membership_over_unnest(left, right):
    assert prove(left, right) is SmtStatus.PROVEN_EQUIVALENT


def test_a_declared_not_null_left_side_proves_the_value_and_its_negation():
    constraints = {"customers": TableConstraints(not_null=frozenset({"customer_id"}))}
    value = (
        "SELECT name, customer_id IN UNNEST(scores) AS hit FROM customers",
        "SELECT name, EXISTS (SELECT 1 FROM UNNEST(scores) AS s WHERE s = customer_id) AS hit FROM customers",
    )
    negated = (
        "SELECT name FROM customers WHERE NOT customer_id IN UNNEST(scores)",
        "SELECT name FROM customers WHERE NOT EXISTS (SELECT 1 FROM UNNEST(scores) AS s WHERE s = customer_id)",
    )
    for left, right in (value, negated):
        assert prove(left, right, constraints=constraints) is SmtStatus.PROVEN_EQUIVALENT
        # without the declaration the left side may be NULL: a NULL against a non-empty array is NULL, not FALSE
        assert prove(left, right) is not SmtStatus.PROVEN_EQUIVALENT


def test_the_storage_fact_is_listed_when_a_proof_needs_it():
    result = prove_equivalent_algebraic(
        "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'sale')",
        schema=SCHEMA, types=TYPES, dialect="bigquery", timeout_ms=10000,
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert ASSUMPTION in result.assumptions


NEVER_PROVEN = [
    # a NULL left side against a non-empty array: NOT IN drops the row, NOT EXISTS keeps it
    (
        "SELECT name FROM customers WHERE NOT customer_id IN UNNEST(scores)",
        "SELECT name FROM customers WHERE NOT EXISTS (SELECT 1 FROM UNNEST(scores) AS s WHERE s = customer_id)",
    ),
    (
        "SELECT name, customer_id IN UNNEST(scores) AS hit FROM customers",
        "SELECT name, EXISTS (SELECT 1 FROM UNNEST(scores) AS s WHERE s = customer_id) AS hit FROM customers",
    ),
    # membership is not a length test, and not an unfiltered existence test
    (
        "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0",
    ),
    (
        "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE EXISTS (SELECT 1 FROM UNNEST(tags) AS t)",
    ),
    (
        "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) = 0",
    ),
    (
        "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)",
    ),
    # another constant, another list, a list with a NULL the other lacks
    (
        "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
        "SELECT order_id FROM orders WHERE 'gift' IN UNNEST(tags)",
    ),
    (
        "SELECT name FROM customers WHERE name IN UNNEST(['a', 'b'])",
        "SELECT name FROM customers WHERE name IN ('a', 'c')",
    ),
    (
        "SELECT name FROM customers WHERE name IN UNNEST(['a', 'b'])",
        "SELECT name FROM customers WHERE name IN ('a', 'b', 'c')",
    ),
    (
        "SELECT name FROM customers WHERE name NOT IN UNNEST(['a', NULL])",
        "SELECT name FROM customers WHERE name NOT IN ('a')",
    ),
    (
        "SELECT name FROM customers WHERE 'z' NOT IN UNNEST(['a', NULL])",
        "SELECT name FROM customers WHERE NOT EXISTS (SELECT 1 FROM UNNEST(['a', NULL]) AS e WHERE e = 'z')",
    ),
    # an array that is not a stored column can hold a NULL element
    (
        "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(ARRAY_CONCAT(tags, ['x', CAST(NULL AS STRING)]))",
        "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(ARRAY_CONCAT(tags, ['x', CAST(NULL AS STRING)])) AS t WHERE t = 'sale')",
    ),
]


@pytest.mark.parametrize("left, right", NEVER_PROVEN)
def test_never_proves_the_near_misses(left, right):
    assert prove(left, right) is not SmtStatus.PROVEN_EQUIVALENT


def test_the_storage_fact_needs_the_schema_to_type_the_column_as_an_array():
    left = "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)"
    right = "SELECT order_id FROM orders WHERE NOT EXISTS (SELECT 1 FROM UNNEST(tags) AS t WHERE t = 'sale')"
    assert prove(left, right, types={}) is not SmtStatus.PROVEN_EQUIVALENT
    assert prove(left, right, types={"orders": {"tags": "STRING"}}) is not SmtStatus.PROVEN_EQUIVALENT


# ---------------------------------------------------------------- the same answers on DuckDB

duckdb = pytest.importorskip("duckdb")

from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful  # noqa: E402

ORDERS = [
    # (order_id, customer_id, tags): a stored array is never NULL and never holds a NULL element
    (1, 10, ["sale", "gift"]),
    (2, None, ["sale", "sale"]),
    (3, 11, []),
    (4, None, []),
    (5, 12, ["gift"]),
    (6, 10, ["sale"]),
]
CUSTOMERS = [
    (10, "a", [10, 11]),
    (11, "b", []),
    (None, "c", [1, 2]),
    (None, "d", []),
    (12, "e", [5, 12, 12]),
    (13, None, [13]),
]
NOT_NULL_CUSTOMERS = [row for row in CUSTOMERS if row[0] is not None]


def database(customers):
    db = duckdb.connect(":memory:")
    configure(db)
    db.execute("CREATE TABLE orders(order_id BIGINT, customer_id BIGINT, tags VARCHAR[], lines STRUCT(sku VARCHAR, qty BIGINT)[])")
    db.execute("CREATE TABLE customers(customer_id BIGINT, name VARCHAR, scores BIGINT[])")
    for order_id, customer_id, tags in ORDERS:
        db.execute("INSERT INTO orders VALUES (?, ?, ?, [])", [order_id, customer_id, tags])
    for customer_id, name, scores in customers:
        db.execute("INSERT INTO customers VALUES (?, ?, ?)", [customer_id, name, scores])
    return db


def rows(db, sql: str) -> Counter:
    columns = {c: t for table in TYPES.values() for c, t in table.items()}
    tree = faithful(sqlglot.parse_one(sql, read="bigquery"), columns)
    return Counter(bigquery_rows(db.execute(tree.sql(dialect="duckdb")).fetchall()))


QUERIES = [
    "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
    "SELECT order_id, 'sale' IN UNNEST(tags) AS hit FROM orders",
    "SELECT order_id, 'sale' NOT IN UNNEST(tags) AS miss FROM orders",
    "SELECT order_id FROM orders WHERE NOT 'sale' IN UNNEST(tags)",
    "SELECT order_id, CAST(customer_id AS STRING) IN UNNEST(tags) AS hit FROM orders",
    "SELECT order_id FROM orders WHERE NOT CAST(customer_id AS STRING) IN UNNEST(tags)",
    "SELECT name, customer_id IN UNNEST(scores) AS hit FROM customers",
    "SELECT name, customer_id NOT IN UNNEST(scores) AS miss FROM customers",
    "SELECT name FROM customers WHERE NOT customer_id IN UNNEST(scores)",
    "SELECT name, 12 IN UNNEST(scores) AS hit FROM customers",
    "SELECT name, NOT 12 IN UNNEST(scores) AS miss FROM customers",
    "SELECT name, customer_id IN UNNEST(ARRAY_CONCAT(scores, [CAST(NULL AS INT64)])) AS hit FROM customers",
    "SELECT name, 99 IN UNNEST(ARRAY_CONCAT(scores, [CAST(NULL AS INT64)])) AS hit FROM customers",
    "SELECT name, NOT 99 IN UNNEST(ARRAY_CONCAT(scores, [CAST(NULL AS INT64)])) AS miss FROM customers",
    "SELECT name, 10 IN UNNEST(CAST(NULL AS ARRAY<INT64>)) AS hit FROM customers",
    "SELECT name, customer_id IN UNNEST(ARRAY<INT64>[]) AS hit FROM customers",
    "SELECT name, customer_id IN UNNEST([10, NULL]) AS hit FROM customers",
    "SELECT name, customer_id NOT IN UNNEST([10, 11]) AS miss FROM customers",
    "SELECT name, name IN UNNEST(['a', NULL]) AS hit FROM customers",
    "SELECT name, 'z' IN UNNEST(['a', NULL]) AS hit FROM customers",
    "SELECT DISTINCT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
    "SELECT COUNT(*) AS n FROM orders AS o WHERE 'gift' IN UNNEST(o.tags) AND o.order_id > 1",
]


@pytest.mark.parametrize("customers", [CUSTOMERS, NOT_NULL_CUSTOMERS, []], ids=["nullable", "not-null", "empty"])
@pytest.mark.parametrize("query", QUERIES)
def test_rewritten_query_returns_the_same_rows(query, customers):
    # customer_id is declared NOT NULL only for the data that respects it
    not_null = {"customers": frozenset({"customer_id"})} if customers is not CUSTOMERS else None
    db = database(customers)
    after = rewrite_in_unnest(sqlglot.parse_one(query, read="bigquery"), TYPES, not_null, set()).sql(dialect="bigquery")
    assert rows(db, after) == rows(db, query)
    if "CAST(NULL AS ARRAY" not in query:  # normalize folds a typed NULL array to an untyped NULL, which DuckDB cannot unnest
        assert rows(db, normalize(query, schema=SCHEMA, types=TYPES, not_null=not_null)) == rows(db, query)


def test_the_traps_differ_on_the_data():
    db = database(CUSTOMERS)
    pairs = [
        (
            "SELECT name FROM customers WHERE NOT customer_id IN UNNEST(scores)",
            "SELECT name FROM customers WHERE NOT EXISTS (SELECT 1 FROM UNNEST(scores) AS s WHERE s = customer_id)",
        ),
        (
            "SELECT order_id FROM orders WHERE 'sale' IN UNNEST(tags)",
            "SELECT order_id FROM orders WHERE ARRAY_LENGTH(tags) > 0",
        ),
        (
            "SELECT name FROM customers WHERE 'z' NOT IN UNNEST(['a', NULL])",
            "SELECT name FROM customers WHERE 'z' NOT IN ('a')",
        ),
    ]
    for left, right in pairs:
        assert rows(db, left) != rows(db, right)


@pytest.mark.parametrize("left, right", PROVEN)
def test_every_proven_pair_agrees_on_the_data(left, right):
    for customers in (CUSTOMERS, NOT_NULL_CUSTOMERS, []):
        db = database(customers)
        assert rows(db, left) == rows(db, right)
