"""Grouped CROSS JOIN LATERAL bodies with equality correlations read as inner joins (lateral_decorrelation)."""

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.lateral_decorrelation import decorrelate_grouped_lateral
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"emp": ["empno", "mgr", "sal", "deptno"]}
TYPES = {"emp": {"empno": "INTEGER", "mgr": "INTEGER", "sal": "INTEGER", "deptno": "INTEGER"}}
DIALECT = "duckdb"
# an employee without reports (4, 5), a manager whose reports span two departments (1), and NULLs
ROWS = "(1, NULL, 50, 10), (2, 1, 30, 10), (3, 1, 40, 20), (4, 2, 20, 20), (5, 3, 25, 20), (6, NULL, NULL, NULL), (7, 1, NULL, 20)"

LATERAL = "SELECT t.empno, d.m FROM emp AS t CROSS JOIN LATERAL (SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY e.deptno) AS d"
JOINED = "SELECT t.empno, d.m FROM emp AS t INNER JOIN (SELECT MAX(e.sal) AS m, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr) AS d ON t.empno = d.k"

# Calcite's testDecorrelateAggWithConstantGroupKey, reduced: the correlation sits two derived tables
# down, under a GROUP BY with a constant key, and the top dedups the maxima and correlates on them
CONSTANT_KEY = (
    "SELECT t.empno FROM emp AS t CROSS JOIN LATERAL (SELECT g.m AS m FROM (SELECT a.m AS m FROM "
    "(SELECT b.deptno AS d, b.c AS c, MAX(b.sal) AS m FROM (SELECT e.empno AS empno, e.sal AS sal, e.deptno AS deptno, 'abc' AS c "
    "FROM emp AS e WHERE t.mgr = e.empno) AS b GROUP BY b.deptno, b.c) AS a GROUP BY a.m) AS g WHERE t.sal = g.m) AS l"
)
CONSTANT_KEY_JOINED = (
    "SELECT t.empno FROM emp AS t INNER JOIN (SELECT MAX(e.sal) AS m, e.empno AS k FROM emp AS e GROUP BY e.deptno, e.empno) AS a "
    "ON t.mgr = a.k AND t.sal = a.m"
)


def _prove(left: str, right: str, constraints=None, types=TYPES, schema=SCHEMA):
    return prove_equivalent_algebraic(left, right, schema=schema, dialect=DIALECT, constraints=constraints, types=types)


def _rows(left: str, right: str, ddl: str = "empno INTEGER, mgr INTEGER, sal INTEGER, deptno INTEGER", rows: str = ROWS) -> tuple[list, list]:
    db = duckdb.connect()
    db.execute(f"CREATE TABLE emp({ddl})")
    db.execute(f"INSERT INTO emp VALUES {rows}")
    left_rows, right_rows = run_unoptimized(db, left, right)
    return sorted(left_rows, key=repr), sorted(right_rows, key=repr)


def _rewrite(sql: str, types=TYPES):
    return decorrelate_grouped_lateral(sqlglot.parse_one(sql, read=DIALECT), SCHEMA, types, DIALECT)


def test_grouped_lateral_becomes_inner_join():
    rewritten = _rewrite(LATERAL)
    assert rewritten is not None
    sql = rewritten.sql(dialect=DIALECT)
    assert "LATERAL" not in sql and "INNER JOIN" in sql
    assert "GROUP BY e.deptno, e.mgr" in sql


@pytest.mark.parametrize("rows", [ROWS, "(1, NULL, NULL, NULL)", "(1, 1, 5, 10), (1, 1, 5, 10), (2, 1, NULL, 10)"])
def test_grouped_lateral_proves_against_decorrelated_join(rows):
    left, right = _rows(LATERAL, JOINED, rows=rows)
    assert left == right
    assert _prove(LATERAL, JOINED).proven


def test_constant_group_key_proves_with_the_key():
    keys = {"emp": [("empno",)]}
    not_null = {"emp": frozenset({"empno", "sal", "deptno"})}
    normal = normalize(CONSTANT_KEY, schema=SCHEMA, dialect=DIALECT, keys=keys, not_null=not_null, types=TYPES)
    assert "LATERAL" not in normal
    constraints = {"emp": TableConstraints(not_null=not_null["emp"], keys=(("empno",),))}
    assert _prove(CONSTANT_KEY, CONSTANT_KEY_JOINED, constraints).proven


def test_constant_group_key_without_the_key_is_not_proven():
    # two departments of one manager EMPNO = 1 (EMPNO is no key here) share the maximum 30: the lateral's
    # GROUP BY on the maximum keeps one row, the join two
    left, right = _rows(CONSTANT_KEY, CONSTANT_KEY_JOINED, rows="(1, NULL, 30, 10), (1, NULL, 30, 20), (2, 1, 30, 10)")
    assert left != right
    assert not _prove(CONSTANT_KEY, CONSTANT_KEY_JOINED).proven


NEAR_MISSES = [
    # LEFT JOIN LATERAL keeps an employee without reports (NULL-extended); the inner join drops it
    (LATERAL.replace("CROSS JOIN LATERAL", "LEFT JOIN LATERAL") + " ON TRUE", JOINED),
    # a global aggregate returns its row (COUNT 0) on an empty input: the COUNT bug
    (
        "SELECT t.empno, d.n FROM emp AS t CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM emp AS e WHERE e.mgr = t.empno) AS d",
        "SELECT t.empno, d.n FROM emp AS t INNER JOIN (SELECT COUNT(*) AS n, e.mgr AS k FROM emp AS e GROUP BY e.mgr) AS d ON t.empno = d.k",
    ),
    # GROUP BY () is a global aggregate too; GROUP BY (), e.mgr groups by e.mgr alone
    (
        "SELECT t.empno, d.n FROM emp AS t CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM emp AS e WHERE e.mgr = t.empno GROUP BY ()) AS d",
        "SELECT t.empno, d.n FROM emp AS t INNER JOIN (SELECT COUNT(*) AS n, e.mgr AS k FROM emp AS e GROUP BY (), e.mgr) AS d ON t.empno = d.k",
    ),
    # a non-equality correlation: grouping by the compared column splits the groups
    (
        "SELECT t.empno, d.m FROM emp AS t CROSS JOIN LATERAL (SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr > t.empno GROUP BY e.deptno) AS d",
        "SELECT t.empno, d.m FROM emp AS t INNER JOIN (SELECT MAX(e.sal) AS m, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr) AS d ON d.k > t.empno",
    ),
    # a correlation in the SELECT list: t.deptno is the outer row's department, not the report's
    (
        "SELECT t.empno, d.n, d.dd FROM emp AS t CROSS JOIN LATERAL (SELECT COUNT(*) AS n, t.deptno AS dd FROM emp AS e WHERE e.mgr = t.empno GROUP BY e.deptno) AS d",
        "SELECT t.empno, d.n, d.dd FROM emp AS t INNER JOIN (SELECT COUNT(*) AS n, e.deptno AS dd, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr) AS d ON t.empno = d.k",
    ),
    # the hidden GROUP BY key e.deptno still splits manager 1's reports in two groups
    (LATERAL, "SELECT t.empno, d.m FROM emp AS t INNER JOIN (SELECT MAX(e.sal) AS m, e.mgr AS k FROM emp AS e GROUP BY e.mgr) AS d ON t.empno = d.k"),
    # LIMIT reads the whole correlated row set, not each group of the decorrelated one
    (
        "SELECT t.empno, d.m FROM emp AS t CROSS JOIN LATERAL (SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY e.deptno ORDER BY m LIMIT 1) AS d",
        "SELECT t.empno, d.m FROM emp AS t INNER JOIN (SELECT MAX(e.sal) AS m, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr ORDER BY m LIMIT 1) AS d ON t.empno = d.k",
    ),
    # IS NOT DISTINCT FROM matches a NULL manager to the NULL-manager rows; = matches nothing
    (
        "SELECT t.empno, d.n FROM emp AS t CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM emp AS e WHERE e.mgr IS NOT DISTINCT FROM t.mgr GROUP BY e.deptno) AS d",
        "SELECT t.empno, d.n FROM emp AS t INNER JOIN (SELECT COUNT(*) AS n, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr) AS d ON t.mgr = d.k",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_are_not_proven(left, right):
    left_rows, right_rows = _rows(left, right)
    assert left_rows != right_rows
    assert not _prove(left, right).proven


def test_lossy_correlation_type_is_not_grouped_by():
    # BIGINT 2**53 + 1 = DOUBLE 2**53 holds, so the correlated filter keeps both employees in one
    # department group (COUNT 2) while grouping by the employee number splits them (COUNT 1, twice)
    types = {"emp": {"empno": "BIGINT", "mgr": "BIGINT", "sal": "DOUBLE", "deptno": "INTEGER"}}
    left = "SELECT t.empno, d.n FROM emp AS t CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM emp AS e WHERE e.empno = t.sal GROUP BY e.deptno) AS d"
    right = "SELECT t.empno, d.n FROM emp AS t INNER JOIN (SELECT COUNT(*) AS n, e.empno AS k FROM emp AS e GROUP BY e.deptno, e.empno) AS d ON t.sal = d.k"
    rows = "(9007199254740992, 1, 9007199254740992.0, 10), (9007199254740993, 1, 0.0, 10)"
    left_rows, right_rows = _rows(left, right, "empno BIGINT, mgr BIGINT, sal DOUBLE, deptno INTEGER", rows)
    assert left_rows != right_rows
    assert _rewrite(left, types) is None
    assert not _prove(left, right, types=types).proven


def test_grouped_correlation_needs_known_types():
    assert _rewrite(LATERAL, types=None) is None
    assert _rewrite(LATERAL, types={"emp": {"empno": "INTEGER"}}) is None  # mgr's type is not known


@pytest.mark.parametrize(
    "body",
    [
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY ROLLUP (e.deptno)",  # a grand total on no input
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY CUBE (e.deptno)",
        "SELECT PRODUCT(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno",  # an aggregate sqlglot does not know
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY ALL",
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno OR e.sal > 1 GROUP BY e.deptno",
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno GROUP BY e.deptno HAVING MAX(e.sal) > t.sal",
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno + 0 GROUP BY e.deptno",
        "SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = t.empno AND RANDOM() > 0.5 GROUP BY e.deptno",
        "SELECT MAX(t.sal) AS m FROM emp AS t WHERE t.mgr = t.empno GROUP BY t.deptno",  # the body's own t shadows the outer one
    ],
)
def test_unsupported_bodies_are_left_alone(body):
    assert _rewrite(f"SELECT t.empno, d.m FROM emp AS t CROSS JOIN LATERAL ({body}) AS d") is None


def test_outer_query_that_could_see_new_columns_is_left_alone():
    assert _rewrite(LATERAL.replace("SELECT t.empno, d.m", "SELECT t.empno, d.*")) is None
    assert _rewrite(LATERAL.replace("SELECT t.empno, d.m", "SELECT t.empno, d")) is None  # the row of d
    assert _rewrite(LATERAL + " NATURAL JOIN emp AS u") is None
