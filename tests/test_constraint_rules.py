"""Rules from Astra's hard-tests pass: declared-constraint rewrites, grouping expansion, counted intersection,
row bounds and structural identity. Each proven pair is replayed on random DuckDB data; each near miss must stay unproven."""

import random

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")
import sqlglot

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.duckdb_load import insert_rows
from kumosql.smt_equivalence import TableConstraints

EMP = {"emp": ["empno", "ename", "job", "deptno", "sal"], "dept": ["deptno", "name"]}
EMP_C = {
    "emp": TableConstraints(not_null=frozenset({"empno", "ename", "job", "deptno", "sal"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}
SHOP = {"customers": ["id", "name"], "orders": ["id", "customer_id", "total"], "order_items": ["order_id", "line_no", "sku"]}
SHOP_C = {
    "customers": TableConstraints(not_null=frozenset({"id", "name"}), keys=(("id",),)),
    "orders": TableConstraints(not_null=frozenset({"id", "customer_id", "total"}), keys=(("id",),), foreign_keys=((("customer_id",), "customers", ("id",)),)),
    "order_items": TableConstraints(not_null=frozenset({"order_id", "line_no", "sku"}), keys=(("order_id", "line_no"),)),
}


def prove(left, right, schema, constraints):
    return prove_equivalent_algebraic(left, right, schema=schema, constraints=constraints, exact_arithmetic=True, compare_names=False).proven


def random_database(schema, constraints, seed, db=None):
    """Random rows that respect keys, NOT NULL and foreign keys (parents are filled first), in new tables on ``db``
    when given (a connection costs about 10 ms)."""

    rng = random.Random(seed)
    db = db or duckdb.connect()
    filled = {}
    order = sorted(schema, key=lambda t: bool(constraints[t].foreign_keys))
    for table in order:
        columns, declared = schema[table], constraints[table]
        db.execute(f"CREATE OR REPLACE TABLE {table} ({', '.join(c + ' INTEGER' for c in columns)})")
        rows, seen = [], set()
        for _ in range(rng.randint(0, 7)):
            row = {c: (None if c not in declared.not_null and rng.random() < 0.25 else rng.randint(0, 3)) for c in columns}
            for cols, parent, parent_cols in declared.foreign_keys:
                parents = filled[parent]
                if not parents:
                    row = None
                    break
                chosen = rng.choice(parents)
                row.update({c: chosen[schema[parent].index(pc)] for c, pc in zip(cols, parent_cols)})
            if row is None:
                continue
            token = [tuple(row[c] for c in key) for key in declared.keys]
            if any(None in t or (i, t) in seen for i, t in enumerate(token)):
                continue
            seen.update(enumerate(token))
            rows.append(list(row.values()))
        filled[table] = rows
        insert_rows(db, table, rows)
    return db


def same_results(left, right, schema, constraints, trials=60):
    db = duckdb.connect()
    left, right = (sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in (left, right))
    for seed in range(trials):
        random_database(schema, constraints, seed, db)
        a = sorted(map(repr, db.execute(left).fetchall()))
        b = sorted(map(repr, db.execute(right).fetchall()))
        if a != b:
            return False
    return True


PROVEN = [
    ("count distinct of a key", SHOP, SHOP_C,
     "SELECT COUNT(DISTINCT id) AS n FROM orders", "SELECT COUNT(id) AS n FROM orders"),
    ("distinct join to exists", SHOP, SHOP_C,
     "SELECT DISTINCT o.id FROM orders AS o JOIN order_items AS i ON i.order_id = o.id",
     "SELECT id FROM orders AS o WHERE EXISTS (SELECT 1 FROM order_items AS i WHERE i.order_id = o.id)"),
    ("foreign key join dropped", SHOP, {**SHOP_C},
     "SELECT o.id FROM orders AS o JOIN customers AS c ON o.customer_id = c.id", "SELECT id FROM orders"),
    ("three-way intersection by counts", EMP, EMP_C,
     "SELECT * FROM (SELECT * FROM emp WHERE deptno = 1 INTERSECT DISTINCT SELECT * FROM emp WHERE deptno = 2) t INTERSECT DISTINCT SELECT * FROM emp WHERE deptno = 3",
     "SELECT empno, ename, job, deptno, sal FROM (SELECT empno, ename, job, deptno, sal, COUNT(*) AS c FROM emp WHERE deptno = 1 GROUP BY empno, ename, job, deptno, sal UNION ALL SELECT empno, ename, job, deptno, sal, COUNT(*) FROM emp WHERE deptno = 2 GROUP BY empno, ename, job, deptno, sal UNION ALL SELECT empno, ename, job, deptno, sal, COUNT(*) FROM emp WHERE deptno = 3 GROUP BY empno, ename, job, deptno, sal) t GROUP BY empno, ename, job, deptno, sal HAVING COUNT(*) = 3"),
    ("count distinct through grouping sets", EMP, EMP_C,
     "SELECT COUNT(DISTINCT ename), COUNT(DISTINCT job) FROM emp",
     "SELECT COUNT(ename) FILTER (WHERE g1), COUNT(job) FILTER (WHERE g2) FROM (SELECT ename, job, GROUPING(ename, job) = 1 AS g1, GROUPING(ename, job) = 2 AS g2 FROM emp GROUP BY GROUPING SETS (ename, job)) t"),
]

NOT_PROVEN = [
    ("count distinct of a non-key", SHOP, SHOP_C,
     "SELECT COUNT(DISTINCT customer_id) AS n FROM orders", "SELECT COUNT(customer_id) AS n FROM orders"),
    ("distinct non-key output kept as a join", SHOP, SHOP_C,
     "SELECT DISTINCT o.customer_id FROM orders AS o JOIN order_items AS i ON i.order_id = o.id",
     "SELECT customer_id FROM orders AS o WHERE EXISTS (SELECT 1 FROM order_items AS i WHERE i.order_id = o.id)"),
    ("join without the foreign key", SHOP, SHOP_C,
     "SELECT o.id FROM orders AS o JOIN customers AS c ON o.total = c.id", "SELECT id FROM orders"),
    ("intersection of two counted as three", EMP, EMP_C,
     "SELECT * FROM emp WHERE deptno = 1 INTERSECT DISTINCT SELECT * FROM emp WHERE deptno = 2",
     "SELECT empno, ename, job, deptno, sal FROM (SELECT empno, ename, job, deptno, sal, COUNT(*) AS c FROM emp WHERE deptno = 1 GROUP BY empno, ename, job, deptno, sal UNION ALL SELECT empno, ename, job, deptno, sal, COUNT(*) FROM emp WHERE deptno = 2 GROUP BY empno, ename, job, deptno, sal) t GROUP BY empno, ename, job, deptno, sal HAVING COUNT(*) = 1"),
    ("grouping flag picks the wrong set", EMP, EMP_C,
     "SELECT COUNT(DISTINCT ename), COUNT(DISTINCT job) FROM emp",
     "SELECT COUNT(ename) FILTER (WHERE g2), COUNT(job) FILTER (WHERE g1) FROM (SELECT ename, job, GROUPING(ename, job) = 1 AS g1, GROUPING(ename, job) = 2 AS g2 FROM emp GROUP BY GROUPING SETS (ename, job)) t"),
    ("limit that does bound the rows", EMP, EMP_C,
     "SELECT empno FROM emp ORDER BY empno LIMIT 1", "SELECT empno FROM emp ORDER BY empno LIMIT 2"),
    ("different filters, same shape", EMP, EMP_C,
     "SELECT empno FROM emp WHERE sal > 1", "SELECT empno FROM emp WHERE sal > 2"),
]


@pytest.mark.parametrize("name,schema,constraints,left,right", PROVEN, ids=[p[0] for p in PROVEN])
def test_rule_proves_and_replays_on_random_data(name, schema, constraints, left, right):
    assert prove(left, right, schema, constraints)
    assert same_results(left, right, schema, constraints)


@pytest.mark.parametrize("name,schema,constraints,left,right", NOT_PROVEN, ids=[p[0] for p in NOT_PROVEN])
def test_near_misses_are_not_proven(name, schema, constraints, left, right):
    assert not prove(left, right, schema, constraints)


def test_count_case_rule_keeps_arms_comparing_different_counts():
    sql = (
        "SELECT CASE WHEN t.a > t.b THEN 1 ELSE 0 END FROM emp AS e LEFT JOIN "
        "(SELECT job, COUNT(*) AS a, COUNT(*) AS b FROM emp GROUP BY job) AS t ON e.job = t.job"
    )
    assert "t.a > t.b" in normalize(sql, schema=EMP, not_null={"emp": {"job"}}).lower().replace("`", "")
