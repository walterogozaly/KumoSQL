"""The bounded check where the solver appears in the app: its own evidence label next to the unbounded proof."""

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql import pipeline_equivalence, prover_context  # noqa: E402

CATALOG = [
    ("p", "d", "orders", {
        "schema": [
            {"name": "id", "type": "INT64", "mode": "REQUIRED"},
            {"name": "amount", "type": "NUMERIC"},
            {"name": "note", "type": "STRING"},
        ],
        "constraints": {"primaryKey": {"columns": ["id"]}},
    }),
]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(prover_context, "bounded_schema", lambda: be.schema_from_bigquery(CATALOG))


def test_settings_validate_the_row_bound():
    assert prover_context.settings()["bounded_rows"] == 3
    assert prover_context.save_settings(bounded_rows=2)["bounded_rows"] == 2
    for bad in (-1, 7, "3", True):
        with pytest.raises(ValueError):
            prover_context.save_settings(bounded_rows=bad)
    assert prover_context.settings()["bounded_rows"] == 2


def test_unproven_queries_get_a_bounded_label():
    # two spellings of the same condition the unbounded prover may not connect: either way the bounded line is separate
    same = pipeline_equivalence.prove_queries("SELECT id FROM d.orders WHERE amount > 1", "SELECT id FROM `p.d.orders` WHERE NOT (amount <= 1)")
    assert same["status"] == "proven_equivalent" or same["bounded"]["label"] == "bounded, 3 rows"
    different = pipeline_equivalence.prove_queries("SELECT id FROM d.orders WHERE amount > 1", "SELECT id FROM d.orders WHERE amount >= 1")
    if different["status"] != "not_equivalent":
        assert different["bounded"]["status"] == "different"
        (rows,) = different["bounded"]["counterexample"]["tables"].values()
        assert rows[0]["amount"] == 1


def test_bounded_check_can_be_turned_off():
    prover_context.save_settings(bounded_rows=0)
    assert prover_context.bounded("SELECT id FROM d.orders", "SELECT id FROM d.orders WHERE amount > 1") is None
    result = pipeline_equivalence.prove_queries("SELECT id FROM d.orders", "SELECT id FROM d.orders WHERE amount > 1")
    assert "bounded" not in result


def test_unknown_tables_give_unknown_not_a_verdict():
    result = prover_context.bounded("SELECT x FROM nowhere", "SELECT x FROM nowhere WHERE x > 1")
    assert result["status"] == "unknown"
