from collections import Counter

import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.limit_rules import limit_rule

SCHEMA = {"t": ["a", "b"], "u": ["a", "b"], "dept": ["deptno", "name"]}


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def _rule(sql):
    tree = sqlglot.parse_one(sql, read="mysql")
    out = limit_rule(tree)
    return out.sql(dialect="mysql") if out is not None else None


def test_top_k_over_union_all_equals_pre_limited_branches_when_the_order_covers_the_output():
    union = "SELECT a FROM t UNION ALL SELECT a FROM u"
    left = f"SELECT d.a FROM ({union}) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    right = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 9)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, right)
    # A branch cut shorter than LIMIT + OFFSET can lose rows the outer cut keeps.
    short = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 3) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, short)
    # A branch with its own OFFSET drops rows.
    skipped = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4 OFFSET 1) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, skipped)
    # The other direction pushes the cut the wrong way.
    descending = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a DESC LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, descending)


def test_union_pushdown_needs_the_order_to_cover_every_output_column():
    # Ordered by a alone, a tie on a can keep either b: the branch cut and the outer cut may break it differently.
    left = "SELECT d.a, d.b FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS d ORDER BY d.a LIMIT 1"
    right = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a LIMIT 1)) AS d ORDER BY d.a LIMIT 1"
    assert not _proven(left, right)
    # Ordered by both columns, ties are equal rows.
    left_total = left.replace("ORDER BY d.a LIMIT", "ORDER BY d.a, d.b LIMIT")
    right_total = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a, b LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a, b LIMIT 1)) AS d ORDER BY d.a, d.b LIMIT 1"
    assert _proven(left_total, right_total)


def test_nested_cuts_on_the_same_order_merge():
    left = "SELECT d.a FROM (SELECT a, b FROM t ORDER BY a LIMIT 10 OFFSET 2) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 3")
    assert not _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 1")
    # The inner cut runs out first: rows 3..11 of t, then 2 skipped, so at most 8 survive.
    short = "SELECT d.a FROM (SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 2) AS d ORDER BY d.a LIMIT 20 OFFSET 1"
    assert _proven(short, "SELECT a FROM t ORDER BY a LIMIT 8 OFFSET 3")
    assert not _proven(short, "SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 3")


def test_nested_cuts_do_not_merge_on_other_orders_or_hidden_ties():
    other = "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(other, "SELECT a FROM t ORDER BY a LIMIT 3")
    # The outer output b is not fixed by the key a, so which tied row each cut keeps matters.
    ties = "SELECT d.b FROM (SELECT a, b FROM t ORDER BY a LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(ties, "SELECT b FROM t ORDER BY a LIMIT 3")


def test_offset_without_limit_is_an_unbounded_cut():
    assert _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT x.a FROM t AS x ORDER BY x.a OFFSET 2")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a OFFSET 1")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a DESC OFFSET 2")
    # OFFSET without ORDER BY skips arbitrary rows.
    assert not _proven("SELECT a FROM t OFFSET 2", "SELECT a FROM t OFFSET 2")


def test_cut_lifts_out_of_nested_projections():
    left = "SELECT e.c FROM (SELECT d.a + 1 AS c FROM (SELECT a, b FROM t ORDER BY b OFFSET 1) AS d) AS e"
    assert _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 1")
    assert not _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 2")
    # DISTINCT before the cut is not a projection of it.
    distinct = "SELECT d.a FROM (SELECT DISTINCT a, b FROM t ORDER BY a, b LIMIT 2) AS d"
    assert _rule(distinct) is None


def test_repeated_order_key_and_unread_order_are_dropped():
    assert _rule("SELECT a FROM t ORDER BY a, b DESC, a DESC LIMIT 2") == "SELECT a FROM t ORDER BY a, b DESC LIMIT 2"
    assert _proven("SELECT d.a FROM (SELECT a FROM t ORDER BY b) AS d", "SELECT a FROM t")


def test_order_read_by_a_cut_without_order_or_an_array_is_kept():
    # LIMIT without ORDER BY may follow the order of its input, so the inner ORDER BY stays.
    sql = "SELECT d.a FROM (SELECT a FROM t ORDER BY a) AS d LIMIT 1"
    tree = sqlglot.parse_one(sql, read="mysql")
    inner = tree.find(sqlglot.exp.Subquery).this
    assert limit_rule(inner) is None
    assert not _proven(sql, "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC) AS d LIMIT 1")
    array = sqlglot.parse_one("SELECT ARRAY(SELECT a FROM t ORDER BY a) AS x", read="bigquery")
    inner = [s for s in array.find_all(sqlglot.exp.Select) if s is not array]
    assert inner and all(limit_rule(s) is None for s in inner)


def test_union_branch_cut_reads_as_a_derived_table():
    branch = "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY a LIMIT 1)"
    assert _proven(f"({branch}) ORDER BY a", branch)
    assert not _proven(branch, "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY b LIMIT 1)")


def test_a_cut_to_no_rows_over_a_union_of_empty_cuts_is_an_empty_query():
    left = "SELECT d.a FROM (SELECT a FROM t UNION ALL SELECT a FROM u) AS d ORDER BY d.a LIMIT 0"
    right = "SELECT e.a FROM ((SELECT a FROM t ORDER BY a LIMIT 0) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 0)) AS e ORDER BY e.a LIMIT 0"
    assert _proven(left, right)
    assert _proven(right, "SELECT a FROM t LIMIT 0")
    # One branch that still returns rows is not empty.
    assert not _proven(right, "SELECT e.a FROM ((SELECT a FROM t ORDER BY a LIMIT 0) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 1)) AS e")
    # A global aggregate over an emptied source still returns its one row.
    count = "SELECT COUNT(*) AS n FROM (SELECT a FROM t ORDER BY a LIMIT 1) AS d WHERE FALSE"
    assert not _proven(count, "SELECT a FROM t LIMIT 0")
    assert _proven(count, "SELECT 0 AS n")


def _differ(left, right, rows):
    """The two BigQuery queries return different bags on DuckDB over ``t(a, b)`` holding ``rows``."""

    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE t (a BIGINT, b BIGINT)")
    for row in rows:
        db.execute("INSERT INTO t VALUES (?, ?)", row)
    queries = [sqlglot.transpile(sql, read="bigquery", write="duckdb")[0] for sql in (left, right)]
    first, second = run_unoptimized(db, *queries)
    return Counter(first) != Counter(second)


def _proven_bigquery(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery").proven


# A select that aggregates without GROUP BY gives one row, even over an empty table; without its
# aggregate it gives a row per input row. (left, right, rows of t on which they differ)
LOST_GLOBAL_AGGREGATE = [
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t",
        [(1, 1), (2, 1), (3, 1)],
        id="s007-001-cut-over-a-global-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t",
        [],
        id="s007-001-cut-over-a-global-aggregate-of-nothing",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t ORDER BY TRUE LIMIT 2",
        [(1, 1), (2, 1), (3, 1)],
        id="s007-001-lifted-cut",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT 7 AS c, COUNT(*) AS n FROM t) d",
        "SELECT 7 AS c FROM t",
        [(1, 1), (2, 1)],
        id="s007-001-pruned-derived-aggregate",
    ),
    pytest.param(
        "SELECT 1 AS one FROM (SELECT MAX(a) AS n FROM t) d",
        "SELECT 1 AS one FROM t",
        [],
        id="s007-001-folded-derived-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 'a' AS c FROM t) d",
        "SELECT 'a' AS c FROM t",
        [(1, 1), (2, 1)],
        id="s007-001-unwrapped-derived-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 'a' AS c FROM t ORDER BY 1 LIMIT 2) d",
        "SELECT 'a' AS c FROM t",
        [],
        id="s007-001-unwrapped-cut-aggregate",
    ),
]


@pytest.mark.parametrize("left, right, rows", LOST_GLOBAL_AGGREGATE)
def test_a_global_aggregate_keeps_its_one_row(left, right, rows):
    assert _differ(left, right, rows)
    assert not _proven_bigquery(left, right)


def test_s007_001_cut_does_not_lift_off_the_last_aggregate():
    assert _rule("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) AS d") is None
    # The aggregate read through the cut still lifts, and a grouped select keeps its groups without one.
    assert _rule("SELECT d.n FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) AS d") == "SELECT COUNT(*) AS n FROM t ORDER BY TRUE LIMIT 2"
    assert _rule("SELECT d.a FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a ORDER BY a LIMIT 2) AS d") == "SELECT a FROM t GROUP BY a ORDER BY a LIMIT 2"


@pytest.mark.parametrize(
    "left, right",
    [
        pytest.param(
            "SELECT d.n FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d", "SELECT COUNT(*) AS n FROM t", id="s007-001-near-miss-aggregate-read"
        ),
        pytest.param("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d", "SELECT 7 AS c", id="s007-001-near-miss-one-row"),
        pytest.param(
            "SELECT d.a FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a ORDER BY a LIMIT 2) d",
            "SELECT a FROM t GROUP BY a ORDER BY a LIMIT 2",
            id="s007-001-near-miss-grouped",
        ),
    ],
)
def test_a_global_aggregate_near_misses_stay_proven(left, right):
    assert not _differ(left, right, [(1, 1), (2, 1), (3, 2)])
    assert not _differ(left, right, [])
    assert _proven_bigquery(left, right)


# GROUP BY and HAVING may name an output alias, and a name an alias shares with a column may be read as
# the alias: a rewrite that gives the grouped select a new list must not change what those names read.
# (left, right, rows of t on which they differ)
REBOUND_GROUPING_NAMES = [
    pytest.param(
        "SELECT d.b + 0 AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b) d",
        "SELECT b + 0 AS bb FROM t GROUP BY b",
        [(1, 1), (2, 1)],
        id="s007-001-folded-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b, d.b + 0 AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b ORDER BY b LIMIT 3) d",
        "SELECT b, b + 0 AS bb FROM t GROUP BY b ORDER BY b LIMIT 3",
        [(1, 1), (2, 1)],
        id="s007-001-lifted-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b ORDER BY b LIMIT 5) d",
        "SELECT b AS bb FROM t GROUP BY b ORDER BY b LIMIT 5",
        [(1, 1), (2, 1)],
        id="s007-001-root-lifted-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b AS a FROM (SELECT b, COUNT(*) AS n FROM t GROUP BY b HAVING MAX(a) > 1) d",
        "SELECT b AS a FROM t GROUP BY b HAVING MAX(b) > 1",
        [(1, 1), (2, 1), (1, 2)],
        id="s007-001-unwrapped-alias-captures-having",
    ),
    pytest.param(
        "SELECT d.b AS a FROM (SELECT b, MAX(a) AS m FROM t GROUP BY b) d WHERE d.m > 1",
        "SELECT b AS a FROM t GROUP BY b HAVING MAX(b) > 1",
        [(1, 1), (2, 1), (1, 2)],
        id="s007-001-folded-alias-captures-filter",
    ),
]


@pytest.mark.parametrize("left, right, rows", REBOUND_GROUPING_NAMES)
def test_a_new_select_list_keeps_what_group_by_and_having_read(left, right, rows):
    assert _differ(left, right, rows)
    assert not _proven_bigquery(left, right)


@pytest.mark.parametrize(
    "left, right",
    [
        pytest.param("SELECT d.bb FROM (SELECT b AS bb, COUNT(*) AS n FROM t GROUP BY bb) d", "SELECT b AS bb FROM t GROUP BY b", id="s007-001-near-miss-kept-alias"),
        pytest.param(
            "SELECT d.s FROM (SELECT b, SUM(a) AS s FROM t GROUP BY b) d WHERE d.s > 1",
            "SELECT SUM(a) AS s FROM t GROUP BY b HAVING SUM(a) > 1",
            id="s007-001-near-miss-filter-into-having",
        ),
    ],
)
def test_grouping_name_near_misses_stay_proven(left, right):
    assert not _differ(left, right, [(1, 1), (2, 1), (3, 2)])
    assert _proven_bigquery(left, right)


# ``ORDER BY CAST(x AS DOUBLE)`` keeps the order of a 32-bit integer, not of a 64-bit one: two integers
# above 2^53 can cast to one double and tie. Whether ``INT`` is 32 bits depends on the dialect.
UNCAST_LEFT = "SELECT x FROM t ORDER BY CAST(x AS DOUBLE), x DESC LIMIT 1"
UNCAST_RIGHT = "SELECT x FROM t ORDER BY x, x DESC LIMIT 1"


def _uncast(dialect, declared):
    tree = sqlglot.parse_one("SELECT x FROM t ORDER BY CAST(x AS DOUBLE) LIMIT 1", read=dialect)
    out = limit_rule(tree, {"t": {"x": declared}}, dialect)
    return out.sql(dialect=dialect) if out is not None else None


def _uncast_proven(dialect):
    return prove_equivalent_algebraic(UNCAST_LEFT, UNCAST_RIGHT, schema={"t": ["x"]}, types={"t": {"x": "int"}}, dialect=dialect).proven


@pytest.mark.parametrize("dialect", [pytest.param("snowflake", id="s007-001-uncast-snowflake-int"), pytest.param("sqlite", id="s007-001-uncast-sqlite-int")])
def test_order_by_uncast_needs_a_documented_32_bit_int(dialect):
    # Snowflake's INT is NUMBER(38, 0) and SQLite's is 64 bits: values a double cannot tell apart.
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE t (x BIGINT)")
    db.execute("INSERT INTO t VALUES (9007199254740992), (9007199254740993)")
    first, second = run_unoptimized(db, *(sqlglot.transpile(sql, read=dialect, write="duckdb")[0] for sql in (UNCAST_LEFT, UNCAST_RIGHT)))
    assert Counter(first) != Counter(second)
    assert _uncast(dialect, "int") is None


def test_s007_001_uncast_snowflake_int_is_not_proven():
    assert not _uncast_proven("snowflake")


def test_s007_001_uncast_near_miss_postgres_int_stays_proven():
    assert _uncast("postgres", "int") == "SELECT x FROM t ORDER BY x LIMIT 1"
    assert _uncast("postgres", "bigint") is None
    assert _uncast_proven("postgres")


# Lifting a cut out of a projection spells the derived table's output names as the values behind them.
# A nested query reads its own tables' names first, and a position written as an ordinal is a position.
def _bags_differ_on_t_and_u(left, right, t_rows, u_rows):
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for name, rows in (("t", t_rows), ("u", u_rows)):
        db.execute(f"CREATE TABLE {name} (a BIGINT, b BIGINT)")
        for row in rows:
            db.execute(f"INSERT INTO {name} VALUES (?, ?)", row)
    queries = [sqlglot.transpile(sql, read="bigquery", write="duckdb")[0] for sql in (left, right)]
    first, second = run_unoptimized(db, *queries)
    return Counter(first) != Counter(second)


NESTED_NAME_CAPTURE = [
    pytest.param(
        "SELECT d.b + (SELECT MAX(b) FROM u) AS m FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d",
        "SELECT a + (SELECT MAX(a) FROM u) AS m FROM t ORDER BY a LIMIT 1",
        id="lifted-cut-subquery-reads-its-own-column",
    ),
    pytest.param(
        "SELECT d.b + (SELECT MAX(b) FROM u WHERE b > 1) AS m FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d",
        "SELECT a + (SELECT MAX(a) FROM u WHERE a > 1) AS m FROM t ORDER BY a LIMIT 1",
        id="lifted-cut-subquery-filter-reads-its-own-column",
    ),
]


@pytest.mark.parametrize("left, right", NESTED_NAME_CAPTURE)
def test_a_lifted_cut_does_not_rename_a_names_a_nested_query_reads_from_its_own_table(left, right):
    assert _bags_differ_on_t_and_u(left, right, [(1, 0)], [(5, 7), (9, 2)])
    assert not _proven_bigquery(left, right)
    assert limit_rule(sqlglot.parse_one(left, read="bigquery")) is None


def test_a_lifted_cut_near_miss_with_a_subquery_in_a_name_it_does_not_rename_stays_proven():
    # The subquery reads u.b whichever name the derived table gives its column.
    left = "SELECT d.b + (SELECT MAX(u.b) FROM u) AS m FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d"
    right = "SELECT a + (SELECT MAX(u.b) FROM u) AS m FROM t ORDER BY a LIMIT 1"
    assert not _bags_differ_on_t_and_u(left, right, [(1, 0)], [(5, 7), (9, 2)])


def test_a_lifted_cut_does_not_draw_a_random_value_twice():
    left = "SELECT d.r, d.r AS r2 FROM (SELECT RAND() AS r FROM t ORDER BY a LIMIT 1) AS d"
    assert limit_rule(sqlglot.parse_one(left, read="bigquery")) is None
    assert not _proven_bigquery(left, "SELECT RAND() AS r, RAND() AS r2 FROM t ORDER BY a LIMIT 1")


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT d.c FROM (SELECT a, COUNT(*) AS c FROM t GROUP BY 1 ORDER BY 2, 1 LIMIT 3) AS d", id="group-by-ordinal-moves"),
        pytest.param("SELECT d.a FROM (SELECT COUNT(*) AS c, a FROM t GROUP BY 2 ORDER BY 1 LIMIT 3) AS d", id="group-by-second-ordinal-moves"),
    ],
)
def test_a_lifted_cut_does_not_move_the_item_a_group_by_ordinal_names(sql):
    assert limit_rule(sqlglot.parse_one(sql, read="bigquery")) is None


def test_a_lifted_cut_keeps_a_group_by_ordinal_whose_item_stays_in_place():
    sql = "SELECT d.a, d.c FROM (SELECT a, COUNT(*) AS c FROM t GROUP BY 1 ORDER BY 2, 1 LIMIT 3) AS d"
    out = limit_rule(sqlglot.parse_one(sql, read="bigquery"))
    assert out is not None
    assert not _differ(sql, out.sql(dialect="bigquery"), [(1, 1), (1, 2), (2, 1), (None, 3)])


def test_a_lifted_cut_does_not_let_a_duplicated_output_name_capture_the_order_key():
    # Two output items are named ``a``; an ORDER BY ``a`` reads the first of them, ``b + b``, not the column.
    left = "SELECT d.a AS z, d.a + d.a AS a, d.b AS a FROM (SELECT a AS b, b AS a FROM t ORDER BY 1, 2 LIMIT 2) AS d"
    right = "SELECT b AS z, b + b AS a, a FROM t ORDER BY a, b LIMIT 2"
    assert _differ(left, right, [(1, 3), (2, 1), (3, 2)])
    assert not _proven_bigquery(left, right)
    assert limit_rule(sqlglot.parse_one(left, read="bigquery")) is None
    # Under distinct names the same lift is sound.
    left = "SELECT d.a AS z, d.a + d.a AS w, d.b AS v FROM (SELECT a AS b, b AS a FROM t ORDER BY 1, 2 LIMIT 2) AS d"
    right = "SELECT b AS z, b + b AS w, a AS v FROM t ORDER BY a, b LIMIT 2"
    assert not _differ(left, right, [(1, 3), (2, 1), (3, 2)])
    assert limit_rule(sqlglot.parse_one(left, read="bigquery")).sql(dialect="bigquery") == right
