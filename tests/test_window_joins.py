"""Windows spelled as the joins they equal: ``window_aggregate_joins`` and ``lag_lead_joins`` on ``events(id, user_id, ts, value)``.

The rewrites run as a later attempt of ``prove_equivalent_algebraic`` (``normalize(..., window_joins=True)``). Every
must-prove pair is also run on DuckDB (optimizer off, one thread, BigQuery semantics from ``bigquery_on_duckdb``) over
databases that keep the declared key and NOT NULL columns and put ties and NULLs everywhere else, and so is the
rewritten text itself. Every must-not-prove pair carries a database on which the two queries return different bags,
and every declined window is checked to survive ``normalize`` untouched.
"""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql import bigquery_on_duckdb  # noqa: E402
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

COLUMNS = ["id", "user_id", "ts", "value"]
SCHEMA = {"events": COLUMNS}
TYPES = {"events": {c: "INT64" for c in COLUMNS}}
FLOAT_VALUE_TYPES = {"events": {"id": "INT64", "user_id": "INT64", "ts": "INT64", "value": "FLOAT64"}}
FLOAT_TYPES = {"events": {"id": "INT64", "user_id": "FLOAT64", "ts": "INT64", "value": "INT64"}}
KEY = {"events": TableConstraints(not_null=frozenset({"id", "user_id"}), keys=(("id",),))}  # id key, user_id never NULL
NULLABLE = {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}  # user_id may be NULL
UNKEYED = {"events": TableConstraints(not_null=frozenset({"id", "user_id"}))}

# databases: id is unique everywhere; user_id has no NULL in KEY_DATABASES
KEY_DATABASES = [
    [(1, 1, 1, 1), (2, 1, 1, 10), (3, 1, 2, 2), (4, 2, 3, None), (5, 2, None, 4), (6, 3, 3, 3)],
    [(1, 1, 3, 5), (2, 1, 3, 5), (3, 2, 3, 7), (4, 2, 3, 1), (5, 4, 3, 2)],
    [(10, 1, 7, 7), (20, 1, 7, 7), (30, 1, 7, 7)],
    [(1, 1, None, None), (2, 1, None, None)],
    [],
]
NULLABLE_DATABASES = KEY_DATABASES + [
    [(1, None, 1, 1), (2, None, 1, 10), (3, 1, 2, 2), (4, None, 2, None), (5, 1, 2, 3)],
    [(1, None, None, None), (2, None, 5, 5)],
]


def databases(constraints):
    return NULLABLE_DATABASES if constraints is NULLABLE else KEY_DATABASES


def grouped(aggregate, where=""):
    return f"(SELECT user_id, {aggregate} AS s FROM events {where} GROUP BY user_id) AS g"


def ordinal(columns, partition="", order="ORDER BY id"):
    return f"SELECT {columns}, ROW_NUMBER() OVER ({partition} {order}) AS rn FROM events"


def lag_join(function_columns, value, partition="", order="ORDER BY id", on="b.rn = a.rn - 1", part_on=""):
    return (
        f"WITH r AS ({ordinal(function_columns, partition, order)}) "
        f"SELECT a.id, {value} AS p FROM r AS a LEFT JOIN r AS b ON {part_on}{on}"
    )


def over(function, partition="PARTITION BY user_id", order="", alias="s"):
    return f"SELECT id, {function} OVER ({partition} {order}) AS {alias} FROM events"


# (name, left, right, constraints)
AGGREGATES = [
    ("sum", over("SUM(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", KEY),
    ("count_star", over("COUNT(*)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('COUNT(*)')} ON e.user_id = g.user_id", KEY),
    ("count_column", over("COUNT(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('COUNT(value)')} ON e.user_id = g.user_id", KEY),
    ("min", over("MIN(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('MIN(value)')} ON e.user_id = g.user_id", KEY),
    ("max", over("MAX(ts)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('MAX(ts)')} ON e.user_id = g.user_id", KEY),
    ("avg", over("AVG(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('AVG(value)')} ON e.user_id = g.user_id", KEY),
    ("sum_of_an_expression", over("SUM(value + ts)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value + ts)')} ON e.user_id = g.user_id", KEY),
    ("no_partition", over("SUM(value)", ""), "SELECT e.id, g.s FROM events AS e CROSS JOIN (SELECT SUM(value) AS s FROM events) AS g", KEY),
    ("nullable_key_null_safe_join", over("SUM(value)"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id IS NOT DISTINCT FROM g.user_id", NULLABLE),
    ("nullable_key_spelled_out_null_safe", over("MAX(value)"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('MAX(value)')} ON e.user_id = g.user_id OR (e.user_id IS NULL AND g.user_id IS NULL)", NULLABLE),
    ("where_in_both", over("SUM(value)").replace("FROM events", "FROM events WHERE ts > 1"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)', 'WHERE ts > 1')} ON e.user_id = g.user_id WHERE e.ts > 1", KEY),
    ("two_windows_one_partition", "SELECT id, SUM(value) OVER (PARTITION BY user_id) AS s, COUNT(*) OVER (PARTITION BY user_id) AS c FROM events",
     "SELECT e.id, g.s, g.c FROM events AS e JOIN (SELECT user_id, SUM(value) AS s, COUNT(*) AS c FROM events GROUP BY user_id) AS g ON e.user_id = g.user_id", KEY),
    ("share_of_the_partition", "SELECT id, value * 100 / SUM(value) OVER (PARTITION BY user_id) AS s FROM events",
     f"SELECT e.id, e.value * 100 / g.s AS s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", KEY),
    ("full_frame_spelled_out", over("SUM(value)", "PARTITION BY user_id", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", KEY),
]

# (name, left, right, constraints, witness rows)
AGGREGATE_NEGATIVES = [
    ("nullable_key_plain_join_drops_the_null_partition", over("SUM(value)"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", NULLABLE,
     [(1, None, 1, 1), (2, 1, 2, 2)]),
    ("nullable_key_left_join_gives_the_null_partition_no_sum", over("SUM(value)"),
     f"SELECT e.id, g.s FROM events AS e LEFT JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", NULLABLE,
     [(1, None, 1, 1), (2, None, 2, 2)]),
    ("grouped_side_misses_the_where", over("SUM(value)").replace("FROM events", "FROM events WHERE ts > 1"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id WHERE e.ts > 1", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 7)]),
    ("running_total_is_not_the_group_total", over("SUM(value)", "PARTITION BY user_id", "ORDER BY id"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 7)]),
    ("offset_frame_can_be_empty", over("SUM(value)", "PARTITION BY user_id", "ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING"),
     f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.user_id = g.user_id", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 7)]),
    ("other_aggregate", over("MIN(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('MAX(value)')} ON e.user_id = g.user_id", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 7)]),
    ("join_on_another_key", over("SUM(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('SUM(value)')} ON e.id = g.user_id", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 7)]),
    ("count_of_distinct_values_is_not_the_count", over("COUNT(value)"), f"SELECT e.id, g.s FROM events AS e JOIN {grouped('COUNT(DISTINCT value)')} ON e.user_id = g.user_id", KEY,
     [(1, 1, 1, 5), (2, 1, 2, 5)]),
]

# windows the rewrite must leave alone: (name, sql, types)
DECLINED = [
    ("ordered_window", over("SUM(value)", "PARTITION BY user_id", "ORDER BY id"), TYPES),
    ("partial_frame", over("SUM(value)", "PARTITION BY user_id", "ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING"), TYPES),
    ("distinct_argument", over("COUNT(DISTINCT value)"), TYPES),
    ("float_partition_key", over("SUM(value)"), FLOAT_TYPES),
    ("window_next_to_row_number", "SELECT id, SUM(value) OVER (PARTITION BY user_id) AS s, ROW_NUMBER() OVER (ORDER BY id) AS n FROM events", TYPES),
    ("unnamed_window_output", "SELECT id, SUM(value) OVER (PARTITION BY user_id) FROM events", TYPES),
    ("after_a_join", "SELECT e.id, SUM(e.value) OVER (PARTITION BY e.user_id) AS s FROM events AS e JOIN events AS f ON e.id = f.id", TYPES),
    ("grouped_select", "SELECT user_id, SUM(COUNT(*)) OVER (PARTITION BY user_id) AS s FROM events GROUP BY user_id", TYPES),
    ("qualify", "SELECT id FROM events QUALIFY SUM(value) OVER (PARTITION BY user_id) > 3", TYPES),
    ("nondeterministic_where", over("SUM(value)").replace("FROM events", "FROM events WHERE RAND() < 0.5"), TYPES),
    ("float_sum_argument", over("SUM(value)"), FLOAT_VALUE_TYPES),
    ("float_avg_argument", over("AVG(value)"), FLOAT_VALUE_TYPES),
    ("derived_table_with_a_limit", "SELECT id, SUM(value) OVER (PARTITION BY user_id) AS s FROM (SELECT * FROM events LIMIT 3) AS d", TYPES),
    ("derived_table_with_a_window", "SELECT id, SUM(value) OVER (PARTITION BY user_id) AS s FROM (SELECT id, user_id, value, ROW_NUMBER() OVER (ORDER BY ts) AS n FROM events) AS d", TYPES),
    ("subquery_in_where", over("SUM(value)").replace("FROM events", "FROM events WHERE value IN (SELECT value FROM events)"), TYPES),
]

LAGS = [
    ("lag", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value"), KEY),
    ("lag_partitioned", "SELECT id, LAG(value) OVER (PARTITION BY user_id ORDER BY id) AS p FROM events",
     lag_join("id, user_id, value", "b.value", "PARTITION BY user_id", part_on="b.user_id = a.user_id AND "), KEY),
    ("lag_partitioned_nullable_key", "SELECT id, LAG(value) OVER (PARTITION BY user_id ORDER BY id) AS p FROM events",
     lag_join("id, user_id, value", "b.value", "PARTITION BY user_id", part_on="b.user_id IS NOT DISTINCT FROM a.user_id AND "), NULLABLE),
    ("lead", "SELECT id, LEAD(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="b.rn = a.rn + 1"), KEY),
    ("lag_neighbour_written_the_other_way", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="a.rn = b.rn + 1"), KEY),
    ("lead_neighbour_as_a_sum", "SELECT id, LEAD(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="a.rn + 1 = b.rn"), KEY),
    ("lead_neighbour_as_a_difference", "SELECT id, LEAD(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="b.rn - 1 = a.rn"), KEY),
    ("lag_offset_two", "SELECT id, LAG(value, 2) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="b.rn = a.rn - 2"), KEY),
    ("lead_default_and_descending_order", "SELECT id, LEAD(value, 2, -1) OVER (ORDER BY id DESC) AS p FROM events",
     lag_join("id, value", "CASE WHEN b.rn IS NULL THEN -1 ELSE b.value END", order="ORDER BY id DESC", on="b.rn = a.rn + 2"), KEY),
    ("lag_unique_key_among_the_order_keys", "SELECT id, LAG(value) OVER (ORDER BY ts, id) AS p FROM events",
     lag_join("id, value, ts", "b.value", order="ORDER BY ts, id"), KEY),
    ("lag_partition_key_completes_the_key", "SELECT id, LAG(value) OVER (PARTITION BY id ORDER BY ts) AS p FROM events",
     lag_join("id, value, ts", "b.value", "PARTITION BY id", "ORDER BY ts", part_on="b.id = a.id AND "), KEY),
]

LAG_NEGATIVES = [
    ("no_key_declared", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value"), UNKEYED, None),
    ("ties_on_the_order", "SELECT id, LAG(value) OVER (ORDER BY ts) AS p FROM events",
     lag_join("id, value, ts", "b.value", order="ORDER BY ts"), KEY, None),
    ("inner_join_loses_the_first_row", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events",
     lag_join("id, value", "b.value").replace("LEFT JOIN", "JOIN"), KEY, [(1, 1, 1, 1), (2, 1, 1, 2)]),
    ("coalesce_replaces_a_null_neighbour_too", "SELECT id, LAG(value, 1, 0) OVER (ORDER BY id) AS p FROM events",
     lag_join("id, value", "COALESCE(b.value, 0)"), KEY, [(1, 1, 1, None), (2, 1, 1, 2)]),
    ("wrong_offset", "SELECT id, LAG(value, 2) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value"), KEY,
     [(1, 1, 1, 1), (2, 1, 1, 2), (3, 1, 1, 3)]),
    ("mirrored_wrong_offset", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value", on="a.rn = b.rn + 2"), KEY,
     [(1, 1, 1, 1), (2, 1, 1, 2), (3, 1, 1, 3)]),
    ("lead_is_not_lag", "SELECT id, LEAD(value) OVER (ORDER BY id) AS p FROM events", lag_join("id, value", "b.value"), KEY,
     [(1, 1, 1, 1), (2, 1, 1, 2)]),
    ("partition_ignored", "SELECT id, LAG(value) OVER (PARTITION BY user_id ORDER BY id) AS p FROM events", lag_join("id, value", "b.value"), KEY,
     [(1, 1, 1, 1), (2, 2, 1, 2)]),
    ("nullable_partition_joined_with_equals", "SELECT id, LAG(value) OVER (PARTITION BY user_id ORDER BY id) AS p FROM events",
     lag_join("id, user_id, value", "b.value", "PARTITION BY user_id", part_on="b.user_id = a.user_id AND "), NULLABLE,
     [(1, None, 1, 1), (2, None, 1, 2)]),
]

# ties make the window itself depend on how rows are stored: (name, query, rows) where reversing the rows changes its bag
TIE_WITNESSES = [
    ("no_key_declared", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", [(1, 1, 1, 1), (1, 1, 1, 2), (3, 1, 1, 3)]),
    ("ties_on_the_order", "SELECT id, LAG(value) OVER (ORDER BY ts) AS p FROM events", [(1, 1, 1, 1), (2, 1, 1, 2), (3, 1, 2, 3)]),
]

LAG_DECLINED = [
    ("ignore_nulls", "SELECT id, LAG(value IGNORE NULLS) OVER (ORDER BY id) AS p FROM events", TYPES, KEY),
    ("offset_zero", "SELECT id, LAG(value, 0) OVER (ORDER BY id) AS p FROM events", TYPES, KEY),
    ("offset_is_a_column", "SELECT id, LAG(value, ts) OVER (ORDER BY id) AS p FROM events", TYPES, KEY),
    ("default_reads_the_row", "SELECT id, LAG(value, 1, ts) OVER (ORDER BY id) AS p FROM events", TYPES, KEY),
    ("order_with_ties", "SELECT id, LAG(value) OVER (ORDER BY ts) AS p FROM events", TYPES, KEY),
    ("no_key", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", TYPES, UNKEYED),
    ("key_may_be_null", "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events", TYPES, {"events": TableConstraints(keys=(("id",),))}),
    ("two_different_orders", "SELECT id, LAG(value) OVER (ORDER BY id) AS p, LEAD(value) OVER (ORDER BY id DESC) AS q FROM events", TYPES, KEY),
    ("float_partition_key", "SELECT id, LAG(value) OVER (PARTITION BY user_id ORDER BY id) AS p FROM events", FLOAT_TYPES, KEY),
    ("after_a_join", "SELECT e.id, LAG(e.value) OVER (ORDER BY e.id) AS p FROM events AS e JOIN events AS f ON e.id = f.id", TYPES, KEY),
    ("row_number_alongside", "SELECT id, LAG(value) OVER (ORDER BY id) AS p, ROW_NUMBER() OVER (ORDER BY id) AS n FROM events", TYPES, KEY),
    ("unnamed_output", "SELECT id, LAG(value) OVER (ORDER BY id) FROM events", TYPES, KEY),
]


def _duck(sql):
    return bigquery_on_duckdb.to_duckdb_sql(sqlglot.parse_one(sql, read="bigquery"))


def _database(rows):
    db = duckdb.connect()
    bigquery_on_duckdb.configure(db)
    db.execute("SET threads=1")
    db.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", rows)
    return db


def _bags(left, right, rows):
    return [sorted(result, key=repr) for result in run_unoptimized(_database(rows), _duck(left), _duck(right))]


def _proven(left, right, constraints, types=TYPES):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types, constraints=constraints, dialect="bigquery").proven


def _normal(sql, constraints, types=TYPES, window_joins=True):
    kwargs = {}
    if constraints:
        kwargs["not_null"] = {t: c.not_null for t, c in constraints.items()}
        kwargs["keys"] = {t: [tuple(k) for k in c.keys] for t, c in constraints.items()}
    return normalize(sql, schema=SCHEMA, types=types, window_joins=window_joins, **kwargs)


def _ids(cases):
    return [case[0] for case in cases]


@pytest.mark.parametrize("name,left,right,constraints", AGGREGATES + LAGS, ids=_ids(AGGREGATES + LAGS))
def test_proves(name, left, right, constraints):
    assert _proven(left, right, constraints)
    assert _proven(right, left, constraints)


@pytest.mark.parametrize("name,left,right,constraints", AGGREGATES + LAGS, ids=_ids(AGGREGATES + LAGS))
def test_proved_pairs_agree_on_duckdb(name, left, right, constraints):
    for rows in databases(constraints):
        a, b = _bags(left, right, rows)
        assert a == b, rows


@pytest.mark.parametrize("name,left,right,constraints", AGGREGATES + LAGS, ids=_ids(AGGREGATES + LAGS))
def test_rewritten_text_agrees_with_the_window_on_duckdb(name, left, right, constraints):
    rewritten = _normal(left, constraints)
    for rows in databases(constraints):
        a, b = _bags(left, rewritten, rows)
        assert a == b, (rows, rewritten)


@pytest.mark.parametrize(
    "name,left,right,constraints,rows", AGGREGATE_NEGATIVES + LAG_NEGATIVES, ids=_ids(AGGREGATE_NEGATIVES + LAG_NEGATIVES)
)
def test_does_not_prove(name, left, right, constraints, rows):
    assert not _proven(left, right, constraints)
    assert not _proven(right, left, constraints)


@pytest.mark.parametrize(
    "name,left,right,constraints,rows", AGGREGATE_NEGATIVES + LAG_NEGATIVES, ids=_ids(AGGREGATE_NEGATIVES + LAG_NEGATIVES)
)
def test_witness_tells_the_pair_apart(name, left, right, constraints, rows):
    if rows is None:
        pytest.skip("a tie case: see test_tied_window_depends_on_storage_order")
    a, b = _bags(left, right, rows)
    assert a != b


@pytest.mark.parametrize("name,query,rows", TIE_WITNESSES, ids=_ids(TIE_WITNESSES))
def test_tied_window_depends_on_storage_order(name, query, rows):
    (forward, _), (backward, _) = _bags(query, query, rows), _bags(query, query, rows[::-1])
    assert forward != backward


@pytest.mark.parametrize("name,sql,types", DECLINED, ids=_ids(DECLINED))
def test_aggregate_window_left_alone(name, sql, types):
    kept = _normal(sql, KEY, types)
    assert "OVER" in kept and "kqg" not in kept


@pytest.mark.parametrize("name,sql,types,constraints", LAG_DECLINED, ids=_ids(LAG_DECLINED))
def test_lag_left_alone(name, sql, types, constraints):
    kept = _normal(sql, constraints, types)
    assert ("LAG" in kept or "LEAD" in kept) and "kqb" not in kept


def test_windows_are_untouched_unless_asked():
    sql = over("SUM(value)")
    assert "kqg" not in _normal(sql, KEY, window_joins=False)
    lag = "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events"
    assert "kqb" not in _normal(lag, KEY, window_joins=False)


def test_the_plain_attempt_does_not_prove_these_pairs():
    # the rewrites are later attempts: with them off a window and its join do not read alike
    for name, left, right, constraints in AGGREGATES[:1] + LAGS[:1]:
        assert _normal(left, constraints, window_joins=False) != _normal(right, constraints, window_joins=False)


def test_a_rewrite_that_fires_records_its_assumption():
    from kumosql.window_aggregate_joins import WINDOW_JOIN_ASSUMPTION

    for sql in (over("SUM(value)"), "SELECT id, LAG(value) OVER (ORDER BY id) AS p FROM events"):
        assumptions: set[str] = set()
        normalize(sql, schema=SCHEMA, types=TYPES, window_joins=True, not_null={"events": KEY["events"].not_null}, keys={"events": [("id",)]}, _assumptions=assumptions)
        assert WINDOW_JOIN_ASSUMPTION in assumptions
    quiet: set[str] = set()
    normalize(over("SUM(value)"), schema=SCHEMA, types=TYPES, window_joins=False, _assumptions=quiet)
    assert WINDOW_JOIN_ASSUMPTION not in quiet


def test_derived_table_source_without_a_limit_is_rewritten_and_agrees_on_duckdb():
    sql = "SELECT id, SUM(value) OVER (PARTITION BY user_id) AS s FROM (SELECT id, user_id, value FROM events WHERE ts IS NOT NULL) AS d"
    rewritten = _normal(sql, KEY)
    assert "kqg" in rewritten
    for rows in KEY_DATABASES:
        a, b = _bags(sql, rewritten, rows)
        assert a == b, rows
