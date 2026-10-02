"""Floors for the targeted-data eval: regression cases stay caught, the suite beats the old checker."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("targeted_data_bench", ROOT / "tools" / "targeted_data_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["targeted_data_bench"] = bench
_spec.loader.exec_module(bench)

from kumosql.result_equivalence import DatasetRunner, compare_outputs  # noqa: E402
from kumosql.targeted_data import database_suite  # noqa: E402

CASES = json.loads((ROOT / "tests" / "fixtures" / "targeted_data" / "cases.json").read_text(encoding="utf-8"))
ITEMS, SUITES = bench.build_corpus("dev")


def test_every_regression_case_is_still_caught_by_the_suite():
    missed = []
    for case in CASES:
        suite = SUITES[case["suite"]]
        with DatasetRunner(suite["schema"]) as runner:
            for labeled in database_suite(case["original"], suite["schema"], suite["rules"]):
                a = runner.run(case["original"], labeled.dataset)
                b = runner.run(case["mutant"], labeled.dataset)
                if not compare_outputs(a, b, check_column_names=False)[0]:
                    break
            else:
                missed.append((case["suite"], case["index"], case["operator"]))
    assert not missed


def test_university_set_floors():
    items = [(i, SUITES["university"]) for i in ITEMS if i["suite"] == "university"]
    records = [bench.process(item) for item in items]
    result = bench.score(records)
    assert result["configs"]["suite"]["score"] >= 0.80
    assert result["configs"]["suite"]["killed"] > result["configs"]["random_8"]["killed"]
    assert result["configs"]["single_seed"]["killed"] < result["configs"]["suite"]["killed"]
