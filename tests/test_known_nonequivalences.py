"""Rewrites that look right and are not, each one a bug Calcite shipped. The prover must refuse them.

CALCITE-5578 (testAggregateCaseToFilter): ``SUM(CASE WHEN c THEN x ELSE 0 END)`` is 0 on a table with no row where ``c``
holds, ``SUM(x) FILTER (WHERE c)`` is NULL. CALCITE-5516 (testReduceWithNonTypePredicate): ``AVG`` over an INTEGER column
is not the integer-cast quotient of ``SUM`` and ``COUNT``.
"""

import duckdb

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, TableConstraints

SCHEMA = {"emp": ["empno", "sal", "deptno"]}
CONSTRAINTS = {"emp": TableConstraints(not_null=frozenset({"empno", "sal", "deptno"}), keys=(("empno",),))}


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=CONSTRAINTS, dialect="postgres", compare_names=False)


def _run(sql, rows):
    con = duckdb.connect()
    con.execute("CREATE TABLE emp (empno INTEGER, sal INTEGER, deptno INTEGER)")
    con.executemany("INSERT INTO emp VALUES (?, ?, ?)", rows)
    return con.execute(sql).fetchall()


def test_a_case_with_else_zero_is_not_a_filtered_sum():
    case = "SELECT SUM(CASE WHEN deptno = 20 THEN sal ELSE 0 END) FROM emp"
    filtered = "SELECT SUM(sal) FILTER (WHERE deptno = 20) FROM emp"
    assert not _prove(case, filtered).proven
    # the database the report gives: no row of department 20 (and a zero salary keeps SAL NOT NULL satisfied)
    rows = [(1, 0, 70)]
    assert _run(case, rows) == [(0,)] and _run(filtered, rows) == [(None,)]
    # without the ELSE the two are the same query
    assert _prove("SELECT SUM(CASE WHEN deptno = 20 THEN sal END) FROM emp", filtered).proven


def test_avg_is_not_an_integer_cast_quotient():
    avg = "SELECT AVG(sal) FROM emp"
    reduced = "SELECT CAST(CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END / COUNT(*) AS INTEGER) FROM emp"
    assert _prove(avg, reduced).status is not SmtStatus.PROVEN_EQUIVALENT
    rows = [(1, 1, 10), (2, 2, 10)]
    assert _run(avg, rows) == [(1.5,)] and _run(reduced, rows) != _run(avg, rows)  # an integer result, 1 or 2 by the engine's rounding
