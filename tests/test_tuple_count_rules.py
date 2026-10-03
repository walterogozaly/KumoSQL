"""Multi-argument COUNT: ``COUNT(a, b)`` counts rows with no NULL argument, ``COUNT(DISTINCT a, b)`` the
distinct such tuples (VeriEQL Calcite 33, 195, 276, 388). The DuckDB oracle reads them through
``counterexample.to_duckdb``; every pair that differs carries a witness re-run with the optimizer off."""

import random
from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.counterexample import to_duckdb  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"emp": ["empno", "deptno", "sal", "comm", "job"]}
KEYS = {"emp": TableConstraints(not_null=frozenset({"empno"}), keys=(("empno",),))}

LEFT_276 = "SELECT deptno, SUM(comm), MIN(comm), COUNT(DISTINCT sal, comm) FROM emp GROUP BY deptno"
REGROUP = "FROM (SELECT deptno, comm, sal, SUM(comm) AS s, MIN(comm) AS m FROM emp GROUP BY deptno, comm, sal) AS t2 GROUP BY deptno"

EQUIVALENT = [
    pytest.param(LEFT_276, f"SELECT deptno, SUM(s), MIN(m), COUNT(sal, comm) {REGROUP}", id="calcite-276"),
    pytest.param(
        "SELECT deptno, SUM(comm), MIN(comm), COUNT(DISTINCT sal, deptno, comm) FROM emp GROUP BY deptno",
        f"SELECT deptno, SUM(s), MIN(m), COUNT(sal, deptno, comm) {REGROUP}",
        id="calcite-388",
    ),
    pytest.param(
        "SELECT deptno, COUNT(DISTINCT job, sal), COUNT(DISTINCT deptno, job), SUM(sal) FROM emp GROUP BY deptno",
        "SELECT t1.deptno, t5.c2, t7.c3, t1.s FROM (SELECT deptno, SUM(sal) AS s FROM emp GROUP BY deptno) AS t1"
        " INNER JOIN (SELECT deptno, COUNT(job, sal) AS c2 FROM (SELECT sal, job, deptno FROM emp GROUP BY sal, job, deptno) AS t4 GROUP BY deptno) AS t5"
        " ON t1.deptno IS NOT DISTINCT FROM t5.deptno"
        " INNER JOIN (SELECT deptno, COUNT(deptno, job) AS c3 FROM (SELECT job, deptno FROM emp GROUP BY job, deptno) AS t6 GROUP BY deptno) AS t7"
        " ON t1.deptno IS NOT DISTINCT FROM t7.deptno",
        id="calcite-33",
    ),
    pytest.param(
        "SELECT deptno, COUNT(sal, comm) FROM emp GROUP BY deptno",
        "SELECT deptno, COUNT(CASE WHEN sal IS NOT NULL AND comm IS NOT NULL THEN 1 END) FROM emp GROUP BY deptno",
        id="count-tuple-is-count-of-non-null-rows",
    ),
]

# (left, right, witness rows of emp: empno, deptno, sal, comm, job)
DIFFERENT = [
    pytest.param(LEFT_276, f"SELECT deptno, SUM(s), MIN(m), COUNT(sal) {REGROUP}", [(1, 10, 5, None, "a"), (2, 10, 5, 1, "a")], id="count-misses-an-extra-key"),
    pytest.param(
        LEFT_276,
        "SELECT deptno, SUM(s), MIN(m), COUNT(sal, comm) FROM (SELECT deptno, comm, sal, job, SUM(comm) AS s, MIN(comm) AS m"
        " FROM emp GROUP BY deptno, comm, sal, job) AS t2 GROUP BY deptno",
        [(1, 10, 5, 1, "a"), (2, 10, 5, 1, "b")],
        id="inner-groups-by-an-uncounted-key",
    ),
    pytest.param(LEFT_276, f"SELECT deptno, SUM(s), MIN(m), COUNT(*) {REGROUP}", [(1, 10, None, None, "a")], id="count-star-counts-null-tuples"),
    pytest.param("SELECT COUNT(sal, comm) FROM emp", "SELECT COUNT(sal) FROM emp", [(1, 10, 5, None, "a")], id="count-tuple-is-not-count-first"),
    pytest.param(
        "SELECT deptno, COUNT(DISTINCT sal, comm) FROM emp GROUP BY deptno",
        "SELECT deptno, COUNT(DISTINCT sal) FROM emp GROUP BY deptno",
        [(1, 10, 5, 1, "a"), (2, 10, 5, 2, "a")],
        id="distinct-tuple-is-not-distinct-first",
    ),
    pytest.param(
        "SELECT deptno, COUNT(DISTINCT sal, comm) FROM emp GROUP BY deptno",
        "SELECT deptno, COUNT(sal, comm) FROM emp GROUP BY deptno",
        [(1, 10, 5, 1, "a"), (2, 10, 5, 1, "b")],
        id="distinct-is-not-a-set-under-count",
    ),
]


def _database(rows):
    db = duckdb.connect()
    db.execute("CREATE TABLE emp (empno INT PRIMARY KEY, deptno INT, sal INT, comm INT, job VARCHAR)")
    insert_rows(db, "emp", rows)
    return db


def _run(rows, left, right):
    return run_unoptimized(_database(rows), to_duckdb(left, "mysql"), to_duckdb(right, "mysql"))


def _bag(rows):
    return Counter(tuple(round(v, 9) if isinstance(v, float) else v for v in row) for row in rows)


def _prove(left, right, constraints):
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, constraints=constraints, compare_names=False, dialect="mysql", exact_arithmetic=True
    )


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pair_is_proved(left, right):
    result = _prove(left, right, KEYS)
    assert result.proven, result.reason


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pair_agrees_on_random_databases(left, right):
    rng = random.Random(276)
    for _ in range(40):
        rows = [
            (k, rng.choice([10, 20, None]), rng.choice([None, 1, 5]), rng.choice([None, 1, 2]), rng.choice([None, "a", "b"]))
            for k in rng.sample(range(1, 9), rng.randint(0, 8))
        ]
        first, second = _run(rows, left, right)
        assert _bag(first) == _bag(second), rows


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
def test_witness_separates_the_pair(left, right, witness):
    first, second = _run(witness, left, right)
    assert _bag(first) != _bag(second)


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
def test_different_pair_is_never_proved(left, right, witness):
    for constraints in (None, KEYS):
        assert not _prove(left, right, constraints).proven
        assert not _prove(right, left, constraints).proven
