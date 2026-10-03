"""A LEFT JOIN whose padded rows a later outer join's ON clause rejects is made inner.

Proved pairs are also run on random DuckDB databases (NULLs, duplicates, empty tables); every
near miss has a DuckDB witness, confirmed with the optimizer off, and must stay unproven.
"""

import random
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.outer_on_rejection import strengthen_under_outer_on

SCHEMA = {"a": ["k", "v"], "b": ["k", "x"], "d": ["k", "y"]}


def _witness(left: str, right: str, trials: int = 200) -> bool:
    """A random database where the two queries return different bags, confirmed with DuckDB's optimizer off."""

    rng = random.Random(11)
    con = duckdb.connect()
    for table, columns in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(c + ' INTEGER' for c in columns)})")
    for _ in range(trials):
        for table, columns in SCHEMA.items():
            con.execute(f"DELETE FROM {table}")
            for _ in range(rng.randint(0, 4)):
                values = [rng.choice([None, 0, 1, 5, 12]) for _ in columns]
                con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", values)
        if Counter(con.execute(left).fetchall()) != Counter(con.execute(right).fetchall()):
            a, b = run_unoptimized(con, left, right)
            if Counter(a) != Counter(b):
                return True
    return False


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def _rule(sql: str) -> str | None:
    out = strengthen_under_outer_on(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


EQUIVALENT = [
    pytest.param(
        "SELECT a.v, d.y FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k",
        "SELECT a.v, d.y FROM a JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k",
        id="right-join-on-rejects-left-join-padding",
    ),
    pytest.param(
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, b.x + 1 AS bx FROM a LEFT JOIN b ON a.k = b.k) AS t ON t.bx = d.k",
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, b.x + 1 AS bx FROM a JOIN b ON a.k = b.k) AS t ON t.bx = d.k",
        id="left-join-on-rejects-derived-padding",
    ),
    pytest.param(
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, (b.x > 1) AS c FROM a LEFT JOIN b ON b.x > 1) AS t ON t.c",
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, (b.x > 1) AS c FROM a JOIN b ON b.x > 1) AS t ON t.c",
        id="boolean-projection-read-as-on",
    ),
]

NOT_EQUIVALENT = [
    pytest.param(
        "SELECT a.v, d.y FROM a LEFT JOIN b ON a.k = b.k FULL JOIN d ON b.x > d.k",
        "SELECT a.v, d.y FROM a JOIN b ON a.k = b.k FULL JOIN d ON b.x > d.k",
        id="full-join-keeps-the-padded-rows",
    ),
    pytest.param(
        "SELECT a.v, d.y FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON b.x IS NULL",
        "SELECT a.v, d.y FROM a JOIN b ON a.k = b.k RIGHT JOIN d ON b.x IS NULL",
        id="on-accepts-the-padded-rows",
    ),
    pytest.param(
        "SELECT a.v, d.y FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k OR a.v = d.k",
        "SELECT a.v, d.y FROM a JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k OR a.v = d.k",
        id="disjunction-on-the-preserved-side",
    ),
    pytest.param(
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, b.x AS bx FROM a LEFT JOIN b ON a.k = b.k) AS t ON t.v = d.k",
        "SELECT d.y, t.v FROM d LEFT JOIN (SELECT a.v, b.x AS bx FROM a JOIN b ON a.k = b.k) AS t ON t.v = d.k",
        id="on-reads-only-the-derived-preserved-side",
    ),
    pytest.param(
        "SELECT t.v, d.y FROM (SELECT a.v, b.x AS bx FROM a LEFT JOIN b ON a.k = b.k) AS t LEFT JOIN d ON t.bx > d.k",
        "SELECT t.v, d.y FROM (SELECT a.v, b.x AS bx FROM a JOIN b ON a.k = b.k) AS t LEFT JOIN d ON t.bx > d.k",
        id="derived-on-the-preserved-side",
    ),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_proved_and_agrees_with_duckdb(left, right):
    assert not _witness(left, right)
    assert _proven(left, right)


@pytest.mark.parametrize("left, right", NOT_EQUIVALENT)
def test_near_miss_has_a_witness_and_stays_unproven(left, right):
    assert _witness(left, right)
    assert not _proven(left, right)


def test_rule_makes_the_rejected_left_join_inner():
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k") == "SELECT a.v FROM a JOIN b ON a.k = b.k RIGHT JOIN d ON b.x > d.k"
    assert _rule("SELECT t.v FROM d LEFT JOIN (SELECT a.v, b.x AS bx FROM a LEFT JOIN b ON a.k = b.k) AS t ON t.bx = d.k") == (
        "SELECT t.v FROM d LEFT JOIN (SELECT a.v, b.x AS bx FROM a JOIN b ON a.k = b.k) AS t ON t.bx = d.k"
    )


def test_rule_leaves_preserved_or_unrejected_sides():
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k FULL JOIN d ON b.x > d.k") is None
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON b.x IS NULL") is None
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN d ON COALESCE(b.x, 0) = d.k") is None
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN d ON b.x > d.k") is None
    # a RIGHT or FULL join between the LEFT join and the rejecting RIGHT join decides its own padding
    assert _rule("SELECT a.v FROM a LEFT JOIN b ON a.k = b.k FULL JOIN a AS e ON e.k = a.k RIGHT JOIN d ON b.x > d.k") is None
    assert _rule("SELECT t.v FROM (SELECT a.v, b.x AS bx FROM a LEFT JOIN b ON a.k = b.k) AS t LEFT JOIN d ON t.bx > d.k") is None
    assert _rule("SELECT a.v FROM a LEFT JOIN b USING (k) RIGHT JOIN d ON b.x > d.k") is None
