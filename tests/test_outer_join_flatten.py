"""Derived tables that hold an outer join: flattening, lifting computed columns, and their limits.

Every proved pair is also run on random DuckDB databases (NULLs, duplicates, empty tables); a
pair that is not equivalent must stay unproven.
"""

import random
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.outer_join_flatten import null_when_inputs_null

SCHEMA = {"a": ["k", "x"], "b": ["k", "y"], "c": ["k", "z"]}


def _differ(left: str, right: str, trials: int = 150) -> bool:
    rng = random.Random(7)
    con = duckdb.connect()
    for table, columns in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(c + ' INTEGER' for c in columns)})")
    for _ in range(trials):
        for table, columns in SCHEMA.items():
            con.execute(f"DELETE FROM {table}")
            for _ in range(rng.randint(0, 4)):
                values = [rng.choice([None, 0, 1, 2, 12]) for _ in columns]
                con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", values)
        if Counter(con.execute(left).fetchall()) != Counter(con.execute(right).fetchall()):
            return True
    return False


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False).proven


EQUIVALENT = [
    pytest.param(
        "SELECT d.x, d.y FROM (SELECT a.x AS x, b.y AS y FROM a LEFT JOIN b ON a.k = b.k) AS d WHERE d.y IS NOT NULL",
        "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k WHERE b.y IS NOT NULL",
        id="null-rejecting-filter-over-derived-left-join",
    ),
    pytest.param(
        "SELECT d.x FROM (SELECT a.x AS x, b.y AS y FROM a LEFT JOIN b ON a.k = b.k WHERE a.x > 1) AS d WHERE d.y < 5 OR d.y IS NULL",
        "SELECT a.x FROM a LEFT JOIN b ON a.k = b.k WHERE a.x > 1 AND (b.y < 5 OR b.y IS NULL)",
        id="filters-inside-and-above",
    ),
    pytest.param(
        "SELECT d.v FROM (SELECT CASE WHEN b.y < 2 THEN 0 ELSE b.y END AS v FROM a LEFT JOIN b ON a.k = b.k) AS d",
        "SELECT CASE WHEN b.y < 2 THEN 0 ELSE b.y END AS v FROM a LEFT JOIN b ON a.k = b.k",
        id="computed-column-of-sole-source",
    ),
    pytest.param(
        "SELECT d.v, COUNT(*) FROM (SELECT CASE WHEN a.x < 2 THEN 2 ELSE a.x END AS v FROM a LEFT JOIN b ON a.k = b.k) AS d GROUP BY d.v",
        "SELECT CASE WHEN a.x < 2 THEN 2 ELSE a.x END AS v, COUNT(*) FROM a LEFT JOIN b ON a.k = b.k GROUP BY CASE WHEN a.x < 2 THEN 2 ELSE a.x END",
        id="grouped-over-computed-column",
    ),
    pytest.param(
        "SELECT d.x, c.z FROM (SELECT a.x AS x, a.k AS k, b.y AS y FROM a LEFT JOIN b ON a.k = b.k) AS d LEFT JOIN c ON d.k = c.k",
        "SELECT a.x, c.z FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON a.k = c.k",
        id="left-left-chain",
    ),
    pytest.param(
        "SELECT d.x, c.z FROM (SELECT a.x AS x, a.k + 1 AS k1 FROM a LEFT JOIN b ON a.k = b.k) AS d JOIN c ON d.k1 = c.k",
        "SELECT d.x, c.z FROM (SELECT a.x AS x, a.k AS k FROM a LEFT JOIN b ON a.k = b.k) AS d JOIN c ON d.k + 1 = c.k",
        id="lift-from-preserved-derived",
    ),
    pytest.param(
        "SELECT c.z, d.v FROM c LEFT JOIN (SELECT a.k AS k, b.y + 1 AS v FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        "SELECT c.z, d.y + 1 FROM c LEFT JOIN (SELECT a.k AS k, b.y AS y FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        id="lift-strict-expression-from-padded-side",
    ),
    pytest.param(
        "SELECT b.y FROM a RIGHT OUTER JOIN b ON a.k = b.k",
        "SELECT b.y FROM b LEFT JOIN a ON a.k = b.k",
        id="right-outer-join-mirrored",
    ),
    pytest.param(
        "SELECT d.x, b.y FROM (SELECT a.x AS x, a.k AS k FROM a WHERE FALSE) AS d FULL JOIN b ON d.k = b.k",
        "SELECT NULL AS x, b.y FROM b",
        id="full-join-with-empty-side",
    ),
    pytest.param(
        "SELECT a.x, b.y, c.z FROM a JOIN (b CROSS JOIN c) ON a.k = b.k AND b.k = c.k",
        "SELECT a.x, b.y, c.z FROM a JOIN b ON a.k = b.k JOIN c ON b.k = c.k",
        id="parenthesized-inner-join-tree",
    ),
]

NOT_EQUIVALENT = [
    pytest.param(
        "SELECT c.z, d.v FROM c LEFT JOIN (SELECT a.k AS k, COALESCE(b.y, 0) AS v FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        "SELECT c.z, COALESCE(d.y, 0) FROM c LEFT JOIN (SELECT a.k AS k, b.y AS y FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        id="coalesce-on-padded-side",
    ),
    pytest.param(
        "SELECT c.z, d.v FROM c LEFT JOIN (SELECT a.k AS k, CASE WHEN b.y IS NULL THEN 1 END AS v FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        "SELECT c.z, CASE WHEN d.y IS NULL THEN 1 END FROM c LEFT JOIN (SELECT a.k AS k, b.y AS y FROM a LEFT JOIN b ON a.k = b.k) AS d ON c.k = d.k",
        id="case-on-padded-side",
    ),
    pytest.param(
        "SELECT d.x, c.z FROM (SELECT a.x AS x, a.k AS k FROM a LEFT JOIN b ON a.k = b.k WHERE a.x > 1) AS d RIGHT JOIN c ON d.k = c.k",
        "SELECT a.x, c.z FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN c ON a.k = c.k WHERE a.x > 1",
        id="filter-moved-above-right-join",
    ),
    pytest.param(
        "SELECT d.f FROM (SELECT a.x AS x, b.y IS NULL AS f FROM a LEFT JOIN b ON a.k = b.k) AS d RIGHT JOIN c ON d.x = c.k",
        "SELECT b.y IS NULL FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN c ON a.x = c.k",
        id="is-null-flattened-under-right-join",
    ),
    pytest.param(
        "SELECT a.x, b.y FROM a LEFT JOIN (b JOIN c ON b.k = c.k) ON a.k = b.k",
        "SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k JOIN c ON b.k = c.k",
        id="outer-join-to-a-join-tree-is-not-left-deep",
    ),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_proved_and_agrees_with_duckdb(left, right):
    assert _proven(left, right)
    assert not _differ(left, right)


@pytest.mark.parametrize("left, right", NOT_EQUIVALENT)
def test_not_equivalent_stays_unproven(left, right):
    assert _differ(left, right)
    assert not _proven(left, right)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("b.y + 1", True),
        ("b.y < 2", True),
        ("CASE WHEN b.y < 2 THEN 0 ELSE b.y END", True),
        ("CASE WHEN b.y < 2 THEN NULL ELSE -b.y END", True),
        ("CASE WHEN b.y IS NULL THEN 1 END", False),
        ("CASE WHEN b.y < 2 THEN 1 END", True),
        ("CASE b.y WHEN 1 THEN 5 END", True),
        ("COALESCE(b.y, 0)", False),
        ("b.y IS NULL", False),
        ("1", False),
        ("b.y > 1 OR TRUE", False),
        ("b.y > 1 AND b.k < 2", True),
    ],
)
def test_null_when_inputs_null(sql, expected):
    assert null_when_inputs_null(sqlglot.parse_one(sql)) is expected
