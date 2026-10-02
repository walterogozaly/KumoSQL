import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.decorrelation_rules import (
    distinct_lateral_to_in,
    existence_joins,
    extreme_of_top_rows,
    merge_correlated_derived,
    null_comparison_filter,
    push_filter_to_lateral,
    self_witnessed_exists,
)
from kumosql.random_check import Column, Schema, Table, find_difference, prover_constraints

SCHEMA = Schema(
    [
        Table("emp", [Column("empno", not_null=True), Column("deptno"), Column("sal"), Column("job", "text")], keys=[("empno",)]),
        Table("dept", [Column("deptno", not_null=True), Column("name", "text")], keys=[("deptno",)]),
    ]
)


def _prove(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA.columns, constraints=prover_constraints(SCHEMA), dialect="postgres", compare_names=False, exact_arithmetic=True).proven


def _rule(rule, sql: str, inner: bool = True) -> str | None:
    tree = sqlglot.parse_one(sql, read="postgres")
    select = next(s for s in tree.find_all(exp_select()) if s is not tree) if inner else tree
    out = rule(select)
    return tree.sql(dialect="postgres") if out is not None else None


def exp_select():
    from sqlglot import exp

    return exp.Select


EQUIVALENT = [
    # a correlated derived table inside IN reaches the shape IN decorrelation reads
    (
        "SELECT e.sal FROM emp AS e WHERE e.empno IN (SELECT d.k FROM (SELECT x.deptno AS k FROM dept AS x WHERE e.job = x.name) AS d)",
        "SELECT e.sal FROM emp AS e WHERE e.empno IN (SELECT x.deptno FROM dept AS x WHERE e.job = x.name)",
    ),
    # a LATERAL body that reads nothing from the query is a derived table
    (
        "SELECT e.empno, d.n FROM emp AS e LEFT JOIN LATERAL (SELECT x.name AS n FROM dept AS x WHERE FALSE) AS d ON TRUE",
        "SELECT e.empno, NULL AS n FROM emp AS e",
    ),
    # an inner join to one constant row per non-empty input is WHERE EXISTS
    (
        "SELECT e.empno FROM emp AS e CROSS JOIN LATERAL (SELECT TRUE AS t FROM dept AS x WHERE x.deptno = e.deptno GROUP BY TRUE) AS d",
        "SELECT e.empno FROM emp AS e WHERE EXISTS (SELECT 1 FROM dept AS x WHERE x.deptno = e.deptno)",
    ),
    # a lateral DISTINCT set joined by equality is IN
    (
        "SELECT w.empno FROM (SELECT e.empno, e.sal, d.s FROM emp AS e CROSS JOIN LATERAL (SELECT x.sal AS s FROM emp AS x WHERE x.deptno > e.deptno GROUP BY x.sal) AS d) AS w WHERE w.sal = w.s",
        "SELECT e.empno FROM emp AS e WHERE e.sal IN (SELECT x.sal FROM emp AS x WHERE x.deptno > e.deptno)",
    ),
    # comparing with a NULL literal never passes
    ("SELECT e.empno FROM emp AS e WHERE e.sal = NULL", "SELECT e.empno FROM emp AS e WHERE FALSE"),
    # a one-row global aggregate joined on TRUE is the scalar subquery
    (
        "SELECT e.empno, d.m FROM emp AS e LEFT JOIN (SELECT MAX(x.sal) AS m FROM emp AS x WHERE x.deptno = 1) AS d ON TRUE",
        "SELECT e.empno, (SELECT MAX(x.sal) FROM emp AS x WHERE x.deptno = 1) AS m FROM emp AS e",
    ),
    # IN over a DISTINCT set joined on an expression of the outer row
    (
        "SELECT e.job FROM emp AS e WHERE e.deptno IN (SELECT x.deptno FROM emp AS x WHERE x.sal + 1 = e.sal + 1)",
        "SELECT e.job FROM emp AS e JOIN (SELECT x.deptno AS k, x.sal + 1 AS s FROM emp AS x GROUP BY x.deptno, x.sal + 1) AS d ON e.sal + 1 = d.s AND e.deptno = d.k",
    ),
    # the outer row witnesses an EXISTS over its own table on a NOT NULL column
    ("SELECT x.empno FROM emp AS x WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.empno = x.empno)", "SELECT x.empno FROM emp AS x"),
    ("SELECT x.empno FROM emp AS x WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.deptno IS NOT DISTINCT FROM x.deptno)", "SELECT x.empno FROM emp AS x"),
    # tests on the compared column inside IN move out to the outer value
    (
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT e.deptno FROM emp AS e WHERE e.sal > 1 AND e.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'a'))",
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT e.deptno FROM emp AS e WHERE e.sal > 1) AND d.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'a')",
    ),
    (
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT e.deptno FROM emp AS e WHERE e.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'a') AND NOT e.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'b'))",
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'a') AND NOT d.deptno IN (SELECT y.deptno FROM emp AS y WHERE y.job = 'b')",
    ),
]

DIFFERENT = [
    # the lateral body keeps duplicates without GROUP BY, so the join repeats outer rows
    (
        "SELECT e.empno FROM emp AS e CROSS JOIN LATERAL (SELECT x.sal AS s FROM emp AS x WHERE x.deptno = e.deptno) AS d WHERE e.sal = d.s",
        "SELECT e.empno FROM emp AS e WHERE e.sal IN (SELECT x.sal FROM emp AS x WHERE x.deptno = e.deptno)",
    ),
    # a LEFT JOIN LATERAL keeps the outer row with NULLs when the body is empty
    (
        "SELECT e.empno FROM emp AS e LEFT JOIN LATERAL (SELECT TRUE AS t FROM dept AS x WHERE x.deptno = e.deptno GROUP BY TRUE) AS d ON TRUE",
        "SELECT e.empno FROM emp AS e WHERE EXISTS (SELECT 1 FROM dept AS x WHERE x.deptno = e.deptno)",
    ),
    # the correlated filter of the derived table is not dropped
    (
        "SELECT e.sal FROM emp AS e WHERE e.empno IN (SELECT d.k FROM (SELECT x.deptno AS k FROM dept AS x WHERE e.job = x.name) AS d)",
        "SELECT e.sal FROM emp AS e WHERE e.empno IN (SELECT x.deptno FROM dept AS x)",
    ),
    # IS NOT DISTINCT FROM NULL is not a NULL comparison
    ("SELECT e.empno FROM emp AS e WHERE e.sal IS NOT DISTINCT FROM NULL", "SELECT e.empno FROM emp AS e WHERE FALSE"),
    # a nullable column with = does not let the row witness itself
    ("SELECT x.empno FROM emp AS x WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.deptno = x.deptno)", "SELECT x.empno FROM emp AS x"),
    # a NULL-extended row is no witness
    (
        "SELECT d.deptno FROM dept AS d LEFT JOIN emp AS x ON x.deptno = d.deptno WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.empno IS NOT DISTINCT FROM x.empno)",
        "SELECT d.deptno FROM dept AS d LEFT JOIN emp AS x ON x.deptno = d.deptno",
    ),
    # a test on another column of the IN body stays inside
    (
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT e.deptno FROM emp AS e WHERE e.sal IN (SELECT y.sal FROM emp AS y WHERE y.job = 'a'))",
        "SELECT d.name FROM dept AS d WHERE d.deptno IN (SELECT e.deptno FROM emp AS e) AND d.deptno IN (SELECT y.sal FROM emp AS y WHERE y.job = 'a')",
    ),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pairs_are_proved_and_agree_on_random_data(left, right):
    assert find_difference(SCHEMA, left, right, trials=60) is None
    assert _prove(left, right)


@pytest.mark.parametrize("left, right", DIFFERENT)
def test_different_pairs_are_not_proved(left, right):
    assert find_difference(SCHEMA, left, right, trials=200) is not None
    assert not _prove(left, right)


def test_correlated_derived_merge_renames_and_moves_the_filter():
    out = _rule(merge_correlated_derived, "SELECT * FROM emp AS e WHERE EXISTS (SELECT d.k FROM (SELECT x.deptno AS k FROM dept AS x WHERE x.name = e.job) AS d WHERE d.k > 1)")
    assert out is not None and "FROM dept AS kumosql_c" in out and ".deptno > 1" in out and ".name = e.job" in out


def test_correlated_derived_merge_refuses_captures_and_null_extended_sides():
    # the reader declares the alias the derived table reads from outside
    assert _rule(merge_correlated_derived, "SELECT * FROM emp AS e WHERE EXISTS (SELECT 1 FROM (SELECT x.deptno AS k FROM dept AS x WHERE x.name = e.job) AS d, emp AS e)") is None
    # a derived table on the NULL-extended side of a LEFT JOIN keeps its rows filtered before the join
    assert _rule(merge_correlated_derived, "SELECT * FROM emp AS e WHERE EXISTS (SELECT 1 FROM dept AS y LEFT JOIN (SELECT x.deptno AS k FROM dept AS x WHERE x.name = e.job) AS d ON d.k = y.deptno)") is None
    # a grouped derived table is not a filter and projection
    assert _rule(merge_correlated_derived, "SELECT * FROM emp AS e WHERE EXISTS (SELECT 1 FROM (SELECT x.deptno AS k FROM dept AS x WHERE x.name = e.job GROUP BY x.deptno) AS d)") is None


def test_existence_join_needs_constant_outputs_and_grouping():
    assert _rule(existence_joins, "SELECT e.empno FROM emp AS e JOIN (SELECT MAX(x.sal) AS m FROM emp AS x GROUP BY TRUE) AS d ON TRUE", inner=False) is None
    assert _rule(existence_joins, "SELECT e.empno FROM emp AS e JOIN (SELECT 1 AS m FROM emp AS x) AS d ON TRUE", inner=False) is None
    out = _rule(existence_joins, "SELECT e.empno, d.m FROM emp AS e JOIN (SELECT 1 AS m FROM emp AS x GROUP BY TRUE) AS d ON TRUE", inner=False)
    assert out == "SELECT e.empno, 1 AS m FROM emp AS e WHERE EXISTS(SELECT 1 FROM emp AS x)"


def test_distinct_lateral_needs_the_value_read_only_by_the_equality():
    sql = "SELECT e.empno, d.s FROM emp AS e CROSS JOIN LATERAL (SELECT x.sal AS s FROM emp AS x WHERE x.deptno > e.deptno GROUP BY x.sal) AS d WHERE e.sal = d.s"
    assert _rule(distinct_lateral_to_in, sql, inner=False) is None
    assert _rule(push_filter_to_lateral, sql, inner=False) is None


def test_null_comparison_filter():
    assert _rule(null_comparison_filter, "SELECT 1 FROM emp WHERE sal > 1 AND sal <> NULL", inner=False) == "SELECT 1 FROM emp WHERE FALSE"
    assert _rule(null_comparison_filter, "SELECT 1 FROM emp WHERE sal > 1 OR sal = NULL", inner=False) is None


def test_extreme_of_top_rows_needs_nulls_last_and_the_ordered_column():
    def rule(sql: str, dialect: str) -> str | None:
        out = extreme_of_top_rows(sqlglot.parse_one(sql, read=dialect))
        return out.sql(dialect=dialect) if out is not None else None

    sql = "SELECT MAX(d.s) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY 1 DESC LIMIT 1) AS d"
    # MySQL sorts NULLs last when descending; PostgreSQL sorts them first, so its top row may be NULL
    assert rule(sql, "mysql") == "SELECT MAX(d.s) FROM (SELECT e.sal AS s FROM emp AS e) AS d"
    assert rule(sql, "postgres") is None
    # ascending, MySQL puts NULLs first and DuckDB last
    assert rule("SELECT MIN(d.s) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY e.sal LIMIT 3) AS d", "mysql") is None
    assert rule("SELECT MIN(d.s) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY e.sal LIMIT 3) AS d", "duckdb") is not None
    assert rule("SELECT MIN(d.s) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY 1 DESC LIMIT 1) AS d", "mysql") is None
    assert rule("SELECT MAX(d.s), COUNT(*) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY 1 DESC LIMIT 2) AS d", "mysql") is None
    assert rule("SELECT MAX(d.s) FROM (SELECT e.sal AS s FROM emp AS e ORDER BY 1 DESC LIMIT 0) AS d", "mysql") is None
    assert rule("SELECT MAX(d.s) FROM (SELECT e.sal AS s, e.deptno AS k FROM emp AS e ORDER BY 2 DESC LIMIT 1) AS d", "mysql") is None


def test_self_witnessed_exists_needs_the_same_table_and_column():
    assert self_witnessed_exists(sqlglot.parse_one("SELECT 1 FROM emp AS x WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.empno = x.deptno)"), {"emp": frozenset({"empno", "deptno"})}) is None
    assert self_witnessed_exists(sqlglot.parse_one("SELECT 1 FROM emp AS x WHERE EXISTS (SELECT 1 FROM dept AS y WHERE y.deptno = x.deptno)"), {"emp": frozenset({"deptno"}), "dept": frozenset({"deptno"})}) is None
    assert self_witnessed_exists(sqlglot.parse_one("SELECT 1 FROM emp AS x WHERE EXISTS (SELECT 1 FROM emp AS y WHERE y.empno = x.empno AND y.sal > 1)"), {"emp": frozenset({"empno"})}) is None
