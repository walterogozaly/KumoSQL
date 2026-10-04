from collections import Counter
import duckdb
import pytest
import sqlglot
from kumosql.singleton_aggregate_rules import COLLATION_ASSUMPTION, singleton_count_sum
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints

SCHEMA={"e":["id","job","sal"],"d":["name","extra"]}
KEYS={"e":[("id",)]};TYPES={"e":{"id":"INT64","job":"STRING","sal":"INT64"},"d":{"name":"STRING","extra":"INT64"}}
QUERY="SELECT a.job,a.sal*b.n AS s FROM (SELECT job,sal FROM e WHERE id=10) a JOIN (SELECT name,COUNT(*) AS n FROM d GROUP BY name) b ON a.job=b.name"
FLAT="SELECT e.job,SUM(e.sal) AS s FROM e JOIN d ON e.job=d.name WHERE e.id=10 GROUP BY e.job,d.name"

def rewrite(sql=QUERY,keys=KEYS,types=TYPES):
    return singleton_count_sum(sqlglot.parse_one(sql),keys,types,set())

@pytest.mark.parametrize("emp,dept", [([],[]),([],[("x",1)]),([(10,"x",None)],[("x",1),("x",2)]),
    ([(10,None,4)],[(None,1)]),([(10,"x",-5)],[("x",1),("x",2)]),([(11,"x",4)],[("x",1)]),
    ([(10,"x",0)],[]),([(10,"x",4)],[("x",1),("x",1),("y",3)])])
def test_singleton_bridge_preserves_nulls_empty_and_duplicate_groups(emp,dept):
    result=rewrite();assert result is not None
    db=duckdb.connect();db.execute("CREATE TABLE e(id BIGINT PRIMARY KEY,job VARCHAR,sal BIGINT)");db.execute("CREATE TABLE d(name VARCHAR,extra BIGINT)")
    if emp:db.executemany("INSERT INTO e VALUES (?,?,?)",emp)
    if dept:db.executemany("INSERT INTO d VALUES (?,?)",dept)
    bags=[Counter(db.execute(q).fetchall()) for q in (QUERY,result.sql(dialect="duckdb"),FLAT)]
    assert bags[0]==bags[1]==bags[2];db.close()

def test_public_proof_uses_declared_key_and_integer_type():
    r=prove_equivalent_algebraic(FLAT,QUERY,schema=SCHEMA,types=TYPES,
       constraints={"e":TableConstraints(keys=(("id",),))},dialect="duckdb",compare_names=False,exact_arithmetic=True)
    assert r.proven
    assert COLLATION_ASSUMPTION in r.assumptions

@pytest.mark.parametrize("sql",[QUERY.replace("id=10","id>10"),QUERY.replace("id=10","id=NULL"),
    QUERY.replace("JOIN (","LEFT JOIN ("),QUERY.replace("GROUP BY name","GROUP BY name,extra"),
    QUERY.replace("COUNT(*)","COUNT(extra)"),QUERY.replace("COUNT(*)","COUNT(DISTINCT extra)"),
    QUERY.replace("id=10","id=10 OR id=11"),QUERY.replace("a.job,a.sal","b.name,a.sal"),
    QUERY.replace("GROUP BY name","GROUP BY ROLLUP(name)"),QUERY+" LIMIT 1"])
def test_boundaries_decline(sql):
    assert rewrite(sql) is None

def test_metadata_boundaries_decline():
    assert rewrite(keys={}) is None
    assert rewrite(keys={"e":[("id","job")]}) is None
    assert rewrite(types={"e":{"sal":"FLOAT64"}}) is None
    assert rewrite(types={}) is None
    assert rewrite(types={"e":{"id":"STRING","sal":"INT64"}}) is None
    assert rewrite(types={"e":{"id":"FLOAT64","sal":"INT64"}}) is None
    assert rewrite(QUERY.replace("id=10","id='10'")) is None
    assert rewrite(QUERY.replace("id=10","id=1.0")) is None
    assert rewrite(QUERY.replace("id=10","id=9007199254740993")) is None
    assert rewrite(types={"e":{"id":"INT64","job":"INT64","sal":"INT64"},"d":{"name":"STRING"}}) is None
    assert rewrite(types={"e":{"id":"INT64","job":"INT64","sal":"INT64"},"d":{"name":"FLOAT64"}}) is None

def test_missing_key_has_real_duplicate_counterexample():
    db=duckdb.connect();db.execute("CREATE TABLE e(id BIGINT,job VARCHAR,sal BIGINT)");db.execute("CREATE TABLE d(name VARCHAR,extra BIGINT)")
    db.execute("INSERT INTO e VALUES (10,'x',2),(10,'x',3)");db.execute("INSERT INTO d VALUES ('x',1)")
    assert Counter(db.execute(QUERY).fetchall()) != Counter(db.execute(FLAT).fetchall());db.close()

def test_group_key_coercion_can_fan_out_a_singleton():
    db=duckdb.connect();db.execute("CREATE TABLE e(id BIGINT PRIMARY KEY,job BIGINT,sal BIGINT)");db.execute("CREATE TABLE d(name VARCHAR,extra BIGINT)")
    db.execute("INSERT INTO e VALUES (10,1,7)");db.execute("INSERT INTO d VALUES ('1',1),('01',2)")
    assert Counter(db.execute(QUERY).fetchall())==Counter({(1,7):2})
    sql=QUERY.replace("a.sal*b.n AS s","SUM(a.sal*b.n) AS s")+" GROUP BY a.job"
    assert Counter(db.execute(sql).fetchall())==Counter({(1,14):1});db.close()


def test_string_bridge_refuses_missing_assumption_channel():
    assert singleton_count_sum(sqlglot.parse_one(QUERY),KEYS,TYPES) is None
    from kumosql.algebraic_equivalence import normalize
    normalized=sqlglot.parse_one(normalize(QUERY,schema=SCHEMA,keys=KEYS,types=TYPES,dialect="duckdb"))
    assert normalized.args.get("group") is None
    assert not any(s.find_ancestor(sqlglot.exp.Select) is normalized for s in normalized.find_all(sqlglot.exp.Sum))

def test_integer_bridge_is_unconditional_and_calls_do_not_leak_conditions():
    integer_types={"e":{"id":"INT64","job":"INT64","sal":"INT64"},"d":{"name":"INT64","extra":"INT64"}}
    assert singleton_count_sum(sqlglot.parse_one(QUERY),KEYS,integer_types) is not None
    constraints={"e":TableConstraints(keys=(("id",),))}
    first=prove_equivalent_algebraic(FLAT,QUERY,schema=SCHEMA,types=TYPES,constraints=constraints,dialect="duckdb",compare_names=False,exact_arithmetic=True)
    second=prove_equivalent_algebraic(FLAT,QUERY,schema=SCHEMA,types=integer_types,constraints=constraints,dialect="duckdb",compare_names=False,exact_arithmetic=True)
    assert first.proven and COLLATION_ASSUMPTION in first.assumptions
    assert second.proven and COLLATION_ASSUMPTION not in second.assumptions

def test_implicit_collation_witness_explains_reported_condition():
    db=duckdb.connect();db.execute("CREATE TABLE e(id BIGINT PRIMARY KEY,job VARCHAR COLLATE NOCASE,sal BIGINT)");db.execute("CREATE TABLE d(name VARCHAR,extra BIGINT)")
    db.execute("INSERT INTO e VALUES (10,'a',7)");db.execute("INSERT INTO d VALUES ('a',1),('A',2)")
    original=Counter(db.execute(QUERY).fetchall())
    introduced=QUERY.replace("a.sal*b.n AS s","SUM(a.sal*b.n) AS s")+" GROUP BY a.job"
    assert original==Counter({("a",7):2})
    assert Counter(db.execute(introduced).fetchall())==Counter({("a",14):1})
    r=prove_equivalent_algebraic(FLAT,QUERY,schema=SCHEMA,types=TYPES,constraints={"e":TableConstraints(keys=(("id",),))},dialect="duckdb",compare_names=False,exact_arithmetic=True)
    assert r.proven and COLLATION_ASSUMPTION in r.assumptions
    db.close()
