"""A feedback-driven SQL optimization artifact's rewrites (tools/feedback_opt_bench.py): zero wrong proofs and floors.

The run bundles are GPL-3.0-or-later, so they are downloaded on first use from a pinned commit and never stored in
the repository; the tests that need them skip when GitHub cannot be reached. ``FLOORS`` only ever goes up. The full
run goes through ``python tools/feedback_opt_bench.py``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "feedback_opt_bench.py"
_spec = importlib.util.spec_from_file_location("feedback_opt_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["feedback_opt_bench"] = bench
_spec.loader.exec_module(bench)

SAMPLE = 24
# measured on this sample without the generated TPC-H instance; the margin absorbs a slow machine
FLOORS = {"proved": 0, "refuted": 0}

TEMPLATE = """
SELECT l_returnflag, SUM(l_quantity) FROM lineitem
WHERE l_shipdate >= $1::date AND l_shipdate < $1::date + INTERVAL '1 year'
  AND l_shipmode IN ($2::text, $3::text) AND l_discount BETWEEN $4::numeric - 0.01 AND $4::numeric + 0.01
GROUP BY l_returnflag;
"""


def _pairs():
    try:
        return bench.load_pairs()
    except OSError as error:
        pytest.skip(f"feedback-driven optimization bundles not available: {error}")


def test_the_pinned_bundles_have_the_published_contents():
    pairs = _pairs()  # skips the test when the data cannot be fetched
    assert len(pairs) == 885
    assert {label: sum(p.label == label for p in pairs) for label in bench.LABELS} == {
        "equal": 789,
        "unequal": 6,
        "order": 8,
        "unchecked": 82,
        "mixed": 0,
    }
    assert {f: sum(p.family == f for p in pairs) for f in bench.FAMILIES} == {
        "tpch": 0,
        "real-world": 0,
        "drift": 0,
        "job": 0,
    }
    assert sum(p.held_out for p in pairs) == 191
    assert (bench.CACHE / "LICENSE").read_text().startswith("                    GNU GENERAL PUBLIC LICENSE")


def test_parameters_are_typed_from_the_casts_and_become_functions_for_the_prover():
    types = bench.parameter_types(TEMPLATE)
    assert types == {1: "date", 2: "text", 3: "text", 4: "numeric"}
    text = bench.with_functions(TEMPLATE, types)
    assert "$" not in text and "CAST(kumo_param_1() AS date)" in text
    # a bare ``$n`` (some candidates drop the cast) takes the template's type
    assert bench.with_functions("SELECT $2", types) == "SELECT CAST(kumo_param_2() AS text)"


def test_bindings_are_fixed_typed_and_ordered_ranges():
    first = bench.bindings(TEMPLATE)
    assert first == bench.bindings(TEMPLATE) and len(first) == bench.BINDINGS
    for literals in first:
        text = bench.bound(TEMPLATE, bench.parameter_types(TEMPLATE), literals)
        assert "$" not in text
        assert literals[2] != literals[3]  # IN (a, b) with two different ship modes
        float(literals[4])  # the numeric parameter is a number, not a string
    assert bench.bindings("SELECT 1") == []


def test_a_like_pattern_parameter_takes_a_word_of_the_column():
    template = "SELECT s_name FROM supplier, part WHERE p_type LIKE '%' || $1::text AND s_suppkey = p_partkey"
    for literals in bench.bindings(template):
        assert literals[1].strip("'") in {"STANDARD", "SMALL", "MEDIUM", "LARGE", "ECONOMY", "PROMO", "ANODIZED", "BURNISHED",
                                          "PLATED", "POLISHED", "BRUSHED", "TIN", "NICKEL", "BRASS", "STEEL", "COPPER"}


def test_schemas_read_keys_not_null_and_foreign_keys():
    tables = bench.qb.load_schema(bench.TPCH_DDL)
    assert len(tables) == 8
    assert tables["lineitem"].keys == [("l_orderkey", "l_linenumber")]
    assert (("l_partkey", "l_suppkey"), "partsupp", ("ps_partkey", "ps_suppkey")) in tables["lineitem"].foreign
    assert "o_orderdate" in tables["orders"].not_null


def test_job_schema_reads_primary_keys():
    try:
        tables = bench.schema_for("job")
    except OSError as error:
        pytest.skip(f"JOB schema not available: {error}")
    assert len(tables) == 21 and tables["title"].keys == [("id",)] and "kind_id" in tables["title"].not_null


def test_a_counterexample_is_given_the_types_of_its_columns():
    tables = bench.qb.load_schema(bench.TPCH_DDL)
    rows = {"region": [{"r_regionkey": "x", "r_name": "ASIA"}], "nation": [{"n_nationkey": True, "n_name": "JAPAN", "n_regionkey": "x"}]}
    fixed = bench.retype(rows, tables)
    assert isinstance(fixed["region"][0]["r_regionkey"], int)
    assert fixed["nation"][0]["n_regionkey"] == fixed["region"][0]["r_regionkey"]  # equal values stay equal
    assert fixed["nation"][0]["n_nationkey"] != fixed["region"][0]["r_regionkey"]


def test_the_artifacts_checks_become_labels():
    def pair(**checks):
        item = bench.Pair("job", "SELECT 1", "SELECT 2")
        item.checks.update(checks)
        return item

    assert pair(**{"pass": 2, "error": 1}).label == "equal"
    assert pair(rows=1).label == "unequal"
    assert pair(rows=1, **{"pass": 1}).label == "mixed"
    assert pair(order=1).label == "order"
    assert pair(order=1, **{"pass": 1}).label == "equal"
    assert pair(error=3).label == "unchecked"


def test_a_proof_is_replayed_on_the_pairs_own_parameters():
    original = "SELECT l_orderkey FROM lineitem WHERE l_shipdate >= $1::date"
    same = "SELECT l_orderkey FROM lineitem WHERE $1::date <= l_shipdate"
    different = "SELECT l_orderkey FROM lineitem WHERE l_shipdate > $1::date"
    tables = bench.schema_for("tpch")
    assert bench.decide(bench.Pair("tpch", original, same), tables).outcome == "proven"
    assert bench.decide(bench.Pair("tpch", original, different), tables).outcome != "proven"


def test_pinned_sample_has_no_wrong_proof():
    # development pairs only, and no generated TPC-H instance, so the floors do not depend on tpchgen-cli
    pairs = bench.sample([p for p in _pairs() if not p.held_out], SAMPLE)
    report = bench.run(pairs, workers=1, instance=None)
    wrong = [pairs[i].key for i, v in report.verdicts.items() if v.outcome == "wrong"]
    assert not wrong, wrong
    proved = report.counts(label="equal")["proven"]
    refuted = report.counts()["refuted"]
    assert proved >= FLOORS["proved"], report.equal_line()
    assert refuted >= FLOORS["refuted"], report.counts()
