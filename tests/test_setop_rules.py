import random
from collections import Counter

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.setop_rules import distinct_rows, normalize_set_operations

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False)


def _same_on_random_data(left, right, trials=40):
    rng = random.Random(7)
    for _ in range(trials):
        db = duckdb.connect()
        for table in SCHEMA:
            db.execute(f"CREATE TABLE {table} (a INTEGER, b INTEGER, c INTEGER)")
            rows = [tuple(rng.choice([None, 1, 2, 3]) for _ in range(3)) for _ in range(rng.randint(0, 6))]
            if rows:
                db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)
        bags = [Counter(db.execute(sqlglot.transpile(sql, read="mysql", write="duckdb")[0]).fetchall()) for sql in (left, right)]
        if bags[0] != bags[1]:
            return False
    return True


EQUIVALENT = [
    # an identity projection over a derived set operation is the set operation
    ("SELECT s.a AS a, s.b AS b FROM (SELECT t.a AS a, t.b AS b FROM t INTERSECT SELECT u.a AS a, u.b AS b FROM u) AS s",
     "SELECT t.a AS a, t.b AS b FROM t INTERSECT SELECT u.a AS a, u.b AS b FROM u"),
    ("SELECT * FROM (SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u) AS s",
     "SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u"),
    # DISTINCT over UNION ALL / INTERSECT ALL with identity aliases
    ("SELECT DISTINCT s.a AS a FROM (SELECT t.a AS a FROM t UNION ALL SELECT u.a AS a FROM u) AS s",
     "SELECT t.a AS a FROM t UNION SELECT u.a AS a FROM u"),
    ("SELECT DISTINCT s.a AS a FROM (SELECT t.a AS a FROM t INTERSECT ALL SELECT u.a AS a FROM u) AS s",
     "SELECT t.a AS a FROM t INTERSECT SELECT u.a AS a FROM u"),
    # ALL forms whose operand has no repeated rows
    ("SELECT DISTINCT t.a AS a FROM t INTERSECT ALL SELECT u.a AS a FROM u",
     "SELECT t.a AS a FROM t INTERSECT SELECT u.a AS a FROM u"),
    ("SELECT t.a AS a FROM t INTERSECT ALL SELECT u.a AS a FROM u GROUP BY u.a",
     "SELECT t.a AS a FROM t INTERSECT SELECT u.a AS a FROM u"),
    ("SELECT t.a AS a FROM t GROUP BY t.a EXCEPT ALL SELECT u.a AS a FROM u",
     "SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u"),
    # a set difference of a query with itself is empty, whatever the aliases
    ("SELECT t.a AS a, 1 AS k FROM t EXCEPT ALL SELECT x.a AS y, CAST(1 AS SIGNED) AS z FROM t AS x",
     "SELECT t.a AS a, 1 AS k FROM t WHERE FALSE"),
    # nothing to take away
    ("SELECT t.a AS a FROM t EXCEPT ALL SELECT u.a AS a FROM u WHERE FALSE", "SELECT t.a AS a FROM t"),
    ("SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u WHERE FALSE", "SELECT DISTINCT t.a AS a FROM t"),
    # filters of one table that output the same columns
    ("SELECT t.a AS a, t.b AS b FROM t WHERE t.c = 1 UNION SELECT t.a AS a, t.b AS b FROM t WHERE t.b = 2",
     "SELECT DISTINCT t.a AS a, t.b AS b FROM t WHERE t.c = 1 OR t.b = 2"),
    ("SELECT t.a AS a, t.b AS b FROM t WHERE t.c = 1 INTERSECT SELECT x.a AS a, x.b AS b FROM t AS x WHERE x.b = 2",
     "SELECT DISTINCT t.a AS a, t.b AS b FROM t WHERE t.c = 1 AND t.b = 2"),
    ("SELECT t.a AS a, t.b AS b FROM t WHERE t.c = 1 EXCEPT SELECT x.a AS a, x.b AS b FROM t AS x WHERE x.b = 2",
     "SELECT DISTINCT t.a AS a, t.b AS b FROM t WHERE t.c = 1 AND NOT COALESCE(t.b = 2, FALSE)"),
    # at the top level an unqualified name is the table's own column, so both spellings meet
    ("SELECT * FROM t WHERE a < 3 UNION SELECT * FROM t WHERE a > 1", "SELECT t.a, t.b, t.c FROM t WHERE t.a > 1 UNION SELECT t.a, t.b, t.c FROM t WHERE t.a < 3"),
]

DIFFERENT = [
    # EXCEPT ALL keeps repeats when the left operand has them
    ("SELECT t.a AS a FROM t EXCEPT ALL SELECT u.a AS a FROM u", "SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u"),
    # DISTINCT over EXCEPT ALL is not EXCEPT
    ("SELECT DISTINCT s.a AS a FROM (SELECT t.a AS a FROM t EXCEPT ALL SELECT u.a AS a FROM u) AS s",
     "SELECT t.a AS a FROM t EXCEPT SELECT u.a AS a FROM u"),
    # a GROUP BY on a column it does not output can repeat rows
    ("SELECT t.a AS a FROM t GROUP BY t.a, t.b INTERSECT ALL SELECT u.a AS a FROM u",
     "SELECT t.a AS a FROM t INTERSECT SELECT u.a AS a FROM u"),
    # a reordered projection is not the identity
    ("SELECT s.b AS a, s.a AS b FROM (SELECT t.a AS a, t.b AS b FROM t UNION SELECT u.a AS a, u.b AS b FROM u) AS s",
     "SELECT t.a AS a, t.b AS b FROM t UNION SELECT u.a AS a, u.b AS b FROM u"),
    # the right filter reads a column the output drops: another row of t can witness it
    ("SELECT t.a AS a FROM t WHERE t.b = 1 INTERSECT SELECT x.a AS a FROM t AS x WHERE x.c = 2",
     "SELECT DISTINCT t.a AS a FROM t WHERE t.b = 1 AND t.c = 2"),
    ("SELECT t.a AS a FROM t WHERE t.b = 1 EXCEPT SELECT x.a AS a FROM t AS x WHERE x.c = 2",
     "SELECT DISTINCT t.a AS a FROM t WHERE t.b = 1 AND NOT COALESCE(t.c = 2, FALSE)"),
    # different operands do not cancel
    ("SELECT t.a AS a FROM t EXCEPT ALL SELECT t.b AS a FROM t", "SELECT t.a AS a FROM t WHERE FALSE"),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_set_operation_identities_prove_and_hold_on_data(left, right):
    assert _same_on_random_data(left, right)
    assert _prove(left, right).proven


@pytest.mark.parametrize("left, right", DIFFERENT)
def test_different_set_operations_are_not_proven(left, right):
    assert not _same_on_random_data(left, right)
    assert not _prove(left, right).proven


def test_self_difference_with_random_values_is_not_empty():
    tree = normalize_set_operations(sqlglot.parse_one("SELECT RAND() AS r FROM t EXCEPT ALL SELECT RAND() AS r FROM t", read="mysql"))
    assert "EXCEPT" in tree.sql()


@pytest.mark.parametrize("sql, expected", [
    ("SELECT DISTINCT a FROM t", True),
    ("SELECT a, b FROM t GROUP BY a, b", True),
    ("SELECT a FROM t GROUP BY a, b", False),
    ("SELECT a FROM t GROUP BY 1", False),
    ("SELECT COUNT(*) FROM t", True),
    ("SELECT s.a FROM (SELECT a, b FROM t GROUP BY a) AS s WHERE s.b > 1", True),
    ("SELECT s.b FROM (SELECT a, b FROM t GROUP BY a) AS s", False),
    ("SELECT a FROM t UNION ALL SELECT a FROM u", False),
    ("SELECT a FROM t EXCEPT ALL SELECT a FROM u", False),
    ("SELECT DISTINCT a FROM t EXCEPT ALL SELECT a FROM u", True),
    ("SELECT a FROM t INTERSECT ALL SELECT DISTINCT a FROM u", True),
])
def test_distinct_rows(sql, expected):
    assert distinct_rows(sqlglot.parse_one(sql, read="mysql")) is expected
