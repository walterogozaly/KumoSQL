"""Execution-based equivalence tests for SQL rewrites.

Each rewrite is run next to its original against deterministic synthetic
tables in DuckDB, and the result sets must match on every seed. The negative
tests make sure the harness actually catches broken rewrites.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from kumosql import lift_subqueries, prove_equivalent
from kumosql.result_equivalence import (
    ExecutionError,
    ResultEquivalenceStatus,
    assert_result_equivalent,
    check_result_equivalence,
    execute_on_dataset,
    generate_synthetic_dataset,
    prepare_statements,
    sqlx_to_sql,
)


SCHEMA = {
    "p.d.customers": {"id": "INT64", "name": "STRING", "region": "STRING", "active": "BOOL"},
    "p.d.orders": {
        "order_id": "INT64",
        "customer_id": "INT64",
        "amount": "FLOAT64",
        "ordered_on": "DATE",
    },
    "p.d.stops": {"stop_id": "INT64", "driver_id": "INT64", "arrived_at": "TIMESTAMP"},
    "p.d.drivers": {"driver_id": "INT64", "region": "STRING", "rate": "NUMERIC"},
}

SEEDS = range(12)


# Synthetic queries that exercise the subquery lifter. Every one must lift at
# least one subquery so the equivalence check is testing a real rewrite.
LIFT_CORPUS = {
    "simple_from": "SELECT c.id FROM (SELECT id FROM `p.d.customers`) AS c",
    "nested_from": """
SELECT *
FROM (
  SELECT id, name FROM (SELECT id, name FROM `p.d.customers` WHERE active) AS inner_c
) AS outer_c""",
    "join_subquery": """
SELECT c.name, o.total
FROM `p.d.customers` AS c
LEFT JOIN (
  SELECT customer_id, SUM(amount) AS total
  FROM `p.d.orders`
  GROUP BY customer_id
) AS o
  ON o.customer_id = c.id""",
    "both_sides_subqueries": """
SELECT a.region, a.n_customers, b.n_drivers
FROM (SELECT region, COUNT(*) AS n_customers FROM `p.d.customers` GROUP BY region) AS a
FULL OUTER JOIN (SELECT region, COUNT(*) AS n_drivers FROM `p.d.drivers` GROUP BY region) AS b
  USING (region)""",
    "inside_existing_cte": """
WITH stops AS (
  SELECT * FROM (SELECT stop_id, driver_id FROM `p.d.stops` WHERE stop_id > 0) AS s
), drivers AS (
  SELECT driver_id, region FROM `p.d.drivers`
)
SELECT stops.stop_id, drivers.region
FROM stops
JOIN drivers USING (driver_id)""",
    "aggregate_over_subquery_empty_input": """
SELECT COUNT(*) AS n, SUM(amount) AS total, MAX(ordered_on) AS latest
FROM (SELECT amount, ordered_on FROM `p.d.orders` WHERE amount > 0) AS o""",
    "union_all_of_subqueries": """
SELECT id FROM (SELECT id FROM `p.d.customers` WHERE active) AS a
UNION ALL
SELECT customer_id FROM (SELECT customer_id FROM `p.d.orders`) AS b""",
    "distinct_and_where_in_left_in_place": """
SELECT DISTINCT x.region
FROM (SELECT region, id FROM `p.d.customers`) AS x
WHERE x.id IN (SELECT customer_id FROM `p.d.orders` WHERE amount >= 1)""",
    "bigquery_functions": """
SELECT
  o.customer_id,
  SAFE_DIVIDE(SUM(o.amount), COUNT(*)) AS avg_amount,
  COUNTIF(o.amount > 1) AS big_orders,
  IFNULL(MIN(o.ordered_on), DATE '1970-01-01') AS first_order
FROM (SELECT * FROM `p.d.orders` WHERE customer_id IS NOT NULL) AS o
GROUP BY o.customer_id""",
    "script_with_create_table": """
CREATE OR REPLACE TABLE `p.d.customer_totals` AS
SELECT c.id, t.total
FROM `p.d.customers` AS c
JOIN (SELECT customer_id, SUM(amount) AS total FROM `p.d.orders` GROUP BY customer_id) AS t
  ON t.customer_id = c.id;""",
}


@pytest.mark.parametrize("name", sorted(LIFT_CORPUS))
def test_lifted_sql_returns_identical_results(name):
    original = LIFT_CORPUS[name]
    lifted = lift_subqueries(original)

    assert lifted.success, lifted.diagnostics
    assert lifted.lifted_subqueries > 0
    assert_result_equivalent(original, lifted.sql, SCHEMA, seeds=SEEDS)


def test_lifted_sqlx_returns_identical_results():
    original = """config { type: "table" }
SELECT c.id, o.total
FROM ${ref("customers")} AS c
JOIN (SELECT customer_id, SUM(amount) AS total FROM ${ref("p", "orders")} GROUP BY customer_id) AS o
  ON o.customer_id = c.id"""
    schema = {"customers": SCHEMA["p.d.customers"], "p.orders": SCHEMA["p.d.orders"]}

    lifted = lift_subqueries(original)

    assert lifted.success, lifted.diagnostics
    assert lifted.lifted_subqueries == 1
    assert "${ref(" in lifted.sql
    assert_result_equivalent(original, lifted.sql, schema, seeds=SEEDS)


# Broken "rewrites" the harness must reject with a counterexample.
BROKEN_REWRITES = {
    "left_join_to_inner_join": (
        "SELECT c.id, o.order_id FROM `p.d.customers` AS c LEFT JOIN `p.d.orders` AS o ON o.customer_id = c.id",
        "SELECT c.id, o.order_id FROM `p.d.customers` AS c JOIN `p.d.orders` AS o ON o.customer_id = c.id",
    ),
    "union_all_to_union_distinct": (
        "SELECT region FROM `p.d.customers` UNION ALL SELECT region FROM `p.d.drivers`",
        "SELECT region FROM `p.d.customers` UNION DISTINCT SELECT region FROM `p.d.drivers`",
    ),
    "dropped_distinct": (
        "SELECT DISTINCT region FROM `p.d.customers`",
        "SELECT region FROM `p.d.customers`",
    ),
    "null_unsafe_predicate": (
        "SELECT id FROM `p.d.customers` WHERE NOT (region = 'a')",
        "SELECT id FROM `p.d.customers` WHERE region IS DISTINCT FROM 'a'",
    ),
    "count_star_vs_count_column": (
        "SELECT COUNT(*) AS n FROM `p.d.customers`",
        "SELECT COUNT(name) AS n FROM `p.d.customers`",
    ),
    "pushed_filter_below_outer_join": (
        "SELECT c.id FROM `p.d.customers` AS c LEFT JOIN `p.d.orders` AS o ON o.customer_id = c.id WHERE o.amount > 1",
        "SELECT c.id FROM `p.d.customers` AS c LEFT JOIN (SELECT * FROM `p.d.orders` WHERE amount > 1) AS o ON o.customer_id = c.id",
    ),
}


@pytest.mark.parametrize("name", sorted(BROKEN_REWRITES))
def test_harness_catches_broken_rewrites(name):
    left, right = BROKEN_REWRITES[name]

    result = check_result_equivalence(left, right, SCHEMA, seeds=SEEDS)

    assert result.status is ResultEquivalenceStatus.DIFFERENT, result.describe()
    assert result.failing_seed is not None
    assert result.only_left or result.only_right
    assert f"failing seed: {result.failing_seed}" in result.describe()


def test_static_proofs_agree_with_execution():
    # The static prover must never prove something execution refutes.
    for original in LIFT_CORPUS.values():
        lifted = lift_subqueries(original).sql
        proof = prove_equivalent(original, lifted)
        if proof.proven:
            assert check_result_equivalence(original, lifted, SCHEMA, seeds=SEEDS).equivalent


def test_ordered_mode_detects_reordering():
    left = "SELECT id FROM `p.d.customers` WHERE id IS NOT NULL ORDER BY id"
    right = "SELECT id FROM `p.d.customers` WHERE id IS NOT NULL ORDER BY id DESC"

    assert check_result_equivalence(left, right, SCHEMA, seeds=SEEDS).equivalent
    ordered = check_result_equivalence(left, right, SCHEMA, seeds=SEEDS, ignore_row_order=False)
    assert ordered.status is ResultEquivalenceStatus.DIFFERENT
    assert ordered.reason == "same rows but in a different order"


def test_column_rename_is_a_difference_unless_names_are_ignored():
    left = "SELECT id FROM `p.d.customers`"
    right = "SELECT id AS customer_id FROM `p.d.customers`"

    assert check_result_equivalence(left, right, SCHEMA).status is ResultEquivalenceStatus.DIFFERENT
    assert check_result_equivalence(left, right, SCHEMA, check_column_names=False).equivalent


def test_float_noise_is_tolerated():
    left = "SELECT SUM(amount) AS s FROM `p.d.orders`"
    right = "SELECT SUM(amount * 3) / 3 AS s FROM `p.d.orders`"

    assert check_result_equivalence(left, right, SCHEMA, seeds=SEEDS).equivalent


def test_unknown_table_fails_closed():
    result = check_result_equivalence(
        "SELECT id FROM `p.d.customers`",
        "SELECT id FROM `p.d.customerz`",
        SCHEMA,
    )

    assert result.status is ResultEquivalenceStatus.ERROR
    assert "p.d.customerz" in result.reason


def test_execution_error_is_never_equivalence():
    result = check_result_equivalence(
        "SELECT id FROM `p.d.customers`",
        "SELECT no_such_column FROM `p.d.customers`",
        SCHEMA,
    )

    assert result.status is ResultEquivalenceStatus.ERROR
    assert result.reason.startswith("right side failed")


def test_synthetic_data_is_deterministic_and_covers_edge_cases():
    first = generate_synthetic_dataset(SCHEMA, seed=3)
    again = generate_synthetic_dataset(SCHEMA, seed=3)
    empty = generate_synthetic_dataset(SCHEMA, seed=0)

    assert first == again
    assert all(not table.rows for table in empty.tables.values())
    for table in first.tables.values():
        assert len(set(table.rows)) < len(table.rows), "expected a duplicate row"
    all_values = [v for t in first.tables.values() for row in t.rows for v in row]
    assert None in all_values


def test_write_targets_are_renamed_per_run_and_runs_are_isolated():
    script = """
CREATE OR REPLACE TABLE `p.d.customers` AS SELECT id FROM `p.d.customers` WHERE FALSE;
INSERT INTO `p.d.customers` (id) SELECT 42;"""
    statements, target = prepare_statements(script, SCHEMA, run_tag="left_1")

    assert target == "__eqv_left_1_target_001"
    assert all("p.d" not in statement and '"p"' not in statement for statement in statements)
    # The first reference reads the source table; later ones read the target.
    assert "src__p__d__customers" in statements[0]
    assert "__eqv_left_1_target_001" in statements[1]

    dataset = generate_synthetic_dataset(SCHEMA, seed=5)
    output, _ = execute_on_dataset(script, SCHEMA, dataset, run_tag="left_5")
    assert output.rows == ((42,),)
    # A second run on the same dataset still sees the original source rows.
    fresh, _ = execute_on_dataset("SELECT COUNT(*) AS n FROM `p.d.customers`", SCHEMA, dataset)
    assert fresh.rows == ((len(dataset.tables["p.d.customers"].rows),),)


def test_cte_names_shadow_unqualified_schema_tables():
    schema = {"orders": SCHEMA["p.d.orders"]}
    statements, _ = prepare_statements(
        "WITH orders AS (SELECT 1 AS order_id) SELECT order_id FROM orders", schema, run_tag="t"
    )

    assert "src__orders" not in statements[0]


def test_sqlx_rejects_unknown_interpolations():
    assert sqlx_to_sql('config { type: "view" }\nSELECT * FROM ${ref("a")}').strip() == "SELECT * FROM `a`"
    with pytest.raises(ExecutionError):
        sqlx_to_sql("SELECT * FROM ${self()}")


# ---------------------------------------------------------------------------
# Reproducibility and nondeterministic baselines
# ---------------------------------------------------------------------------

PINNED_DIGEST = "5ae4f0478bb9833d8ae6581075f62776b24765c0edb74185018f3a1cc8c7742f"


def _digest_of_pinned_dataset() -> str:
    import hashlib

    dataset = generate_synthetic_dataset(
        {"p.d.t": {"a": "INT64", "b": "STRING"}}, seed=3, rows_per_table=6
    )
    return hashlib.sha256(repr(dataset.tables["p.d.t"].rows).encode()).hexdigest()


def test_synthetic_dataset_is_pinned_across_versions():
    assert _digest_of_pinned_dataset() == PINNED_DIGEST


def test_nondeterministic_baseline_is_inconclusive():
    sql = "SELECT customer_id, RAND() AS r FROM `p.d.orders`"
    result = check_result_equivalence(sql, sql, SCHEMA, seeds=range(1, 4))
    assert result.status is ResultEquivalenceStatus.INCONCLUSIVE
    assert not result.equivalent
    assert "left side" in result.reason
    assert result.describe().startswith("inconclusive")


def test_nondeterministic_right_side_is_named():
    result = check_result_equivalence(
        "SELECT customer_id, 1.0 AS r FROM `p.d.orders`",
        "SELECT customer_id, RAND() AS r FROM `p.d.orders`",
        SCHEMA,
        seeds=range(1, 4),
    )
    assert result.status is ResultEquivalenceStatus.INCONCLUSIVE
    assert "right side" in result.reason


def test_stable_baseline_still_reports_different_and_equivalent():
    base = "SELECT customer_id FROM `p.d.orders`"
    different = check_result_equivalence(
        base, base + " WHERE amount > 1", SCHEMA, seeds=range(1, 6)
    )
    assert different.status is ResultEquivalenceStatus.DIFFERENT
    same = check_result_equivalence(base, base, SCHEMA, seeds=range(1, 4))
    assert same.status is ResultEquivalenceStatus.EQUIVALENT


def test_time_function_never_escapes_as_exception():
    sql = "SELECT CURRENT_TIMESTAMP() AS t FROM `p.d.orders`"
    result = check_result_equivalence(sql, sql, SCHEMA, seeds=range(1, 3))
    assert isinstance(result.status, ResultEquivalenceStatus)


def test_generated_values_include_the_queries_constants():
    # JOB-style filters on long string constants never match a small fixed domain, so a
    # faulty rewrite would agree with the original on empty results. With the constants in
    # the domain, the extra condition is caught.
    schema = {"p.d.info": {"id": "INT64", "info": "STRING", "note": "STRING"}}
    left = "SELECT id FROM `p.d.info` WHERE info = 'top 250 rank' AND note LIKE '%(co-production)%'"
    right = left + " AND id > 3"
    caught = check_result_equivalence(left, right, schema, seeds=range(1, 9))
    assert caught.status is ResultEquivalenceStatus.DIFFERENT
    blind = check_result_equivalence(left, right, schema, seeds=range(1, 9), use_query_constants=False)
    assert blind.status is ResultEquivalenceStatus.EQUIVALENT
