"""Aggregation and DISTINCT pushdown pairs: 0 false proofs, and a witness for every pair that differs.

The pairs come from an outside review of eager aggregation, partial-aggregate merging and DISTINCT
pushdown (Yan and Larson; Chaudhuri and Shim; Calcite's aggregate rules), written out as SQL over
E(empno PK, deptno, sal), D(deptno PK, name), P(empno, tag), S(sid, lid) and L(id PK, pop). Each pair
that differs carries a witness database, re-run here on DuckDB with its optimizer off. The provers
must never prove those; the equivalent ones must agree on random databases, and the ones proved
today stay proved.
"""

import random
from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints, prove_equivalent_smt  # noqa: E402

SCHEMA = {"e": ["empno", "deptno", "sal"], "d": ["deptno", "name"], "p": ["empno", "tag"], "s": ["sid", "lid"], "l": ["id", "pop"]}
DDL = {
    "e": "empno INT PRIMARY KEY, deptno INT, sal INT",
    "d": "deptno INT PRIMARY KEY, name VARCHAR",
    "p": "empno INT, tag VARCHAR",
    "s": "sid INT, lid INT",
    "l": "id INT PRIMARY KEY, pop INT",
}
KEYS = {
    "e": TableConstraints(keys=(("empno",),), not_null=frozenset({"empno"})),
    "d": TableConstraints(keys=(("deptno",),), not_null=frozenset({"deptno"})),
    "l": TableConstraints(keys=(("id",),), not_null=frozenset({"id"})),
}

Q1, Q2 = "SELECT sal FROM e WHERE deptno = 10", "SELECT sal FROM e WHERE deptno = 20"

# (id, equivalent, left, right, witness rows for a pair that differs)
PAIRS = [
    (1, True, "SELECT d.name, SUM(e.sal) FROM e JOIN d ON e.deptno = d.deptno GROUP BY d.name",
     "SELECT d.name, SUM(a.s) FROM (SELECT deptno, SUM(sal) AS s FROM e GROUP BY deptno) AS a JOIN d ON a.deptno = d.deptno GROUP BY d.name", None),
    (2, True, "SELECT deptno, AVG(sal) FROM e GROUP BY deptno",
     "SELECT deptno, SUM(s) / SUM(c) FROM (SELECT deptno, SUM(sal) AS s, COUNT(sal) AS c FROM e GROUP BY deptno) AS a GROUP BY deptno", None),
    (3, True, f"SELECT MIN(sal) FROM ({Q1} UNION ALL {Q2}) AS u",
     "SELECT MIN(m) FROM (SELECT MIN(sal) AS m FROM e WHERE deptno = 10 UNION ALL SELECT MIN(sal) AS m FROM e WHERE deptno = 20) AS u", None),
    (4, True, f"SELECT MAX(sal) FROM ({Q1} UNION ALL {Q2}) AS u",
     "SELECT MAX(m) FROM (SELECT MAX(sal) AS m FROM e WHERE deptno = 10 UNION ALL SELECT MAX(sal) AS m FROM e WHERE deptno = 20) AS u", None),
    (5, True, f"SELECT COUNT(*) FROM ({Q1} UNION ALL {Q2}) AS u",
     "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM e WHERE deptno = 10 UNION ALL SELECT COUNT(*) AS c FROM e WHERE deptno = 20) AS u", None),
    (6, False, "SELECT DISTINCT deptno FROM (SELECT deptno FROM e WHERE sal > 5 UNION ALL SELECT deptno FROM e WHERE sal < 3) AS u",
     "SELECT DISTINCT deptno FROM e WHERE sal > 5 UNION ALL SELECT DISTINCT deptno FROM e WHERE sal < 3",
     {"e": [(1, 1, 10), (2, 1, 1)]}),
    (7, False, "SELECT COUNT(DISTINCT sal) FROM e",
     "SELECT SUM(c) FROM (SELECT deptno, COUNT(DISTINCT sal) AS c FROM e GROUP BY deptno) AS g",
     {"e": [(1, 10, 7), (2, 20, 7)]}),
    (8, False, "SELECT AVG(sal) FROM e",
     "SELECT AVG(a) FROM (SELECT deptno, AVG(sal) AS a FROM e GROUP BY deptno) AS g",
     {"e": [(1, 10, 0), (2, 10, 10), (3, 20, 100)]}),
    (9, False, "SELECT SUM(DISTINCT sal) FROM e",
     "SELECT SUM(s) FROM (SELECT deptno, SUM(DISTINCT sal) AS s FROM e GROUP BY deptno) AS g",
     {"e": [(1, 10, 7), (2, 20, 7)]}),
    (10, False, "SELECT SUM(DISTINCT sal) FROM e", "SELECT SUM(sal) FROM e", {"e": [(1, 10, 2), (2, 10, 2)]}),
    (11, False, "SELECT COUNT(sal) FROM e",
     "SELECT SUM(c) FROM (SELECT deptno, COUNT(*) AS c FROM e GROUP BY deptno) AS g", {"e": [(1, 10, None)]}),
    ("11b", False, "SELECT deptno, COUNT(sal) FROM e GROUP BY deptno",
     "SELECT deptno, SUM(c) FROM (SELECT deptno, empno, COUNT(*) AS c FROM e GROUP BY deptno, empno) AS g GROUP BY deptno", {"e": [(1, 10, None)]}),
    (12, False, "SELECT d.deptno, COUNT(*) FROM d LEFT JOIN e ON d.deptno = e.deptno GROUP BY d.deptno",
     "SELECT d.deptno, SUM(c.n) FROM d LEFT JOIN (SELECT deptno, COUNT(*) AS n FROM e GROUP BY deptno) AS c ON d.deptno = c.deptno GROUP BY d.deptno",
     {"d": [(10, "A")]}),
    (13, False, "SELECT d.deptno, SUM(e.sal) FROM d LEFT JOIN e ON d.deptno = e.deptno GROUP BY d.deptno",
     "SELECT d.deptno, COALESCE(SUM(c.s), 0) FROM d LEFT JOIN (SELECT deptno, SUM(sal) AS s FROM e GROUP BY deptno) AS c ON d.deptno = c.deptno GROUP BY d.deptno",
     {"d": [(10, "A")]}),
    (14, True, "SELECT SUM(l.pop) FROM s JOIN l ON s.lid = l.id",
     "SELECT SUM(x.p) FROM s JOIN (SELECT id, SUM(pop) AS p FROM l GROUP BY id) AS x ON s.lid = x.id", None),
    (15, False, "SELECT SUM(l.pop) FROM s JOIN l ON s.lid = l.id",
     "SELECT SUM(pop) FROM l WHERE id IN (SELECT lid FROM s)", {"s": [(1, 1), (2, 1)], "l": [(1, 100)]}),
    (16, False, "SELECT e.deptno, SUM(e.sal) FROM e JOIN p ON e.empno = p.empno GROUP BY e.deptno",
     "SELECT g.deptno, g.s FROM (SELECT deptno, SUM(sal) AS s FROM e GROUP BY deptno) AS g WHERE g.deptno IN (SELECT e.deptno FROM e JOIN p ON e.empno = p.empno)",
     {"e": [(1, 10, 5), (2, 10, 7)], "p": [(1, "x")]}),
    (17, True, "SELECT MIN(sal) FROM e", "SELECT MIN(DISTINCT sal) FROM e", None),
    (18, False, "SELECT COUNT(DISTINCT sal) FROM e", "SELECT COUNT(sal) FROM e", {"e": [(1, 10, 4), (2, 10, 4)]}),
    (19, True, "SELECT DISTINCT d.deptno FROM d LEFT JOIN e ON d.deptno = e.deptno",
     "SELECT DISTINCT x.deptno FROM (SELECT DISTINCT deptno FROM d) AS x LEFT JOIN e ON x.deptno = e.deptno", None),
    (20, False, "SELECT DISTINCT d.deptno FROM d JOIN e ON d.deptno = e.deptno", "SELECT DISTINCT deptno FROM d", {"d": [(10, "A")]}),
    ("7b", False, "SELECT deptno, COUNT(DISTINCT sal) FROM e GROUP BY deptno",
     "SELECT deptno, SUM(c) FROM (SELECT deptno, empno, COUNT(DISTINCT sal) AS c FROM e GROUP BY deptno, empno) AS g GROUP BY deptno",
     {"e": [(1, 10, 7), (2, 10, 7)]}),
    ("8b", False, "SELECT deptno, AVG(sal) FROM e GROUP BY deptno",
     "SELECT deptno, AVG(a) FROM (SELECT deptno, sal > 5 AS hi, AVG(sal) AS a FROM e GROUP BY deptno, sal > 5) AS g GROUP BY deptno",
     {"e": [(1, 10, 0), (2, 10, 10), (3, 10, 100)]}),
    ("10b", False, "SELECT SUM(DISTINCT sal) FROM e", "SELECT SUM(s) FROM (SELECT sal, SUM(sal) AS s FROM e GROUP BY sal) AS g", {"e": [(1, 10, 2), (2, 10, 2)]}),
    ("12b", False, "SELECT d.deptno, COUNT(*) FROM d LEFT JOIN e ON d.deptno = e.deptno GROUP BY d.deptno",
     "SELECT d.deptno, COALESCE(SUM(c.n), 0) FROM d LEFT JOIN (SELECT deptno, COUNT(*) AS n FROM e GROUP BY deptno) AS c ON d.deptno = c.deptno GROUP BY d.deptno",
     {"d": [(10, "A")]}),
    ("12c", True, "SELECT d.deptno, COUNT(e.empno) FROM d LEFT JOIN e ON d.deptno = e.deptno GROUP BY d.deptno",
     "SELECT d.deptno, COALESCE(SUM(c.n), 0) FROM d LEFT JOIN (SELECT deptno, COUNT(*) AS n FROM e GROUP BY deptno) AS c ON d.deptno = c.deptno GROUP BY d.deptno", None),
    ("19b", True, "SELECT d.deptno FROM d LEFT JOIN e ON d.deptno = e.deptno",
     "SELECT x.deptno FROM (SELECT DISTINCT deptno FROM d) AS x LEFT JOIN e ON x.deptno = e.deptno", None),
    ("20b", False, "SELECT DISTINCT d.deptno FROM d JOIN e ON d.deptno = e.deptno", "SELECT DISTINCT d.deptno FROM d WHERE EXISTS (SELECT 1 FROM e WHERE e.deptno = d.deptno AND e.sal > 0)", {"d": [(10, "A")], "e": [(1, 10, None)]}),
]

# Equivalent pairs the algebraic prover proves today (with the keys above); they must stay proved.
PROVED = {1, 2, 3, 4, 5, 14, 17, 19, "19b"}


def _database(rows):
    db = duckdb.connect()
    for table, ddl in DDL.items():
        db.execute(f"CREATE TABLE {table} ({ddl})")
        insert_rows(db, table, rows.get(table, []))
    return db


def _bag(rows):
    return Counter(tuple(round(v, 9) if isinstance(v, float) else v for v in row) for row in rows)


DIFFERENT = [pytest.param(left, right, witness, id=f"r009-{n}") for n, same, left, right, witness in PAIRS if not same]
EQUIVALENT = [pytest.param(n, left, right, id=f"r009-{n}") for n, same, left, right, _ in PAIRS if same]


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
def test_witness_separates_the_pair(left, right, witness):
    first, second = run_unoptimized(_database(witness), left, right)
    assert _bag(first) != _bag(second)


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
@pytest.mark.parametrize("dialect", ["duckdb", "mysql"])
def test_different_pair_is_never_proved(left, right, witness, dialect):
    for constraints in (None, KEYS):
        result = prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=constraints, compare_names=False, dialect=dialect)
        assert not result.proven, result.reason
    assert not prove_equivalent_smt(left, right, schema=SCHEMA, constraints=KEYS, compare_names=False, dialect=dialect).proven


def _random_rows(rng):
    depts = rng.sample([10, 20, 30], rng.randint(0, 3))
    return {
        "d": [(k, rng.choice(["A", "B"])) for k in depts],
        "e": [(k, rng.choice([10, 20, 30, None]), rng.choice([None, 0, 2, 7, 10])) for k in rng.sample(range(1, 7), rng.randint(0, 6))],
        "p": [(rng.randint(1, 6), rng.choice(["x", "y"])) for _ in range(rng.randint(0, 4))],
        "s": [(i, rng.choice([1, 2, 3])) for i in range(rng.randint(0, 4))],
        "l": [(k, rng.choice([None, 100, 5])) for k in rng.sample([1, 2], rng.randint(0, 2))],
    }


@pytest.mark.parametrize("n, left, right", EQUIVALENT)
def test_equivalent_pair_agrees_on_random_databases(n, left, right):
    rng = random.Random(9)
    for _ in range(60):
        first, second = run_unoptimized(_database(_random_rows(rng)), left, right)
        assert _bag(first) == _bag(second), (n, first, second)


@pytest.mark.parametrize("n, left, right", [p for p in EQUIVALENT if p.values[0] in PROVED])
def test_proved_pairs_stay_proved(n, left, right):
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=KEYS, compare_names=False, dialect="duckdb")
    assert result.proven, result.reason
