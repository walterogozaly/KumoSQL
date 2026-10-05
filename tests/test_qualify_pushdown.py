"""``qualify_filter`` (QUALIFY of a grouped select as a derived-table filter) and ``window_pushdown`` (a filter on
PARTITION BY columns below the windows), on ``events(user_id, ts, value)``.

Every must-prove pair is also run on DuckDB (``SET threads=1``, optimizer off) over databases with ties, NULL
partitions and an empty table; every must-not-prove pair carries a database on which the two spellings differ;
every precondition of the two rules has a case that leaves the query as written.
"""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.qualify_filter import qualify_to_filter  # noqa: E402
from kumosql.window_pushdown import push_filter_through_windows  # noqa: E402

SCHEMA = {"events": ["user_id", "ts", "value"]}
TYPES = {"events": {"user_id": "INT64", "ts": "INT64", "value": "INT64"}}
W = "ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, value)"
SUMW = "SUM(value) OVER (PARTITION BY user_id)"
INNER = f"SELECT user_id, ts, {SUMW} AS s FROM events"


def pushed_form(sql):
    return push_filter_through_windows(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def qualified_form(sql):
    return qualify_to_filter(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


# (name, left, right)
MUST_PROVE = [
    # QUALIFY of a grouped select
    ("grouped_qualify_rank",
     "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",
     "SELECT user_id, c FROM (SELECT user_id, COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id) WHERE r = 1"),
    ("grouped_qualify_with_having_and_where",
     "SELECT user_id, SUM(value) AS s FROM events WHERE ts > 1 GROUP BY user_id HAVING COUNT(*) > 1 "
     "QUALIFY DENSE_RANK() OVER (ORDER BY SUM(value) DESC) <= 2",
     "SELECT user_id, s FROM (SELECT user_id, SUM(value) AS s, DENSE_RANK() OVER (ORDER BY SUM(value) DESC) AS r FROM events "
     "WHERE ts > 1 GROUP BY user_id HAVING COUNT(*) > 1) WHERE r <= 2"),
    ("grouped_qualify_reuses_select_window",
     "SELECT user_id, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",
     "SELECT user_id, r FROM (SELECT user_id, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id) WHERE r = 1"),
    ("grouped_qualify_group_column_condition",
     "SELECT COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1 AND user_id > 1",
     "SELECT c FROM (SELECT COUNT(*) AS c, user_id, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id) WHERE r = 1 AND user_id > 1"),
    ("grouped_qualify_distinct",
     "SELECT DISTINCT COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) <= 2",
     "SELECT DISTINCT c FROM (SELECT COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id) WHERE r <= 2"),
    # QUALIFY of an ungrouped select: already one form (_isolate_windows), pinned here
    ("ungrouped_qualify",
     f"SELECT user_id, ts, value FROM events WHERE value >= 0 QUALIFY {W} = 1",
     f"SELECT user_id, ts, value FROM (SELECT user_id, ts, value, {W} AS rn FROM events WHERE value >= 0) WHERE rn = 1"),
    ("ungrouped_qualify_two_windows_and_cte",
     "SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, value) = RANK() OVER (PARTITION BY user_id ORDER BY ts, value)",
     "WITH r AS (SELECT user_id, ts, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, value) AS a, "
     "RANK() OVER (PARTITION BY user_id ORDER BY ts, value) AS b FROM events) SELECT user_id, ts FROM r WHERE a = b"),
    ("ungrouped_qualify_ties_kept_by_rank",
     "SELECT user_id, ts FROM events QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts) = 1",
     "SELECT user_id, ts FROM (SELECT user_id, ts, RANK() OVER (PARTITION BY user_id ORDER BY ts) AS r FROM events) WHERE r = 1"),
    # pruning an unread window: covered by window_canonical.prune_unread_windows; the COUNT(*) reader was the gap
    ("unread_window_under_count_star",
     f"SELECT user_id, COUNT(*) AS c FROM (SELECT user_id, {W} AS rn FROM events) GROUP BY user_id",
     "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id"),
    ("unread_window_one_of_two",
     f"SELECT user_id, a FROM (SELECT user_id, {SUMW} AS a, {W} AS b FROM events)",
     f"SELECT user_id, a FROM (SELECT user_id, {SUMW} AS a FROM events)"),
    # a filter on PARTITION BY columns, above and below the window
    ("pushdown_comparison", f"SELECT user_id, ts, s FROM ({INNER}) WHERE user_id > 1",
     f"SELECT user_id, ts, s FROM ({INNER} WHERE user_id > 1)"),
    ("pushdown_equality_row_number",
     f"SELECT user_id, ts, rn FROM (SELECT user_id, ts, {W} AS rn FROM events) WHERE user_id = 1",
     f"SELECT user_id, ts, rn FROM (SELECT user_id, ts, {W} AS rn FROM events WHERE user_id = 1)"),
    ("pushdown_alias_and_in_list",
     f"SELECT d.user_id, d.s FROM ({INNER}) AS d WHERE d.user_id IN (1, 3)",
     f"SELECT d.user_id, d.s FROM ({INNER} WHERE user_id IN (1, 3)) AS d"),
    ("pushdown_is_null_partition",
     f"SELECT user_id, s FROM ({INNER}) WHERE user_id IS NULL",
     f"SELECT user_id, s FROM ({INNER} WHERE user_id IS NULL)"),
    ("pushdown_is_not_null_partition",
     f"SELECT user_id, s FROM ({INNER}) WHERE user_id IS NOT NULL",
     f"SELECT user_id, s FROM ({INNER} WHERE user_id IS NOT NULL)"),
    ("pushdown_disjunction_on_partition_keys",
     f"SELECT user_id, s FROM ({INNER}) WHERE user_id = 1 OR user_id IS NULL",
     f"SELECT user_id, s FROM ({INNER} WHERE user_id = 1 OR user_id IS NULL)"),
    ("pushdown_next_to_inner_where",
     f"SELECT user_id, s FROM (SELECT user_id, ts, {SUMW} AS s FROM events WHERE value > 0) WHERE user_id <> 2",
     f"SELECT user_id, s FROM (SELECT user_id, ts, {SUMW} AS s FROM events WHERE value > 0 AND user_id <> 2)"),
    ("pushdown_splits_a_mixed_where",
     f"SELECT user_id, ts, s FROM ({INNER}) WHERE user_id = 1 AND ts > 1",
     f"SELECT user_id, ts, s FROM (SELECT user_id, ts, s FROM ({INNER} WHERE user_id = 1)) WHERE ts > 1"),
    ("pushdown_two_windows_share_the_key",
     f"SELECT user_id, a, b FROM (SELECT user_id, {W} AS a, {SUMW} AS b FROM events) WHERE user_id >= 2",
     f"SELECT user_id, a, b FROM (SELECT user_id, {W} AS a, {SUMW} AS b FROM events WHERE user_id >= 2)"),
    ("pushdown_through_inner_qualify",
     f"SELECT user_id, ts FROM (SELECT user_id, ts FROM events QUALIFY {W} = 1) WHERE user_id = 1",
     f"SELECT user_id, ts FROM (SELECT user_id, ts FROM events WHERE user_id = 1 QUALIFY {W} = 1)"),
    ("pushdown_column_pair_of_keys",
     "SELECT a, b, s FROM (SELECT a, b, SUM(value) OVER (PARTITION BY a, b) AS s FROM pairs) WHERE a = b",
     "SELECT a, b, s FROM (SELECT a, b, SUM(value) OVER (PARTITION BY a, b) AS s FROM pairs WHERE a = b)"),
]

# (name, left, right, witness rows): the spellings differ on the rows, so no rule may identify them
MUST_NOT_PROVE = [
    ("pushdown_non_partition_column", f"SELECT user_id, ts, s FROM ({INNER}) WHERE ts > 1",
     f"SELECT user_id, ts, s FROM ({INNER} WHERE ts > 1)", [(1, 1, 1), (1, 2, 2), (1, 2, 5)]),
    ("pushdown_partition_by_another_column",
     f"SELECT user_id, s FROM (SELECT user_id, ts, SUM(value) OVER (PARTITION BY ts) AS s FROM events) WHERE user_id = 1",
     f"SELECT user_id, s FROM (SELECT user_id, ts, SUM(value) OVER (PARTITION BY ts) AS s FROM events WHERE user_id = 1)",
     [(1, 1, 1), (2, 1, 5)]),
    ("pushdown_key_missing_from_one_window",
     "SELECT user_id, a, b FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS a, SUM(value) OVER (PARTITION BY ts) AS b FROM events) WHERE user_id = 1",
     "SELECT user_id, a, b FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS a, SUM(value) OVER (PARTITION BY ts) AS b FROM events WHERE user_id = 1)",
     [(1, 1, 1), (2, 1, 5)]),
    ("pushdown_window_without_partition",
     "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER () AS s FROM events) WHERE user_id = 1",
     "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER () AS s FROM events WHERE user_id = 1)", [(1, 1, 1), (2, 1, 5)]),
    ("pushdown_mixed_partition_and_other_column",
     f"SELECT user_id, s FROM ({INNER}) WHERE user_id = 1 OR ts = 2",
     f"SELECT user_id, s FROM ({INNER} WHERE user_id = 1 OR ts = 2)", [(1, 1, 1), (2, 2, 5), (2, 3, 7)]),
    ("pushdown_below_a_limit",
     "SELECT user_id FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events ORDER BY ts LIMIT 1) WHERE user_id = 2",
     "SELECT user_id FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events WHERE user_id = 2 ORDER BY ts LIMIT 1)",
     [(1, 1, 1), (2, 2, 5)]),
    ("pushdown_renamed_column_is_not_the_key",
     "SELECT user_id, s FROM (SELECT ts AS user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events) WHERE user_id = 1",
     "SELECT user_id, s FROM (SELECT ts AS user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events WHERE user_id = 1)",
     [(1, 5, 1), (2, 1, 5)]),
    ("grouped_qualify_rank_threshold",
     "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",
     "SELECT user_id, c FROM (SELECT user_id, COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM events GROUP BY user_id) WHERE r <= 2",
     [(1, 1, 1), (1, 2, 1), (2, 1, 1)]),
    ("grouped_qualify_is_after_having_not_before",
     "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id HAVING COUNT(*) > 1 QUALIFY RANK() OVER (ORDER BY COUNT(*)) = 1",
     "SELECT user_id, c FROM (SELECT user_id, COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*)) AS r FROM events GROUP BY user_id) WHERE r = 1 AND c > 1",
     [(1, 1, 1), (2, 1, 1), (2, 2, 1)]),
    # a window column that is read stays: QUALIFY keeps rows by it, so dropping it would keep every row
    ("read_window_is_not_pruned",
     f"SELECT user_id FROM (SELECT user_id, {W} AS rn FROM events QUALIFY rn = 1)",
     "SELECT user_id FROM events", [(1, 1, 1), (1, 2, 1)]),
]

DATABASES = [
    # ties on every window ordering key, a NULL partition, a repeated row
    [(1, 1, 1), (1, 1, 10), (1, 2, 2), (2, 3, None), (2, None, 4), (3, 3, 3), (None, 1, 1), (None, 1, 1), (None, 2, 5)],
    [(1, 3, 5), (1, 3, 5), (2, 3, 7), (2, 4, 1), (None, 3, 2), (None, None, None), (3, 3, 5), (3, 3, 5), (3, 3, 5)],
    [(1, 1, 1), (2, 1, 1), (3, 2, 2)],
    [],
]
PAIR_DATABASES = [[(1, 1, 5), (1, 1, 5), (1, 2, 7), (2, 2, 1), (None, None, 3), (None, 1, 4), (3, 3, 2), (3, 3, 2)], []]


def _duckdb_rows(left, right, rows, table="events"):
    db = duckdb.connect()
    db.execute("SET threads=1")
    columns = "a BIGINT, b BIGINT, value BIGINT" if table == "pairs" else "user_id BIGINT, ts BIGINT, value BIGINT"
    db.execute(f"CREATE TABLE {table} ({columns})")
    insert_rows(db, table, rows)
    queries = [sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (left, right)]
    return [sorted(result, key=repr) for result in run_unoptimized(db, *queries)]


def _proven(left, right):
    schema = {**SCHEMA, "pairs": ["a", "b", "value"]}
    types = {**TYPES, "pairs": {"a": "INT64", "b": "INT64", "value": "INT64"}}
    return prove_equivalent_algebraic(left, right, schema=schema, types=types, dialect="bigquery").proven


@pytest.mark.parametrize("name,left,right", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proves(name, left, right):
    assert _proven(left, right)
    assert _proven(right, left)


@pytest.mark.parametrize("name,left,right", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proved_pairs_agree_on_duckdb(name, left, right):
    table = "pairs" if "pairs" in left else "events"
    for rows in PAIR_DATABASES if table == "pairs" else DATABASES:
        a, b = _duckdb_rows(left, right, rows, table)
        assert a == b, rows


@pytest.mark.parametrize("name,left,right,rows", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_witness_tells_the_pair_apart(name, left, right, rows):
    a, b = _duckdb_rows(left, right, rows)
    assert a != b


@pytest.mark.parametrize("name,left,right,rows", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_does_not_prove(name, left, right, rows):
    assert not _proven(left, right)
    assert not _proven(right, left)


# the rules themselves: what they rewrite, and each precondition that leaves a query as written

def test_pushdown_moves_only_partition_conjuncts():
    out = pushed_form(f"SELECT user_id, ts, s FROM ({INNER}) AS d WHERE d.user_id = 1 AND d.ts > 1 AND s > 0")
    assert "FROM events WHERE user_id = 1)" in out
    assert out.endswith("WHERE d.ts > 1 AND s > 0")


def test_pushdown_removes_the_outer_where_when_everything_moves():
    out = pushed_form(f"SELECT user_id, s FROM ({INNER}) WHERE user_id IS NULL")
    assert out == f"SELECT user_id, s FROM ({INNER} WHERE user_id IS NULL)"


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT user_id, s FROM ({INNER}) WHERE ts > 1",  # not a partition column
        f"SELECT user_id, s FROM ({INNER}) WHERE s > 1",  # the window result
        f"SELECT user_id, s FROM ({INNER}) WHERE user_id = s",  # mixes in the window result
        f"SELECT user_id, s FROM ({INNER}) WHERE user_id + 0 = 1",  # arithmetic on the key
        f"SELECT user_id, s FROM ({INNER}) WHERE ABS(user_id) = 1",  # a function of the key
        f"SELECT user_id, s FROM ({INNER}) WHERE user_id = (SELECT MAX(user_id) FROM events)",  # a subquery
        f"SELECT user_id, s FROM ({INNER}) WHERE user_id IN (SELECT user_id FROM events)",
        f"SELECT user_id, s FROM ({INNER}) WHERE RAND() < 0.5",  # no column at all
        f"SELECT user_id, s FROM ({INNER}) WHERE 1 = 1",
        f"SELECT user_id, s FROM ({INNER}) WHERE user_id = 1 OR ts = 2",  # one conjunct, one non-key column
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER () AS s FROM events) WHERE user_id = 1",  # no PARTITION BY
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS a, SUM(value) OVER (PARTITION BY ts) AS s FROM events) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id + 1) AS s FROM events) WHERE user_id = 1",
        "SELECT x, s FROM (SELECT ts AS x, user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events) WHERE x = 1",  # an output that is not a key
        "SELECT user_id, s FROM (SELECT user_id + 0 AS user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events ORDER BY ts LIMIT 2) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events LIMIT 2 OFFSET 1) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT DISTINCT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT user_id, SUM(COUNT(*)) OVER (PARTITION BY user_id) AS s FROM events GROUP BY user_id) WHERE user_id = 1",
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER w AS s FROM events WINDOW w AS (PARTITION BY user_id)) WHERE user_id = 1",
        "SELECT user_id, ts FROM (SELECT user_id, ts FROM events) WHERE user_id = 1",  # no window: another rule's job
        f"SELECT d.user_id, e.ts FROM ({INNER}) AS d JOIN events AS e ON d.user_id = e.user_id WHERE d.user_id = 1",  # a join above
        f"SELECT user_id, s FROM ({INNER}) AS d(user_id, ts, s) WHERE user_id = 1",  # renamed columns
        "SELECT user_id, s FROM (SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s, (SELECT 1) AS one FROM events) WHERE user_id = 1",
        "SELECT * FROM (SELECT *, SUM(value) OVER (PARTITION BY user_id) AS s FROM events) WHERE user_id = 1",
    ],
)
def test_pushdown_declines(sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert push_filter_through_windows(tree.copy()).sql() == tree.sql()


def test_pushdown_declines_a_float_key_function():
    # -0.0 and 0.0 share one partition but a function of the key can tell them apart: only comparisons move
    sql = "SELECT x, s FROM (SELECT x, SUM(v) OVER (PARTITION BY x) AS s FROM f) WHERE 1 / x > 0"
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert push_filter_through_windows(tree.copy()).sql() == tree.sql()
    moved = push_filter_through_windows(sqlglot.parse_one(sql.replace("1 / x > 0", "x > 0"), read="bigquery")).sql()
    assert "FROM f WHERE x > 0)" in moved


def test_pushdown_moves_a_date_literal_comparison():
    sql = "SELECT dt, s FROM (SELECT dt, SUM(v) OVER (PARTITION BY dt) AS s FROM f) WHERE dt = DATE '2024-01-01'"
    assert "FROM f WHERE dt = " in pushed_form(sql)


def test_qualify_rule_builds_the_derived_filter():
    out = qualified_form("SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1")
    assert out.startswith("SELECT kqf")
    assert "RANK() OVER (ORDER BY COUNT(*) DESC) AS kqh0" in out
    assert "GROUP BY user_id" in out and "QUALIFY" not in out


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT user_id, ts, value FROM events QUALIFY {W} = 1",  # ungrouped: _isolate_windows already reads it
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1 ORDER BY user_id",
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1 LIMIT 1",
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY c > 1",  # a bare alias: alias or source column
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY COUNT(*) > 1",  # an aggregate outside a window
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY ts > 1",  # not a group column
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY user_id IN (SELECT 1)",
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY (SELECT 1)) = 1 AND user_id IN (SELECT 1)",
        "SELECT *, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",
        "SELECT user_id, COUNT(*) AS c, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",
        "SELECT user_id, COUNT(*) FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1",  # an unnamed output
        "SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id",
    ],
)
def test_qualify_rule_declines(sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert qualify_to_filter(tree.copy()).sql() == tree.sql()
