import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import TableConstraints
from kumosql.sum_of_counts import sum_of_grouped_counts

SCHEMA = {"emp": ["empno", "deptno", "mgr", "comm"]}
CONSTRAINTS = {"emp": TableConstraints(not_null=frozenset({"empno", "deptno"}), keys=(("empno",),))}
GROUPED = "(SELECT COUNT(*) AS c FROM emp GROUP BY deptno, mgr) AS t"


def _rule(sql):
    out = sum_of_grouped_counts(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def _proven(left, right):
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, constraints=CONSTRAINTS, dialect="mysql", compare_names=False, exact_arithmetic=True
    ).proven


def _differ(left, right, rows):
    db = duckdb.connect()
    db.execute("CREATE TABLE emp (empno INT NOT NULL PRIMARY KEY, deptno INT NOT NULL, mgr INT, comm INT)")
    for row in rows:
        db.execute("INSERT INTO emp VALUES (?, ?, ?, ?)", row)
    a, b = run_unoptimized(db, left, right)
    return sorted(a, key=repr) != sorted(b, key=repr)


def test_sum_of_counts_becomes_a_guarded_count_over_the_groups_rows():
    out = _rule(f"SELECT SUM(c) FROM {GROUPED}")
    assert out == "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COUNT(*) END FROM emp"
    out = _rule("SELECT SUM(t.c) FROM (SELECT deptno, COUNT(comm) AS c FROM emp WHERE mgr > 1 GROUP BY deptno) AS t")
    assert out == "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COUNT(comm) END FROM emp WHERE mgr > 1"


def test_calcite_count_through_aggregate_is_proven():
    left = f"SELECT CASE WHEN SUM(c) IS NOT NULL THEN CAST(SUM(c) AS BIGINT) ELSE 0 END FROM {GROUPED}"
    assert _proven(left, "SELECT COUNT(*) FROM emp")
    left = "SELECT COALESCE(SUM(c), 0) FROM (SELECT COUNT(comm) AS c FROM emp GROUP BY deptno) AS t"
    assert _proven(left, "SELECT COUNT(comm) FROM emp")


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT MAX(c) FROM {GROUPED}",
        f"SELECT AVG(c) FROM {GROUPED}",
        f"SELECT COUNT(*) FROM {GROUPED}",
        f"SELECT SUM(c), COUNT(*) FROM {GROUPED}",
        f"SELECT SUM(DISTINCT c) FROM {GROUPED}",
        f"SELECT SUM(c) FROM {GROUPED} WHERE c > 1",
        f"SELECT SUM(c) + MAX(c) FROM {GROUPED}",
        f"SELECT SUM(c) FROM {GROUPED} GROUP BY c",
        "SELECT SUM(c), deptno FROM (SELECT deptno, COUNT(*) AS c FROM emp GROUP BY deptno) AS t",
        "SELECT SUM(deptno) FROM (SELECT deptno, COUNT(*) AS c FROM emp GROUP BY deptno) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(DISTINCT mgr) AS c FROM emp GROUP BY deptno) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM emp GROUP BY deptno HAVING COUNT(*) > 1) AS t",
        "SELECT SUM(c) FROM (SELECT DISTINCT COUNT(*) AS c FROM emp GROUP BY deptno) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM emp GROUP BY deptno WITH ROLLUP) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM emp GROUP BY deptno LIMIT 1) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(*) + 1 AS c FROM emp GROUP BY deptno) AS t",
        "SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM emp) AS t",
    ],
)
def test_other_outer_aggregates_and_inner_shapes_are_left_alone(sql):
    assert _rule(sql) is None


# Each near miss returns different rows than COUNT(*) on the listed rows (checked in DuckDB), so the
# prover must not call it equivalent.
NEAR_MISSES = [
    (f"SELECT SUM(c) FROM {GROUPED}", []),  # no groups: NULL, not 0
    (f"SELECT MAX(c) FROM {GROUPED}", [(1, 10, 1, 1), (2, 20, 1, 1)]),
    (f"SELECT COUNT(*) FROM {GROUPED}", [(1, 10, 1, 1), (2, 10, 1, 1)]),
    (f"SELECT CAST(AVG(c) AS BIGINT) FROM {GROUPED}", [(1, 10, 1, 1), (2, 20, 1, 1)]),
    (f"SELECT COALESCE(SUM(c), 0) FROM {GROUPED} WHERE c > 1", [(1, 10, 1, 1)]),
    ("SELECT COALESCE(SUM(c), 0) FROM (SELECT COUNT(*) AS c FROM emp GROUP BY deptno HAVING COUNT(*) > 1) AS t", [(1, 10, 1, 1)]),
    ("SELECT COALESCE(SUM(c), 0) FROM (SELECT COUNT(DISTINCT mgr) AS c FROM emp GROUP BY deptno) AS t", [(1, 10, 1, 1), (2, 10, 1, 1)]),
    ("SELECT COALESCE(SUM(c), 0) FROM (SELECT COUNT(mgr) AS c FROM emp GROUP BY deptno) AS t", [(1, 10, None, 1)]),
]


@pytest.mark.parametrize("left,rows", NEAR_MISSES)
def test_near_misses_are_not_proven(left, rows):
    right = "SELECT COUNT(*) FROM emp"
    assert _differ(left, right, rows)
    assert not _proven(left, right)
