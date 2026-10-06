"""``_drop_implied_exists`` drops a grouped derived table's EXISTS only when it reads the grouping column itself."""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic

SCHEMA = {"t": ["id", "x", "y"], "u": ["k", "w"], "p": ["id", "tid"]}
OUTER = "WHERE EXISTS (SELECT 1 FROM u WHERE u.k = p.tid)"


def _query(source: str, test: str, group: str = "t.y", where: str = "") -> str:
    return (
        f"SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t2.x) AS s FROM {source} "
        f"WHERE {test}{where} GROUP BY {group}) AS g ON p.tid = g.k {OUTER}"
    )


def _rows(sql: str):
    con = duckdb.connect()
    con.execute("PRAGMA disable_optimizer")
    con.execute("CREATE TABLE t (id INT, x INT, y INT); CREATE TABLE u (k INT, w INT); CREATE TABLE p (id INT, tid INT)")
    con.execute("INSERT INTO t VALUES (1, 4, 5), (2, 3, 9), (3, 3, 5)")
    con.execute("INSERT INTO u VALUES (5, 0)")
    con.execute("INSERT INTO p VALUES (1, 5)")
    return sorted(con.execute(sql).fetchall(), key=repr)


def test_a_test_on_another_source_of_the_derived_table_is_kept():
    # t2.y is not the grouping column t.y: the groups' sums depend on which t2 rows have a partner in u.
    source = "t JOIN t AS t2 ON t2.id = t.x - 2"
    kept = _query(source, "EXISTS (SELECT 1 FROM u WHERE u.k = t2.y)")
    dropped = _query(source, "TRUE")
    assert _rows(kept) != _rows(dropped)
    assert not prove_equivalent_algebraic(kept, dropped, schema=SCHEMA, dialect="bigquery").proven


def test_a_test_on_the_grouping_column_itself_is_still_dropped():
    source = "t JOIN t AS t2 ON t2.id = t.x - 2"
    kept = _query(source, "EXISTS (SELECT 1 FROM u WHERE u.k = t.y)")
    dropped = _query(source, "TRUE")
    assert _rows(kept) == _rows(dropped)
    assert prove_equivalent_algebraic(kept, dropped, schema=SCHEMA, dialect="bigquery").proven
    single = "SELECT p.id, g.s FROM p JOIN (SELECT t.y AS k, SUM(t.x) AS s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) GROUP BY t.y) AS g ON p.tid = g.k "
    assert prove_equivalent_algebraic(
        single + OUTER, single.replace("WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y) ", "") + OUTER, schema=SCHEMA, dialect="bigquery"
    ).proven
