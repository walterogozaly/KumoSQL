"""The difference-explanation benchmark keeps its SPJ cohort and sample deterministic."""

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("sqlglot")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("difference_bench", ROOT / "tools" / "difference_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["difference_bench"] = bench
_spec.loader.exec_module(bench)


def test_the_spj_filter_keeps_supported_single_block_pairs():
    assert bench.is_spj_pair(
        "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x > 5",
        "SELECT t.id FROM t JOIN u ON t.id = u.id WHERE t.x >= 5",
        "mysql",
    )


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT id FROM t UNION ALL SELECT id FROM u", "SELECT id FROM t UNION ALL SELECT id FROM u"),
        ("SELECT id FROM t WHERE id IN (SELECT id FROM u UNION ALL SELECT id FROM u)", "SELECT id FROM t WHERE id IN (SELECT id FROM u UNION ALL SELECT id FROM u)"),
        ("SELECT DISTINCT id FROM t", "SELECT id FROM t"),
        ("SELECT COUNT(*) FROM t", "SELECT COUNT(*) FROM t"),
        ("SELECT id FROM t WHERE x > 5 LIMIT 3", "SELECT id FROM t WHERE x >= 5 LIMIT 3"),
        ("SELECT id FROM t WHERE id IN (SELECT id FROM u)", "SELECT id FROM t WHERE id IN (SELECT id FROM u)"),
        ("SELECT p.id FROM t p JOIN t q ON p.id = q.id", "SELECT p.id FROM t p JOIN t q ON p.id = q.id"),
        ("SELECT id FROM t", "SELECT id FROM u"),
    ],
)
def test_non_spj_or_different_input_relations_are_outside_the_cohort(left, right):
    assert not bench.is_spj_pair(left, right, "mysql")


def test_a_fixed_sample_does_not_depend_on_input_order():
    pairs = [
        bench.QueryPair("source", str(index), f"SELECT {index} FROM t", f"SELECT {index + 1} FROM t", "mysql")
        for index in range(12)
    ]
    assert [p.case_id for p in bench.stable_sample(pairs, 5)] == [
        p.case_id for p in bench.stable_sample(list(reversed(pairs)), 5)
    ]


def test_the_results_row_counts_only_independently_refuted_pairs():
    dev = {
        "source_refuted_spj": 10,
        "explained": 7,
        "coverage": 0.7,
        "single_block_refuted_spj": 10,
        "single_block_explained": 7,
        "single_block_coverage": 0.7,
        "multi_branch": 0,
        "by_source": {},
        "median_explain_seconds": 0.2,
    }
    held = {"source_refuted_spj": 2, "explained": 1, "single_block_refuted_spj": 2, "single_block_explained": 1, "by_source": {}}
    row = bench.result_row(dev, held)
    assert row["size"] == 10
    assert row["coverage"] == {"proven": 7, "unknown": 3}
    assert row["score"].startswith("7/10")
