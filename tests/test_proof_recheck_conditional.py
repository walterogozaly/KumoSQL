"""The conditional-proof adapter (tools/recheck/conditional.py): conditions become declared constraints."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from kumosql.conditional_equivalence import Condition  # noqa: E402

from recheck import conditional, engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402


def _tables() -> dict[str, Table]:
    return {
        "child": Table("child", [Column("id"), Column("parent_id")]),
        "parent": Table("parent", [Column("id")]),
    }


def test_conditions_become_declared_constraints():
    conditions = [
        Condition("not_null", "child", ("parent_id",)),
        Condition("unique", "parent", ("id",)),
        Condition("foreign_key", "child", ("parent_id",), "parent", ("id",)),
    ]
    tables = conditional.apply_conditions(_tables(), conditions)
    assert tables["child"].columns[1].not_null and not tables["child"].columns[0].not_null
    assert tables["parent"].keys == [("id",)]
    assert tables["child"].foreign_keys == [(("parent_id",), "parent", ("id",))]


def test_a_conditional_pair_survives_only_with_its_condition():
    left, right = "SELECT COUNT(parent_id) FROM child", "SELECT COUNT(*) FROM child"
    assert engine.recheck(Case("t", "plain", left, right, _tables()), budget=300)["verdict"] == "differs"
    tables = conditional.apply_conditions(_tables(), [Condition("not_null", "child", ("parent_id",))])
    assert engine.recheck(Case("t", "conditional", left, right, tables), budget=300)["verdict"] == "survived"


def test_a_foreign_key_condition_keeps_every_child_row_in_a_join():
    left, right = "SELECT c.id FROM child c JOIN parent p ON c.parent_id = p.id", "SELECT id FROM child"
    conditions = [
        Condition("not_null", "child", ("parent_id",)),
        Condition("unique", "parent", ("id",)),
        Condition("foreign_key", "child", ("parent_id",), "parent", ("id",)),
    ]
    tables = conditional.apply_conditions(_tables(), conditions)
    assert engine.recheck(Case("t", "fk", left, right, tables), budget=300)["verdict"] == "survived"


def test_adapters_are_registered():
    assert set(conditional.ADAPTERS) == {"conditional-equivalence-singh", "conditional-equivalence-verieql"}
