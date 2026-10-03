"""A constant regrouping (``GROUP BY TRUE``) of a constant grouping reads the inner select's one row."""

import pytest

pytest.importorskip("z3")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.constant_regroup_rules import collapse_constant_regroup  # noqa: E402

SCHEMA = {"emp": ["empno", "deptno"], "t": ["a", "x"]}

# VeriEQL Calcite 336: an aggregate pushed into each UNION ALL branch, each branch with its own constant key
UNION_LEFT = (
    "SELECT COUNT(T1), T2 FROM (SELECT CASE WHEN DEPTNO = 0 THEN 1 ELSE NULL END AS T1, 1 AS T2 FROM EMP "
    "UNION ALL SELECT CASE WHEN DEPTNO = 0 THEN 1 ELSE NULL END AS T1, 2 AS T2 FROM EMP) AS t1 GROUP BY T2"
)
UNION_RIGHT = (
    "SELECT COALESCE(SUM(EXPR$0), 0), T2 FROM (SELECT t5.T2, COUNT(CASE WHEN EMP1.DEPTNO = 0 THEN 1 ELSE NULL END) AS EXPR$0 "
    "FROM EMP AS EMP1, (VALUES (1)) AS t5 (T2) GROUP BY t5.T2 UNION ALL SELECT t8.T2, "
    "COUNT(CASE WHEN EMP2.DEPTNO = 0 THEN 1 ELSE NULL END) AS EXPR$0 FROM EMP AS EMP2, (VALUES (2)) AS t8 (T2) "
    "GROUP BY t8.T2) AS t11 GROUP BY T2"
)

COUNTED = "SELECT COUNT(x) AS n, 1 AS k FROM t GROUP BY TRUE"


def _prove(left: str, right: str, dialect: str = "bigquery") -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect=dialect).proven


def _differ_on_empty_t(left: str, right: str) -> bool:
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE t (a INTEGER, x INTEGER)")
    rows_left, rows_right = run_unoptimized(db, left, right)
    return sorted(map(repr, rows_left)) != sorted(map(repr, rows_right))


def test_rule_reads_the_inner_row():
    sql = "SELECT SUM(c) AS n, MAX(m) AS hi, 1 AS k FROM (SELECT COUNT(x) AS c, MAX(a) AS m FROM t GROUP BY TRUE) AS d GROUP BY TRUE"
    rewritten = collapse_constant_regroup(sqlglot.parse_one(sql))
    assert rewritten.sql() == "SELECT COUNT(x) AS n, MAX(a) AS hi, 1 AS k FROM t GROUP BY TRUE"


def test_constant_regroup_proves():
    sql = "SELECT SUM(c) AS n, 1 AS k FROM (SELECT COUNT(x) AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE"
    assert _prove(sql, COUNTED)


def test_count_pushed_into_constant_keyed_union_branches_proves():
    assert _prove(UNION_LEFT, UNION_RIGHT, dialect="mysql")


def test_global_outer_aggregate_is_left_alone():
    # on an empty t the outer global aggregate returns one row (NULL, 1), the grouped one no row
    sql = "SELECT SUM(c) AS n, 1 AS k FROM (SELECT COUNT(x) AS c FROM t GROUP BY TRUE) AS d"
    assert collapse_constant_regroup(sqlglot.parse_one(sql)) is None
    assert _differ_on_empty_t(sql, COUNTED)
    assert not _prove(sql, COUNTED)


def test_global_inner_aggregate_is_left_alone():
    # the inner global COUNT returns a row (0) on an empty t, so the outer group exists: (0, 1) against no row
    sql = "SELECT SUM(c) AS n, 1 AS k FROM (SELECT COUNT(x) AS c FROM t) AS d GROUP BY TRUE"
    assert collapse_constant_regroup(sqlglot.parse_one(sql)) is None
    assert _differ_on_empty_t(sql, COUNTED)
    assert not _prove(sql, COUNTED)


def test_inner_grouping_by_a_column_is_left_alone():
    # several inner groups fold into one outer group: the sum of their counts, not one count per a
    sql = "SELECT SUM(c) AS n, 1 AS k FROM (SELECT a, COUNT(x) AS c FROM t GROUP BY a) AS d GROUP BY TRUE"
    per_a = "SELECT COUNT(x) AS n, 1 AS k FROM t GROUP BY a"
    assert collapse_constant_regroup(sqlglot.parse_one(sql)) is None
    assert not _prove(sql, per_a)


@pytest.mark.parametrize(
    "outer",
    [
        "COUNT(c)",  # a count of the one row, not its value
        "SUM(c) + 0 * 0",  # arithmetic around the aggregate is not read
    ],
)
def test_other_outer_items_are_left_alone(outer):
    sql = f"SELECT {outer} AS n FROM (SELECT COUNT(x) AS c FROM t GROUP BY TRUE) AS d GROUP BY TRUE"
    assert collapse_constant_regroup(sqlglot.parse_one(sql)) is None


def test_outer_filter_is_left_alone():
    sql = "SELECT SUM(c) AS n FROM (SELECT COUNT(x) AS c FROM t GROUP BY TRUE) AS d WHERE c > 1 GROUP BY TRUE"
    assert collapse_constant_regroup(sqlglot.parse_one(sql)) is None
