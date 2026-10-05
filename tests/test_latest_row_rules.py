"""The "latest row per key" rewrite (``kumosql.latest_row_rules``) on ``events(id, user_id, ts, value)``.

Every case that must rewrite is run on DuckDB (``bigquery_on_duckdb``, one thread, optimizer off) before and
after, on random databases that respect the declared keys and NOT NULL columns and carry ties and NULLs. Every
case that must not rewrite names the failed precondition and carries a database on which the join the rule would
have written returns a different bag than the query as written (checked on DuckDB in both storage orders), so
the refusal is not just caution.
"""

import functools
import random

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql import bigquery_on_duckdb as bd  # noqa: E402
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.latest_row_rules import latest_row_to_grouped_join  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"events": ["id", "user_id", "ts", "value"]}
TYPES = {"events": {"id": "INT64", "user_id": "INT64", "ts": "INT64", "value": "INT64"}}
FLOAT_TYPES = {"events": {"id": "INT64", "user_id": "INT64", "ts": "FLOAT64", "value": "INT64"}}

# scenario -> (keys, NOT NULL columns): which columns a random database keeps unique and non-NULL
ID_KEY = ([("id",)], {"id"})
ID_KEY_TS_VALUE = ([("id",)], {"id", "ts", "value"})
ID_KEY_VALUE = ([("id",)], {"id", "value"})
ID_KEY_TS = ([("id",)], {"id", "ts"})
ID_KEY_USER_TS = ([("id",)], {"id", "user_id", "ts"})
USER_TS = ([("user_id", "ts")], {"id", "user_id", "ts"})
USER_TS_NN = ([("user_id", "ts")], {"id", "user_id", "ts", "value"})
USER_TS_ID = ([("user_id", "ts"), ("id",)], {"id", "user_id", "ts"})
TS_KEY_VALUE = ([("ts",)], {"id", "ts", "value"})
TS_KEY = ([("ts",)], {"id", "ts"})
NO_FACTS = ([], set())

P_DESC = "PARTITION BY user_id ORDER BY ts DESC"
P_ASC = "PARTITION BY user_id ORDER BY ts"


@functools.lru_cache(maxsize=None)
def duck(sql):
    """BigQuery SQL as DuckDB runs it; ``LIMIT 1`` inside ``ARRAY_AGG`` (DuckDB has none) is the first element."""

    tree = sqlglot.parse_one(sql.replace(" LIMIT 1)[", ")["), read="bigquery")
    return bd.faithful(tree).sql(dialect="duckdb")


def random_rows(rng, scenario):
    keys, not_null = scenario
    rows, seen = [], set()
    if rng.random() < 0.08:
        return rows  # an empty table: a global aggregate still returns a row, a window query none
    for ident in range(1, rng.randint(1, 8) + 1):
        for _ in range(20):
            row = [ident, rng.choice([1, 2, 3, None]), rng.choice([1, 2, 3, None]), rng.choice([5, 6, None])]
            for position, name in enumerate(SCHEMA["events"]):
                if name in not_null and row[position] is None:
                    row[position] = rng.randint(1, 3)
            marks = [tuple(row[SCHEMA["events"].index(c)] for c in key) for key in keys]
            if not any((i, m) in seen for i, m in enumerate(marks)):
                seen.update((i, m) for i, m in enumerate(marks))
                rows.append(tuple(row))
                break
    return rows


# the total order lets a select that reads only the partition and order expressions read the grouping itself
COLLAPSES = [
    ("collapse_row_number_desc", f"SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", USER_TS),
    ("collapse_row_number_asc_without_a_key", f"SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER ({P_ASC}) = 1", ID_KEY_TS),
    ("collapse_row_number_desc_nullable_without_a_key", f"SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", NO_FACTS),
    ("collapse_rank_asc", f"SELECT user_id, ts FROM events QUALIFY RANK() OVER ({P_ASC}) = 1", USER_TS),
    ("collapse_dense_rank_with_names", f"SELECT user_id AS u, ts + 1 AS next_ts FROM events QUALIFY DENSE_RANK() OVER ({P_ASC}) <= 1", USER_TS),
    ("collapse_nullable_order_through_the_partition_key", "SELECT id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1", ID_KEY),
    ("collapse_with_where", f"SELECT user_id, ts FROM events WHERE value > 5 QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", USER_TS),
    (
        "collapse_derived_filter",
        f"SELECT d.user_id, d.ts FROM (SELECT user_id, ts, ROW_NUMBER() OVER ({P_DESC}) AS rn FROM events) AS d WHERE d.rn = 1",
        USER_TS,
    ),
    (
        "collapse_derived_filter_with_the_number_read",
        f"SELECT d.user_id, d.rn FROM (SELECT user_id, RANK() OVER ({P_ASC}) AS rn, ts FROM events) AS d WHERE d.rn = 1",
        USER_TS,
    ),
]

# a membership test against the grouped extreme is the join to it
MEMBERSHIPS = [
    ("member_of_grouped_max", "SELECT id, value FROM events WHERE (user_id, ts) IN (SELECT user_id, MAX(ts) FROM events GROUP BY user_id)", NO_FACTS),
    ("member_of_grouped_min_among_conditions", "SELECT id, value FROM events WHERE value > 5 AND (user_id, ts) IN (SELECT user_id, MIN(ts) FROM events GROUP BY user_id) AND id > 1", NO_FACTS),
    ("member_in_the_other_order", "SELECT id FROM events WHERE (ts, user_id) IN (SELECT MAX(ts), user_id FROM events GROUP BY user_id)", ID_KEY),
    ("member_of_two_extremes", "SELECT id FROM events WHERE (user_id, ts, value) IN (SELECT user_id, MIN(ts), MAX(value) FROM events GROUP BY user_id)", NO_FACTS),
    ("member_with_inner_where", "SELECT id FROM events WHERE (user_id, ts) IN (SELECT user_id, MAX(ts) FROM events WHERE value > 3 GROUP BY user_id)", NO_FACTS),
    ("member_with_two_group_keys", "SELECT id FROM events WHERE (user_id, value, ts) IN (SELECT user_id, value, MAX(ts) FROM events GROUP BY user_id, value)", NO_FACTS),
]


def run(sql, rows, reverse=False):
    db = duckdb.connect(config={"threads": 1})
    bd.configure(db)
    db.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", list(reversed(rows)) if reverse else rows)
    return sorted(run_unoptimized(db, duck(sql))[0], key=repr)


def rewritten(sql, scenario, types=TYPES):
    keys, not_null = scenario
    tree = sqlglot.parse_one(sql, read="bigquery")
    out = latest_row_to_grouped_join(
        tree.copy(), {"events": keys}, {"events": frozenset(not_null)}, SCHEMA, types, "bigquery"
    )
    text = out.sql(dialect="bigquery")
    return text if text != tree.sql(dialect="bigquery") else None


# (name, sql, scenario)
MUST_REWRITE = [
    ("row_number_desc_qualify", f"SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", USER_TS),
    ("row_number_asc_qualify", f"SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER ({P_ASC}) = 1", USER_TS),
    ("row_number_less_equal_one", f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_ASC}) <= 1", USER_TS),
    ("row_number_less_than_two", f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) < 2", USER_TS),
    ("row_number_one_on_the_left", f"SELECT user_id, value FROM events QUALIFY 1 = ROW_NUMBER() OVER ({P_DESC})", USER_TS),
    ("row_number_with_where", f"SELECT user_id, value FROM events WHERE value IS NOT NULL QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", USER_TS),
    ("row_number_extra_qualify_condition", f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1 AND value > 5", USER_TS),
    ("row_number_read_in_select", f"SELECT user_id, ROW_NUMBER() OVER ({P_DESC}) AS rn, value FROM events QUALIFY rn = 1", USER_TS),
    ("row_number_order_by_limit_on_top", f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1 ORDER BY user_id LIMIT 2", USER_TS),
    ("row_number_global", "SELECT id, ts FROM events QUALIFY ROW_NUMBER() OVER (ORDER BY ts DESC) = 1", TS_KEY),
    ("row_number_global_reading_only_the_order", "SELECT ts FROM events QUALIFY ROW_NUMBER() OVER (ORDER BY ts DESC) = 1", TS_KEY),
    ("row_number_partition_expression", "SELECT id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY ts + 1 ORDER BY ts DESC) = 1", TS_KEY),
    ("row_number_aliased_table", f"SELECT e.user_id, e.value FROM events AS e QUALIFY ROW_NUMBER() OVER (PARTITION BY e.user_id ORDER BY e.ts DESC) = 1", USER_TS),
    ("rank_needs_no_key", f"SELECT user_id, ts, value FROM events QUALIFY RANK() OVER ({P_ASC}) = 1", ID_KEY_TS),
    ("rank_desc_nullable_order", f"SELECT user_id, ts, value FROM events QUALIFY RANK() OVER ({P_DESC}) = 1", ID_KEY),
    ("rank_nulls_last_nullable_order", "SELECT user_id, ts, value FROM events QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts NULLS LAST) = 1", ID_KEY),
    ("rank_desc_where_rejects_null", "SELECT user_id, ts, value FROM events WHERE ts IS NOT NULL QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts) = 1", ID_KEY),
    ("dense_rank_no_key", f"SELECT user_id, ts, value FROM events QUALIFY DENSE_RANK() OVER ({P_ASC}) <= 1", ID_KEY_TS),
    ("dense_rank_global_no_key", "SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER (ORDER BY ts DESC) = 1", NO_FACTS),
    (
        "derived_filter_row_number",
        f"SELECT user_id, value FROM (SELECT user_id, value, ROW_NUMBER() OVER ({P_DESC}) AS rn FROM events) AS d WHERE rn = 1",
        USER_TS,
    ),
    (
        "derived_filter_rank_with_inner_where_and_outer_condition",
        f"SELECT d.user_id, d.value, d.rn FROM (SELECT user_id, value, RANK() OVER ({P_ASC}) AS rn FROM events WHERE value > 5) AS d WHERE d.rn = 1 AND d.value > 5",
        ID_KEY_TS,
    ),
    (
        "derived_filter_dense_rank_nullable_desc",
        f"SELECT COUNT(*) AS n FROM (SELECT user_id, DENSE_RANK() OVER ({P_DESC}) AS r FROM events) AS d WHERE r <= 1",
        NO_FACTS,
    ),
    ("max_by", "SELECT user_id, MAX_BY(value, ts) AS v FROM events GROUP BY user_id", USER_TS_NN),
    ("min_by", "SELECT user_id, MIN_BY(value, ts) AS v FROM events GROUP BY user_id", USER_TS_NN),
    ("max_by_and_max", "SELECT user_id, MAX(ts) AS t, MAX_BY(value, ts) AS v FROM events GROUP BY user_id", USER_TS_NN),
    ("max_by_two_values", "SELECT user_id, MAX_BY(value, ts) AS v, MAX_BY(id, ts) AS i FROM events GROUP BY user_id", USER_TS_NN),
    ("array_agg_desc", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS),
    ("array_agg_asc", "SELECT user_id, ARRAY_AGG(value ORDER BY ts LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS),
    ("array_agg_ordinal", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[ORDINAL(1)] AS v FROM events GROUP BY user_id", USER_TS),
    ("array_agg_safe_offset", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS),
    ("array_agg_with_where", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events WHERE value IS NOT NULL GROUP BY user_id", USER_TS),
    ("array_agg_nullable_value", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS_ID),
    ("array_agg_nulls_last_nullable_order_desc", "SELECT id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY id", ID_KEY),
]


@pytest.mark.parametrize("name,sql,scenario", MUST_REWRITE, ids=[c[0] for c in MUST_REWRITE])
def test_rewrites_to_the_grouped_join(name, sql, scenario):
    text = rewritten(sql, scenario)
    assert text is not None and " JOIN (SELECT " in text
    assert "ROW_NUMBER" not in text and "RANK" not in text and "ARRAY_AGG" not in text and "MAX_BY" not in text and "MIN_BY" not in text


@pytest.mark.parametrize("name,sql,scenario", COLLAPSES, ids=[c[0] for c in COLLAPSES])
def test_a_select_that_reads_only_the_key_and_the_extreme_is_the_grouping_itself(name, sql, scenario):
    text = rewritten(sql, scenario)
    assert text is not None and " GROUP BY " in text and " JOIN " not in text
    assert "ROW_NUMBER" not in text and "RANK" not in text


@pytest.mark.parametrize("name,sql,scenario", MEMBERSHIPS, ids=[c[0] for c in MEMBERSHIPS])
def test_membership_in_the_grouped_extreme_is_the_join(name, sql, scenario):
    text = rewritten(sql, scenario)
    assert text is not None and " JOIN (SELECT " in text and " IN (" not in text


def exists_reading(sql):
    """The membership ``(l1, .., ln) IN (SELECT ..)`` as an ``EXISTS`` over the same subquery compared with ``=``.

    DuckDB compares the fields of a tuple ``IN`` as equal when both are NULL, BigQuery does not (the NULL is
    unknown), so the oracle reads the membership the way BigQuery does: a row matches only when every field is equal."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        if query is None:
            continue
        lefts = list(node.this.expressions) if isinstance(node.this, exp.Tuple) else [node.this]
        sub = query.this.copy()
        sub.set("expressions", [exp.alias_(i.this if isinstance(i, exp.Alias) else i, f"c{n}") for n, i in enumerate(sub.expressions)])
        condition = exp.and_(*[exp.EQ(this=left.copy(), expression=exp.column(f"c{n}", table="kqs")) for n, left in enumerate(lefts)])
        probe = exp.select("1").from_(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier("kqs")))).where(condition)
        node.replace(exp.Exists(this=probe))
    return tree.sql(dialect="bigquery")


@pytest.mark.parametrize("name,sql,scenario", MUST_REWRITE + COLLAPSES + MEMBERSHIPS, ids=[c[0] for c in MUST_REWRITE + COLLAPSES + MEMBERSHIPS])
def test_rewrite_agrees_on_duckdb(name, sql, scenario):
    text = rewritten(sql, scenario)
    assert text is not None
    oracle = exists_reading(sql)
    rng = random.Random(f"latest-row-{name}")
    for trial in range(30):
        rows = random_rows(rng, scenario)
        # a result that depends on the storage order is not a verdict; every case here has a total order
        # or keeps every tied row, so the original must not depend on it
        before = run(oracle, rows)
        assert before == run(oracle, rows, reverse=True), rows
        assert before == run(text, rows), rows


def _naive(direction):
    return (
        "SELECT e.user_id, e.value FROM events AS e JOIN (SELECT user_id, "
        f"{direction}(ts) AS m FROM events GROUP BY user_id) AS g "
        "ON e.user_id IS NOT DISTINCT FROM g.user_id AND e.ts IS NOT DISTINCT FROM g.m"
    )


NAIVE_MAX, NAIVE_MIN = _naive("MAX"), _naive("MIN")


# (name, failed precondition, sql, scenario, naive join, rows on which they differ)
MUST_NOT_REWRITE = [
    (
        "row_number_order_not_total",
        "ROW_NUMBER picks one of the tied rows; (user_id, ts) is not a key",
        f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1",
        ID_KEY_TS,
        NAIVE_MAX,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "row_number_no_facts_at_all",
        "no declared key",
        f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1",
        NO_FACTS,
        NAIVE_MAX,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "row_number_key_only_in_the_select",
        "id is a key but not in the window's PARTITION BY or ORDER BY",
        f"SELECT id, user_id FROM events QUALIFY ROW_NUMBER() OVER ({P_ASC}) = 1",
        ID_KEY_TS,
        NAIVE_MIN,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "derived_row_number_order_not_total",
        "ROW_NUMBER over a derived filter, order not total",
        f"SELECT user_id, value FROM (SELECT user_id, value, ROW_NUMBER() OVER ({P_DESC}) AS rn FROM events) AS d WHERE rn = 1",
        ID_KEY_TS,
        NAIVE_MAX,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "max_by_order_not_total",
        "MAX_BY picks one of the tied rows",
        "SELECT user_id, MAX_BY(value, ts) AS value FROM events GROUP BY user_id",
        ID_KEY_TS_VALUE,
        NAIVE_MAX,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "array_agg_order_not_total",
        "ARRAY_AGG .. LIMIT 1 picks one of the tied rows",
        "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS value FROM events GROUP BY user_id",
        ID_KEY_TS,
        NAIVE_MAX,
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "rank_ascending_nullable_order",
        "ascending puts NULL first, MIN skips it",
        f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({P_ASC}) = 1",
        ID_KEY,
        NAIVE_MIN,
        [(1, 1, None, 5), (2, 1, 2, 6)],
    ),
    (
        "dense_rank_descending_nulls_first",
        "NULLS FIRST on a nullable key",
        "SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER (PARTITION BY user_id ORDER BY ts DESC NULLS FIRST) = 1",
        ID_KEY,
        NAIVE_MAX,
        [(1, 1, None, 5), (2, 1, 2, 6)],
    ),
    (
        "row_number_ascending_nullable_order_total",
        "total, but ascending puts the NULL row first and MIN skips it",
        "SELECT id, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts) = 1",
        ID_KEY,
        "SELECT e.id, e.value FROM events AS e JOIN (SELECT id, MIN(ts) AS m FROM events GROUP BY id) AS g ON e.id = g.id AND e.ts = g.m",
        [(1, 1, 1, 5), (2, 1, None, 6)],
    ),
    (
        "max_by_nullable_order",
        "MAX_BY skips NULL ordering values and returns NULL for a group of only NULLs",
        "SELECT id, MAX_BY(value, ts) AS value FROM events GROUP BY id",
        ID_KEY_VALUE,
        "SELECT e.id, e.value FROM events AS e JOIN (SELECT id, MAX(ts) AS m FROM events GROUP BY id) AS g ON e.id = g.id AND e.ts IS NOT DISTINCT FROM g.m",
        [(1, 1, None, 5)],
    ),
]


@pytest.mark.parametrize("name,why,sql,scenario,naive,rows", MUST_NOT_REWRITE, ids=[c[0] for c in MUST_NOT_REWRITE])
def test_declines_when_a_precondition_fails(name, why, sql, scenario, naive, rows):
    assert rewritten(sql, scenario) is None, why


@pytest.mark.parametrize("name,why,sql,scenario,naive,rows", MUST_NOT_REWRITE, ids=[c[0] for c in MUST_NOT_REWRITE])
def test_the_refused_join_would_have_been_wrong(name, why, sql, scenario, naive, rows):
    original = sql
    for reverse in (False, True):
        assert run(original, rows, reverse=reverse) != run(naive, rows), why


# shapes the rule leaves alone for reasons other than totality or NULLs
DECLINED_SHAPES = [
    ("rank_equals_two", f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({P_DESC}) = 2", USER_TS, TYPES),
    ("rank_at_most_two", f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({P_DESC}) <= 2", USER_TS, TYPES),
    ("two_order_keys", "SELECT user_id, value FROM events QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY ts DESC, id) = 1", USER_TS_ID, TYPES),
    ("float_order_key", f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1", USER_TS, FLOAT_TYPES),
    ("float_cast_order_key", "SELECT user_id, value FROM events QUALIFY RANK() OVER (PARTITION BY user_id ORDER BY CAST(ts AS FLOAT64) DESC) = 1", USER_TS, TYPES),
    ("another_window_in_the_select", f"SELECT user_id, SUM(value) OVER (PARTITION BY user_id) AS s FROM events QUALIFY RANK() OVER ({P_DESC}) = 1", USER_TS, TYPES),
    ("window_with_a_frame", f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({P_DESC} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) = 1", USER_TS, TYPES),
    ("star_in_the_select", f"SELECT * FROM events QUALIFY RANK() OVER ({P_DESC}) = 1", USER_TS, TYPES),
    ("where_with_a_subquery", f"SELECT user_id, value FROM events WHERE ts > (SELECT MIN(ts) FROM events) QUALIFY RANK() OVER ({P_DESC}) = 1", USER_TS, TYPES),
    ("where_with_a_random_function", f"SELECT user_id, value FROM events WHERE RAND() > 0.5 QUALIFY RANK() OVER ({P_DESC}) = 1", USER_TS, TYPES),
    ("a_join_in_the_from", f"SELECT a.user_id FROM events a JOIN events b ON a.id = b.id QUALIFY RANK() OVER (PARTITION BY a.user_id ORDER BY a.ts DESC) = 1", USER_TS, TYPES),
    ("a_derived_table_in_the_from", f"SELECT user_id FROM (SELECT * FROM events) AS d QUALIFY RANK() OVER ({P_DESC}) = 1", USER_TS, TYPES),
    ("a_grouped_select", f"SELECT user_id, COUNT(*) AS c FROM events GROUP BY user_id QUALIFY RANK() OVER (ORDER BY COUNT(*) DESC) = 1", USER_TS, TYPES),
    ("not_in_the_grouped_extreme", "SELECT id FROM events WHERE (user_id, ts) NOT IN (SELECT user_id, MAX(ts) FROM events GROUP BY user_id)", NO_FACTS, TYPES),
    ("member_of_a_disjunction", "SELECT id FROM events WHERE id = 3 OR (user_id, ts) IN (SELECT user_id, MAX(ts) FROM events GROUP BY user_id)", NO_FACTS, TYPES),
    ("member_group_key_not_selected", "SELECT id FROM events WHERE ts IN (SELECT MAX(ts) FROM events GROUP BY user_id)", NO_FACTS, TYPES),
    ("member_of_another_aggregate", "SELECT id FROM events WHERE (user_id, value) IN (SELECT user_id, SUM(value) FROM events GROUP BY user_id)", NO_FACTS, TYPES),
    ("member_of_a_correlated_subquery", "SELECT id FROM events WHERE (user_id, ts) IN (SELECT u.user_id, MAX(u.ts) FROM events AS u WHERE u.id <> events.id GROUP BY u.user_id)", NO_FACTS, TYPES),
    ("member_with_having", "SELECT id FROM events WHERE (user_id, ts) IN (SELECT user_id, MAX(ts) FROM events GROUP BY user_id HAVING COUNT(*) > 1)", NO_FACTS, TYPES),
    ("member_of_a_global_aggregate", "SELECT id FROM events WHERE ts IN (SELECT MAX(ts) FROM events)", NO_FACTS, TYPES),
    ("member_with_a_join_in_the_outer_select", "SELECT a.id FROM events AS a JOIN events AS b ON a.id = b.id WHERE (a.user_id, a.ts) IN (SELECT user_id, MAX(ts) FROM events GROUP BY user_id)", NO_FACTS, TYPES),
    ("array_agg_ignore_nulls", "SELECT user_id, ARRAY_AGG(value IGNORE NULLS ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS, TYPES),
    ("array_agg_distinct", "SELECT user_id, ARRAY_AGG(DISTINCT value ORDER BY value DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", USER_TS, TYPES),
    ("array_agg_second_element", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 2)[OFFSET(1)] AS v FROM events GROUP BY user_id", USER_TS, TYPES),
    ("array_agg_without_limit_offset_zero", "SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC)[OFFSET(1)] AS v FROM events GROUP BY user_id", USER_TS, TYPES),
    ("pick_without_group_by", "SELECT MAX_BY(value, ts) AS v FROM events", TS_KEY_VALUE, TYPES),
    ("pick_with_having", "SELECT user_id, MAX_BY(value, ts) AS v FROM events GROUP BY user_id HAVING COUNT(*) > 1", USER_TS_NN, TYPES),
    ("pick_next_to_another_aggregate", "SELECT user_id, COUNT(*) AS c, MAX_BY(value, ts) AS v FROM events GROUP BY user_id", USER_TS_NN, TYPES),
    ("pick_next_to_an_ungrouped_column", "SELECT user_id, id, MAX_BY(value, ts) AS v FROM events GROUP BY user_id", USER_TS_NN, TYPES),
    ("picks_with_different_orders", "SELECT user_id, MAX_BY(value, ts) AS v, MIN_BY(id, ts) AS i FROM events GROUP BY user_id", USER_TS_NN, TYPES),
]


@pytest.mark.parametrize("name,sql,scenario,types", DECLINED_SHAPES, ids=[c[0] for c in DECLINED_SHAPES])
def test_leaves_other_shapes_alone(name, sql, scenario, types):
    assert rewritten(sql, scenario, types) is None


def test_the_derived_filter_conjunct_stays_where_it_was():
    text = rewritten(
        f"SELECT d.user_id FROM (SELECT user_id, ROW_NUMBER() OVER ({P_DESC}) AS rn FROM events) AS d WHERE d.rn = 1", USER_TS
    )
    assert "1 AS rn" in text and "WHERE d.rn = 1" in text


def test_a_rank_over_a_tied_order_is_never_the_grouping_itself():
    # a RANK = 1 select returns every tied row, a GROUP BY one row
    text = rewritten(f"SELECT user_id, ts FROM events QUALIFY RANK() OVER ({P_ASC}) = 1", ID_KEY_TS)
    assert text is not None and " JOIN (SELECT " in text and text.count("GROUP BY") == 1
    rows = [(1, 1, 1, 5), (2, 1, 1, 6)]
    assert run(text, rows) == [(1, 1), (1, 1)]


def test_a_row_number_that_reads_more_than_the_key_and_the_extreme_needs_a_total_order():
    # (user_id, ts) is not a key: value differs between the tied rows, so the first row is not determined
    sql = f"SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER ({P_ASC}) = 1"
    assert rewritten(sql, ID_KEY_TS) is None


def test_a_non_first_row_filter_in_a_derived_table_is_left_alone():
    sql = f"SELECT user_id FROM (SELECT user_id, ROW_NUMBER() OVER ({P_DESC}) AS rn FROM events) AS d WHERE rn = 2"
    assert rewritten(sql, USER_TS) is None


def test_the_rewrite_is_applied_by_normalize_once_and_only_with_the_facts():
    sql = f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1"
    keyed = normalize(sql, schema=SCHEMA, not_null={"events": frozenset({"id", "user_id", "ts"})}, keys={"events": [("user_id", "ts")]})
    plain = normalize(sql, schema=SCHEMA)
    assert "ROW_NUMBER" not in keyed and "ROW_NUMBER" in plain


# --- the prover -------------------------------------------------------------------------------

ROW_NUMBER = f"SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER ({P_DESC}) = 1"
RANK = f"SELECT user_id, ts, value FROM events QUALIFY RANK() OVER ({P_DESC}) = 1"
CORRELATED = "SELECT e.user_id, e.ts, e.value FROM events AS e WHERE e.ts = (SELECT MAX(u.ts) FROM events AS u WHERE u.user_id = e.user_id)"
GROUPED_JOIN = (
    "SELECT e.user_id, e.ts, e.value FROM events AS e JOIN (SELECT user_id, MAX(ts) AS m FROM events GROUP BY user_id) AS g "
    "ON e.user_id = g.user_id AND e.ts = g.m"
)
ARRAY_AGG = (
    "SELECT user_id, ARRAY_AGG(ts ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS ts, "
    "ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS value FROM events GROUP BY user_id"
)
MAX_BY = "SELECT user_id, MAX(ts) AS ts, MAX_BY(value, ts) AS value FROM events GROUP BY user_id"


def proven(left, right, scenario):
    keys, not_null = scenario
    constraints = {"events": TableConstraints(not_null=frozenset(not_null), keys=tuple(keys))}
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, constraints=constraints, dialect="bigquery").proven


# must prove under the scenario, and the pair agrees on DuckDB databases that respect it
PROOFS = [
    ("row_number_is_the_correlated_max", ROW_NUMBER, CORRELATED, USER_TS),
    ("row_number_is_the_grouped_join", ROW_NUMBER, GROUPED_JOIN, USER_TS),
    ("row_number_is_rank_with_a_total_order", ROW_NUMBER, RANK, USER_TS),
    ("row_number_is_array_agg", ROW_NUMBER, ARRAY_AGG, USER_TS),
    ("row_number_is_max_by", ROW_NUMBER, MAX_BY, USER_TS_NN),
    ("array_agg_is_max_by", ARRAY_AGG, MAX_BY, USER_TS_NN),
    ("rank_is_the_correlated_max_without_a_key", RANK, CORRELATED, ID_KEY_USER_TS),
    ("rank_is_the_grouped_join_without_a_key", RANK, GROUPED_JOIN, ID_KEY_USER_TS),
]


@pytest.mark.parametrize("name,left,right,scenario", PROOFS, ids=[c[0] for c in PROOFS])
def test_proves(name, left, right, scenario):
    assert proven(left, right, scenario)
    assert proven(right, left, scenario)


@pytest.mark.parametrize("name,left,right,scenario", PROOFS, ids=[c[0] for c in PROOFS])
def test_proved_pairs_agree_on_duckdb(name, left, right, scenario):
    rng = random.Random(f"proof-{name}")
    for trial in range(25):
        rows = random_rows(rng, scenario)
        assert run(left, rows) == run(right, rows), rows


# a pair that is only equivalent with a total order must not be proved without one (and DuckDB tells them apart)
NOT_PROVED = [
    ("row_number_vs_correlated_max_without_a_key", ROW_NUMBER, CORRELATED, ID_KEY_TS, [(1, 1, 3, 5), (2, 1, 3, 6)]),
    ("row_number_vs_rank_without_a_key", ROW_NUMBER, RANK, ID_KEY_TS, [(1, 1, 3, 5), (2, 1, 3, 6)]),
    ("max_by_vs_correlated_max_without_a_key", MAX_BY, CORRELATED, ID_KEY_TS_VALUE, [(1, 1, 3, 5), (2, 1, 3, 6)]),
    ("array_agg_vs_rank_without_a_key", ARRAY_AGG, RANK, ID_KEY_TS, [(1, 1, 3, 5), (2, 1, 3, 6)]),
]


@pytest.mark.parametrize("name,left,right,scenario,rows", NOT_PROVED, ids=[c[0] for c in NOT_PROVED])
def test_not_proved_without_a_total_order(name, left, right, scenario, rows):
    assert not proven(left, right, scenario)
    assert not proven(right, left, scenario)
    assert any(run(left, rows, reverse=reverse) != run(right, rows) for reverse in (False, True))


# --- the LeetCode "first login" shapes (MySQL), the pairs this rule was measured on ------------

ACTIVITY = {"activity": ["player_id", "event_date", "device_id", "games_played"]}
FIRST_LOGIN = "SELECT player_id, MIN(event_date) AS first_login FROM activity GROUP BY player_id"
FIRST_DEVICE_IN = "SELECT player_id, device_id FROM activity WHERE (player_id, event_date) IN (SELECT player_id, MIN(event_date) FROM activity GROUP BY player_id)"


def first_login_window(function):
    return (
        "SELECT a.player_id, a.event_date AS first_login FROM (SELECT player_id, event_date, "
        f"{function}() OVER (PARTITION BY player_id ORDER BY event_date) AS rnk FROM activity) AS a WHERE a.rnk = 1"
    )


def first_device_window(function):
    return (
        "SELECT player_id, device_id FROM (SELECT player_id, device_id, "
        f"{function}() OVER (PARTITION BY player_id ORDER BY event_date) AS rn FROM activity) AS t WHERE rn = 1"
    )


def proven_mysql(left, right, keys):
    constraints = {"activity": TableConstraints(not_null=frozenset({"player_id", "event_date"}), keys=tuple(keys))}
    return prove_equivalent_algebraic(left, right, schema=ACTIVITY, constraints=constraints, dialect="mysql", compare_names=False).proven


PK = [("player_id", "event_date")]
LEETCODE_PROOFS = [
    (name, left, right, keys)
    for function in ("ROW_NUMBER", "RANK", "DENSE_RANK")
    for name, left, right, keys in [
        (f"first_login_by_{function.lower()}", FIRST_LOGIN, first_login_window(function), PK),
        (f"first_device_in_subquery_vs_{function.lower()}", FIRST_DEVICE_IN, first_device_window(function), PK),
    ]
] + [
    # ROW_NUMBER keeps one row per player whichever of the tied rows, and only (player_id, event_date) is read
    ("first_login_by_row_number_without_a_key", FIRST_LOGIN, first_login_window("ROW_NUMBER"), []),
    # RANK and DENSE_RANK keep every tied row, like the IN test, so they need no key at all
    ("first_device_in_subquery_vs_rank_without_a_key", FIRST_DEVICE_IN, first_device_window("RANK"), []),
    ("first_device_in_subquery_vs_dense_rank_without_a_key", FIRST_DEVICE_IN, first_device_window("DENSE_RANK"), []),
]


@pytest.mark.parametrize("name,left,right,keys", LEETCODE_PROOFS, ids=[c[0] for c in LEETCODE_PROOFS])
def test_leetcode_shapes_are_proved(name, left, right, keys):
    assert proven_mysql(left, right, keys)
    assert proven_mysql(right, left, keys)


@pytest.mark.parametrize("name,left,right,keys", LEETCODE_PROOFS, ids=[c[0] for c in LEETCODE_PROOFS])
def test_leetcode_shapes_agree_on_duckdb(name, left, right, keys):
    rng = random.Random(name)
    for trial in range(25):
        seen, rows = set(), []
        for _ in range(rng.randint(0, 8)):
            row = (rng.choice([1, 2, 3]), rng.choice([1, 2, 3]), rng.choice([5, 6, None]), rng.choice([1, 2]))
            if not keys and rng.random() < 0.4:
                pass  # duplicates of (player_id, event_date) are allowed without the key
            elif (row[0], row[1]) in seen:
                continue
            seen.add((row[0], row[1]))
            rows.append(row)
        results = []
        for sql in (left, right):
            db = duckdb.connect(config={"threads": 1})
            db.execute("CREATE TABLE activity (player_id BIGINT, event_date BIGINT, device_id BIGINT, games_played BIGINT)")
            insert_rows(db, "activity", rows)
            tree = sqlglot.parse_one(sql, read="mysql")
            results.append(sorted(run_unoptimized(db, tree.sql(dialect="duckdb"))[0], key=repr))
        assert results[0] == results[1], rows


LEETCODE_NOT_PROVED = [
    # the key is (player_id, device_id): two rows can share the first event_date
    ("first_login_by_dense_rank_without_the_key", FIRST_LOGIN, first_login_window("DENSE_RANK"), [("player_id", "device_id")], [(1, 1, 5, 1), (1, 1, 6, 1)]),
    ("first_login_by_rank_without_the_key", FIRST_LOGIN, first_login_window("RANK"), [], [(1, 1, 5, 1), (1, 1, 6, 1)]),
    ("first_device_row_number_without_the_key", FIRST_DEVICE_IN, first_device_window("ROW_NUMBER"), [], [(1, 1, 5, 1), (1, 1, 6, 1)]),
]


@pytest.mark.parametrize("name,left,right,keys,rows", LEETCODE_NOT_PROVED, ids=[c[0] for c in LEETCODE_NOT_PROVED])
def test_leetcode_shapes_are_not_proved_without_the_key(name, left, right, keys, rows):
    assert not proven_mysql(left, right, keys)
    assert not proven_mysql(right, left, keys)
    outputs = []
    for sql in (left, right):
        db = duckdb.connect(config={"threads": 1})
        db.execute("CREATE TABLE activity (player_id BIGINT, event_date BIGINT, device_id BIGINT, games_played BIGINT)")
        insert_rows(db, "activity", rows)
        tree = sqlglot.parse_one(sql, read="mysql")
        outputs.append(sorted(run_unoptimized(db, tree.sql(dialect="duckdb"))[0], key=repr))
    assert outputs[0] != outputs[1]
