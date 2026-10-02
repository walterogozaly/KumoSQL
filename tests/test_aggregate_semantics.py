"""Aggregate facts in the SMT model and the set-of-values reduction (failing-tests cluster 19)."""

import random

import duckdb
import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.set_aggregates import reduce

SCHEMA = {"t": ["k", "x", "y"], "u": ["num"]}


def _proven(left: str, right: str, **kwargs) -> bool:
    options = dict(schema=SCHEMA, dialect="duckdb", compare_names=False)
    options.update(kwargs)
    return prove_equivalent_algebraic(left, right, **options).proven


def _differ(left: str, right: str, trials: int = 300) -> bool:
    """Whether random small tables (with NULLs and repeats) tell the two queries apart."""

    rng = random.Random(7)
    con = duckdb.connect()
    for _ in range(trials):
        con.execute("CREATE OR REPLACE TABLE t (k INTEGER, x INTEGER, y INTEGER)")
        con.execute("CREATE OR REPLACE TABLE u (num INTEGER)")
        pick = lambda: rng.choice([None, 0, 1, 2, 3])  # noqa: E731
        rows = [(pick(), pick(), pick()) for _ in range(rng.randint(0, 6))]
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
        nums = [(pick(),) for _ in range(rng.randint(0, 6))]
        if nums:
            con.executemany("INSERT INTO u VALUES (?)", nums)
        a = sorted(map(repr, con.execute(left).fetchall()))
        b = sorted(map(repr, con.execute(right).fetchall()))
        if a != b:
            return True
    return False


EQUIVALENT = [
    # MAX ignores the group of NULLs that COUNT(*) keeps and COUNT(num) drops.
    ("SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(num) = 1) AS a",
     "SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(*) = 1) AS b"),
    ("SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(num) = 1) AS a",
     "SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(*) < 2) AS b"),
    # DISTINCT aggregates and BIT_AND read the set of values; SUM over a GROUP BY x source is SUM(DISTINCT x).
    ("SELECT SUM(DISTINCT x), COUNT(DISTINCT x), BIT_AND(x) FROM t",
     "SELECT SUM(x), COUNT(x), BIT_AND(x) FROM (SELECT x FROM t GROUP BY x) AS d"),
    # COUNT only sees whether its argument is NULL.
    ("SELECT k, COUNT(CASE WHEN x = 1 THEN 'a' END) FROM t GROUP BY k",
     "SELECT k, COUNT(CASE WHEN x = 1 THEN 1 END) FROM t GROUP BY k"),
    # Counts are whole numbers.
    ("SELECT k FROM t GROUP BY k HAVING COUNT(x) > 1", "SELECT k FROM t GROUP BY k HAVING COUNT(x) >= 2"),
    # A group's key is its own MIN and MAX, and COUNT of the key is 0 only for the NULL group.
    ("SELECT k, MAX(k), MIN(k) FROM t GROUP BY k", "SELECT k, k, k FROM t GROUP BY k"),
    ("SELECT k, COUNT(k) FROM t GROUP BY k", "SELECT k, CASE WHEN k IS NULL THEN 0 ELSE COUNT(*) END FROM t GROUP BY k"),
    ("SELECT k, COUNT(DISTINCT k) FROM t GROUP BY k", "SELECT k, CASE WHEN k IS NULL THEN 0 ELSE 1 END FROM t GROUP BY k"),
    # SUM is NULL exactly when it saw no value.
    ("SELECT COALESCE(SUM(x), 0), COUNT(x) FROM t", "SELECT CASE WHEN COUNT(x) = 0 THEN 0 ELSE SUM(x) END, COUNT(x) FROM t"),
    ("SELECT k, SUM(x) FROM t WHERE x IS NOT NULL GROUP BY k", "SELECT k, COALESCE(SUM(x), 0) FROM t WHERE x IS NOT NULL GROUP BY k"),
    # A row joined back to its own group: the group's MAX is not NULL when the row's value is not,
    # and is at least that value; MIN is at most.
    ("SELECT s.y, CASE WHEN g.m > 1 THEN 1 ELSE s.x END FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k",
     "SELECT s.y, CASE WHEN g.m > 1 THEN 1 WHEN g.m IS NULL THEN NULL ELSE s.x END FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k"),
    ("SELECT s.y, CASE WHEN s.x > g.m THEN 1 ELSE g.m END FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k",
     "SELECT s.y, g.m FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k"),
    ("SELECT s.y, CASE WHEN s.x < g.m THEN 1 ELSE g.m END FROM t AS s JOIN (SELECT k, MIN(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k",
     "SELECT s.y, g.m FROM t AS s JOIN (SELECT k, MIN(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k"),
    # Exact literal arithmetic, x * 1, ROUND(x, 0) and ROUND over CASE arms.
    ("SELECT x * (1 - 0.25), x * 1 FROM t", "SELECT x * 0.75, x FROM t"),
    ("SELECT ROUND(CASE WHEN k > 1 THEN x END, 0) FROM t", "SELECT CASE WHEN k > 1 THEN ROUND(x) END FROM t"),
    ("SELECT CAST(CAST(x AS BIGINT) AS BIGINT) FROM t", "SELECT CAST(x AS BIGINT) FROM t"),
]

DIFFERENT = [
    # The sets of values differ (the right keeps values that occur twice).
    ("SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(num) = 1) AS a",
     "SELECT MAX(num) FROM (SELECT num FROM u GROUP BY num HAVING COUNT(*) >= 1) AS b"),
    # SUM and COUNT see repeats: no reduction over a source that repeats values.
    ("SELECT SUM(DISTINCT x) FROM t", "SELECT SUM(x) FROM t"),
    ("SELECT COUNT(x) FROM (SELECT x, y FROM t GROUP BY x, y) AS d", "SELECT COUNT(DISTINCT x) FROM t"),
    ("SELECT BIT_XOR(x) FROM (SELECT x FROM t GROUP BY x) AS d", "SELECT BIT_XOR(x) FROM t"),
    ("SELECT BIT_AND(x) FROM t", "SELECT BIT_OR(x) FROM t"),
    # COUNT of the group key misses the NULL group; SUM of the key is the key times the group size.
    ("SELECT k, COUNT(k) FROM t GROUP BY k", "SELECT k, COUNT(*) FROM t GROUP BY k"),
    ("SELECT k, SUM(k) FROM t GROUP BY k", "SELECT k, k FROM t GROUP BY k"),
    ("SELECT k, COUNT(CASE WHEN x = 1 THEN 1 END) FROM t GROUP BY k",
     "SELECT k, COUNT(CASE WHEN x = 2 THEN 1 END) FROM t GROUP BY k"),
    ("SELECT k FROM t GROUP BY k HAVING COUNT(x) > 1", "SELECT k FROM t GROUP BY k HAVING COUNT(x) >= 1"),
    ("SELECT COALESCE(SUM(x), 0) FROM t", "SELECT SUM(x) FROM t"),
    # MIN is not at least the row's value; a filtered group need not contain the row.
    ("SELECT s.y, CASE WHEN s.x > g.m THEN 1 ELSE g.m END FROM t AS s JOIN (SELECT k, MIN(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k",
     "SELECT s.y, g.m FROM t AS s JOIN (SELECT k, MIN(x) AS m FROM t GROUP BY k) AS g ON s.k = g.k"),
    ("SELECT s.y, CASE WHEN s.x > g.m THEN 1 ELSE g.m END FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t WHERE x < 2 GROUP BY k) AS g ON s.k = g.k",
     "SELECT s.y, g.m FROM t AS s JOIN (SELECT k, MAX(x) AS m FROM t WHERE x < 2 GROUP BY k) AS g ON s.k = g.k"),
    ("SELECT x * (1 - 0.25) FROM t", "SELECT x * 0.5 FROM t"),
    ("SELECT CAST(CAST(x AS DOUBLE) AS VARCHAR) FROM t", "SELECT CAST(x AS VARCHAR) FROM t"),
]


@pytest.mark.parametrize("left,right", EQUIVALENT)
def test_equivalent_pairs_are_proven(left, right):
    assert not _differ(left, right)
    assert _proven(left, right)


@pytest.mark.parametrize("left,right", DIFFERENT)
def test_different_pairs_are_not_proven(left, right):
    assert _differ(left, right)
    assert not _proven(left, right)


def test_reduction_needs_matching_set_functions():
    assert reduce("SELECT MAX(x) FROM t", "SELECT MIN(x) FROM t", "duckdb") is None
    assert reduce("SELECT MAX(x) FROM t GROUP BY k", "SELECT MAX(x) FROM t GROUP BY k", "duckdb") is None
    assert reduce("SELECT SUM(x) FROM t", "SELECT SUM(x) FROM t", "duckdb") is None
    assert reduce("SELECT MAX(x) AS a FROM t", "SELECT MAX(x) AS b FROM t", "duckdb") is None
    pairs = reduce("SELECT MAX(x), MIN(x) FROM t WHERE k = 1", "SELECT MAX(x), MIN(x) FROM t WHERE k = 1", "duckdb", compare_names=False)
    assert pairs == [("SELECT DISTINCT x AS v FROM t WHERE (k = 1) AND NOT x IS NULL",) * 2]


def test_empty_bit_aggregate_is_not_folded_to_null():
    # MySQL's BIT_AND over no rows is all ones, not NULL.
    assert not _proven("SELECT BIT_AND(x) FROM t WHERE 1 = 0", "SELECT NULL FROM t WHERE 1 = 0 GROUP BY k", dialect="mysql")
    assert not _proven("SELECT BIT_AND(x) FROM t WHERE 1 = 0", "SELECT CAST(NULL AS SIGNED)", dialect="mysql")


def test_decimal_literals_are_not_folded_where_they_are_floats():
    # BigQuery reads 0.25 as FLOAT64, so literal arithmetic is left alone there (integers still fold).
    assert not _proven("SELECT x * (1 - 0.25) FROM t", "SELECT x * 0.75 FROM t", dialect="bigquery")
    assert _proven("SELECT x * (3 - 1) FROM t", "SELECT x * 2 FROM t", dialect="bigquery")


def test_bit_aggregate_of_a_null_group_key_stays_unknown():
    # MySQL's BIT_AND over a group of NULLs is all ones, so it is the key only when the key is not NULL.
    assert not _proven("SELECT k, BIT_AND(k) FROM t GROUP BY k", "SELECT k, k FROM t GROUP BY k", dialect="mysql")
    assert _proven(
        "SELECT k, CASE WHEN k IS NULL THEN NULL ELSE BIT_AND(k) END FROM t GROUP BY k", "SELECT k, k FROM t GROUP BY k", dialect="mysql"
    )
