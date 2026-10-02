"""Targeted databases, declared-rule generation and the shared dataset runner."""

import pytest

pytest.importorskip("duckdb")

from kumosql.query_mutants import OPERATORS, mutate
from kumosql.result_equivalence import (
    DataRules,
    DatasetRunner,
    ExecutionError,
    QueryTimeout,
    compare_outputs,
    execute_on_dataset,
    generate_synthetic_dataset,
)
from kumosql.targeted_data import database_suite, edge_datasets, random_datasets, targeted_datasets

SCHEMA = {
    "emp": {"id": "INT64", "dept": "INT64", "sal": "INT64", "name": "STRING"},
    "dept": {"id": "INT64", "title": "STRING"},
}
RULES = {
    "emp": DataRules(frozenset({"id", "name"}), (("id",),)),
    "dept": DataRules(frozenset({"id"}), (("id",),)),
}
QUERY = "SELECT name FROM emp WHERE sal > 100 AND dept = 3"


def _killers(query, mutant, datasets):
    with DatasetRunner(SCHEMA) as runner:
        return [
            d.label
            for d in datasets
            if not compare_outputs(
                runner.run(query, d.dataset), runner.run(mutant, d.dataset), check_column_names=False
            )[0]
        ]


def test_rules_are_respected_by_every_generated_database():
    suites = database_suite(QUERY, SCHEMA, RULES) + random_datasets(SCHEMA, RULES, range(20))
    for labeled in suites:
        for name, table in labeled.dataset.tables.items():
            names = [c.lower() for c, _ in table.columns]
            keys = [row[names.index("id")] for row in table.rows]
            assert None not in keys
            assert len(keys) == len(set(keys)), (labeled.label, name)
            if name == "emp":
                assert all(row[names.index("name")] is not None for row in table.rows)


def test_default_generation_without_rules_is_unchanged():
    dataset = generate_synthetic_dataset(SCHEMA, seed=3)
    assert dataset == generate_synthetic_dataset(SCHEMA, seed=3, rules=None)
    assert len(dataset.tables["emp"].rows) >= 2  # an exact duplicate is added


def test_boundary_databases_separate_a_comparison_from_its_boundary_mutant():
    mutant = "SELECT name FROM emp WHERE sal >= 100 AND dept = 3"
    killers = _killers(QUERY, mutant, targeted_datasets(QUERY, SCHEMA, RULES))
    assert any(label.startswith("boundary:sal") for label in killers)
    # one random database of the engine's usual shape does not see the boundary
    assert _killers(QUERY, mutant, random_datasets(SCHEMA, RULES, [1])) == []


def test_suite_has_corner_cases_and_a_table_empty_in_turn():
    labels = [d.label for d in database_suite("SELECT e.name FROM emp e JOIN dept d ON e.dept = d.id", SCHEMA, RULES)]
    for expected in ("empty", "single_row", "all_null", "empty:emp", "empty:dept", "one_value", "doubled"):
        assert expected in labels
    assert [d.rows for d in edge_datasets(SCHEMA, RULES) if d.label == "empty"] == [0]


def test_empty_table_database_separates_inner_from_left_join():
    inner = "SELECT e.name, d.title FROM emp e JOIN dept d ON e.dept = d.id"
    left = "SELECT e.name, d.title FROM emp e LEFT JOIN dept d ON e.dept = d.id"
    assert "empty:dept" in _killers(inner, left, database_suite(inner, SCHEMA, RULES))


def test_runner_matches_single_run_execution_and_reports_errors():
    dataset = generate_synthetic_dataset(SCHEMA, seed=2, rules=RULES)
    with DatasetRunner(SCHEMA) as runner:
        direct, _ = execute_on_dataset(QUERY, SCHEMA, dataset)
        assert runner.run(QUERY, dataset).rows == direct.rows
        with pytest.raises(ExecutionError):
            runner.run("SELECT nope FROM emp", dataset)
        with pytest.raises(ExecutionError):
            runner.run("SELECT * FROM missing_table", dataset)
        with pytest.raises(ExecutionError):
            runner.run("SELECT 1; SELECT 2", dataset)


def test_runner_times_out_a_runaway_query():
    dataset = generate_synthetic_dataset(SCHEMA, seed=2, rules=RULES)
    slow = "SELECT COUNT(*) FROM range(1000000000) a, range(1000000000) b"
    with DatasetRunner(SCHEMA) as runner:
        with pytest.raises(QueryTimeout):
            runner.run(slow, dataset, timeout=0.2)


def test_mutants_are_deterministic_distinct_and_cover_the_operators():
    query = (
        "SELECT d.title, SUM(e.sal) AS s, COUNT(*) AS c FROM emp e JOIN dept d ON e.dept = d.id "
        "WHERE e.sal + 1 > 100 AND e.name = 'x' AND COALESCE(e.dept, 0) BETWEEN 1 AND 5 "
        "GROUP BY d.title, e.dept HAVING COUNT(*) > 1 ORDER BY s LIMIT 3"
    )
    first = mutate(query)
    assert first == mutate(query)
    assert len({m.sql for m in first}) == len(first)
    assert {m.operator for m in first} == set(OPERATORS)
    assert query not in {m.sql for m in first}
    assert all(m.operator != "limit_changed" for m in mutate("SELECT sal FROM emp LIMIT 3"))


def test_null_keys_in_both_tables_separate_intersect_from_a_join():
    schema = {"t": {"a": "INT64"}, "u": {"b": "INT64"}}
    left = "SELECT a AS k FROM t INTERSECT DISTINCT SELECT b AS k FROM u"
    right = "SELECT DISTINCT t.a AS k FROM t JOIN u ON t.a = u.b"
    with DatasetRunner(schema) as runner:
        killers = [
            d.label
            for d in database_suite(left, schema, random_seeds=())
            if not compare_outputs(runner.run(left, d.dataset), runner.run(right, d.dataset))[0]
        ]
    assert "null_keys" in killers
