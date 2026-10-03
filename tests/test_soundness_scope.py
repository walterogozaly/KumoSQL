"""Wrong proofs from renames and inlining that ignored lexical scope, kept as regression cases.

Each pair returns different rows (DuckDB shows it on the database next to it), so neither prover may
call it equivalent: an inner ``WITH q`` hides an outer one, a derived table hides its base table's
other columns from a bare reference, ``UNNEST(..) AS a`` hides an outer alias ``a``, and an alias
column list reads the CTE in its own scope. Near misses that are equivalent stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.canonical import canonical_copy
from kumosql.smt_equivalence import prove_equivalent_smt

DDL = ["CREATE TABLE t (x INTEGER, y INTEGER)", "CREATE TABLE u (k INTEGER, x INTEGER)"]
ROWS = {"t": [(2, 4), (2, 5)], "u": [(1, 0)]}
UNNEST_LEFT = "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM UNNEST([STRUCT(1 AS x)]) AS a WHERE a.x = 2)"


def _bags_differ(left: str, right: str) -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for create in DDL:
        db.execute(create)
    for name, rows in ROWS.items():
        for row in rows:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    db.execute("PRAGMA disable_optimizer")  # DuckDB's optimizer misreads some correlated subqueries
    return Counter(db.execute(left).fetchall()) != Counter(db.execute(right).fetchall())


def _duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


# (left, right, DuckDB spelling of left and right when transpiling cannot write it, provers that proved it)
WRONG_PROOFS = [
    pytest.param(
        "WITH q AS (SELECT 1 AS x) SELECT d.x FROM (WITH q AS (SELECT 2 AS x) SELECT x FROM q) d",
        "SELECT d.x FROM (SELECT x FROM (SELECT 1 AS x) AS q) AS d",
        None,
        id="S009-004-inner-with-hides-the-outer-cte",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k FROM u) d WHERE x = 2)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE x = 2)",
        None,
        id="S009-007-derived-table-hides-a-column-from-a-bare-reference",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE a.x IN (SELECT x FROM (SELECT k FROM u) d)",
        "SELECT a.x FROM t AS a WHERE a.x IN (SELECT x FROM u AS d)",
        None,
        id="bare-column-of-an-in-subquery-reads-the-outer-row",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k + 1 AS k1 FROM u) d WHERE x = 2)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE x = 2)",
        None,
        id="computed-projection-hides-a-column-from-a-bare-reference",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k AS kk FROM u) d WHERE x = 2)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE x = 2)",
        None,
        id="renamed-projection-hides-a-column-from-a-bare-reference",
    ),
    pytest.param(
        UNNEST_LEFT,
        "SELECT _a1.x FROM t AS _a1 WHERE EXISTS(SELECT 1 FROM UNNEST([STRUCT(1 AS x)]) AS a WHERE _a1.x = 2)",
        (
            "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM (SELECT UNNEST([{'x': 1}], max_depth => 2)) AS a WHERE a.x = 2)",
            "SELECT _a1.x FROM t AS _a1 WHERE EXISTS(SELECT 1 FROM (SELECT UNNEST([{'x': 1}], max_depth => 2)) AS a WHERE _a1.x = 2)",
        ),
        id="S009-008-unnest-element-hides-the-outer-alias",
    ),
    pytest.param(
        "WITH q AS (SELECT 1 AS x, 2 AS y) SELECT d.a FROM (WITH q AS (SELECT 2 AS y, 1 AS x) SELECT * FROM q) AS z, q AS d(a)",
        "SELECT 2 AS a",
        None,
        id="alias-column-list-reads-the-cte-in-scope",
    ),
    pytest.param(
        "SELECT w.b FROM (SELECT d.y FROM (SELECT 1 AS x, 2 AS y) AS d(y, c)) AS w(b)",
        "SELECT 2 AS b",
        None,
        id="alias-column-list-inside-another-is-expanded",
    ),
]


@pytest.mark.parametrize("left,right,duck", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, duck):
    assert _bags_differ(*(duck or (_duckdb(left), _duckdb(right))))
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, dialect="bigquery", timeout_ms=3000).proven, prove.__name__


STILL_PROVEN = [
    pytest.param("WITH q AS (SELECT x FROM t WHERE y > 4) SELECT q.x FROM q", "SELECT x FROM t WHERE y > 4", None, id="cte"),
    pytest.param(
        "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT y.x FROM (WITH a AS (SELECT 2 AS x) SELECT x FROM b) y",
        "SELECT 1 AS x",
        None,
        id="cte-read-through-an-unshadowed-name",
    ),
    pytest.param("WITH q AS (SELECT x FROM t) SELECT d.x FROM (SELECT x FROM q) d", "SELECT x FROM t", None, id="cte-under-a-derived-table"),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k FROM u) d WHERE d.k = a.x)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE d.k = a.x)",
        None,
        id="qualified-correlation-through-a-derived-table",
    ),
    pytest.param("SELECT k FROM (SELECT k FROM u) d WHERE k > 1", "SELECT k FROM u WHERE k > 1", None, id="bare-column-the-derived-table-exposes"),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k FROM u) d WHERE x = 2)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE x = 2)",
        {"t": ["x", "y"], "u": ["k"]},
        id="bare-column-the-base-table-lacks",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM (SELECT k + 1 AS k1 FROM u) d WHERE d.k1 = a.x)",
        "SELECT a.x FROM t AS a WHERE EXISTS(SELECT 1 FROM u AS d WHERE d.k + 1 = a.x)",
        None,
        id="computed-projection-read-by-name",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM UNNEST([1, 2]) AS e WHERE e = a.x)",
        "SELECT b.x FROM t AS b WHERE EXISTS(SELECT 1 FROM UNNEST([1, 2]) AS e WHERE e = b.x)",
        None,
        id="outer-alias-renamed-past-an-unnest",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM UNNEST([1, 2]) AS a WHERE a = 2)",
        "SELECT b.x FROM t AS b WHERE EXISTS(SELECT 1 FROM UNNEST([1, 2]) AS e WHERE e = 2)",
        None,
        id="unnest-element-named-like-the-outer-alias",
    ),
    pytest.param(
        "SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM u AS a WHERE a.k = 2)",
        "SELECT b.x FROM t AS b WHERE EXISTS(SELECT 1 FROM u AS c WHERE c.k = 2)",
        None,
        id="inner-table-alias-shadows-the-outer-one",
    ),
    pytest.param("WITH q AS (SELECT 1 AS x, 2 AS y) SELECT d.a FROM q AS d(a)", "SELECT 1 AS a", None, id="alias-column-list-over-a-cte"),
]


@pytest.mark.parametrize("left,right,schema", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right, schema):
    assert prove_equivalent_algebraic(left, right, dialect="bigquery", timeout_ms=3000, schema=schema).proven


def test_the_inner_cte_is_read_where_it_hides_the_outer_one():
    assert normalize("WITH q AS (SELECT 1 AS x) SELECT d.x FROM (WITH q AS (SELECT 2 AS x) SELECT x FROM q) d") == "SELECT 2 AS x"


def test_canonical_renaming_stops_at_an_unnest_that_declares_the_alias():
    copy = canonical_copy(sqlglot.parse_one(UNNEST_LEFT, read="bigquery")).sql(dialect="bigquery")
    assert "WHERE a.x = 2" in copy and "FROM t AS _a1" in copy
    other = canonical_copy(sqlglot.parse_one(UNNEST_LEFT.replace("a.x = 2", "b.x = 2").replace("t a", "t b").replace("SELECT a.x", "SELECT b.x"), read="bigquery"))
    assert other.sql(dialect="bigquery") != copy  # the outer row's x is not the element's x
