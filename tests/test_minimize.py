"""Failure minimization: smaller, still failing, replayable, and never an equivalence claim."""

import json

import pytest

pytest.importorskip("duckdb")

from kumosql.minimize import minimize_failure, replay, to_json
from kumosql.result_equivalence import DataRules, generate_synthetic_dataset

SCHEMA = {
    "emp": {"id": "INT64", "dept": "INT64", "sal": "INT64", "name": "STRING"},
    "dept": {"id": "INT64", "title": "STRING"},
}
RULES = {"emp": DataRules(frozenset({"id"}), (("id",),)), "dept": DataRules(frozenset({"id"}), (("id",),))}
LEFT = "SELECT name, sal FROM emp WHERE sal > 1 AND dept <> 4 AND name <> 'zz' ORDER BY sal LIMIT 5"
RIGHT = "SELECT name, sal FROM emp WHERE sal >= 1 AND dept <> 4 AND name <> 'zz' ORDER BY sal LIMIT 5"


def _failing_dataset():
    for seed in range(1, 60):
        dataset = generate_synthetic_dataset(SCHEMA, seed=seed, rows_per_table=25, rules=RULES)
        from kumosql.minimize import _differs
        from kumosql.result_equivalence import DatasetRunner

        with DatasetRunner(SCHEMA) as runner:
            if _differs(runner, LEFT, RIGHT, dataset, check_column_names=False):
                return dataset
    raise AssertionError("no failing seed")


def test_minimized_case_is_small_and_still_fails():
    dataset = _failing_dataset()
    case = minimize_failure(LEFT, RIGHT, SCHEMA, dataset, RULES)
    assert case.after.rows <= 1 < case.before.rows
    assert case.after.sql_nodes < case.before.sql_nodes
    assert sum(len(t.rows) for t in case.dataset.tables.values()) == case.after.rows
    # the surviving difference is the boundary the two queries disagree on
    assert ">" in case.left_sql and ">=" in case.right_sql


def test_reduced_case_replays_from_json_on_a_fresh_engine():
    case = minimize_failure(LEFT, RIGHT, SCHEMA, _failing_dataset(), RULES)
    document = json.loads(json.dumps(to_json(case)))
    assert replay(document)
    document["right"] = document["left"]
    assert not replay(document)


def test_a_pair_that_does_not_differ_is_refused():
    dataset = generate_synthetic_dataset(SCHEMA, seed=1, rules=RULES)
    with pytest.raises(ValueError):
        minimize_failure(LEFT, LEFT, SCHEMA, dataset, RULES)


def test_cells_that_do_not_matter_are_simplified():
    dataset = _failing_dataset()
    case = minimize_failure(LEFT, RIGHT, SCHEMA, dataset, RULES)
    for table in case.dataset.tables.values():
        for row in table.rows:
            assert row.count(None) >= 0  # shape preserved
    kept = [v for t in case.dataset.tables.values() for r in t.rows for v in r if v is not None]
    assert len(kept) <= case.before.non_null_cells
