"""The three SQLSolver pairs the first re-check could not run on DuckDB, made runnable.

Each is a pair the eval proves and counts. DuckDB rejected its SQL (so the heavy search had nothing to run):

* Calcite 0: ``UNIX_TIMESTAMP('12:34:56')`` on both sides (DuckDB has no ``unix_timestamp``).
* Calcite 193: scalar subqueries in a LEFT JOIN condition (DuckDB: "Cannot perform non-inner join on
  subquery") and Calcite's ``SINGLE_VALUE`` aggregate on the other side.
* Spark 32: ``CASE`` branches of type BIGINT and VARCHAR (DuckDB wants an explicit cast; Spark promotes to string).

The prover reads the same source SQL as before; only the DuckDB SQL the search runs changes, and only for
these pairs (each override is guarded by the exact source text, so a changed fixture falls back to the
plain translation). These adapters replace the ``sqlsolver-calcite`` and ``sqlsolver-spark`` ones of
``calcite_family`` (modules load in name order and a later ``ADAPTERS`` entry wins); every other pair is
translated exactly as before.
"""

from __future__ import annotations

from recheck.calcite_family import SqlSolver
from recheck.engine import Case

# Calcite's SINGLE_VALUE aggregate: the value of a one-row group, an error for more rows (like MySQL's
# "Subquery returns more than 1 row"). UNIX_TIMESTAMP of a string: seconds since the epoch, 0 when the text
# is not a date (MySQL's answer for an invalid value); only constants reach it in these pairs.
MACROS = (
    "CREATE MACRO single_value(x) AS CASE WHEN count(*) > 1 THEN error('single_value: more than one row') ELSE any_value(x) END",
    "CREATE MACRO unix_timestamp(x) AS COALESCE(CAST(epoch(TRY_CAST(x AS TIMESTAMP)) AS BIGINT), 0)",
)

# (suite, pair) -> (expected left source, expected right source, DuckDB left, DuckDB right)
OVERRIDES = {
    (
        "calcite",
        "193",
    ): (
        "SELECT EMP.EMPNO FROM EMP AS EMP LEFT JOIN DEPT AS DEPT ON (((SELECT EMP0.DEPTNO FROM EMP AS EMP0 WHERE EMP0.EMPNO < 20))) < (((SELECT EMP1.DEPTNO FROM EMP AS EMP1 WHERE EMP1.EMPNO > 100)))",
        # the two scalar subqueries do not depend on EMP or DEPT: one row of values joined to every EMP row, then the same LEFT JOIN
        "SELECT EMP.EMPNO FROM EMP AS EMP CROSS JOIN (SELECT (SELECT EMP0.DEPTNO FROM EMP AS EMP0 WHERE EMP0.EMPNO < 20) AS s1,"
        " (SELECT EMP1.DEPTNO FROM EMP AS EMP1 WHERE EMP1.EMPNO > 100) AS s2) AS sv LEFT JOIN DEPT AS DEPT ON sv.s1 < sv.s2",
        None,  # the right side only needs the single_value macro
    ),
    (
        "spark",
        "32",
    ): (
        "SELECT CASE WHEN deptno = 1 THEN deptno WHEN TRUE THEN name WHEN deptno = 10 THEN 10 ELSE deptno + 1 END FROM dept",
        # Spark promotes the integer branches to string when another branch is a string
        "SELECT CASE WHEN deptno = 1 THEN CAST(deptno AS VARCHAR) WHEN TRUE THEN name WHEN deptno = 10 THEN CAST(10 AS VARCHAR)"
        " ELSE CAST(deptno + 1 AS VARCHAR) END FROM dept",
        "SELECT CASE WHEN deptno = 1 THEN CAST(deptno AS VARCHAR) WHEN TRUE THEN name END FROM dept",
    ),
}


class RunnableSqlSolver(SqlSolver):
    """``SqlSolver`` with the macros every case needs and the DuckDB SQL of the overridden pairs."""

    def case(self, item: dict) -> Case | None:
        case = super().case(item)
        if case is None:
            return None
        override = OVERRIDES.get((self.suite, item["pair"]))
        left, right = case.left, case.right
        if override is not None and tuple(case.source) == (item["left"], item["right"]) and item["left"] == override[0]:
            left = override[1] or left
            right = override[2] or right
            case.meta = {**case.meta, "runnable_fix": "hand-translated for DuckDB"}
        case.left, case.right = left, right
        case.setup = (*case.setup, *MACROS)
        return case


ADAPTERS = {a.name: a for a in [RunnableSqlSolver("calcite"), RunnableSqlSolver("spark")]}
