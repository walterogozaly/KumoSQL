"""FK join elimination when the child is a derived table that only filters and renames one table's columns.

``(SELECT customer_id AS cid FROM orders WHERE total > 1) AS o JOIN customers c ON o.cid = c.id`` keeps each
``orders`` row once (NOT NULL reference to a unique key), so the join is dropped. Anything else a derived
table can do (group, distinct, limit, compute, join) leaves the join in place.
"""

import pytest
import sqlglot

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.fk_rules import drop_fk_join  # noqa: E402
from kumosql.random_check import Column, Schema, Table, find_difference, prover_constraints  # noqa: E402

SHOP = Schema(
    [
        Table("customers", [Column("id", "int", True), Column("name", "text", True)], [("id",)]),
        Table("orders", [Column("id", "int", True), Column("customer_id", "int", True), Column("total", "int", True)], [("id",)], [(("customer_id",), "customers", ("id",))]),
    ]
)
NULLABLE = Schema(
    [
        SHOP.tables[0],
        Table("orders", [Column("id", "int", True), Column("customer_id", "int"), Column("total", "int", True)], [("id",)], [(("customer_id",), "customers", ("id",))]),
    ]
)
KEYS = {"customers": [("id",)], "orders": [("id",)]}
NOT_NULL = {"customers": frozenset({"id", "name"}), "orders": frozenset({"id", "customer_id", "total"})}
FOREIGN_KEYS = {"orders": [(("customer_id",), "customers", ("id",))]}
FILTERED = "(SELECT customer_id AS cid FROM orders WHERE total > 1) AS o"


def dropped(sql):
    out = drop_fk_join(sqlglot.parse_one(sql), KEYS, NOT_NULL, FOREIGN_KEYS)
    return out.sql() if out else None


def test_the_join_to_the_parent_is_dropped():
    assert dropped(f"SELECT o.cid FROM {FILTERED} JOIN customers c ON o.cid = c.id") == f"SELECT o.cid FROM {FILTERED}"


def test_parent_columns_read_through_the_join_become_child_columns():
    out = dropped("SELECT o.cid, c.id FROM (SELECT customer_id AS cid, total FROM orders WHERE total > 1) AS o JOIN customers c ON o.cid = c.id")
    assert out == "SELECT o.cid, o.cid AS id FROM (SELECT customer_id AS cid, total FROM orders WHERE total > 1) AS o"


@pytest.mark.parametrize(
    "child",
    [
        "(SELECT customer_id AS cid FROM orders GROUP BY customer_id) AS o",
        "(SELECT DISTINCT customer_id AS cid FROM orders) AS o",
        "(SELECT customer_id + 0 AS cid FROM orders) AS o",
        "(SELECT customer_id AS cid FROM orders LIMIT 3) AS o",
        "(SELECT customer_id AS cid FROM orders x JOIN customers y ON x.customer_id = y.id) AS o",
    ],
)
def test_a_derived_table_that_does_more_than_filter_and_rename_keeps_the_join(child):
    assert dropped(f"SELECT o.cid FROM {child} JOIN customers c ON o.cid = c.id") is None


def test_reading_another_parent_column_keeps_the_join():
    assert dropped(f"SELECT o.cid, c.name FROM {FILTERED} JOIN customers c ON o.cid = c.id") is None


def test_the_dropped_join_is_proven_and_agrees_on_random_databases():
    left = f"SELECT o.cid FROM {FILTERED} JOIN customers c ON o.cid = c.id"
    right = f"SELECT o.cid FROM {FILTERED}"
    proof = prove_equivalent_algebraic(left, right, schema=SHOP.columns, constraints=prover_constraints(SHOP), dialect="postgres", compare_names=False)
    assert proof.proven, proof.reason
    assert find_difference(SHOP, left, right, trials=100) is None


def test_a_nullable_child_column_is_not_dropped_and_not_proven():
    """A NULL reference matches no parent, so the join removes that child row: the two sides differ."""

    left = f"SELECT o.cid FROM {FILTERED} JOIN customers c ON o.cid = c.id"
    right = f"SELECT o.cid FROM {FILTERED}"
    proof = prove_equivalent_algebraic(left, right, schema=NULLABLE.columns, constraints=prover_constraints(NULLABLE), dialect="postgres", compare_names=False)
    assert not proof.proven
    assert find_difference(NULLABLE, left, right, trials=200) is not None


def test_without_the_declared_foreign_key_the_join_stays():
    plain = Schema([Table(t.name, t.columns, t.keys) for t in SHOP.tables])
    left = f"SELECT o.cid FROM {FILTERED} JOIN customers c ON o.cid = c.id"
    right = f"SELECT o.cid FROM {FILTERED}"
    proof = prove_equivalent_algebraic(left, right, schema=plain.columns, constraints=prover_constraints(plain), dialect="postgres", compare_names=False)
    assert not proof.proven
