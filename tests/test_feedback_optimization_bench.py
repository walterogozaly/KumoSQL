"""The feedback-driven optimizer's checked TPC-H rewrites are pinned and scored without vendoring GPL SQL."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "feedback_optimization_bench.py"
_spec = importlib.util.spec_from_file_location("feedback_optimization_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["feedback_optimization_bench"] = bench
_spec.loader.exec_module(bench)

# Filled from the first baseline run, limited to development pairs. Held-out pairs are only scored in the
# final results file and are not used as per-case test floors.
EXPECTED_DEV = {
    "c71a3c1f-5448-4199-9ea9-e5c08faf5674": "unknown",
    "c4c2e5ab-02c5-43dc-857b-c2ccf4a147fd": "proven",
    "7d0da489-58f6-46ee-afd7-3c8c946d86a6": "proven",
    "7e2fe5e4-e695-4d69-953c-2ee62c8f2d2a": "unknown",
    "cd32a5d9-2a6b-4d3d-ac8d-45f023afa8c8": "proven",
    "5733cfbb-9f02-4dc0-9e3c-56a36f0123ca": "proven",
    "37c07048-0cec-4f10-855a-a9aaaccd712c": "unknown",
    "94f82ffc-d3a6-4118-90c8-c082cf7e2be5": "unknown",
}


def _load():
    try:
        return bench.load_cases()
    except OSError as error:
        pytest.skip(f"feedback-driven SQL source bundle not available: {error}")


def test_pinned_bundle_contains_only_full_comparison_passes():
    cases = _load()
    assert len(cases) == 10
    assert len({case.id for case in cases}) == 10
    assert all(case.schema_name == "tpch" and case.left and case.right for case in cases)
    assert sum(case.held_out for case in cases) == 2


def test_symbolic_binds_are_typed_and_leave_no_placeholders():
    sql = "SELECT $1::date, $2::numeric, $3::text, $4::integer, $3::text"
    assert bench._symbolize(sql) == (
        "SELECT CAST((SELECT MAX(p1) FROM __kumo_param_source) AS DATE), "
        "CAST((SELECT MAX(p2) FROM __kumo_param_source) AS NUMERIC), "
        "CAST((SELECT MAX(p3) FROM __kumo_param_source) AS TEXT), "
        "CAST((SELECT MAX(p4) FROM __kumo_param_source) AS INTEGER), "
        "CAST((SELECT MAX(p3) FROM __kumo_param_source) AS TEXT)"
    )
    assert bench._symbolize("SELECT CAST($1 AS numeric)") == (
        "SELECT CAST((SELECT MAX(p1) FROM __kumo_param_source) AS NUMERIC)"
    )
    with pytest.raises(ValueError, match="untyped bind"):
        bench._symbolize("SELECT $1")


def test_development_subset_outcomes_and_zero_wrong():
    cases = [case for case in _load() if not case.held_out]
    assert len(cases) == 8
    results = {row["id"]: row for row in bench.run(cases)}
    assert set(results) == set(EXPECTED_DEV)
    assert {case_id: row["outcome"] for case_id, row in results.items()} == EXPECTED_DEV
    assert not [row for row in results.values() if row["wrong"]]
