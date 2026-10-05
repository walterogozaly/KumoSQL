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
