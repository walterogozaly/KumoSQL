"""Bag-equivalence backend (kumosql.uexpr): proofs found for the remaining Calcite shapes.

Each case is a synthetic pair over a tiny schema, so these run offline and in parallel.
"""

import pytest

from kumosql.smt_equivalence import TableConstraints
from kumosql.uexpr import prove_bag_equivalent

SCHEMA = {"emp": ["empno", "ename", "deptno", "sal", "mgr"], "dept": ["deptno", "name"]}
TYPES = {
    "emp": {"empno": "INT", "ename": "VARCHAR(20)", "deptno": "INT", "sal": "INT", "mgr": "INT"},
    "dept": {"deptno": "INT", "name": "VARCHAR(10)"},
}
CONSTRAINTS = {
    "emp": TableConstraints(not_null=frozenset({"empno", "ename", "deptno", "sal"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}


def proves(left: str, right: str) -> bool:
    result = prove_bag_equivalent(
        left, right, schema=SCHEMA, constraints=CONSTRAINTS, types=TYPES, dialect="mysql",
        exact_arithmetic=True, compare_names=False, use_foreign_keys=False,
    )
    return result.proven


def test_single_value_join_is_a_scalar_subquery():
    assert proves(
        "SELECT empno, (SELECT deptno FROM emp WHERE empno < 20) AS d FROM emp",
        "SELECT e.empno, t.f0 AS d FROM emp AS e LEFT JOIN (SELECT SINGLE_VALUE(x.deptno) AS f0 FROM emp AS x WHERE x.empno < 20) AS t ON TRUE",
    )


def test_single_value_does_not_equal_another_column():
    assert not proves(
        "SELECT empno, (SELECT deptno FROM emp WHERE empno < 20) AS d FROM emp",
        "SELECT e.empno, t.f0 AS d FROM emp AS e LEFT JOIN (SELECT SINGLE_VALUE(x.sal) AS f0 FROM emp AS x WHERE x.empno < 20) AS t ON TRUE",
    )


def test_average_is_sum_over_count_of_a_keyed_column():
    # the bag of key values of dept is the bag of its rows, so AVG(deptno) = SUM(deptno) / COUNT(*)
    assert proves(
        "SELECT name, AVG(deptno) FROM dept GROUP BY name",
        "SELECT name, SUM(deptno) / COUNT(*) FROM dept GROUP BY name",
    )
    assert not proves(
        "SELECT name, AVG(deptno) FROM dept GROUP BY name",
        "SELECT name, SUM(deptno) / COUNT(*) + 1 FROM dept GROUP BY name",
    )


def test_having_on_a_group_key_is_the_same_as_filtering_before_grouping():
    # the condition on the group key reaches the rows through the group equality
    assert proves(
        "SELECT name FROM dept WHERE name > 'b' GROUP BY name HAVING name > 'c' AND (COUNT(*) > 3 OR name < 'z')",
        "SELECT t.name FROM (SELECT name FROM dept WHERE name > 'b') AS t WHERE t.name > 'c' GROUP BY t.name HAVING COUNT(*) > 3 OR t.name < 'z'",
    )
    assert not proves(
        "SELECT name FROM dept WHERE name > 'b' GROUP BY name HAVING name > 'c' AND (COUNT(*) > 3 OR name < 'z')",
        "SELECT t.name FROM (SELECT name FROM dept WHERE name > 'b') AS t WHERE t.name > 'd' GROUP BY t.name HAVING COUNT(*) > 3 OR t.name < 'z'",
    )


def test_existence_inside_existence_merges():
    # a group of a group exists exactly when a row does
    assert proves(
        "SELECT deptno FROM emp GROUP BY deptno",
        "SELECT t.deptno FROM (SELECT ename, deptno FROM emp GROUP BY ename, deptno) AS t GROUP BY t.deptno",
    )
    assert not proves(
        "SELECT deptno FROM emp GROUP BY deptno",
        "SELECT t.deptno FROM (SELECT ename, deptno FROM emp WHERE sal > 1 GROUP BY ename, deptno) AS t GROUP BY t.deptno",
    )


def test_sum_of_an_outside_value_over_a_join_is_the_value_times_the_count():
    left = (
        "SELECT t.ename, SUM(t.sal) FROM (SELECT * FROM emp WHERE empno = 10) AS t "
        "INNER JOIN dept AS d ON t.ename = d.name GROUP BY t.ename, d.name"
    )
    right = (
        "SELECT t1.ename, CAST(t1.sal * t2.c AS SIGNED) FROM (SELECT ename, sal FROM emp WHERE empno = 10) AS t1 "
        "INNER JOIN (SELECT name, COUNT(*) AS c FROM dept GROUP BY name) AS t2 ON t1.ename = t2.name"
    )
    assert proves(left, right)
    assert not proves(left, right.replace("t1.sal * t2.c", "t1.sal + t2.c"))


def test_string_functions_fold_over_a_union_of_literals():
    left = (
        "SELECT u FROM (SELECT UPPER(CONCAT(SUBSTRING(x, 1, 2), SUBSTRING(x, 3))) AS u "
        "FROM (SELECT 'table' AS x UNION SELECT 'view' UNION SELECT 'foreign table') AS t) AS t2 WHERE u = 'TABLE'"
    )
    assert proves(left, "SELECT 'TABLE' AS u")
    assert not proves(left, "SELECT 'TABLES' AS u")
    assert not proves(left, "SELECT 'VIEW' AS u")
