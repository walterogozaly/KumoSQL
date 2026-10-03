"""Window-function and QUALIFY soundness pairs (R011) on ``events(user_id, ts, value)`` in BigQuery.

Each must-not-prove pair carries a database on which the two queries return different bags, checked
on DuckDB with the optimizer off. Neither prover may prove such a pair: the algebraic prover keeps
window computations opaque, and the SQLSolver backend declines windows and QUALIFY before it runs.
"""

from pathlib import Path

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402
from kumosql.sqlsolver_backend import SqlSolverRuntime, prove_equivalent_sqlsolver  # noqa: E402

SCHEMA = {"events": ["user_id", "ts", "value"]}
TYPED = {"events": [("user_id", "INT64"), ("ts", "INT64"), ("value", "INT64")]}
NOT_NULL = frozenset({"user_id", "ts", "value"})
UNIQUE_TS = {"events": TableConstraints(not_null=NOT_NULL, keys=(("user_id", "ts"),))}

SEL = "SELECT user_id, ts, value FROM events"
RN_DESC = "ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC)"
RN = "ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts)"
RN_SUB = f"SELECT user_id, ts, value FROM (SELECT user_id, ts, value, {RN} AS rn FROM events)"

# (name, left, right, witness rows, constraints)
MUST_NOT_PROVE = [
    ("rn1_vs_max_ties", f"{SEL} QUALIFY {RN_DESC} = 1", f"{SEL} QUALIFY ts = MAX(ts) OVER (PARTITION BY user_id)",
     [(1, 10, 4), (1, 10, 9)], None),
    ("rn1_vs_max_ties_subquery",
     f"SELECT user_id, ts, value FROM (SELECT user_id, ts, value, {RN_DESC} AS rn FROM events) WHERE rn = 1",
     "SELECT user_id, ts, value FROM (SELECT user_id, ts, value, MAX(ts) OVER (PARTITION BY user_id) AS m FROM events) WHERE ts = m",
     [(1, 10, 4), (1, 10, 9)], None),
    ("rank1_vs_rn1_ties", f"{SEL} QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1", f"{SEL} QUALIFY {RN_DESC} = 1",
     [(1, 10, 4), (1, 10, 9)], None),
    ("rank_le2_vs_dense_le2", f"{SEL} QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts) <= 2",
     f"{SEL} QUALIFY DENSE_RANK() OVER (PARTITION BY user_id ORDER BY ts) <= 2", [(1, 1, 5), (1, 1, 6), (1, 2, 7)], None),
    ("rank2_vs_dense2", f"{SEL} QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts) = 2",
     f"{SEL} QUALIFY DENSE_RANK() OVER (PARTITION BY user_id ORDER BY ts) = 2", [(1, 1, 1), (1, 1, 2), (1, 2, 3)], None),
    ("ts_filter_before_vs_after_qualify", f"{SEL} WHERE ts >= 2 QUALIFY {RN} = 1", f"{SEL} QUALIFY {RN} = 1 AND ts >= 2",
     [(1, 1, 10), (1, 2, 20)], None),
    ("ts_filter_before_vs_after_subquery", f"{SEL} WHERE ts >= 2 QUALIFY {RN} = 1", f"{RN_SUB} WHERE rn = 1 AND ts >= 2",
     [(1, 1, 10), (1, 2, 20)], None),
    ("value_filter_before_vs_after", f"{SEL} WHERE value > 0 QUALIFY {RN} = 1", f"{RN_SUB} WHERE rn = 1 AND value > 0",
     [(1, 1, -1), (1, 2, 5)], None),
    # a unique, non-null order does not make a non-partition filter commute with the ranking
    ("value_filter_before_vs_after_unique_order", f"{SEL} WHERE value > 0 QUALIFY {RN} = 1", f"{SEL} QUALIFY {RN} = 1 AND value > 0",
     [(1, 1, -1), (1, 2, 5)], UNIQUE_TS),
    ("default_range_sum_vs_rows",
     "SELECT user_id, ts, value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events",
     "SELECT user_id, ts, value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     [(1, 1, 1), (1, 1, 10), (1, 2, 2)], None),
    ("last_value_default_vs_full_frame",
     "SELECT user_id, ts, value, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts) AS l FROM events",
     "SELECT user_id, ts, value, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts "
     "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS l FROM events",
     [(1, 1, 10), (1, 2, 20)], None),
    ("sum_no_order_vs_cumulative",
     "SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events",
     "SELECT user_id, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events", [(1, 1, 1), (1, 2, 2)], None),
    ("nulls_first_vs_nulls_last", f"{SEL} QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts ASC NULLS FIRST) = 1",
     f"{SEL} QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts ASC NULLS LAST) = 1", [(1, None, 3), (1, 1, 4)], None),
    # BigQuery puts NULLs first for ASC and last for DESC
    ("implicit_asc_vs_nulls_last", f"{SEL} QUALIFY {RN} = 1",
     f"{SEL} QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts NULLS LAST) = 1", [(1, None, 3), (1, 1, 4)], None),
    ("implicit_desc_vs_nulls_first", f"{SEL} QUALIFY {RN_DESC} = 1",
     f"{SEL} QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC NULLS FIRST) = 1", [(1, None, 3), (1, 1, 4)], None),
    ("max_window_vs_plain_equality_join", f"{SEL} QUALIFY ts = MAX(ts) OVER (PARTITION BY user_id)",
     "SELECT e.user_id, e.ts, e.value FROM events AS e JOIN (SELECT user_id, MAX(ts) AS m FROM events GROUP BY user_id) AS g "
     "ON e.user_id = g.user_id AND e.ts = g.m",
     [(None, 1, 1)], None),
    # the declared key makes ts unique, not value
    ("rank_swap_unique_key_on_other_column", f"{SEL} QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY value) = 1",
     f"{SEL} QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY value) = 1", [(1, 1, 5), (1, 2, 5)], UNIQUE_TS),
    # (user_id, ts) is unique, but ts alone is not across partitions
    ("rank_swap_unique_key_without_partition", f"{SEL} QUALIFY ROW_NUMBER() OVER (ORDER BY ts) <= 1",
     f"{SEL} QUALIFY RANK() OVER (ORDER BY ts) <= 1", [(1, 1, 5), (2, 1, 5)], UNIQUE_TS),
    ("partition_on_left_joined_side",
     "SELECT a.user_id, COUNT(*) OVER (PARTITION BY b.user_id) AS c FROM events AS a LEFT JOIN events AS b ON a.user_id = b.user_id AND b.ts > 5",
     "SELECT a.user_id, COUNT(*) OVER (PARTITION BY a.user_id) AS c FROM events AS a LEFT JOIN events AS b ON a.user_id = b.user_id AND b.ts > 5",
     [(1, 1, 1), (2, 1, 1)], None),
    ("lag_vs_lead",
     "SELECT user_id, LAG(value) OVER (PARTITION BY user_id ORDER BY ts) AS p FROM events",
     "SELECT user_id, LEAD(value) OVER (PARTITION BY user_id ORDER BY ts) AS p FROM events", [(1, 1, 1), (1, 2, 2)], None),
]

SHOULD_PROVE = [
    ("qualify_vs_subquery_where", f"{SEL} QUALIFY {RN} = 1", f"{RN_SUB} WHERE rn = 1", None),
]


def _duckdb_rows(left, right, rows):
    db = duckdb.connect()
    db.execute("CREATE TABLE events (user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", rows)
    queries = [sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (left, right)]
    return [sorted(result, key=repr) for result in run_unoptimized(db, *queries)]


def _algebraic(left, right, constraints):
    extra = {"constraints": constraints} if constraints else {}
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery", **extra)


def _sqlsolver_saying_eq(left, right):
    """The SQLSolver backend with a stand-in solver that answers EQ to everything it is handed."""

    runtime = SqlSolverRuntime(java=Path("java"), jar=Path("sqlsolver.jar"), lib_dir=Path("lib"))
    return prove_equivalent_sqlsolver(
        left, right, schema=TYPED, runtime=runtime, runner=lambda pairs, *a, **k: ["EQ"] * len(pairs)
    )


@pytest.mark.parametrize("name,left,right,rows,constraints", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_witness_tells_the_pair_apart(name, left, right, rows, constraints):
    a, b = _duckdb_rows(left, right, rows)
    assert a != b
    if name in ("rn1_vs_max_ties", "rn1_vs_max_ties_subquery", "rank1_vs_rn1_ties"):
        assert sorted(map(len, (a, b))) == [1, 2]  # ROW_NUMBER keeps one tied row, whichever it is


@pytest.mark.parametrize("name,left,right,rows,constraints", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_algebraic_prover_does_not_prove(name, left, right, rows, constraints):
    assert not _algebraic(left, right, constraints).proven
    assert not _algebraic(right, left, constraints).proven


@pytest.mark.parametrize("name,left,right,rows,constraints", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_sqlsolver_backend_declines(name, left, right, rows, constraints):
    result = _sqlsolver_saying_eq(left, right)
    assert not result.proven
    assert "cannot translate" in result.reason


@pytest.mark.parametrize("name,left,right,constraints", SHOULD_PROVE, ids=[p[0] for p in SHOULD_PROVE])
def test_algebraic_prover_proves(name, left, right, constraints):
    assert _algebraic(left, right, constraints).proven
    assert _algebraic(right, left, constraints).proven
