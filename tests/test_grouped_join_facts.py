"""Integer key facts across inner joins: bags, empty sources and guarded boundaries."""
from collections import Counter
import pytest
import sqlglot
from kumosql.grouped_join_facts import propagate_grouped_join_facts
from kumosql.algebraic_equivalence import prove_equivalent_algebraic

TYPES = {"t": {"k": "INTEGER", "v": "INTEGER"}, "u": {"k": "INTEGER"}, "v": {"k": "INTEGER"}}
SCHEMA = {name: list(columns) for name, columns in TYPES.items()}


def rewrite(sql, types=TYPES):
    return propagate_grouped_join_facts(sqlglot.parse_one(sql, read="duckdb"), types)


def proves(left, right, types=TYPES):
    return prove_equivalent_algebraic(left, right, dialect="duckdb", schema=SCHEMA, types=types, compare_names=False, timeout_ms=2000).proven


@pytest.mark.parametrize("predicate", ["a.k > 7", "7 < a.k", "a.k <> 0", "a.k = 9"])
def test_filter_on_projected_group_key_transfers(predicate):
    left = f"SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE {predicate} GROUP BY a.k) g JOIN u b ON g.k=b.k"
    right = left + " WHERE " + predicate.replace("a.k", "b.k")
    assert rewrite(left) is not None
    assert proves(left, right)


def test_transitive_fact_crosses_projection_and_second_join():
    left = "SELECT d.k,c.k FROM (SELECT b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k) d JOIN v c ON d.k=c.k"
    right = "SELECT d.k,c.k FROM (SELECT b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k WHERE b.k>7) d JOIN v c ON d.k=c.k WHERE c.k>7"
    assert proves(left, right)


@pytest.mark.parametrize("sql", [
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g LEFT JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g RIGHT JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY ROLLUP(a.k)) g JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY CUBE(a.k)) g JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY GROUPING SETS ((a.k),())) g JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.v>7 GROUP BY a.k) g JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g(z) JOIN u b ON g.z=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k OR b.k=1",
])
def test_unsupported_boundaries_decline(sql):
    assert rewrite(sql) is None


def test_missing_or_coerced_types_decline():
    sql = "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k"
    assert rewrite(sql, {}) is None
    assert rewrite(sql, {"t": {"k": "INTEGER"}, "u": {"k": "VARCHAR"}}) is None


@pytest.mark.parametrize("sql", [
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a(v,k) WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k",
    "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b(v) ON g.k=b.v",
])
def test_physical_alias_column_lists_decline(sql):
    assert rewrite(sql) is None


@pytest.mark.parametrize("outer_alias", [False, True])
def test_alias_position_can_hide_a_noninjective_float_coercion(outer_alias):
    duckdb = pytest.importorskip("duckdb")
    if outer_alias:
        sql = "SELECT d.k,d.n,b.v FROM (SELECT f.k,COUNT(*) n FROM f WHERE f.k>9007199254740992 GROUP BY f.k) d JOIN b AS b(v,k) ON d.k=b.k"
        ddl = "CREATE TABLE f(k BIGINT,j BIGINT,v BIGINT);CREATE TABLE b(k BIGINT,v DOUBLE);INSERT INTO f VALUES (9007199254740993,1,5);INSERT INTO b VALUES (0,9007199254740992)"
        types = {"f":{"k":"BIGINT","j":"BIGINT","v":"BIGINT"},"b":{"k":"BIGINT","v":"DOUBLE"}}
        bad = sql+" WHERE b.k>9007199254740992"
    else:
        sql = "SELECT d.k,d.n,b.v FROM (SELECT f.k,COUNT(*) n FROM f AS f(v,j,k) WHERE f.k<=9007199254740992 GROUP BY f.k) d JOIN b ON d.k=b.k"
        ddl = "CREATE TABLE f(k BIGINT,j BIGINT,v DOUBLE);CREATE TABLE b(k BIGINT,v BIGINT);INSERT INTO f VALUES (0,1,9007199254740992);INSERT INTO b VALUES (9007199254740993,20)"
        types = {"f":{"k":"BIGINT","j":"BIGINT","v":"DOUBLE"},"b":{"k":"BIGINT","v":"BIGINT"}}
        bad = sql+" WHERE b.k<=9007199254740992"
    assert rewrite(sql,types) is None
    db=duckdb.connect()
    try:
        db.execute("SET threads=1");db.execute(ddl);db.execute("PRAGMA disable_optimizer")
        assert len(db.execute(sql).fetchall())==1
        assert db.execute(bad).fetchall()==[]
    finally:
        db.close()


@pytest.mark.parametrize("rows", [[], [(None,0)], [(8,1),(8,2),(9,None),(None,0),(7,3)]])
def test_rewrite_preserves_exact_bags(rows):
    duckdb = pytest.importorskip("duckdb")
    sql = "SELECT g.k,b.k FROM (SELECT a.k,COUNT(*) n FROM t a WHERE a.k>7 GROUP BY a.k) g JOIN u b ON g.k=b.k"
    changed = rewrite(sql)
    assert changed is not None
    db = duckdb.connect()
    try:
        db.execute("SET threads=1; CREATE TABLE t(k INTEGER,v INTEGER); CREATE TABLE u(k INTEGER)")
        if rows:
            db.executemany("INSERT INTO t VALUES (?,?)", rows)
        db.execute("INSERT INTO u VALUES (8),(8),(9),(NULL),(7)")
        db.execute("PRAGMA disable_optimizer")
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(changed.sql(dialect="duckdb")).fetchall())
    finally:
        db.close()


def test_outer_padding_is_a_real_near_miss():
    duckdb = pytest.importorskip("duckdb")
    left = "SELECT g.k,b.k FROM (SELECT a.k FROM t a WHERE a.k>7 GROUP BY a.k) g LEFT JOIN u b ON g.k=b.k"
    right = left + " WHERE b.k>7"
    assert not proves(left, right)
    db = duckdb.connect()
    try:
        db.execute("SET threads=1; CREATE TABLE t(k INTEGER,v INTEGER); CREATE TABLE u(k INTEGER); INSERT INTO t VALUES (8,1); PRAGMA disable_optimizer")
        assert db.execute(left).fetchall() == [(8,None)]
        assert db.execute(right).fetchall() == []
    finally:
        db.close()


def test_different_window_null_orderings_decline_rule():
    for order in ("FIRST", "LAST"):
        sql = f"SELECT a.k,ROW_NUMBER() OVER (ORDER BY a.v NULLS {order}) rn FROM t a"
        assert rewrite(sql) is None


@pytest.mark.parametrize("source", [
    "b PIVOT (AVG(v) FOR k IN (1 AS k)) AS b",
    "(SELECT b.k,b.j,b.v FROM b) PIVOT (AVG(v) FOR k IN (1 AS k)) AS b",
])
def test_pivot_output_cannot_inherit_the_physical_integer_type(source):
    duckdb=pytest.importorskip("duckdb")
    sql="SELECT d.k,d.n,b.k FROM (SELECT f.k,COUNT(*) n FROM f WHERE f.k>9007199254740992 GROUP BY f.k) d JOIN "+source+" ON d.k=b.k"
    types={"f":{"k":"BIGINT","j":"BIGINT","v":"BIGINT"},"b":{"k":"BIGINT","j":"BIGINT","v":"DOUBLE"}}
    assert rewrite(sql,types) is None
    db=duckdb.connect()
    try:
        db.execute("SET threads=1;CREATE TABLE f(k BIGINT,j BIGINT,v BIGINT);CREATE TABLE b(k BIGINT,j BIGINT,v DOUBLE);INSERT INTO f VALUES (9007199254740993,1,5);INSERT INTO b VALUES (1,0,9007199254740992);PRAGMA disable_optimizer")
        assert len(db.execute(sql).fetchall())==1
        assert db.execute(sql+" WHERE b.k>9007199254740992").fetchall()==[]
    finally:db.close()
