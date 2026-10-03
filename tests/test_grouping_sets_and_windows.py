"""ROLLUP / CUBE / GROUPING SETS expansion, key-only HAVING, and window rewrites (cluster 20)."""

from collections import Counter

import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.grouping_sets import expand_grouping_sets
from kumosql.having_rules import key_having_to_where
from kumosql.window_rules import never_null_counts

SCHEMA = {
    "emp": ["empno", "ename", "job", "mgr", "sal", "comm", "deptno"],
    "bonus": ["ename", "job", "sal", "comm"],
    "emps": ["empid", "deptno", "salary"],
}


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def _sets(sql: str) -> str:
    return expand_grouping_sets(sqlglot.parse_one(sql, read="mysql")).sql(dialect="mysql")


def test_rollup_cube_and_mixed_lists_spell_out_their_sets():
    assert _sets("SELECT a FROM t GROUP BY ROLLUP(a, b)") == "SELECT a FROM t GROUP BY GROUPING SETS ((a, b), (a), ())"
    assert _sets("SELECT a FROM t GROUP BY CUBE(a, b)") == "SELECT a FROM t GROUP BY GROUPING SETS ((a, b), (a), (b), ())"
    assert _sets("SELECT a FROM t GROUP BY x, ROLLUP(a)") == "SELECT a FROM t GROUP BY GROUPING SETS ((x, a), (x))"
    assert _sets("SELECT a FROM t GROUP BY a, b WITH ROLLUP") == "SELECT a FROM t GROUP BY GROUPING SETS ((a, b), (a), ())"
    try:
        nested = _sets("SELECT a FROM t GROUP BY GROUPING SETS (a, ROLLUP(b))")
    except sqlglot.errors.ParseError:
        return  # older sqlglot cannot parse a ROLLUP inside GROUPING SETS
    assert nested == "SELECT a FROM t GROUP BY GROUPING SETS ((a), (b), ())"


def test_a_repeated_set_is_left_alone():
    # engines disagree on whether a repeated set repeats its groups
    assert "ROLLUP" in _sets("SELECT a FROM t GROUP BY a, ROLLUP(a)")
    assert "GROUPING SETS ((a), (a))" in _sets("SELECT a FROM t GROUP BY GROUPING SETS ((a), (a))")


def test_rollup_over_a_union_is_proven_against_the_pushed_down_aggregate():
    left = "SELECT deptno, job, SUM(mgr) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t1 GROUP BY ROLLUP(deptno, job)"
    right = (
        "SELECT deptno, job, SUM(s) FROM (SELECT deptno, job, SUM(mgr) AS s FROM emp GROUP BY deptno, job "
        "UNION ALL SELECT deptno, job, SUM(mgr) AS s FROM emp GROUP BY deptno, job) AS t8 GROUP BY ROLLUP(deptno, job)"
    )
    assert _proven(left, right)


def test_rollup_with_a_filter_below_or_above_the_grouping():
    left = "SELECT ename, sal, deptno FROM emp WHERE sal > 5000 GROUP BY ROLLUP(ename, sal, deptno)"
    right = (
        "SELECT ename, sal, deptno FROM (SELECT ename, sal, deptno FROM emp GROUP BY ename, sal, deptno HAVING sal > 5000) AS t4 "
        "GROUP BY ROLLUP(ename, sal, deptno)"
    )
    assert _proven(left, right)


def test_the_empty_set_is_one_row_even_without_an_aggregate():
    # () is one group: without an aggregate it is still one row, not one row per input row
    left = "SELECT deptno FROM emp GROUP BY GROUPING SETS ((deptno), ())"
    assert not _proven(left, "SELECT deptno FROM emp GROUP BY deptno UNION ALL SELECT NULL AS deptno FROM emp")
    assert _proven(left, "SELECT deptno FROM emp GROUP BY deptno UNION ALL SELECT NULL AS deptno")


def test_a_missing_key_is_null_only_in_the_grouped_select_itself():
    # the derived table's own columns keep their values; only the grouped select's list reads NULL
    left = "SELECT deptno FROM (SELECT deptno, job FROM emp WHERE job = 'x') t GROUP BY GROUPING SETS ((deptno), (job))"
    wrong = (
        "SELECT deptno FROM (SELECT deptno, job FROM emp WHERE job = 'x') t GROUP BY deptno "
        "UNION ALL SELECT NULL AS deptno FROM (SELECT deptno, NULL AS job FROM emp WHERE NULL = 'x') t GROUP BY job"
    )
    assert not _proven(left, wrong)


def test_global_sum_of_counts_is_not_the_count():
    # over no rows SUM is NULL while COUNT is 0
    assert not _proven("SELECT COUNT(*) FROM emps", "SELECT SUM(c) FROM (SELECT empid, COUNT(*) AS c FROM emps GROUP BY empid) t")
    assert not _proven(
        "SELECT COUNT(*) + 1 AS c, deptno FROM emps GROUP BY CUBE(deptno, empid)",
        "SELECT SUM(mv0.c) + 1 AS c, mv0.deptno FROM (SELECT empid, deptno, COUNT(*) AS c FROM emps GROUP BY empid, deptno) mv0 "
        "GROUP BY CUBE(mv0.deptno, mv0.empid)",
    )
    assert _proven(
        "SELECT deptno, COUNT(*) FROM emps GROUP BY deptno",
        "SELECT deptno, SUM(c) FROM (SELECT empid, deptno, COUNT(*) AS c FROM emps GROUP BY empid, deptno) t GROUP BY deptno",
    )


def _having(sql: str) -> str | None:
    out = key_having_to_where(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def test_key_only_having_moves_to_where():
    assert _having("SELECT a, SUM(b) FROM t GROUP BY a HAVING a > 1 AND SUM(b) > 2") == "SELECT a, SUM(b) FROM t WHERE a > 1 GROUP BY a HAVING SUM(b) > 2"
    assert _having("SELECT COUNT(*) FROM t HAVING 1 = 1") is None  # a global aggregate keeps its row
    assert _having("SELECT a + 1 AS a, SUM(b) FROM t GROUP BY a HAVING a > 1") is None  # the alias shadows the key


def test_window_over_a_join_of_projections():
    left = (
        "SELECT emp.sal + bonus.comm, SUM(bonus.sal + bonus.sal + 100) OVER (PARTITION BY bonus.job) "
        "FROM emp INNER JOIN bonus ON emp.ename = bonus.ename AND emp.deptno = 10"
    )
    right = (
        "SELECT t0.sal + t1.comm, SUM(t1.x) OVER (PARTITION BY t1.job) FROM (SELECT ename, sal, deptno = 10 AS x FROM emp) AS t0 "
        "INNER JOIN (SELECT ename, job, comm, sal + sal + 100 AS x FROM bonus) AS t1 ON t0.ename = t1.ename AND t0.x"
    )
    assert _proven(left, right)


def test_window_over_a_union_with_the_projection_pushed_into_its_branches():
    left = "SELECT job, SUM(sal + 100) OVER (PARTITION BY deptno) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t1"
    right = (
        "SELECT job, SUM(x) OVER (PARTITION BY deptno) FROM "
        "(SELECT job, deptno, sal + 100 AS x FROM emp UNION ALL SELECT job, deptno, sal + 100 AS x FROM emp) AS t7"
    )
    assert _proven(left, right)
    # the window sums before the +100 on one side only: different
    assert not _proven(left, right.replace("SUM(x)", "SUM(x) + 100"))


def test_star_over_a_union_keeps_its_column_order():
    assert not _proven(
        "SELECT * FROM (SELECT job, sal FROM emp UNION ALL SELECT job, sal FROM emp) t",
        "SELECT * FROM (SELECT sal, job FROM emp UNION ALL SELECT sal, job FROM emp) t",
    )


def test_a_count_is_never_null():
    sql = "SELECT n FROM (SELECT COUNT(empno) OVER (PARTITION BY deptno) AS n FROM emp) AS d WHERE n IS NULL"
    out = never_null_counts(sqlglot.parse_one(sql, read="mysql"))
    assert out is not None and out.sql(dialect="mysql").endswith("WHERE FALSE")
    joined = "SELECT n FROM emp LEFT JOIN (SELECT COUNT(*) AS n FROM emp) AS d ON TRUE WHERE n IS NULL"
    assert never_null_counts(sqlglot.parse_one(joined, read="mysql")) is None
    assert _proven(sql, "SELECT n FROM (SELECT COUNT(empno) OVER (PARTITION BY deptno) AS n FROM emp) AS d WHERE FALSE")


def test_weighted_sum_of_a_count_of_nulls_is_zero():
    # every x NULL: COUNT(x) is 0, so SUM(c * w) is 0; the flat rewrite must add 0 * w, not NULL
    schema = {"t": ["k", "x"], "u": ["k", "w"]}
    left = "SELECT u.k, SUM(m.c * u.w) AS s FROM u JOIN (SELECT k, COUNT(x) AS c FROM t GROUP BY k) m ON m.k = u.k GROUP BY u.k"
    wrong = "SELECT u.k, SUM(CASE WHEN NOT t.x IS NULL THEN u.w END) AS s FROM u JOIN t ON t.k = u.k GROUP BY u.k"
    right = "SELECT u.k, SUM(CASE WHEN NOT t.x IS NULL THEN u.w ELSE 0 * u.w END) AS s FROM u JOIN t ON t.k = u.k GROUP BY u.k"
    assert not prove_equivalent_algebraic(left, wrong, schema=schema, dialect="mysql").proven
    assert prove_equivalent_algebraic(left, right, schema=schema, dialect="mysql").proven


def test_rollup_over_a_union_of_named_columns_prunes_like_a_star():
    # the derived table's own column names are not reads of it, so unused union columns drop on both sides
    left = "SELECT deptno, job, AVG(empno) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t1 GROUP BY ROLLUP(deptno, job)"
    right = (
        "SELECT deptno, job, AVG(empno) FROM (SELECT deptno, job, empno FROM emp UNION ALL SELECT deptno, job, empno FROM emp) AS t6 "
        "GROUP BY ROLLUP(deptno, job)"
    )
    assert _proven(left, right)


def test_a_constant_of_a_null_extended_derived_table_is_not_inlined():
    # an unmatched p row reads d.i as NULL, not 1
    schema = {"p": ["id", "k"], "q": ["id", "k"]}
    for body in ("SELECT k, 1 AS i FROM q", "SELECT q.k, 1 AS i FROM q", "SELECT q.k, COALESCE(q.id, 0) AS i FROM q"):
        left = f"SELECT p.id FROM p LEFT JOIN ({body}) AS d ON p.k = d.k WHERE d.i IS NULL"
        assert not prove_equivalent_algebraic(left, "SELECT p.id FROM p WHERE 1 = 2", schema=schema, dialect="mysql").proven
    # an expression that is NULL on NULL input still folds in
    assert prove_equivalent_algebraic(
        "SELECT p.id, d.i FROM p LEFT JOIN (SELECT q.k, q.id + 1 AS i FROM q) AS d ON p.k = d.k",
        "SELECT p.id, q.id + 1 AS i FROM p LEFT JOIN q ON p.k = q.k",
        schema=schema, dialect="mysql",
    ).proven


def test_global_sum_of_counts_under_nullif_is_the_count():
    # NULLIF(.., 0) reads the NULL of an empty SUM and the 0 of an empty COUNT alike
    def proven(right: str) -> bool:
        return prove_equivalent_algebraic(
            "SELECT AVG(sal) FROM emp", right, schema=SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True
        ).proven

    right = "SELECT SUM(t.s) / NULLIF(SUM(t.n), 0) FROM (SELECT deptno, SUM(sal) AS s, COUNT(sal) AS n FROM emp GROUP BY deptno) AS t"
    assert proven(right)
    assert not proven(right.replace("NULLIF(SUM(t.n), 0)", "NULLIF(SUM(t.n), 1)"))


def _rows_differ(left: str, right: str, rows: list[tuple]) -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE t (k BIGINT)")
    for row in rows:
        db.execute("INSERT INTO t VALUES (?)", row)
    first, second = run_unoptimized(db, left, right)
    return Counter(first) != Counter(second)


NESTED_COUNT = "SELECT k, (SELECT COUNT(*)) AS c FROM t GROUP BY ROLLUP(k)"


@pytest.mark.parametrize(
    "rows",
    [pytest.param([], id="s007-002-empty-input"), pytest.param([(1,), (2,), (3,)], id="s007-002-three-rows")],
)
def test_an_aggregate_of_a_nested_query_does_not_make_the_empty_set_an_aggregate(rows):
    # COUNT(*) belongs to the source-free subquery; the grand total is still one row, not one per input row
    right = "SELECT k, (SELECT COUNT(*)) AS c FROM t GROUP BY k UNION ALL SELECT NULL AS k, (SELECT COUNT(*)) AS c FROM t"
    assert _rows_differ(NESTED_COUNT, right, rows)
    assert not prove_equivalent_algebraic(NESTED_COUNT, right, dialect="bigquery").proven


@pytest.mark.parametrize(
    "left, right",
    [
        pytest.param(
            NESTED_COUNT,
            "SELECT k, (SELECT COUNT(*)) AS c FROM t GROUP BY k UNION ALL SELECT NULL AS k, (SELECT COUNT(*)) AS c",
            id="s007-002-near-miss-one-grand-total-row",
        ),
        pytest.param(
            "SELECT k, (SELECT COUNT(*)) AS c, COUNT(*) AS n FROM t GROUP BY ROLLUP(k)",
            "SELECT k, (SELECT COUNT(*)) AS c, COUNT(*) AS n FROM t GROUP BY k "
            "UNION ALL SELECT NULL AS k, (SELECT COUNT(*)) AS c, COUNT(*) AS n FROM t",
            id="s007-002-near-miss-own-aggregate-too",
        ),
    ],
)
def test_a_nested_aggregate_next_to_the_grand_total_stays_proven(left, right):
    for rows in ([], [(1,), (2,), (3,)], [(None,), (1,), (1,)]):
        assert not _rows_differ(left, right, rows)
    assert prove_equivalent_algebraic(left, right, dialect="bigquery").proven
