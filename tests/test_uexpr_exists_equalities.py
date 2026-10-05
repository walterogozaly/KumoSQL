"""Existence tests in the bag-equivalence backend: equalities an ``∃`` forces, nested ``∃``,
non-NULL facts and DISTINCT aggregates. Each proof is paired with a near miss that must stay unproven."""

from __future__ import annotations

from kumosql.smt_equivalence import TableConstraints
from kumosql.uexpr import prove_bag_equivalent

SCHEMA = {"emp": ["empno", "job", "sal", "deptno", "mgr"], "dept": ["deptno", "name"]}
CONSTRAINTS = {
    "emp": TableConstraints(not_null=frozenset({"empno", "job", "sal", "deptno"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}
NULLABLE = {"t": TableConstraints()}


def proves(left: str, right: str, schema=SCHEMA, constraints=CONSTRAINTS) -> bool:
    result = prove_bag_equivalent(
        left, right, schema=schema, constraints=constraints, dialect="mysql", exact_arithmetic=True, compare_names=False
    )
    return result.proven


def test_group_by_a_column_fixed_by_the_filter():
    left = "SELECT COUNT(*) FROM emp WHERE deptno = 10 GROUP BY deptno, sal"
    right = "SELECT COUNT(*) FROM emp WHERE deptno = 10 GROUP BY sal"
    assert proves(left, right)


def test_group_by_a_column_not_fixed_by_the_filter_is_not_proven():
    left = "SELECT COUNT(*) FROM emp WHERE deptno > 10 GROUP BY deptno, sal"
    right = "SELECT COUNT(*) FROM emp WHERE deptno > 10 GROUP BY sal"
    assert not proves(left, right)


def test_not_in_against_a_left_join_flag():
    left = "SELECT * FROM emp WHERE sal = 4 OR empno NOT IN (SELECT deptno FROM dept)"
    right = (
        "SELECT e.empno, e.job, e.sal, e.deptno, e.mgr FROM emp AS e "
        "LEFT JOIN (SELECT deptno, 1 AS i FROM dept) AS t ON e.empno = t.deptno "
        "WHERE e.sal = 4 OR NOT CASE WHEN t.i IS NOT NULL THEN TRUE ELSE FALSE END"
    )
    assert proves(left, right)


def test_not_in_over_a_nullable_column_is_not_a_left_join():
    schema = {"t": ["a"], "u": ["b"]}
    left = "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)"
    right = "SELECT t.a FROM t LEFT JOIN (SELECT b, 1 AS i FROM u) AS x ON t.a = x.b WHERE x.i IS NULL"
    assert not proves(left, right, schema, {})


def test_in_subquery_against_a_join_on_two_keys():
    left = "SELECT sal FROM emp WHERE empno IN (SELECT deptno FROM dept WHERE emp.job = dept.name)"
    right = "SELECT emp.sal FROM emp INNER JOIN dept ON emp.job = dept.name AND emp.empno = dept.deptno"
    assert proves(left, right)


def test_join_with_a_grouped_side_splits_into_independent_existence_tests():
    left = (
        "SELECT d.name, SUM(e.sal), COUNT(*) FROM emp AS e "
        "INNER JOIN (SELECT name FROM dept GROUP BY name) AS d ON e.job = d.name GROUP BY d.name"
    )
    right = (
        "SELECT d.name, x.s, x.c FROM (SELECT job, SUM(sal) AS s, COUNT(*) AS c FROM emp GROUP BY job) AS x "
        "INNER JOIN (SELECT name FROM dept GROUP BY name) AS d ON x.job = d.name"
    )
    assert proves(left, right)


def test_count_distinct_against_counting_a_deduplicated_subquery():
    left = "SELECT deptno, job, COUNT(DISTINCT sal) FROM emp GROUP BY deptno, job"
    right = "SELECT deptno, job, COUNT(sal) FROM (SELECT deptno, job, sal FROM emp GROUP BY deptno, job, sal) AS t GROUP BY deptno, job"
    assert proves(left, right)


def test_count_distinct_is_not_count():
    left = "SELECT deptno, COUNT(DISTINCT sal) FROM emp GROUP BY deptno"
    right = "SELECT deptno, COUNT(sal) FROM emp GROUP BY deptno"
    assert not proves(left, right)


def test_equality_of_nullable_columns_does_not_fix_a_null_group():
    schema = {"t": ["a", "b"]}
    left = "SELECT COUNT(*) FROM t GROUP BY a, b"
    right = "SELECT COUNT(*) FROM t WHERE a = b GROUP BY a"
    assert not proves(left, right, schema, {})
