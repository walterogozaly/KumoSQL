"""Scoped set-tree identity and the semantic distinctions it must retain."""
from collections import Counter

import duckdb
import pytest

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.structural_identity import same_scoped_query
from kumosql.set_identity import expose_set_identity
import sqlglot

SCHEMA = {"t": ["x", "k"], "u": ["x", "k"]}

def mixed(a, b, c, op="UNION ALL", cut=""):
    return (f"SELECT * FROM (SELECT {a}.x FROM t {a} WHERE {a}.k=10 "
            f"UNION SELECT {b}.x FROM t {b} WHERE {b}.k=20) d "
            f"{op} SELECT {c}.x FROM t {c} WHERE {c}.k=30 {cut}")

def same(left, right, schema=SCHEMA):
    trees=[expose_set_identity(sqlglot.parse_one(q,read="duckdb"),schema).sql(dialect="duckdb") for q in (left,right)]
    return same_scoped_query(*trees,schema=schema,dialect="duckdb",compare_names=False)

@pytest.mark.parametrize("rows", [[], [(None,10),(None,20),(None,30)],
    [(1,10),(1,10),(1,20),(1,30),(1,30)], [(2,10),(3,20),(4,30)]])
def test_mixed_union_alias_identity_on_bags(rows):
    left,right=mixed("a","b","c"),mixed("aa","bb","cc")
    assert same(left,right)
    assert prove_equivalent_algebraic(left,right,schema=SCHEMA,dialect="duckdb",compare_names=False).proven
    db=duckdb.connect();db.execute("CREATE TABLE t(x BIGINT,k BIGINT)")
    if rows:db.executemany("INSERT INTO t VALUES (?,?)",rows)
    assert Counter(db.execute(left).fetchall())==Counter(db.execute(right).fetchall())
    db.close()

@pytest.mark.parametrize("right", [mixed("aa","bb","cc",op="UNION"),
    mixed("aa","bb","cc").replace("k = 30","k = 31").replace("k=30","k=31"),
    mixed("aa","bb","cc").replace("UNION SELECT","UNION ALL SELECT"),
    mixed("aa","bb","cc").replace("FROM t cc","FROM u cc")])
def test_mixed_union_near_misses(right):
    assert not same(mixed("a","b","c"),right)

@pytest.mark.parametrize("cut", ["LIMIT 0", "ORDER BY x LIMIT 1", "LIMIT 1 OFFSET 1"])
def test_cut_is_never_erased(cut):
    assert not same(mixed("a","b","c",cut=cut),mixed("aa","bb","cc",cut=cut))

def test_schema_required_and_unknown_sources_refused():
    assert not same(mixed("a","b","c"),mixed("aa","bb","cc"),schema=None)
    assert not same(mixed("a","b","c").replace("FROM t","FROM missing"),mixed("aa","bb","cc"))

def test_cte_and_volatile_set_trees_refused():
    cte="WITH t AS (SELECT x,k FROM u) "+mixed("a","b","c")
    assert not same(cte,cte)
    volatile="SELECT RANDOM() FROM t UNION ALL SELECT x FROM t"
    assert not same(volatile,volatile)

def test_correlated_alias_binding_is_not_captured():
    left="SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM u a WHERE a.k=a.x) UNION ALL SELECT x FROM t"
    right="SELECT a.x FROM t a WHERE EXISTS(SELECT 1 FROM u b WHERE a.k=b.x) UNION ALL SELECT x FROM t"
    assert not same(left,right)

def test_output_names_kept_for_set_trees():
    assert not same("SELECT x AS a FROM t UNION ALL SELECT x FROM u",
                    "SELECT x AS b FROM t UNION ALL SELECT x FROM u")

def test_window_null_order_is_a_real_semantic_difference():
    left="SELECT x,RANK() OVER (ORDER BY x NULLS FIRST) AS r FROM t UNION ALL SELECT x,0 AS r FROM u"
    right=left.replace("NULLS FIRST","NULLS LAST")
    assert not same(left,right)
    db=duckdb.connect();db.execute("CREATE TABLE t(x BIGINT,k BIGINT)");db.execute("CREATE TABLE u(x BIGINT,k BIGINT)")
    db.execute("INSERT INTO t VALUES (NULL,1),(1,2),(2,3)")
    assert Counter(db.execute(left).fetchall())!=Counter(db.execute(right).fetchall());db.close()


@pytest.mark.parametrize("left,right", [
    ("SELECT r.z+r.x FROM (SELECT u.x+u.y AS z,u.x FROM (SELECT * FROM (VALUES (10,1),(30,3)) v(x,y) WHERE x+y>50) u) r",
     "SELECT * FROM (VALUES (NULL)) v(a) WHERE 1=0"),
    ("SELECT * FROM (SELECT * FROM (VALUES (10,1),(30,3)) v(x,y) UNION ALL SELECT * FROM (VALUES (20,2))) r WHERE x+y>30",
     "SELECT * FROM (VALUES (30,3)) v(x,y)"),
    ("SELECT * FROM (SELECT x FROM t WHERE k=10 UNION ALL SELECT x FROM t WHERE k=20) d UNION SELECT x FROM t WHERE k=30",
     "SELECT x FROM t WHERE k=10 UNION SELECT x FROM t WHERE k=20 UNION SELECT x FROM t WHERE k=30"),
])
def test_existing_constant_and_root_distinct_proofs_are_preserved(left, right):
    assert prove_equivalent_algebraic(left,right,schema=SCHEMA,dialect="mysql",compare_names=False,
                                      exact_arithmetic=True,group_by_constants=True).proven
    db=duckdb.connect();db.execute("CREATE TABLE t(x BIGINT,k BIGINT)")
    db.execute("INSERT INTO t VALUES (1,10),(1,10),(1,20),(2,30)")
    queries=[sqlglot.transpile(q,read="mysql",write="duckdb")[0] for q in (left,right)]
    assert Counter(db.execute(queries[0]).fetchall())==Counter(db.execute(queries[1]).fetchall())
    db.close()


def test_mixed_set_bridge_retains_window_null_order_difference():
    left=("SELECT * FROM (SELECT x,RANK() OVER (ORDER BY x NULLS FIRST) AS r FROM t "
          "UNION SELECT x,0 AS r FROM u) d UNION ALL SELECT x,0 AS r FROM u")
    right=left.replace("NULLS FIRST","NULLS LAST")
    assert not same(left,right)
    assert not prove_equivalent_algebraic(left,right,schema=SCHEMA,dialect="duckdb",compare_names=False).proven
    db=duckdb.connect();db.execute("CREATE TABLE t(x BIGINT,k BIGINT)");db.execute("CREATE TABLE u(x BIGINT,k BIGINT)")
    db.execute("INSERT INTO t VALUES (NULL,1),(1,2),(2,3)")
    assert Counter(db.execute(left).fetchall())!=Counter(db.execute(right).fetchall());db.close()
