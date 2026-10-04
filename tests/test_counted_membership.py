"""Counted membership identities and refusal boundaries, with bag witnesses."""

from collections import Counter

import duckdb
import pytest
import sqlglot
from kumosql.counted_membership import drop_equal_count_guard, nonnull_not_in
from kumosql.passthrough_sources import remove_passthrough_sources


SCHEMA = {"a": ["x", "g"], "b": ["y", "g"]}
NN = {"a": frozenset({"x"}), "b": frozenset({"y"})}
GUARD = "SELECT a.x FROM a LEFT JOIN (SELECT g,COUNT(*) c,COUNT(y) ck FROM b GROUP BY g) q ON a.g=q.g WHERE q.c=0 OR (EXISTS(SELECT 1 FROM b z WHERE z.y=a.x AND z.g=a.g) OR q.ck<q.c) IS NOT TRUE"


@pytest.mark.parametrize(
    "rows_a,rows_b",
    [
        ([], []),
        ([(1, "a")], []),
        ([(1, "a"), (1, "a")], [(2, "a")]),
        ([(1, "a"), (2, "b"), (3, None)], [(1, "a"), (4, "a"), (2, "b")]),
        ([(1, "a"), (2, "a")], [(1, "a"), (1, "a"), (3, "a")]),
        ([(1, "a")], [(1, None), (2, "b")]),
    ],
)
def test_equal_count_guard_preserves_padding_and_bags(rows_a, rows_b):
    original = sqlglot.parse_one(GUARD, read="duckdb")
    candidate = drop_equal_count_guard(original, NN, set())
    assert candidate is not None
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    if rows_a:
        db.executemany("INSERT INTO a VALUES (?,?)", rows_a)
    if rows_b:
        db.executemany("INSERT INTO b VALUES (?,?)", rows_b)
    assert Counter(db.execute(GUARD).fetchall()) == Counter(
        db.execute(candidate.sql(dialect="duckdb")).fetchall()
    )


@pytest.mark.parametrize(
    "query",
    [
        GUARD.replace("COUNT(y)", "COUNT(DISTINCT y)"),
        GUARD.replace("GROUP BY g", "GROUP BY g,y"),
        GUARD.replace("GROUP BY g", "GROUP BY ROLLUP(g)"),
        GUARD.replace("ON a.g=q.g", "ON a.g=q.g AND a.x>0"),
        GUARD.replace("SELECT a.x FROM", "SELECT a.x,q.c FROM"),
        GUARD.replace("SELECT a.x FROM", "SELECT * FROM"),
        GUARD.replace("q.c=0 OR", "q.c=2 OR"),
        GUARD.replace("FROM b GROUP", "FROM other.b GROUP"),
    ],
)
def test_count_guard_refuses_changed_premises(query):
    assert (
        drop_equal_count_guard(sqlglot.parse_one(query, read="duckdb"), NN, set())
        is None
    )


def test_nullable_count_not_treated_as_count_star():
    assert (
        drop_equal_count_guard(
            sqlglot.parse_one(GUARD, read="duckdb"), {"a": {"x"}}, set()
        )
        is None
    )


def test_exists_with_distinct_string_literals_not_conflated():
    query = GUARD.replace(
        "q.c=0 OR", "EXISTS(SELECT 1 FROM b s WHERE s.g='A') OR q.c=0 OR"
    )
    assert (
        drop_equal_count_guard(sqlglot.parse_one(query, read="duckdb"), NN, set())
        is None
    )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT p.x FROM (SELECT x FROM a) p",
        "SELECT x FROM (SELECT x FROM a) p WHERE x IN (SELECT y FROM b)",
        "SELECT p.x FROM b LEFT JOIN (SELECT x FROM a) p ON b.y=p.x",
    ],
)
def test_passthrough_preserves_same_named_reads(query):
    result = remove_passthrough_sources(sqlglot.parse_one(query), SCHEMA)
    assert "(SELECT x FROM a)" not in result.sql()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM (SELECT x FROM a) p",
        "SELECT p.x FROM (SELECT x FROM a) p WHERE EXISTS(SELECT 1 FROM b WHERE g=p.x)",
        "SELECT p.z FROM (SELECT x AS z FROM a) p",
        "SELECT p.x FROM (SELECT DISTINCT x FROM a) p",
        "SELECT p.x FROM (SELECT x FROM a LIMIT 1) p",
        "SELECT p.x FROM (SELECT x FROM other.a) p",
        "SELECT p.g FROM (SELECT x FROM a) p",
    ],
)
def test_passthrough_refuses_capture_and_observable_extras(query):
    tree = sqlglot.parse_one(query)
    before = tree.sql()
    assert remove_passthrough_sources(tree, SCHEMA).sql() == before


def test_not_in_requires_both_nonnull_values():
    query = "SELECT a.x FROM a WHERE a.x NOT IN (SELECT b.y FROM b WHERE b.g=a.g)"
    tree = sqlglot.parse_one(query)
    assert "EXISTS" in nonnull_not_in(tree, NN).sql()
    tree = sqlglot.parse_one(query)
    assert "EXISTS" not in nonnull_not_in(tree, {"a": {"x"}}).sql()


def test_grouped_collation_contract_is_explicit():
    from kumosql.counted_membership import GROUP_EQUALITY_ASSUMPTION

    parsed = sqlglot.parse_one(GUARD, read="duckdb")
    assert drop_equal_count_guard(parsed, NN) is None
    assumptions = set()
    assert drop_equal_count_guard(parsed, NN, assumptions) is not None
    assert assumptions == {GROUP_EQUALITY_ASSUMPTION}
    assert drop_equal_count_guard(parsed, NN) is None
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR COLLATE NOCASE)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    db.execute("INSERT INTO a VALUES (9,'a')")
    db.execute("INSERT INTO b VALUES (1,'a'),(2,'A')")
    candidate = drop_equal_count_guard(parsed, NN, set())
    assert Counter(db.execute(GUARD).fetchall()) != Counter(
        db.execute(candidate.sql(dialect="duckdb")).fetchall()
    )


@pytest.mark.parametrize("query", [
    "SELECT COUNT(*) FROM (SELECT x FROM a) p",
    "SELECT p.x FROM (SELECT x FROM a) p GROUP BY p.x",
    "SELECT ROW_NUMBER() OVER(ORDER BY p.x) FROM (SELECT x FROM a) p",
    "SELECT COUNT(*),(SELECT p.x FROM (SELECT x FROM a) p LIMIT 1) FROM b",
])
def test_passthrough_keeps_aggregate_and_window_ancestor_boundaries(query):
    tree = sqlglot.parse_one(query)
    before = tree.sql()
    assert remove_passthrough_sources(tree, SCHEMA).sql() == before


def test_empty_sqlite_aggregate_projected_in_is_not_row_exists():
    import sqlite3
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import TableConstraints
    original = "WITH q AS(SELECT x AS x FROM t) SELECT COUNT(*),x IN(SELECT y FROM u) AS d FROM q"
    changed = "SELECT COUNT(*),EXISTS(SELECT 1 FROM u WHERE y=t.x) AS d FROM t"
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(x INTEGER NOT NULL)")
    db.execute("CREATE TABLE u(y INTEGER NOT NULL)")
    db.execute("INSERT INTO u VALUES(1)")
    assert db.execute(original).fetchall() == [(0, None)]
    assert db.execute(changed).fetchall() == [(0, 0)]
    result = prove_equivalent_algebraic(
        original, changed, dialect="sqlite", compare_names=False,
        schema={"t":["x"],"u":["y"]}, types={"t":{"x":"INT"},"u":{"y":"INT"}},
        constraints={"t":TableConstraints(frozenset({"x"})),"u":TableConstraints(frozenset({"y"}))},
    )
    assert not result.proven


def test_nonnull_not_in_keeps_empty_aggregate_output_phase():
    import sqlite3
    original = "SELECT COUNT(*),x NOT IN(SELECT y FROM b) FROM a"
    tree = sqlglot.parse_one(original, read="sqlite")
    before = tree.sql()
    assert nonnull_not_in(tree, NN).sql() == before
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g TEXT)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g TEXT)")
    db.execute("INSERT INTO b VALUES(1,'a')")
    assert db.execute(original).fetchall() == [(0, None)]
    assert db.execute("SELECT COUNT(*),NOT EXISTS(SELECT 1 FROM b WHERE y=a.x) FROM a").fetchall() == [(0, 1)]
