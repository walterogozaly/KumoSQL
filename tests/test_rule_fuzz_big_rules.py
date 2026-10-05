"""The ``distinct_variants``, ``eager_variants`` and ``aggregate_variants`` rule-fuzz generators (tools/rule_fuzz_targets):
every rewrite they aim at still fires on its ``FIRES`` query. Tracing only, no DuckDB, so a template edit that quietly
stops a variant from firing fails here instead of leaving the fuzzer with a rule it no longer exercises."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import rule_fuzz as fuzz  # noqa: E402
from rule_fuzz_targets import aggregate_variants, distinct_variants, eager_variants  # noqa: E402

from kumosql import aggregate_rules, dedup_join_rules, distinct_rules, join_rewrites, set_split_rules  # noqa: E402

# rewrites inside one traced rule: ``module.function`` targets are recorded when they return a rewrite
INSIDE = {
    distinct_rules: [
        "drop_membership_dedup", "drop_dedup_read_as_set", "merge_grouped_source", "distinct_join_to_exists",
        "regroup_distinct", "unwrap_column_parens", "drop_group_under_distinct", "drop_distinct_over_group_keys",
        "fold_count_casts",
    ],
    aggregate_rules: [
        "_empty_global_aggregate", "_fromless_aggregate", "_pull_shared_filter", "_key_expression_aggregates",
        "_count_of_filtered_value", "_drop_nonempty_group_having", "_coalesce_counted_sum", "_filter_into_having",
        "_lift_aggregate_expressions", "_split_compound_aggregates", "_having_existence_to_where",
        "_merge_joined_aggregates", "_distribute_over_aggregating_branches", "_merge_projection_over_grouped_join",
    ],
    join_rewrites: ["_fk_left_join_to_inner", "_having_left_join_to_inner", "_nested_join_to_derived", "_map_equalities_into_left_join"],
    set_split_rules: ["_split_in_over_union", "_distribute_union_source", "_split_case_key", "_split_case_comparison"],
    dedup_join_rules: ["_drop_join"],
}

ENTRIES = [
    (module.__name__.rsplit(".", 1)[-1], target, case)
    for module in (distinct_variants, eager_variants, aggregate_variants)
    for target, case in module.fire_list()
]


@pytest.fixture
def recorded(monkeypatch):
    """The ``module.function`` names of rewrites that returned a result since the fixture was set up."""

    hits: set = set()
    for module, names in INSIDE.items():
        for name in names:
            original = getattr(module, name)

            def wrapper(*args, _original=original, _key=f"{module.__name__.rsplit('.', 1)[-1]}.{name}", **kwargs):
                result = _original(*args, **kwargs)
                if result is not None:
                    hits.add(_key)
                return result

            wrapper.__name__ = name
            monkeypatch.setattr(module, name, wrapper)
    # the dispatch tuple holds the originals
    monkeypatch.setattr(distinct_rules, "_RULES", tuple(getattr(distinct_rules, n) for n in INSIDE[distinct_rules][:9]))
    return hits


@pytest.mark.parametrize("module,target,case", ENTRIES, ids=[f"{m}-{i}-{t}" for i, (m, t, _) in enumerate(ENTRIES)])
def test_each_targeted_rewrite_fires(recorded, module, target, case):
    # a ``module.function`` target is seen by the recorder, so the tracer need not record anything for it
    fires, crashes = fuzz.trace_case(case, only=set() if "." in target else {target})
    assert not crashes
    seen = {f.rule for f in fires} | recorded
    assert target in seen, f"{target} did not fire on {case['sql']}"


def test_every_template_expands_to_a_parsable_query():
    import sqlglot

    for module in (distinct_variants, eager_variants, aggregate_variants):
        for case in module.cases(1, len(module.TEMPLATES)):
            sqlglot.parse_one(case["sql"], read="bigquery")
            assert set(case["schema"]) == {"t", "u", "p"}


def test_chained_foreign_keys_hold_in_every_generated_database():
    """p.tid -> t.id -> u.k: emptying u empties t, which must empty p (one pass in table order left dangling rows)."""

    case = {
        "sql": "SELECT p.id FROM p LEFT JOIN t ON p.tid = t.id",
        "dialect": "bigquery",
        "schema": {"p": [["id", "INT64"], ["tid", "INT64"]], "t": [["id", "INT64"], ["y", "INT64"]], "u": [["k", "INT64"]]},
        "constraints": {
            "p": {"keys": [["id"]], "not_null": ["id", "tid"], "foreign_keys": [[["tid"], "t", ["id"]]]},
            "t": {"keys": [["id"]], "not_null": ["id", "y"], "foreign_keys": [[["y"], "u", ["k"]]]},
            "u": {"keys": [["k"]], "not_null": ["k"]},
        },
        "options": {},
        "source": "test",
    }
    for seed in range(3):
        for database in fuzz.build_databases(case, seed):
            assert not fuzz.fixture_errors(case, database["tables"]), database["name"]
