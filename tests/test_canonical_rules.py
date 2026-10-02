"""The extra canonical rewrites keep every result bag: each case is checked on random DuckDB databases."""

import random
from collections import Counter

import pytest

from kumosql.canonical_rules import canonicalize

duckdb = pytest.importorskip("duckdb")
import sqlglot  # noqa: E402

SCHEMA = {"t": ["k", "x", "y"], "u": ["k", "z"]}


def same_bags(left: str, right: str, trials: int = 300) -> bool:
    db = duckdb.connect(":memory:")
    for table, columns in SCHEMA.items():
        db.execute(f"CREATE TABLE {table} ({', '.join(c + ' BIGINT' for c in columns)})")
    rng = random.Random(3)
    left_sql = sqlglot.transpile(left, read="mysql", write="duckdb")[0]
    right_sql = sqlglot.transpile(right, read="mysql", write="duckdb")[0]
    for _ in range(trials):
        for table, columns in SCHEMA.items():
            db.execute(f"DELETE FROM {table}")
            for _ in range(rng.randint(0, 6)):
                values = [rng.choice([None, 0, 1, 2]) for _ in columns]
                db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", values)
        if Counter(db.execute(left_sql).fetchall()) != Counter(db.execute(right_sql).fetchall()):
            return False
    return True


REWRITTEN = [
    "SELECT DISTINCT k, MIN(x) FROM t GROUP BY k",
    "SELECT k FROM t GROUP BY k HAVING COUNT(*) > 0",
    "SELECT k FROM t GROUP BY k HAVING COUNT(1) >= 1 AND MAX(x) > 0",
    "SELECT DISTINCT k FROM (SELECT k, x FROM t GROUP BY k, x HAVING COUNT(DISTINCT y) > 1) AS s",
    "SELECT k FROM (SELECT k, COUNT(DISTINCT y) AS n FROM t GROUP BY k) AS s WHERE n >= 1",
    "SELECT s.k, s.z FROM (SELECT t.k, u.z FROM t LEFT JOIN u ON t.k = u.k) AS s WHERE s.z < 2 OR s.z IS NULL",
    "SELECT k, x FROM t WHERE (k, x) IN (SELECT k, MIN(x) FROM t GROUP BY k)",
    "SELECT k, x FROM t ORDER BY k",
]


@pytest.mark.parametrize("sql", REWRITTEN)
def test_rewrite_changes_the_text_and_keeps_the_result(sql):
    rewritten = canonicalize(sql, "mysql", SCHEMA)
    assert rewritten != sqlglot.transpile(sql, read="mysql", write="mysql")[0]
    assert same_bags(sql, rewritten)


LEFT_ALONE = [
    "SELECT DISTINCT k FROM t GROUP BY k, x",  # a key is not selected
    "SELECT k FROM t GROUP BY k HAVING COUNT(x) > 0",  # COUNT(x) can be 0
    "SELECT COUNT(*) FROM t HAVING COUNT(*) > 0",  # no GROUP BY: an empty table gives no row
    "SELECT k FROM (SELECT DISTINCT k, x FROM t) AS s",  # projecting a DISTINCT keeps duplicates
    "SELECT n FROM (SELECT COUNT(*) AS n FROM t) AS s WHERE n > 0",  # global aggregate
    "SELECT k, x FROM t WHERE (k, x) IN (SELECT k, x FROM t GROUP BY k, x, y)",  # not one row per compared key
    "SELECT k FROM t ORDER BY k LIMIT 1",
]


@pytest.mark.parametrize("sql", LEFT_ALONE)
def test_unsound_shapes_are_left_alone(sql):
    assert canonicalize(sql, "mysql", SCHEMA) == sqlglot.transpile(sql, read="mysql", write="mysql")[0]


def test_correlated_in_subquery_is_not_joined():
    sql = "SELECT k FROM t AS a WHERE (a.k, a.x) IN (SELECT k, MAX(z) FROM u WHERE u.z = a.y GROUP BY k)"
    assert "JOIN" not in canonicalize(sql, "mysql", SCHEMA)


def test_two_in_tests_get_their_own_join_names():
    sql = "SELECT k FROM t WHERE (k, x) IN (SELECT k, MIN(x) FROM t GROUP BY k) AND (k, y) IN (SELECT k, MAX(y) FROM t GROUP BY k)"
    rewritten = canonicalize(sql, "mysql", SCHEMA)
    assert "kq_in0" in rewritten and "kq_in1" in rewritten
    assert same_bags(sql, rewritten)
