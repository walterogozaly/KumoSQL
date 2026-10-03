"""Table-minimization eval: the committed cases hold, the harness catches wrong outputs, and the floor holds."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mc = _load("minimization_cases")
bench = _load("minimization_bench")
gen = _load("make_minimization_cases")

CASES = mc.load_cases()
GENERATED = [c for c in CASES if c["source"] == "generated"]
DEV = [c for c in CASES if c["split"] == "dev"]
FAMILIES = {"passthrough_chain", "duplicated_logic", "dead_tables", "unused_columns_joins", "cte_repeats_table",
            "mergeable_tables", "redundant_filters", "irreducible"}

# Floors for the Refactor search on every other dev case of 6 to 8 tables (27 cases; see floor_cases).
REFACTOR_IMPROVED_FLOOR = 22
REFACTOR_PROVED_FLOOR = 27
# Floors for the table minimizer on the same cases (measured 23 improved, 22 proved, 4 same, 1 agreed, quality 0.82).
MINIMIZER_IMPROVED_FLOOR = 22
MINIMIZER_PROVED_FLOOR = 21


def floor_cases():
    return [c for c in DEV if 6 <= len(c["tables"]) <= 8][::2]


def test_case_fields_splits_and_sizes():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert len(GENERATED) == gen.COUNT
    for case in CASES:
        assert case["split"] in ("dev", "held_out")
        assert set(case["protected"]) <= set(case["tables"]) & set(case["reference"]["tables"])
        assert not set(case["tables"]) & set(case["sources"])
        original, reference = case["original"]["complexity"]["score"], case["reference"]["complexity"]["score"]
        assert reference <= original, case["id"]
    for case in GENERATED + [c for c in CASES if c["source"] == "handwritten"]:
        assert case["split"] == mc.held_out_split(case["id"])
        assert 3 <= len(case["tables"]) <= 20, case["id"]
        original, reference = case["original"]["complexity"]["score"], case["reference"]["complexity"]["score"]
        assert reference < original or case["reference"]["tables"] == case["tables"], case["id"]
        for trap in case["traps"]:
            assert trap["changes"] and set(trap["changes"]) <= set(case["protected"])
    held = sum(c["split"] == "held_out" for c in GENERATED)
    assert 0.12 * len(GENERATED) < held < 0.28 * len(GENERATED)
    assert {f for c in GENERATED for f in c["families"]} == FAMILIES
    assert sum(len(c["traps"]) for c in GENERATED) >= len(GENERATED)


def test_every_case_file_checks_out_on_duckdb():
    problems = bench.verify_cases(CASES, databases=8)
    assert problems == [], "\n".join(problems[:20])


def test_generator_is_deterministic():
    stored = {c["id"]: c for c in GENERATED}
    for number in (1, 2, 50):
        built = gen.build_case(number)
        case = stored[built["id"]]
        assert built["tables"] == case["tables"] and built["protected"] == case["protected"]
        assert built["reference"]["tables"] == case["reference"]["tables"]


def test_pipeline_complexity_counts_tables_and_structure():
    from kumosql.formatting import pipeline_complexity

    assert pipeline_complexity({"a": "SELECT * FROM x"}) == {"score": 1.0, "structural": 0.0, "tables": 1}
    two = pipeline_complexity({"a": "SELECT * FROM x", "b": "SELECT a.i FROM a JOIN y ON a.i = y.i WHERE a.i > 1 AND y.j < 2"})
    assert two == {"score": 4.5, "structural": 2.5, "tables": 2}


def _case(tables=None, protected=("report",)):
    sources = {"orders": {"columns": {"id": "INT64", "amount": "INT64", "status": "STRING"}, "key": ["id"],
                          "values": {"status": ["paid", "open"]}}}
    tables = tables or {
        "stg": "SELECT * FROM orders",
        "paid": "SELECT id, amount FROM stg WHERE status = 'paid'",
        "report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM paid",
    }
    reference = {"report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders WHERE status = 'paid'"}
    from kumosql.formatting import pipeline_complexity

    return {"id": "unit", "split": "dev", "dialect": "bigquery", "sources": sources, "tables": tables,
            "protected": list(protected), "families": ["mergeable_tables"],
            "original": {"complexity": pipeline_complexity(tables)},
            "reference": {"tables": reference, "complexity": pipeline_complexity(reference)}, "traps": []}


@pytest.mark.parametrize("output, status", [
    ({"report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders WHERE status = 'paid'"}, "proved"),
    ({"report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders"}, "wrong"),  # drops the filter
    ({"report": "SELECT COUNT(*) AS rows, SUM(amount) AS total FROM orders WHERE status = 'paid'"}, "wrong"),  # renamed column
    ({"report": "SELECT SUM(amount) AS total, COUNT(*) AS n FROM orders WHERE status = 'paid'"}, "wrong"),  # column order
    ({"other": "SELECT 1 AS x"}, "wrong"),  # protected table missing
    ({"report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM nowhere"}, "wrong"),  # does not run
    ({"stg": "SELECT * FROM orders", "paid": "SELECT id, amount FROM stg WHERE status = 'paid'",
      "report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM paid"}, "same"),
])
def test_checker_statuses(output, status):
    got, reason, _ = bench.check_output(_case(), output, databases=30)
    assert got == status, reason


def test_an_unchanged_protected_query_over_a_changed_upstream_needs_a_proof():
    output = {"paid": "SELECT id, amount FROM orders WHERE status = 'paid'",
              "report": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM paid"}
    status, reason, proofs = bench.check_output(_case(), output, databases=30)
    assert status == "proved", reason
    assert proofs["report"]["status"] == "proved"


def test_databases_where_the_original_depends_on_row_order_are_skipped_when_asked():
    sources = {"t": {"columns": {"id": "INT64"}}}
    engine = mc.Engine(sources, {"o": {"p": "SELECT id FROM t LIMIT 1"}, "n": {"p": "SELECT MAX(id) AS id FROM t"}})
    try:
        db = [{"t": [(1,), (2,)]}]
        assert mc.order_sensitive({"p": "SELECT id FROM t LIMIT 1"})
        assert mc.compare(engine, db, "o", ["n"], ["p"])["n"] is not None
        assert mc.compare(engine, db, "o", ["n"], ["p"], stable_only=True)["n"] is None
    finally:
        engine.close()


def test_traps_are_counted_wrong_and_references_are_not():
    sample = [c for c in DEV if c["traps"]][:24]
    traps = [bench.run_case(c, "trap", databases=5, prove=False) for c in sample]
    assert all(r.status == "wrong" for r in traps), [r.id for r in traps if r.status != "wrong"]
    references = [bench.run_case(c, "reference", databases=5, prove=False) for c in sample]
    assert all(r.status in ("agreed", "same") for r in references)
    assert all(r.quality in (None, 1.0) for r in references)
    summary = bench.summarise(references, "reference")
    assert summary["correctness"]["wrong"] == 0
    assert summary["coverage"]["improved"] == summary["coverage"]["improvable"]


def test_identity_is_never_wrong_and_never_improves():
    results = [bench.run_case(c, bench.identity, databases=5) for c in DEV[:10]]
    assert {r.status for r in results} == {"same"}
    assert not any(r.improved for r in results)


def test_refactor_search_floor():
    summary, results = bench.run(floor_cases(), "refactor", databases=40)
    assert summary["correctness"]["wrong"] == 0, summary["wrong"]
    assert summary["coverage"]["improved"] >= REFACTOR_IMPROVED_FLOOR
    assert summary["correctness"]["proved"] >= REFACTOR_PROVED_FLOOR


def test_table_minimizer_floor():
    summary, results = bench.run(floor_cases(), "minimizer", databases=40)
    assert summary["correctness"]["wrong"] == 0, summary["wrong"]
    assert summary["correctness"]["error"] == 0, summary["errors"]
    assert summary["coverage"]["improved"] >= MINIMIZER_IMPROVED_FLOOR
    assert summary["correctness"]["proved"] >= MINIMIZER_PROVED_FLOOR


def test_results_file_matches_the_case_set():
    row = json.loads((ROOT / "benchmarks" / "results" / "table-minimization.json").read_text(encoding="utf-8"))
    assert row["size"] == len(DEV)
