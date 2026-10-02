"""The shared targeted refutation search: constraints respected, both engines, replayable."""

import json

import pytest

pytest.importorskip("duckdb")

from kumosql.minimize import minimize_failure, replay, to_json
from kumosql.refute import SqliteRunner, find_targeted_difference, repair_foreign_keys
from kumosql.result_equivalence import DataRules, SyntheticDataset, SyntheticTable

SCHEMA = {
    "emp": {"id": "INT64", "dept": "INT64", "sal": "INT64"},
    "dept": {"id": "INT64", "name": "STRING"},
}
RULES = {"emp": DataRules(frozenset({"id"}), (("id",),)), "dept": DataRules(frozenset({"id"}), (("id",),))}
FKS = (("emp", "dept", "dept", "id"),)


def test_foreign_key_repair_points_children_at_parents_or_drops_them():
    cols_e = (("id", "INT64"), ("dept", "INT64"), ("sal", "INT64"))
    cols_d = (("id", "INT64"), ("name", "STRING"))
    dataset = SyntheticDataset(0, {"emp": SyntheticTable(cols_e, ((1, 9, 5), (2, None, 5))), "dept": SyntheticTable(cols_d, ((1, "a"),))})
    fixed = repair_foreign_keys(dataset, FKS, RULES)
    assert fixed.tables["emp"].rows == ((1, 1, 5), (2, None, 5))
    empty = SyntheticDataset(0, {"emp": SyntheticTable(cols_e, ((1, 9, 5),)), "dept": SyntheticTable(cols_d, ())})
    assert repair_foreign_keys(empty, FKS, RULES).tables["emp"].rows == ((1, None, 5),)


@pytest.mark.parametrize("engine,dialect", [("duckdb", "bigquery"), ("sqlite", "sqlite")])
def test_boundary_difference_is_found_and_equivalent_pair_is_not(engine, dialect):
    left = "SELECT id FROM emp WHERE sal > 100"
    right = "SELECT id FROM emp WHERE sal >= 100"
    found = find_targeted_difference(left, right, SCHEMA, RULES, foreign_keys=FKS, engine=engine, dialect=dialect)
    assert found is not None and found.rows <= 6
    same = "SELECT id FROM emp WHERE NOT (sal <= 100)"
    # NULL sal: both drop the row, so the pair is equivalent
    assert find_targeted_difference(left, same, SCHEMA, RULES, foreign_keys=FKS, engine=engine, dialect=dialect) is None


def test_foreign_keys_keep_a_join_pair_equal_that_differs_without_them():
    inner = "SELECT e.id FROM emp e JOIN dept d ON e.dept = d.id"
    plain = "SELECT e.id FROM emp e WHERE e.dept IS NOT NULL"
    assert find_targeted_difference(inner, plain, SCHEMA, RULES) is not None
    assert find_targeted_difference(inner, plain, SCHEMA, RULES, foreign_keys=FKS) is None


def test_refutation_minimizes_and_replays_on_sqlite():
    left, right = "SELECT id FROM emp WHERE sal > 100", "SELECT id FROM emp WHERE sal >= 100"
    found = find_targeted_difference(left, right, SCHEMA, RULES, engine="sqlite", dialect="sqlite")
    case = minimize_failure(left, right, SCHEMA, found.dataset, RULES, dialect="sqlite", runner_class=SqliteRunner)
    assert case.after.rows == 1
    assert replay(json.loads(json.dumps(to_json(case))), runner_class=SqliteRunner)


def test_sqlite_runner_reports_errors_and_timeouts():
    from kumosql.result_equivalence import ExecutionError, QueryTimeout

    dataset = SyntheticDataset(0, {"emp": SyntheticTable((("id", "INT64"),), ((1,),))})
    with SqliteRunner() as runner:
        with pytest.raises(ExecutionError):
            runner.run("SELECT nope FROM emp", dataset)
        with pytest.raises(QueryTimeout):
            runner.run(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
                dataset,
                timeout=0.2,
            )


def test_prove_queries_returns_a_minimal_counterexample():
    from kumosql.pipeline_equivalence import prove_queries

    result = prove_queries("SELECT a FROM t WHERE b > 5", "SELECT a FROM t WHERE b >= 5")
    assert result["status"] == "not_equivalent"
    assert sum(len(rows) for rows in result["counterexample"]["tables"].values()) == 1
