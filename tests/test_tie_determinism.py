"""Tie sites: which windows, LIMITs and aggregates can change their result with the order of tied rows."""

from collections import Counter
import itertools
import random

import pytest
import sqlglot

from kumosql.smt_equivalence import TableConstraints
from kumosql.tie_determinism import DETERMINISTIC, UNKNOWN, analyze, tie_dependence

KEYED = {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}

# (query, verdict of its one site without declared keys, verdict with events.id a NOT NULL key)
CASES = [
    # the latest row per user: the user's tied rows differ in value, so which one is kept matters
    ("SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1", UNKNOWN, UNKNOWN),
    # ... unless the ORDER BY ends with a key
    ("SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC, id) = 1", UNKNOWN, DETERMINISTIC),
    # ... or nothing after the window can tell tied rows apart
    ("SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, ts, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) AS rn FROM events QUALIFY rn = 1", DETERMINISTIC, DETERMINISTIC),
    ("SELECT COUNT(*) FROM (SELECT * FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1) AS d", DETERMINISTIC, DETERMINISTIC),
    ("WITH d AS (SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1) SELECT user_id, ts FROM d", DETERMINISTIC, DETERMINISTIC),
    ("WITH d AS (SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1) SELECT user_id, value FROM d", UNKNOWN, UNKNOWN),
    ("SELECT user_id, ts FROM events WHERE value = 3 QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1 AND value = 3", DETERMINISTIC, DETERMINISTIC),
    # functions that give peers one value
    ("SELECT user_id, value, RANK() OVER (PARTITION BY user_id ORDER BY ts) AS r FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, DENSE_RANK() OVER (ORDER BY ts) AS r FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts) AS s FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) AS s FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, COUNT(*) OVER (PARTITION BY user_id) AS n FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, MAX(value) OVER (ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS m FROM events", DETERMINISTIC, DETERMINISTIC),
    # positional frames and navigation
    ("SELECT value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts ROWS UNBOUNDED PRECEDING) AS s FROM events", UNKNOWN, UNKNOWN),
    ("SELECT value, SUM(value) OVER (PARTITION BY user_id ORDER BY ts, id ROWS UNBOUNDED PRECEDING) AS s FROM events", UNKNOWN, DETERMINISTIC),
    ("SELECT value, LAG(value) OVER (PARTITION BY user_id ORDER BY ts) AS p FROM events", UNKNOWN, UNKNOWN),
    ("SELECT ts, LAG(ts) OVER (ORDER BY value) AS p FROM events", UNKNOWN, UNKNOWN),
    ("SELECT user_id, ts, LAG(ts) OVER (PARTITION BY user_id ORDER BY ts) AS p FROM events", DETERMINISTIC, DETERMINISTIC),
    ("SELECT value, FIRST_VALUE(value) OVER (PARTITION BY user_id ORDER BY ts) AS f FROM events", UNKNOWN, UNKNOWN),
    ("SELECT value, ROW_NUMBER() OVER () AS n FROM events", UNKNOWN, UNKNOWN),
    ("SELECT id, value, ROW_NUMBER() OVER (PARTITION BY id) AS n FROM events", UNKNOWN, DETERMINISTIC),
    ("SELECT value, NTILE(4) OVER (ORDER BY id) AS q FROM events", UNKNOWN, DETERMINISTIC),
    # windows over a grouped query: the group keys are unique
    ("SELECT user_id, ROW_NUMBER() OVER (ORDER BY user_id) AS n FROM events GROUP BY user_id", DETERMINISTIC, DETERMINISTIC),
    ("SELECT k, s, ROW_NUMBER() OVER (ORDER BY s DESC) AS n FROM (SELECT user_id AS k, SUM(value) AS s FROM events GROUP BY user_id) AS g", UNKNOWN, UNKNOWN),
    ("SELECT k, s, ROW_NUMBER() OVER (ORDER BY s DESC, k) AS n FROM (SELECT user_id AS k, SUM(value) AS s FROM events GROUP BY user_id) AS g", DETERMINISTIC, DETERMINISTIC),
    # a window nobody reads cannot change the result
    ("SELECT user_id FROM (SELECT user_id, ROW_NUMBER() OVER () AS n FROM events) AS d", None, None),
    # LIMIT
    ("SELECT * FROM events ORDER BY ts LIMIT 10", UNKNOWN, UNKNOWN),
    ("SELECT * FROM events ORDER BY ts, id LIMIT 10", UNKNOWN, DETERMINISTIC),
    ("SELECT ts FROM events ORDER BY ts LIMIT 10", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, ts FROM events ORDER BY 2, 1 LIMIT 3", DETERMINISTIC, DETERMINISTIC),
    ("SELECT * FROM events LIMIT 10", UNKNOWN, UNKNOWN),
    ("SELECT COUNT(*) FROM events LIMIT 1", DETERMINISTIC, DETERMINISTIC),
    ("SELECT id FROM events WHERE EXISTS (SELECT 1 FROM events AS e LIMIT 1)", DETERMINISTIC, DETERMINISTIC),
    ("SELECT DISTINCT user_id FROM events ORDER BY user_id LIMIT 2", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, SUM(value) AS s FROM events GROUP BY user_id ORDER BY s DESC LIMIT 2", UNKNOWN, UNKNOWN),
    ("SELECT user_id, SUM(value) AS s FROM events GROUP BY user_id ORDER BY s DESC, user_id LIMIT 2", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id FROM events UNION ALL SELECT user_id FROM events ORDER BY user_id LIMIT 3", DETERMINISTIC, DETERMINISTIC),
    # aggregates that pick or collect rows
    ("SELECT user_id, ANY_VALUE(value) AS v FROM events GROUP BY user_id", UNKNOWN, UNKNOWN),
    ("SELECT id, ANY_VALUE(value) AS v FROM events GROUP BY id", UNKNOWN, DETERMINISTIC),
    ("SELECT user_id, ANY_VALUE(user_id) AS v FROM events GROUP BY user_id", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, MAX_BY(value, ts) AS v FROM events GROUP BY user_id", UNKNOWN, UNKNOWN),
    ("SELECT user_id, ARRAY_AGG(value ORDER BY ts DESC LIMIT 1)[OFFSET(0)] AS v FROM events GROUP BY user_id", UNKNOWN, UNKNOWN),
    ("SELECT user_id, ARRAY_AGG(value ORDER BY ts, id) AS v FROM events GROUP BY user_id", UNKNOWN, DETERMINISTIC),
    ("SELECT user_id, ARRAY_AGG(ts ORDER BY ts) AS v FROM events GROUP BY user_id", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, ARRAY_AGG(DISTINCT ts ORDER BY ts) AS v FROM events GROUP BY user_id", DETERMINISTIC, DETERMINISTIC),
    ("SELECT user_id, STRING_AGG(CAST(value AS STRING)) AS v FROM events GROUP BY user_id", UNKNOWN, UNKNOWN),
    ("SELECT user_id, ARRAY_AGG(e ORDER BY ts DESC LIMIT 1)[OFFSET(0)].value AS v FROM events AS e GROUP BY user_id", UNKNOWN, UNKNOWN),
    ("SELECT ARRAY(SELECT value FROM events ORDER BY ts) AS a", UNKNOWN, UNKNOWN),
    ("SELECT ARRAY(SELECT value FROM events ORDER BY ts, id) AS a", UNKNOWN, DETERMINISTIC),
]


@pytest.mark.parametrize("sql, plain, keyed", CASES, ids=[str(i) for i in range(len(CASES))])
def test_site_verdicts(sql, plain, keyed):
    for constraints, expected in ((None, plain), (KEYED, keyed)):
        report = analyze(sql, constraints=constraints)
        assert not report.unsupported, report.unsupported
        if expected is None:
            assert not report.sites, report.sites
            continue
        assert len(report.sites) == 1, [s.to_json() for s in report.sites]
        site = report.sites[0]
        assert site.verdict == expected, site.to_json()
        if expected == UNKNOWN:
            assert site.fix, site.to_json()


def test_deterministic_verdicts_name_the_declared_keys_they_rest_on():
    site = analyze(
        "SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts, id) = 1",
        constraints=KEYED,
    ).sites[0]
    assert site.deterministic
    assert site.facts == ("(id) is unique in events",)


def test_the_fix_names_a_key_that_would_make_the_order_total():
    site = analyze(
        "SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1",
        constraints=KEYED,
    ).sites[0]
    assert "events.id" in site.fix


def test_named_windows_are_read_like_the_window_they_name():
    report = analyze("SELECT value, LAG(value) OVER w AS p FROM events WINDOW w AS (PARTITION BY user_id ORDER BY ts, id)", constraints=KEYED)
    assert [s.verdict for s in report.sites] == [DETERMINISTIC]


def test_tie_dependence_takes_unique_non_null_keys():
    sql = "SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1"
    assert tie_dependence(sql, keys={"events": [("user_id", "ts")]}) == []
    reasons = tie_dependence(sql)
    assert len(reasons) == 1 and reasons[0].startswith("ROW_NUMBER")
    assert tie_dependence("SELECT a FROM t ORDER BY b LIMIT 1", keys={"t": [("b",)]}) == []
    assert tie_dependence("SELECT a FROM t LIMIT 1") == ["LIMIT (LIMIT 1): LIMIT or OFFSET without ORDER BY keeps arbitrary rows"]
    assert tie_dependence("SELECT a FROM") and tie_dependence("SELECT a FROM")[0].startswith("not analyzed")


def test_every_site_of_a_query_is_listed_with_where_it_is():
    report = analyze(
        "WITH latest AS (SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1) "
        "SELECT * FROM latest ORDER BY value LIMIT 5"
    )
    assert [(s.kind, s.scope) for s in report.sites] == [("limit", "query"), ("window", "WITH latest")]
    assert not report.deterministic
    assert report.to_json()["sites"][1]["function"] == "ROW_NUMBER"


def _hashable(value):
    return tuple(_hashable(v) for v in value) if isinstance(value, (list, tuple)) else value


def _bag(con, sql):
    return Counter(_hashable(row) for row in con.execute(sql).fetchall())


def test_deterministic_verdicts_hold_for_every_row_order_on_duckdb():
    """A query judged deterministic returns the same rows whatever order its table's rows are stored in."""

    duckdb = pytest.importorskip("duckdb")
    from kumosql.bigquery_on_duckdb import configure

    rng = random.Random(7)
    con = duckdb.connect()
    con.execute("SET threads = 1")  # ties then fall in storage order, so changing it changes them
    configure(con)
    con.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    databases = []
    for _ in range(12):
        ids = rng.sample(range(1, 9), rng.randint(2, 4))
        databases.append([(i, rng.choice([1, 2, None]), rng.choice([1, 2, None]), rng.choice([5, 6, None])) for i in ids])
    checked = 0
    for sql, plain, keyed in CASES:
        if "ARRAY(" in sql:
            continue
        for constraints, expected in ((None, plain), (KEYED, keyed)):
            if expected != DETERMINISTIC:
                continue
            duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
            for rows in databases:
                outputs = set()
                for order in itertools.permutations(rows):
                    con.execute("DELETE FROM events")
                    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", list(order))
                    outputs.add(frozenset(_bag(con, duck).items()))
                assert len(outputs) == 1, (sql, rows)
            checked += 1
    assert checked >= 40


# A SELECT alias that is also a column name: `value AS ts` hides the real `ts` from ORDER BY, GROUP BY and QUALIFY,
# but not from a window's OVER clause or an aggregate's arguments. Without a schema the analysis only knows the columns
# the query reads, and it used to take every name that matched an alias for the alias.
COMPOSITE = {"events": TableConstraints(not_null=frozenset({"user_id", "ts"}), keys=(("user_id", "ts"),))}
# (query, constraints, rows on which two storage orders give different results)
ALIAS_TRAPS = [
    # the window orders by the column ts, but the output called ts is value: tied rows differ in it
    (
        "SELECT value AS ts, ts AS value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1",
        None, [(1, 1, 1, 5), (2, 1, 1, 6)],
    ),
    (
        "SELECT value AS ts, ts AS value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1",
        KEYED, [(1, 1, 1, 5), (2, 1, 1, 6)],
    ),
    # ORDER BY ts is the alias (user_id) or the column; the second output differs between rows tied on the first
    ("SELECT user_id AS ts, ts AS user_id FROM events ORDER BY ts LIMIT 1", None, [(1, 1, 1, 5), (2, 1, 2, 5)]),
    ("SELECT user_id AS ts, ts AS user_id FROM events ORDER BY ts LIMIT 1", KEYED, [(1, 1, 1, 5), (2, 1, 2, 5)]),
    # GROUP BY ts is the column, because an aggregate cannot be a group key
    ("SELECT user_id, ANY_VALUE(value) AS ts FROM events GROUP BY user_id, ts", None, [(1, 1, 1, 5), (2, 1, 1, 6)]),
    ("SELECT user_id, ANY_VALUE(value) AS ts FROM events GROUP BY user_id, ts", KEYED, [(1, 1, 1, 5), (2, 1, 1, 6)]),
    # the window's ORDER BY id is the column id, not the key-looking alias of ts
    (
        "SELECT user_id, ts AS id FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) = 1",
        COMPOSITE, [(1, 1, 1, 5), (1, 1, 2, 6)],
    ),
]
# the same shapes where the alias hides nothing that matters: still deterministic
ALIAS_SAFE = [
    "SELECT value AS ts FROM events ORDER BY ts LIMIT 1",
    "SELECT ts AS value FROM events ORDER BY value LIMIT 1",
    "SELECT ts AS ts, value AS value FROM events ORDER BY ts, value LIMIT 1",
    "SELECT user_id, ts AS value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1",
    "SELECT user_id, value AS ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY value) = 1",
]


def _storage_orders_differ(sql, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.bigquery_on_duckdb import configure

    con = duckdb.connect()
    con.execute("SET threads = 1")
    configure(con)
    con.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    outputs = set()
    for order in itertools.permutations(rows):
        con.execute("DELETE FROM events")
        con.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", list(order))
        outputs.add(frozenset(_bag(con, duck).items()))
    return len(outputs) > 1


@pytest.mark.parametrize("sql, constraints, rows", ALIAS_TRAPS, ids=[str(i) for i in range(len(ALIAS_TRAPS))])
def test_an_alias_that_shadows_a_column_is_not_taken_for_it(sql, constraints, rows):
    assert _storage_orders_differ(sql, rows)  # the trap is real: DuckDB returns different rows
    report = analyze(sql, constraints=constraints)
    assert not report.unsupported, report.unsupported
    assert not report.deterministic, [s.to_json() for s in report.sites]


@pytest.mark.parametrize("sql", ALIAS_SAFE, ids=[str(i) for i in range(len(ALIAS_SAFE))])
def test_shadowing_aliases_that_hide_nothing_keep_their_verdict(sql):
    for constraints in (None, KEYED):  # (with ts a declared column, `ORDER BY ts` could mean either, so it is unknown)
        assert analyze(sql, constraints=constraints).deterministic
    rng = random.Random(3)
    for _ in range(6):
        rows = [(i, rng.choice([1, 2]), rng.choice([1, 2, None]), rng.choice([5, 6, None])) for i in rng.sample(range(1, 9), 3)]
        assert not _storage_orders_differ(sql, rows), (sql, rows)
