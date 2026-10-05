"""``random_check`` draws databases that respect declared foreign keys.

Parents are generated first and a child's key values are copied from a parent row (or left NULL), so a
rewrite that is only valid under a foreign key is not refuted by an orphan row. A schema with no foreign
key keeps its draws exactly as before.
"""

import hashlib
import json

import pytest

pytest.importorskip("duckdb")

from kumosql import random_check as rc  # noqa: E402
from kumosql.random_check import Column, Schema, Table  # noqa: E402

# the child is declared before its parent, and a grandchild hangs off the child
SHOP = Schema(
    [
        Table("items", [Column("item_id", "int", True), Column("order_id", "int", True), Column("part", "int")], [("item_id",)], [(("order_id",), "orders", ("order_id",))]),
        Table("orders", [Column("order_id", "int", True), Column("cust", "int"), Column("region", "int"), Column("note", "text")], [("order_id",)], [(("cust", "region"), "customers", ("cust", "region"))]),
        Table("customers", [Column("cust", "int", True), Column("region", "int", True), Column("name", "text")], [("cust", "region")]),
    ]
)


def draws(schema, seeds=range(80)):
    domains = rc._domains(["SELECT 1"])
    return [rc.random_tables(schema, seed, domains) for seed in seeds]


def test_every_non_null_foreign_key_value_has_a_parent_row():
    seen_child_rows = seen_null_keys = 0
    for tables in draws(SHOP):
        customers = {(c, r) for c, r, _ in tables["customers"]}
        orders = {o for o, *_ in tables["orders"]}
        for order_id, cust, region, _ in tables["orders"]:
            if cust is not None and region is not None:
                assert (cust, region) in customers
                seen_child_rows += 1
            else:
                seen_null_keys += 1
        for _, order_id, _ in tables["items"]:
            assert order_id in orders  # NOT NULL child column: always has a parent
    assert seen_child_rows > 20 and seen_null_keys > 0  # keys are copied from parents, and nullable ones are sometimes NULL


def test_a_not_null_child_has_no_rows_when_its_parent_has_none():
    for tables in draws(SHOP):
        if not tables["orders"]:
            assert tables["items"] == []


def test_declared_keys_stay_unique_in_children():
    for tables in draws(SHOP):
        ids = [o for o, *_ in tables["orders"]]
        assert len(ids) == len(set(ids))


def test_a_foreign_key_cycle_does_not_loop():
    cyclic = Schema(
        [
            Table("a", [Column("id", "int", True), Column("b_id", "int")], [("id",)], [(("b_id",), "b", ("id",))]),
            Table("b", [Column("id", "int", True), Column("a_id", "int")], [("id",)], [(("a_id",), "a", ("id",))]),
        ]
    )
    assert all(set(tables) == {"a", "b"} for tables in draws(cyclic, range(10)))


def test_the_prover_is_given_the_foreign_keys():
    constraints = rc.prover_constraints(SHOP)
    assert constraints["orders"].foreign_keys == ((("cust", "region"), "customers", ("cust", "region")),)
    assert constraints["customers"].foreign_keys == ()


def test_a_schema_without_foreign_keys_draws_what_it_always_did():
    plain = Schema(
        [
            Table("a", [Column("id", "int", True), Column("v", "text")], [("id",)]),
            Table("b", [Column("x", "int", True), Column("aid", "int"), Column("d", "date")]),
        ]
    )
    domains = rc._domains(["SELECT * FROM a WHERE v = 'q'", "SELECT 7 FROM b"])
    drawn = json.dumps([rc.random_tables(plain, seed, domains) for seed in range(25)], default=str)
    # digest of the same draws made by the code before foreign keys were read
    assert hashlib.sha256(drawn.encode()).hexdigest()[:16] == "f130eba94e40fa38"


def test_find_difference_does_not_report_an_orphan_row():
    """``items`` joined to ``orders`` on the foreign key equals ``items`` alone only because no orphan exists."""

    left = "SELECT i.item_id FROM items i JOIN orders o ON i.order_id = o.order_id"
    right = "SELECT item_id FROM items"
    assert rc.find_difference(SHOP, left, right, trials=120) is None
    unkeyed = Schema([Table(t.name, t.columns, t.keys) for t in SHOP.tables])
    assert rc.find_difference(unkeyed, left, right, trials=120) is not None
