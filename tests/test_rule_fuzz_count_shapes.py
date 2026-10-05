"""The count and aggregate target generators (``tools/rule_fuzz_targets/count_shapes.py`` and ``aggregate_forms.py``)
still make the rules they aim at fire, so a template that stops matching its rule is caught without running DuckDB."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import aggregate_forms, count_shapes  # noqa: E402

COUNT_SHAPES = {
    "collapse_constant_regroup",
    "fold_grouped_count_cases",
    "singleton_count_sum",
    "regroup_tuple_count",
    "drop_grouped_sum_coalesce",
    "sum_of_grouped_counts",
    "normalize_key_counts",
    "collapse_grouping_expansion",
    "propagate_grouped_join_facts",
}
AGGREGATE_FORMS = {
    "_constant_counts",
    "_constant_keys",
    "_drop_constant_groupings",
    "_mean_times_count",
    "_fold_filter_into_grouping",
    "_group_by_to_distinct",
    "_key_aggregates",
    "_shifted_sums",
    "_fold_count_coalesce",
    "_regroup_distinct",
    "_split_aggregates",
    "rewrite_aggregates",
}


def fired(module, count, targets) -> set[str]:
    rules = set()
    for case in module.cases(1, count):
        fires, _ = fuzz.trace_case(case, only=targets)
        rules.update(fire.rule for fire in fires)
    return rules


def test_count_shape_templates_fire_their_rules():
    missing = COUNT_SHAPES - fired(count_shapes, len(count_shapes.TEMPLATES) * 2, COUNT_SHAPES)
    assert not missing


def test_aggregate_form_templates_fire_their_rules():
    count = (len(aggregate_forms.TEMPLATES) + len(aggregate_forms.CONSTANT_GROUPING)) * 2
    missing = AGGREGATE_FORMS - fired(aggregate_forms, count, AGGREGATE_FORMS)
    assert not missing


def test_constant_grouping_templates_read_a_literal_as_a_constant():
    cases = aggregate_forms.cases(1, len(aggregate_forms.TEMPLATES) + len(aggregate_forms.CONSTANT_GROUPING))
    flagged = [case for case in cases if case["options"].get("group_by_constants")]
    assert len(flagged) == len(aggregate_forms.CONSTANT_GROUPING)


def test_multi_argument_counts_skip_rows_with_a_null_argument():
    duckdb = pytest.importorskip("duckdb")
    import sqlglot

    rows = "(VALUES (1, 1), (1, 1), (1, NULL), (NULL, 2), (2, 2)) AS t(a, b)"
    sql = "SELECT COUNT(a, b) AS n, COUNT(DISTINCT a, b) AS d FROM t"
    tree = sqlglot.parse_one(sql, read="bigquery")
    text = fuzz.to_duckdb(tree).replace("FROM t", f"FROM {rows}")
    assert duckdb.connect().execute(text).fetchall() == [(3, 2)]
