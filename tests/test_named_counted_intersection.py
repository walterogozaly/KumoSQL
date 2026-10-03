"""Calcite's IntersectToDistinct encoding (per-branch GROUP BY, UNION ALL, COUNT(*) = n) as INTERSECT."""

import random
from collections import Counter

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}

INTERSECT = "SELECT t.a, t.b FROM t WHERE t.c > 1 INTERSECT SELECT u.a, u.b FROM u"


def _encoded(union="UNION ALL", having="COUNT(*) = 2", second_group="u.a, u.b"):
    return (
        "SELECT s.a AS x, s.b AS y FROM ("
        "SELECT t.a AS a, t.b AS b, COUNT(*) AS n FROM t WHERE t.c > 1 GROUP BY t.a, t.b "
        f"{union} SELECT u.a AS a, u.b AS b, COUNT(*) AS n FROM u GROUP BY {second_group}"
        f") AS s GROUP BY s.a, s.b HAVING {having}"
    )


# Calcite's own form: the count filtered by WHERE over the grouped union, then projected away
CALCITE_SHAPE = (
    "SELECT q.a AS x, q.b AS y FROM (SELECT g.a AS a, g.b AS b, g.k AS k FROM ("
    "SELECT s.a AS a, s.b AS b, COUNT(*) AS k FROM ("
    "SELECT t.a AS a, t.b AS b, COUNT(*) AS n FROM t WHERE t.c > 1 GROUP BY t.a, t.b "
    "UNION ALL SELECT u.a AS a, u.b AS b, COUNT(*) AS n FROM u GROUP BY u.a, u.b) AS s "
    "GROUP BY s.a, s.b) AS g WHERE g.k = 2) AS q"
)


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False)


def _rows(db, *queries):
    return [Counter(rows) for rows in run_unoptimized(db, *(sqlglot.transpile(q, read="mysql", write="duckdb")[0] for q in queries))]


def _database(t_rows, u_rows):
    db = duckdb.connect()
    for table, rows in (("t", t_rows), ("u", u_rows)):
        db.execute(f"CREATE TABLE {table} (a INTEGER, b INTEGER, c INTEGER)")
        if rows:
            db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)
    return db


@pytest.mark.parametrize("encoded", [_encoded(), CALCITE_SHAPE])
def test_encoding_is_intersect(encoded):
    result = _prove(encoded, INTERSECT)
    assert result.proven, result.reason
    rng = random.Random(11)
    for _ in range(30):
        rows = [[tuple(rng.choice([None, 1, 2, 3]) for _ in range(3)) for _ in range(rng.randint(0, 6))] for _ in range(2)]
        left, right = _rows(_database(*rows), encoded, INTERSECT)
        assert left == right


@pytest.mark.parametrize(
    "encoded, t_rows, u_rows",
    [
        # UNION (distinct) merges the two branches' identical (a, b, n) rows, so the count is 1
        (_encoded(union="UNION"), [(1, 1, 2)], [(1, 1, 0)]),
        # a count of 1 keeps keys found in only one branch
        (_encoded(having="COUNT(*) = 1"), [(1, 1, 2)], []),
        # grouping u by (a, b, c) emits a key twice, so the count reaches 2 without t holding it
        (_encoded(second_group="u.a, u.b, u.c"), [], [(1, 1, 1), (1, 1, 2)]),
    ],
)
def test_near_misses_are_not_proven(encoded, t_rows, u_rows):
    assert not _prove(encoded, INTERSECT).proven
    left, right = _rows(_database(t_rows, u_rows), encoded, INTERSECT)
    assert left != right
