"""Constraint-dependent rewrites: foreign-key join elimination, needed guarantees, and the labelled corpus."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.constraint_dependence import constraints_with, guarantees_of, needed_guarantees
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"orders": ["id", "customer_id", "total"], "customers": ["id", "name"]}
CONSTRAINTS = {
    "orders": TableConstraints(
        not_null=frozenset({"id", "customer_id"}), keys=(("id",),), foreign_keys=((("customer_id",), "customers", ("id",)),)
    ),
    "customers": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),)),
}
JOINED = "SELECT o.id, o.total FROM orders AS o JOIN customers AS c ON o.customer_id = c.id"
PLAIN = "SELECT id, total FROM orders"


def test_a_foreign_key_join_is_redundant_and_the_proof_names_what_it_needs():
    report = needed_guarantees(JOINED, PLAIN, schema=SCHEMA, constraints=CONSTRAINTS)
    assert report.status == "proven"
    assert set(report.labels) == {
        "orders(customer_id) references customers(id)",
        "orders.customer_id is NOT NULL",
        "(id) is unique in customers",
    }


@pytest.mark.parametrize("drop", ["foreign_key", "not_null", "unique"])
def test_removing_any_one_guarantee_stops_the_proof(drop):
    facts = [g for g in guarantees_of(CONSTRAINTS) if not (g.kind == drop and (g.table, g.columns) in {("orders", ("customer_id",)), ("customers", ("id",))})]
    result = prove_equivalent_algebraic(JOINED, PLAIN, schema=SCHEMA, constraints=constraints_with(facts))
    assert not result.proven


def test_the_join_stays_when_the_parent_is_read_or_filtered():
    read = prove_equivalent_algebraic(
        "SELECT o.id, c.name FROM orders AS o JOIN customers AS c ON o.customer_id = c.id", PLAIN, schema=SCHEMA, constraints=CONSTRAINTS
    )
    assert not read.proven
    filtered = prove_equivalent_algebraic(
        "SELECT o.id, o.total FROM orders AS o JOIN customers AS c ON o.customer_id = c.id AND c.name = 'x'", PLAIN, schema=SCHEMA, constraints=CONSTRAINTS
    )
    assert not filtered.proven


def test_counterexamples_respect_foreign_keys():
    result = prove_equivalent_algebraic(
        "SELECT o.id FROM orders AS o JOIN customers AS c ON o.customer_id = c.id AND c.name = 'x'", PLAIN, schema=SCHEMA, constraints=CONSTRAINTS
    )
    assert not result.proven
    if result.counterexample is not None:
        parents = {r.get("id") for r in result.counterexample.tables.get("customers", [])}
        assert all(r.get("customer_id") in parents for r in result.counterexample.tables.get("orders", []) if r.get("customer_id") is not None)


_path = Path(__file__).resolve().parent.parent / "tools" / "constraint_rewrite_bench.py"
_spec = importlib.util.spec_from_file_location("constraint_rewrite_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["constraint_rewrite_bench"] = bench
_spec.loader.exec_module(bench)

pytest.importorskip("duckdb")

FLOOR = 10


def test_labelled_corpus_has_no_wrong_proofs_and_keeps_its_floor():
    result = bench.run("cases.json", trials=40)
    assert result["wrong"] == [] and result["label_wrong"] == [], (result["wrong"], result["label_wrong"])
    assert result["error"] == 0
    assert result["exact_guarantees"] >= FLOOR, result["exact_guarantees"]
    assert result["ablations_refused"] == result["ablations"]
