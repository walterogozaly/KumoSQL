"""Correlation-fixed integer keys preserve grouped cardinality and empty input."""
from collections import Counter
import pytest
import sqlglot
from sqlglot import exp
from kumosql.correlated_key_groups import expose_correlated_key_groups
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"t": ["id","k","x"], "o": ["mgr"]}
TYPES = {"t": {"id":"BIGINT","k":"INTEGER","x":"INTEGER"}, "o":{"mgr":"BIGINT"}}
KEYS = {"t":[("id",)]}; NN = {"t":frozenset({"id"})}
CONSTRAINTS = {"t": TableConstraints(not_null=NN["t"],keys=(("id",),))}
LEFT = "SELECT o.mgr,d.m FROM o CROSS JOIN LATERAL (SELECT DISTINCT MAX(i.x) m FROM t i WHERE i.id=o.mgr GROUP BY i.k,'constant') d"
RIGHT = "SELECT o.mgr,i.x FROM o JOIN t i ON i.id=o.mgr"


def proves(left,right,types=TYPES):
    return prove_equivalent_algebraic(left,right,schema=SCHEMA,types=types,constraints=CONSTRAINTS,dialect="duckdb",compare_names=False,timeout_ms=2000).proven


@pytest.mark.parametrize("aggregate", ["MAX", "MIN"])
def test_singleton_group_and_constant_key_prove(aggregate):
    assert proves(LEFT.replace("MAX",aggregate),RIGHT)


@pytest.mark.parametrize("rows,outer", [([],[[1],[None],[1]]), ([[1,3,None]],[[1],[1],[None],[2]]), ([[1,3,9],[2,3,7]],[[1],[2],[1],[None]]), ([[1,3,9]],[])])
def test_original_and_flat_join_have_exact_bags(rows,outer):
    duckdb=pytest.importorskip("duckdb");db=duckdb.connect()
    try:
        db.execute("SET threads=1;CREATE TABLE t(id BIGINT PRIMARY KEY,k INTEGER,x INTEGER);CREATE TABLE o(mgr BIGINT)")
        if rows:db.executemany("INSERT INTO t VALUES (?,?,?)",rows)
        if outer:db.executemany("INSERT INTO o VALUES (?)",outer)
        db.execute("PRAGMA disable_optimizer")
        assert Counter(db.execute(LEFT).fetchall())==Counter(db.execute(RIGHT).fetchall())
    finally:db.close()


@pytest.mark.parametrize("change", [
    lambda s:s.replace("GROUP BY i.k,'constant'","GROUP BY ROLLUP(i.k)"),
    lambda s:s.replace("GROUP BY i.k,'constant'","GROUP BY GROUPING SETS ((i.k),())"),
    lambda s:s.replace("i.id=o.mgr","i.k=o.mgr"),
    lambda s:s.replace("i.id=o.mgr","i.id=o.mgr OR i.k=1"),
    lambda s:s.replace("t i","t i(x,k,id)"),
])
def test_key_or_grouping_boundaries_decline(change):
    tree=sqlglot.parse_one(change(LEFT),read="duckdb")
    grouped=next(s for s in tree.find_all(exp.Select) if s.args.get("group"))
    assert expose_correlated_key_groups(grouped,KEYS,NN,TYPES) is None


def test_global_aggregate_keeps_its_empty_row():
    tree=sqlglot.parse_one("SELECT o.mgr,d.m FROM o CROSS JOIN LATERAL (SELECT COUNT(*) m FROM t i WHERE i.id=o.mgr) d",read="duckdb")
    assert all(expose_correlated_key_groups(s,KEYS,NN,TYPES) is None for s in tree.find_all(exp.Select))
    assert not proves(tree.sql(dialect="duckdb"),"SELECT o.mgr,1 m FROM o JOIN t i ON i.id=o.mgr")


@pytest.mark.parametrize("keys,nn,types", [({},NN,TYPES),(KEYS,{},TYPES),(KEYS,NN,{}),(KEYS,NN,dict(TYPES,o={"mgr":"DOUBLE"}))])
def test_missing_constraints_or_noninjective_coercion_decline(keys,nn,types):
    tree=sqlglot.parse_one(LEFT,read="duckdb")
    grouped=next(s for s in tree.find_all(exp.Select) if s.args.get("group"))
    assert expose_correlated_key_groups(grouped,keys,nn,types) is None


def test_float_equality_really_can_match_two_distinct_integer_keys():
    duckdb=pytest.importorskip("duckdb");db=duckdb.connect()
    try:
        db.execute("SET threads=1;CREATE TABLE t(id BIGINT PRIMARY KEY,k INTEGER,x INTEGER);CREATE TABLE o(mgr DOUBLE);INSERT INTO t VALUES (9007199254740992,1,4),(9007199254740993,1,8);INSERT INTO o VALUES (9007199254740992);PRAGMA disable_optimizer")
        original="SELECT o.mgr,d.m FROM o CROSS JOIN LATERAL (SELECT MAX(i.x) m FROM t i WHERE i.id=o.mgr GROUP BY i.k) d"
        split=original.replace("GROUP BY i.k","GROUP BY i.k,i.id")
        assert db.execute(original).fetchall()==[(float(2**53),8)]
        assert Counter(db.execute(split).fetchall())==Counter([(float(2**53),4),(float(2**53),8)])
    finally:db.close()


def test_pivot_cannot_borrow_the_physical_integer_type():
    tree=sqlglot.parse_one(LEFT.replace("FROM o CROSS", "FROM o PIVOT (AVG(v) FOR k IN (1 AS mgr)) AS o CROSS"),read="duckdb")
    grouped=next(s for s in tree.find_all(exp.Select) if s.args.get("group"))
    assert expose_correlated_key_groups(grouped,KEYS,NN,dict(TYPES,o={"mgr":"BIGINT","v":"DOUBLE","k":"INTEGER"})) is None


@pytest.mark.parametrize("function", ["RANDOM()", "UUID()", "CURRENT_TIMESTAMP"])
def test_lateral_function_filter_is_not_relocated(function):
    sql="SELECT o.mgr,d.x FROM o CROSS JOIN LATERAL (SELECT i.x FROM t i WHERE i.id=o.mgr AND "+function+" IS NOT NULL) d"
    tree=sqlglot.parse_one(sql,read="duckdb")
    assert expose_correlated_key_groups(tree,KEYS,NN,TYPES) is None


def test_conjunctive_filter_keeps_logical_operators():
    left=LEFT.replace("i.id=o.mgr", "i.id=o.mgr AND i.x>0")
    assert proves(left,RIGHT+" WHERE i.x>0")


@pytest.mark.parametrize("keys,not_null,types", [(None, NN, TYPES), (KEYS, None, TYPES), (KEYS, NN, None)])
def test_missing_schema_facts_decline_instead_of_crashing(keys, not_null, types):
    select = sqlglot.parse_one("SELECT MAX(i.x) FROM t i WHERE i.id = o.mgr GROUP BY i.k", dialect="duckdb")
    assert expose_correlated_key_groups(select, keys, not_null, types) is None
