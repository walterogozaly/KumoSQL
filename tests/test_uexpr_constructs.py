"""Constructs the bag-equivalence backend (``kumosql.uexpr``) reads: ROLLUP / GROUPING SETS, aggregate
FILTER, COUNT over several arguments, LIMIT in its several shapes, LATERAL, windows, parenthesized joins
and comparisons of mixed types.

Every proved pair is also run on random data in DuckDB (the backend must never prove a pair that differs),
and the unproved pairs show where the translation stops. The tables and queries are synthetic.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from kumosql.uexpr import prove_bag_equivalent

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "d"]}

# (name, left, right, whether DuckDB can run both sides)
PROVEN = [
    ("rollup", "SELECT a, SUM(b) FROM t GROUP BY ROLLUP(a)", "SELECT a, SUM(b) FROM t GROUP BY a UNION ALL SELECT NULL, SUM(b) FROM t", True),
    (
        "grouping sets",
        "SELECT a, b, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), (b))",
        "SELECT a, NULL, COUNT(*) FROM t GROUP BY a UNION ALL SELECT NULL, b, COUNT(*) FROM t GROUP BY b",
        True,
    ),
    (
        "grouping call",
        "SELECT a, GROUPING(a) AS g, COUNT(*) FROM t GROUP BY ROLLUP(a)",
        "SELECT a, 0, COUNT(*) FROM t GROUP BY a UNION ALL SELECT NULL, 1, COUNT(*) FROM t",
        True,
    ),
    ("filter on a sum", "SELECT SUM(b) FILTER (WHERE a > 1) FROM t", "SELECT SUM(b) FROM t WHERE a > 1", True),
    (
        "filter on a count",
        "SELECT a, COUNT(*) FILTER (WHERE b > 1) FROM t GROUP BY a",
        "SELECT a, COUNT(CASE WHEN b > 1 THEN 1 END) FROM t GROUP BY a",
        True,
    ),
    ("count distinct of two", "SELECT COUNT(DISTINCT a, b) FROM t", "SELECT COUNT(DISTINCT t.a, t.b) FROM t", False),
    ("fetch is limit", "SELECT a FROM t ORDER BY a FETCH FIRST 3 ROWS ONLY", "SELECT a FROM t ORDER BY a LIMIT 3", False),
    ("star under a limit", "SELECT * FROM t ORDER BY a, b, c LIMIT 3", "SELECT a, b, c FROM t ORDER BY a, b, c LIMIT 3", True),
    (
        "limit source, aliases renamed",
        "SELECT s.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) s WHERE s.a > 1",
        "SELECT q.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) AS q WHERE q.a > 1",
        True,
    ),
    (
        "limit 1 over a constant is an existence test",
        "SELECT a FROM t WHERE EXISTS (SELECT b FROM u)",
        "SELECT a FROM t WHERE (SELECT 1 FROM (SELECT b FROM u) x LIMIT 1) IS NOT NULL",
        True,
    ),
    (
        "limit 1 keeps at most one row",
        "SELECT a FROM (SELECT a FROM t LIMIT 1) s GROUP BY a",
        "SELECT a FROM (SELECT a FROM t LIMIT 1) s",
        True,
    ),
    (
        "limit under a projection of its keys",
        "SELECT a, a AS b FROM t ORDER BY a LIMIT 5",
        "SELECT a, a AS b FROM (SELECT * FROM t ORDER BY a LIMIT 5) s ORDER BY a",
        True,
    ),
    (
        "inner lateral",
        "SELECT * FROM t INNER JOIN LATERAL (SELECT * FROM u WHERE t.a = u.a) x ON TRUE",
        "SELECT * FROM t INNER JOIN u ON t.a = u.a",
        True,
    ),
    (
        "left lateral",
        "SELECT * FROM t LEFT JOIN LATERAL (SELECT * FROM u WHERE t.a = u.a) x ON TRUE",
        "SELECT * FROM t LEFT JOIN u ON t.a = u.a",
        True,
    ),
    (
        "window source, aliases renamed",
        "SELECT * FROM (SELECT a, RANK() OVER (PARTITION BY b ORDER BY c) AS r FROM t) s WHERE s.r < 2",
        "SELECT * FROM (SELECT t1.a, RANK() OVER (PARTITION BY t1.b ORDER BY t1.c) AS r FROM t AS t1) AS q WHERE q.r < 2",
        True,
    ),
    (
        "nth value of one is first value",
        "SELECT FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY c) FROM t",
        "SELECT NTH_VALUE(a, 1) OVER (PARTITION BY b ORDER BY c) FROM t",
        True,
    ),
    (
        "mixed types, same operands",
        "SELECT t.a FROM t WHERE t.b = t.c AND t.a + 1 = 'x'",
        "SELECT u1.a FROM t AS u1 WHERE u1.a + 1 = 'x' AND u1.c = u1.b",
        False,
    ),
    (
        "parenthesized join",
        "SELECT t.a FROM t LEFT JOIN (u AS u1 JOIN u AS u2 ON u1.a = u2.a) ON t.a = u1.a",
        "SELECT t.a FROM t LEFT JOIN (SELECT u1.a FROM u AS u1 JOIN u AS u2 ON u1.a = u2.a) x ON t.a = x.a",
        True,
    ),
]

NOT_PROVEN = [
    ("grouping sets are not one group by", "SELECT a, b, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), (b))", "SELECT a, b, COUNT(*) FROM t GROUP BY a, b"),
    ("filter is not no filter", "SELECT COUNT(*) FILTER (WHERE a > 1) FROM t", "SELECT COUNT(*) FROM t"),
    (
        "filter is not where when groups can vanish",
        "SELECT a, COUNT(*) FILTER (WHERE b > 1) FROM t GROUP BY a",
        "SELECT a, COUNT(*) FROM t WHERE b > 1 GROUP BY a",
    ),
    ("a second count argument matters", "SELECT COUNT(DISTINCT a, b) FROM t", "SELECT COUNT(DISTINCT a) FROM t"),
    ("count of two is not count of one", "SELECT COUNT(a, b) FROM t", "SELECT COUNT(a) FROM t"),
    ("distinct pairs are not rows", "SELECT COUNT(DISTINCT a, b) FROM t", "SELECT COUNT(a, b) FROM t"),
    ("another limit", "SELECT a FROM t ORDER BY a FETCH FIRST 3 ROWS ONLY", "SELECT a FROM t ORDER BY a LIMIT 4"),
    (
        "another limit inside",
        "SELECT s.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) s WHERE s.a > 1",
        "SELECT q.a FROM (SELECT a FROM t ORDER BY a LIMIT 3) AS q WHERE q.a > 1",
    ),
    ("a limit is not no limit", "SELECT s.a FROM (SELECT a FROM t ORDER BY a LIMIT 2) s", "SELECT a FROM t"),
    ("a top-level limit without order by picks arbitrary rows", "SELECT a FROM t LIMIT 2", "SELECT a FROM t LIMIT 2"),
    (
        "limit 2 over a constant is not an existence test",
        "SELECT a FROM t WHERE EXISTS (SELECT b FROM u)",
        "SELECT a FROM t WHERE (SELECT 1 FROM (SELECT b FROM u) x LIMIT 2) IS NOT NULL",
    ),
    (
        "two rows can be distinct",
        "SELECT a FROM (SELECT a FROM t LIMIT 2) s GROUP BY a",
        "SELECT a FROM (SELECT a FROM t LIMIT 2) s",
    ),
    ("a limit under a projection of a non-key", "SELECT c FROM t ORDER BY a LIMIT 5", "SELECT c FROM (SELECT * FROM t ORDER BY a LIMIT 5) s"),
    (
        "a left lateral keeps unmatched rows",
        "SELECT * FROM t LEFT JOIN LATERAL (SELECT * FROM u WHERE t.a = u.a) x ON TRUE",
        "SELECT * FROM t INNER JOIN u ON t.a = u.a",
    ),
    (
        "another window order",
        "SELECT * FROM (SELECT a, RANK() OVER (PARTITION BY b ORDER BY c) AS r FROM t) s WHERE s.r < 2",
        "SELECT * FROM (SELECT a, RANK() OVER (PARTITION BY b ORDER BY c DESC) AS r FROM t) s WHERE s.r < 2",
    ),
    (
        "another filter on a window",
        "SELECT * FROM (SELECT a, RANK() OVER (PARTITION BY b ORDER BY c) AS r FROM t) s WHERE s.r < 2",
        "SELECT * FROM (SELECT a, RANK() OVER (PARTITION BY b ORDER BY c) AS r FROM t) s WHERE s.r < 3",
    ),
    (
        "the second value is not the first",
        "SELECT FIRST_VALUE(a) OVER (PARTITION BY b ORDER BY c) FROM t",
        "SELECT NTH_VALUE(a, 2) OVER (PARTITION BY b ORDER BY c) FROM t",
    ),
    ("another mixed comparison", "SELECT t.a FROM t WHERE t.a + 1 = 'x'", "SELECT t.a FROM t WHERE t.a + 1 <= 'x'"),
    (
        "a window over an outer column",
        "SELECT a FROM t WHERE EXISTS (SELECT RANK() OVER (ORDER BY d) FROM u WHERE u.a = t.a)",
        "SELECT a FROM t WHERE EXISTS (SELECT RANK() OVER (ORDER BY d) FROM u WHERE u.a = t.b)",
    ),
]


def prove(left: str, right: str):
    return prove_bag_equivalent(left, right, schema=SCHEMA, dialect="mysql", exact_arithmetic=True, compare_names=False)


@pytest.mark.parametrize("name,left,right,runs", PROVEN, ids=[p[0] for p in PROVEN])
def test_proved(name, left, right, runs):
    result = prove(left, right)
    assert result.proven, result.reason
    if runs:
        _same_on_random_data(left, right)


@pytest.mark.parametrize("name,left,right", NOT_PROVEN, ids=[p[0] for p in NOT_PROVEN])
def test_not_proved(name, left, right):
    assert not prove(left, right).proven


def test_limit_sources_are_an_assumption():
    result = prove(*PROVEN[8][1:3])
    assert any("LIMIT subquery" in a for a in result.assumptions)


def test_window_sources_are_an_assumption():
    result = prove(*PROVEN[14][1:3])
    assert any("window functions" in a for a in result.assumptions)


def _same_on_random_data(left: str, right: str, trials: int = 12) -> None:
    duckdb = pytest.importorskip("duckdb")
    rng = random.Random(510)
    for _ in range(trials):
        con = duckdb.connect()
        for table, columns in SCHEMA.items():
            con.execute(f"CREATE TABLE {table} ({', '.join(c + ' INTEGER' for c in columns)})")
            rows = [tuple(rng.choice([None, 0, 1, 2, 3]) for _ in columns) for _ in range(rng.randint(0, 6))]
            for row in rows:
                con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", list(row))
        assert Counter(con.execute(left).fetchall()) == Counter(con.execute(right).fetchall()), (left, right)
        con.close()
