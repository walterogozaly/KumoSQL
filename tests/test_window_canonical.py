"""Window spellings read alike (``kumosql.window_canonical``) and their near misses on ``events(user_id, ts, value)``.

Every must-prove pair is also run on DuckDB (optimizer off) over databases with ties and NULLs, and every
must-not-prove pair carries a database on which the two queries return different bags.
"""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402

SCHEMA = {"events": ["user_id", "ts", "value"]}
TYPES = {"events": {"user_id": "INT64", "ts": "INT64", "value": "INT64"}}
SEL = "SELECT user_id, ts, value"
GROUPED = "(SELECT user_id, MIN(ts) AS m, SUM(value) AS s FROM events GROUP BY user_id) AS d"

# (name, left, right)
MUST_PROVE = [
    ("explicit_default_frame_with_order",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events"),
    ("explicit_default_frame_nulls_last",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts NULLS LAST "
     "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts NULLS LAST) AS s FROM events"),
    ("full_rows_frame_without_order",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events"),
    ("range_to_current_row_without_order",
     f"{SEL}, MAX(value) OVER (PARTITION BY user_id RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, MAX(value) OVER (PARTITION BY user_id) AS s FROM events"),
    ("full_frame_makes_order_moot",
     f"{SEL}, COUNT(value) OVER (PARTITION BY user_id ORDER BY ts DESC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM events",
     f"{SEL}, COUNT(value) OVER (PARTITION BY user_id) AS s FROM events"),
    ("navigation_explicit_default_frame",
     f"{SEL}, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts, value RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts, value) AS s FROM events"),
    ("order_by_partition_key",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY user_id DESC) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events"),
    ("order_by_partition_key_then_other",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY user_id, ts) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events"),
    ("where_constant_order_key",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events WHERE ts = 3",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events WHERE ts = 3"),
    ("where_constant_argument_and_partition",
     "SELECT user_id, SUM(ts) OVER (PARTITION BY ts, user_id) AS s FROM events WHERE ts = 3",
     "SELECT user_id, SUM(3) OVER (PARTITION BY user_id) AS s FROM events WHERE ts = 3"),
    ("unread_window_column_pruned",
     "SELECT user_id, ts FROM (SELECT user_id, ts, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) AS rn FROM events) AS d",
     "SELECT user_id, ts FROM events"),
    ("window_over_grouped_derived_table",
     f"SELECT d.m, SUM(d.s) OVER (PARTITION BY d.m ORDER BY d.s) AS w FROM {GROUPED}",
     "SELECT MIN(ts) AS m, SUM(SUM(value)) OVER (PARTITION BY MIN(ts) ORDER BY SUM(value)) AS w FROM events GROUP BY user_id"),
    ("grouped_windows_unnamed",
     "SELECT user_id, RANK() OVER (ORDER BY COUNT(*)) FROM events GROUP BY user_id",
     "SELECT d.user_id, RANK() OVER (ORDER BY d.c) FROM (SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id) AS d"),
]

# (name, left, right, witness rows)
MUST_NOT_PROVE = [
    ("rows_vs_range_with_peers",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     [(1, 1, 1), (1, 1, 10), (1, 2, 2)]),
    ("rows_to_current_row_without_order",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("frame_from_current_row_is_not_default",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("offset_frame_is_not_default",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events", [(1, 1, 1), (1, 2, 2), (1, 3, 4)]),
    ("default_frame_with_order_is_not_partition",
     f"{SEL}, MAX(value) OVER (ORDER BY ts RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM events",
     f"{SEL}, MAX(value) OVER () AS s FROM events", [(1, 1, 5), (1, 2, 2), (1, 3, 9)]),
    ("order_direction",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts DESC) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("nulls_placement",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ASC NULLS FIRST) AS s FROM events",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ASC NULLS LAST) AS s FROM events", [(1, None, 1), (1, 1, 2)]),
    # sqlglot cannot spell this NULLS placement in BigQuery inside such a window: the text must not hide it
    ("nulls_placement_full_frame_navigation",
     f"{SEL}, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts NULLS LAST "
     "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM events",
     f"{SEL}, LAST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts NULLS FIRST "
     "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM events", [(1, None, 1), (1, 1, 2)]),
    ("order_key_not_a_partition_key",
     f"{SEL}, COUNT(*) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events",
     f"{SEL}, COUNT(*) OVER (PARTITION BY user_id) AS s FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("filter_is_not_an_equality",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events WHERE ts >= 3",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events WHERE ts >= 3", [(1, 3, 1), (1, 4, 2)]),
    ("filter_is_a_disjunction",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events WHERE ts = 3 OR value = 2",
     f"{SEL}, SUM(value) OVER (PARTITION BY user_id) AS s FROM events WHERE ts = 3 OR value = 2", [(1, 3, 1), (1, 4, 2)]),
    ("pruned_column_is_read",
     "SELECT user_id, ts FROM (SELECT user_id, ts, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) AS rn FROM events) AS d WHERE rn = 1",
     "SELECT user_id, ts FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("pruned_column_read_by_order_limit",
     "SELECT user_id FROM (SELECT user_id, COUNT(*) OVER (PARTITION BY user_id) AS c FROM events) AS d ORDER BY c DESC, user_id LIMIT 1",
     "SELECT user_id FROM events ORDER BY user_id LIMIT 1", [(1, 1, 1), (2, 1, 1), (2, 2, 2)]),
    ("pruned_column_holds_the_only_aggregate",
     "SELECT x FROM (SELECT 1 AS x, SUM(COUNT(*)) OVER () AS w FROM events) AS d",
     "SELECT 1 AS x FROM events", [(1, 1, 1), (1, 2, 2)]),
    ("grouped_partition_by_other_aggregate",
     f"SELECT d.m, SUM(d.s) OVER (PARTITION BY d.m) AS w FROM {GROUPED}",
     "SELECT MIN(ts) AS m, SUM(SUM(value)) OVER (PARTITION BY MAX(ts)) AS w FROM events GROUP BY user_id",
     [(1, 1, 1), (1, 2, 2), (2, 1, 3), (2, 3, 4)]),
    ("grouped_source_filtered_outside",
     f"SELECT d.m, SUM(d.s) OVER () AS w FROM {GROUPED} WHERE d.s > 2",
     "SELECT MIN(ts) AS m, SUM(SUM(value)) OVER () AS w FROM events GROUP BY user_id", [(1, 1, 1), (2, 1, 3)]),
]

DATABASES = [
    [(1, 1, 1), (1, 1, 10), (1, 2, 2), (2, 3, None), (2, None, 4), (3, 3, 3)],
    [(1, 3, 5), (1, 3, 5), (2, 3, 7), (2, 4, 1), (None, 3, 2), (None, None, None)],
    [],
]


def _duckdb_rows(left, right, rows):
    db = duckdb.connect()
    db.execute("CREATE TABLE events (user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", rows)
    queries = [sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (left, right)]
    return [sorted(result, key=repr) for result in run_unoptimized(db, *queries)]


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="bigquery").proven


@pytest.mark.parametrize("name,left,right", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proves(name, left, right):
    assert _proven(left, right)
    assert _proven(right, left)


@pytest.mark.parametrize("name,left,right", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proved_pairs_agree_on_duckdb(name, left, right):
    for rows in DATABASES:
        a, b = _duckdb_rows(left, right, rows)
        assert a == b, rows


@pytest.mark.parametrize("name,left,right,rows", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_witness_tells_the_pair_apart(name, left, right, rows):
    a, b = _duckdb_rows(left, right, rows)
    assert a != b


@pytest.mark.parametrize("name,left,right,rows", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_does_not_prove(name, left, right, rows):
    assert not _proven(left, right)
    assert not _proven(right, left)
