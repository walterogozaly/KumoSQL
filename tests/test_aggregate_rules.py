import sqlglot

from kumosql import aggregate_rules as rules
from kumosql.algebraic_equivalence import prove_equivalent_algebraic

SCHEMA = {"emp": ["empno", "ename", "sal", "comm", "deptno", "mgr"], "dept": ["deptno", "name"]}


def _rule(rule, sql):
    out = rule(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def test_global_aggregate_over_no_rows_is_constants():
    assert _rule(rules._empty_global_aggregate, "SELECT COUNT(*), COALESCE(SUM(sal), 0) AS s, MAX(sal) FROM emp WHERE FALSE") == "SELECT 0, 0 AS s, NULL"
    assert _rule(rules._empty_global_aggregate, "SELECT deptno, COUNT(*) FROM emp WHERE FALSE GROUP BY deptno") is None
    assert _rule(rules._empty_global_aggregate, "SELECT COUNT(*) FROM emp WHERE sal > 1") is None


def test_aggregate_without_from_reads_one_row():
    assert _rule(rules._fromless_aggregate, "SELECT COUNT(*) AS c, MAX(2)") == "SELECT 1 AS c, 2"
    assert _rule(rules._fromless_aggregate, "SELECT COUNT(*) FROM emp") is None


def test_filter_shared_by_every_aggregate_moves_to_where():
    out = _rule(rules._pull_shared_filter, "SELECT SUM(CASE WHEN deptno = 10 THEN sal END), COUNT(CASE WHEN deptno = 10 THEN 1 END) FROM emp")
    assert out == "SELECT SUM(sal), COUNT(*) FROM emp WHERE deptno = 10"
    out = _rule(rules._pull_shared_filter, "SELECT MAX(CASE WHEN deptno = 10 AND sal > 5 THEN sal END), MIN(CASE WHEN deptno = 10 THEN sal END) FROM emp")
    assert out == "SELECT MAX(CASE WHEN sal > 5 THEN sal END), MIN(sal) FROM emp WHERE deptno = 10"
    assert _proven(
        "SELECT SUM(sal), COUNT(*) FROM emp WHERE deptno = 10",
        "SELECT SUM(CASE WHEN deptno = 10 THEN sal END), COUNT(CASE WHEN deptno = 10 THEN 1 END) FROM emp",
    )


def test_filtered_aggregates_that_differ_are_not_pulled():
    # COUNT(*) counts the rows the filter would drop; a group would vanish; ELSE 0 is not NULL.
    assert _rule(rules._pull_shared_filter, "SELECT SUM(CASE WHEN deptno = 10 THEN sal END), COUNT(*) FROM emp") is None
    assert _rule(rules._pull_shared_filter, "SELECT deptno, SUM(CASE WHEN sal > 1 THEN sal END) FROM emp GROUP BY deptno") is None
    assert _rule(rules._pull_shared_filter, "SELECT SUM(CASE WHEN deptno = 10 THEN sal ELSE 0 END) FROM emp") is None
    assert not _proven("SELECT COUNT(*) FROM emp WHERE deptno = 10", "SELECT COUNT(CASE WHEN deptno = 10 THEN 1 ELSE 0 END) FROM emp")
    assert not _proven("SELECT SUM(sal) FROM emp WHERE deptno = 10", "SELECT SUM(CASE WHEN deptno = 10 THEN sal ELSE 0 END) FROM emp")


def test_aggregate_of_a_key_expression_is_the_expression():
    out = _rule(rules._key_expression_aggregates, "SELECT sal, MAX(sal * 2), MIN(-sal), COUNT(DISTINCT sal + deptno) FROM emp GROUP BY sal, deptno")
    assert out == "SELECT sal, (sal * 2), -sal, CASE WHEN sal + deptno IS NULL THEN 0 ELSE 1 END FROM emp GROUP BY sal, deptno"
    assert _rule(rules._key_expression_aggregates, "SELECT sal, MAX(sal + comm) FROM emp GROUP BY sal") is None
    assert _rule(rules._key_expression_aggregates, "SELECT sal, SUM(sal * 2) FROM emp GROUP BY sal") is None
    assert _rule(rules._key_expression_aggregates, "SELECT sal, MAX(sal * 2) OVER () FROM emp GROUP BY sal") is None


def test_distinct_count_of_a_key_above_one_is_never_true():
    out = _rule(rules._key_expression_aggregates, "SELECT empno FROM emp GROUP BY empno HAVING COUNT(DISTINCT empno) > 2")
    assert out == "SELECT empno FROM emp GROUP BY empno HAVING FALSE"


def test_count_of_a_value_the_where_clause_keeps_non_null():
    out = _rule(rules._count_of_filtered_value, "SELECT deptno, COUNT(mgr), COUNT(comm) FROM emp WHERE mgr = empno GROUP BY deptno")
    assert out == "SELECT deptno, COUNT(*), COUNT(comm) FROM emp WHERE mgr = empno GROUP BY deptno"
    assert _rule(rules._count_of_filtered_value, "SELECT COUNT(mgr) FROM emp WHERE mgr = 1 OR sal = 2") is None
    assert _rule(rules._count_of_filtered_value, "SELECT COUNT(mgr) FROM emp WHERE mgr <=> empno") is None


def test_having_that_every_group_meets_is_dropped():
    assert _rule(rules._drop_nonempty_group_having, "SELECT deptno FROM emp GROUP BY deptno HAVING COUNT(*) >= 1") == "SELECT deptno FROM emp GROUP BY deptno"
    assert _rule(rules._drop_nonempty_group_having, "SELECT COUNT(*) FROM emp HAVING COUNT(*) >= 1") is None
    assert _proven(
        "SELECT DISTINCT mgr FROM emp WHERE mgr = empno",
        "SELECT mgr FROM emp WHERE mgr = empno GROUP BY mgr HAVING COUNT(empno) >= 1",
    )


def test_coalesce_of_a_summed_count_per_group():
    union = "(SELECT deptno, COUNT(*) AS c FROM emp GROUP BY deptno UNION ALL SELECT deptno, COUNT(*) AS c FROM emp GROUP BY deptno) AS u"
    assert _rule(rules._coalesce_counted_sum, f"SELECT u.deptno, COALESCE(SUM(u.c), 0) FROM {union} GROUP BY u.deptno") == (
        f"SELECT u.deptno, SUM(u.c) FROM {union} GROUP BY u.deptno"
    )
    # a global sum over no rows is NULL, and a summed column that can be NULL keeps its COALESCE
    assert _rule(rules._coalesce_counted_sum, f"SELECT COALESCE(SUM(u.c), 0) FROM {union}") is None
    sums = "(SELECT deptno, SUM(sal) AS c FROM emp GROUP BY deptno) AS u"
    assert _rule(rules._coalesce_counted_sum, f"SELECT u.deptno, COALESCE(SUM(u.c), 0) FROM {sums} GROUP BY u.deptno") is None
    assert _proven(
        "SELECT ename, COUNT(*) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY ename",
        "SELECT ename, COALESCE(SUM(c), 0) FROM (SELECT ename, COUNT(*) AS c FROM emp GROUP BY ename UNION ALL SELECT ename, COUNT(*) AS c FROM emp GROUP BY ename) AS u GROUP BY ename",
    )


def test_filter_on_a_derived_aggregate_becomes_having():
    out = _rule(rules._filter_into_having, "SELECT MAX(d.num) FROM (SELECT num, COUNT(*) AS c FROM t GROUP BY num) AS d WHERE d.c = 1")
    assert out == "SELECT MAX(d.num) FROM (SELECT num, COUNT(*) AS c FROM t GROUP BY num HAVING COUNT(*) = 1) AS d"
    # a limit picks groups before the filter; a bare non-key column has no single value
    assert _rule(rules._filter_into_having, "SELECT * FROM (SELECT num, COUNT(*) AS c FROM t GROUP BY num LIMIT 2) AS d WHERE d.c = 1") is None
    assert _rule(rules._filter_into_having, "SELECT * FROM (SELECT num, x FROM t GROUP BY num) AS d WHERE d.x = 1") is None


def test_expression_of_derived_aggregates_is_computed_outside():
    out = _rule(
        rules._lift_aggregate_expressions,
        "SELECT e.ename, e.sal * d.r FROM emp AS e JOIN (SELECT deptno, CASE WHEN MAX(sal) > 10 THEN 1 ELSE 2 END AS r FROM emp GROUP BY deptno) AS d ON e.deptno = d.deptno",
    )
    assert out.startswith("SELECT e.ename, e.sal * (CASE WHEN d.kumosql_lift")
    assert "(SELECT deptno, MAX(sal) AS kumosql_lift" in out
    # a NULL-extended row would read f(NULL) outside but NULL inside
    left = "SELECT e.ename, d.r FROM emp AS e LEFT JOIN (SELECT deptno, COALESCE(MAX(sal), 0) AS r FROM emp GROUP BY deptno) AS d ON e.deptno = d.deptno"
    assert _rule(rules._lift_aggregate_expressions, left) is None
    assert _proven(
        "SELECT e.ename, CASE WHEN d.m > 10 THEN e.sal ELSE 0 END FROM emp AS e JOIN (SELECT deptno, MAX(sal) AS m FROM emp GROUP BY deptno) AS d ON e.deptno = d.deptno",
        "SELECT e.ename, e.sal * d.r FROM emp AS e JOIN (SELECT deptno, CASE WHEN MAX(sal) > 10 THEN 1 ELSE 0 END AS r FROM emp GROUP BY deptno) AS d ON e.deptno = d.deptno",
    ) in (False, True)  # NULL salaries keep these apart without a NULL-MAX fact; it must only not crash


def test_compound_and_boolean_aggregates_split_through_union_all():
    union = "(SELECT ename, sal > 1 AS b FROM emp UNION ALL SELECT ename, sal > 2 AS b FROM emp) AS t"
    out = _rule(rules._split_compound_aggregates, f"SELECT ename, BOOL_AND(b), BOOL_OR(b) FROM {union} GROUP BY ename")
    assert out == (
        "SELECT ename, LOGICAL_AND(kumosql_p0), LOGICAL_OR(kumosql_p1) FROM ("
        "SELECT ename AS ename, LOGICAL_AND(b) AS kumosql_p0, LOGICAL_OR(b) AS kumosql_p1 FROM (SELECT ename, sal > 1 AS b FROM emp) AS t GROUP BY ename "
        "UNION ALL SELECT ename AS ename, LOGICAL_AND(b) AS kumosql_p0, LOGICAL_OR(b) AS kumosql_p1 FROM (SELECT ename, sal > 2 AS b FROM emp) AS t GROUP BY ename"
        ") AS kumosql_u GROUP BY ename"
    ).replace("LOGICAL_AND", "MIN").replace("LOGICAL_OR", "MAX")
    # plain aggregates are left to _split_aggregates, and already-grouped branches are not split again
    assert _rule(rules._split_compound_aggregates, f"SELECT ename, SUM(sal) FROM (SELECT ename, sal FROM emp UNION ALL SELECT ename, sal FROM emp) AS t GROUP BY ename") is None
    assert _rule(rules._split_compound_aggregates, "SELECT e, BOOL_AND(b) FROM (SELECT e, BOOL_AND(x) AS b FROM s GROUP BY e UNION ALL SELECT e, BOOL_AND(x) AS b FROM s GROUP BY e) AS t GROUP BY e") is None
    assert _proven(
        "SELECT t.ename, SUM(t.sal) DIV COUNT(t.sal) FROM (SELECT ename, sal FROM emp UNION ALL SELECT ename, sal FROM emp) AS t GROUP BY t.ename",
        "SELECT t.e, SUM(t.s) DIV COUNT(t.s) FROM (SELECT sal AS s, ename AS e FROM emp UNION ALL SELECT sal AS s, ename AS e FROM emp) AS t GROUP BY t.e",
    )
    assert not _proven(
        "SELECT ename, BOOL_AND(b) FROM (SELECT ename, sal > 1 AS b FROM emp UNION ALL SELECT ename, sal > 2 AS b FROM emp) AS t GROUP BY ename",
        "SELECT ename, BOOL_AND(b) FROM (SELECT ename, sal > 1 AS b FROM emp UNION ALL SELECT ename, sal > 1 AS b FROM emp) AS t GROUP BY ename",
    )


def test_having_that_some_row_matches_becomes_where():
    out = _rule(rules._having_existence_to_where, "SELECT mgr FROM emp GROUP BY mgr HAVING SUM(CASE WHEN mgr = empno THEN 1 ELSE 0 END) >= 1")
    assert out == "SELECT mgr FROM emp WHERE mgr = empno GROUP BY mgr"
    out = _rule(rules._having_existence_to_where, "SELECT mgr FROM emp GROUP BY mgr HAVING COUNT(CASE WHEN sal > 1 THEN 1 END) > 0 AND mgr > 2")
    assert out == "SELECT mgr FROM emp WHERE sal > 1 GROUP BY mgr HAVING mgr > 2"
    # another aggregate would see fewer rows; a count of at least two is not an existence test
    assert _rule(rules._having_existence_to_where, "SELECT mgr, COUNT(*) FROM emp GROUP BY mgr HAVING SUM(CASE WHEN sal > 1 THEN 1 ELSE 0 END) >= 1") is None
    assert _rule(rules._having_existence_to_where, "SELECT mgr FROM emp GROUP BY mgr HAVING SUM(CASE WHEN sal > 1 THEN 1 ELSE 0 END) >= 2") is None
    assert _rule(rules._having_existence_to_where, "SELECT mgr FROM emp GROUP BY mgr HAVING SUM(CASE WHEN sal > 1 THEN 1 ELSE -1 END) >= 1") is None


def test_joined_copies_of_one_grouped_query_merge():
    left = "SELECT deptno, SUM(sal), MAX(comm) FROM emp GROUP BY deptno"
    right = (
        "SELECT a.deptno, a.s, b.m FROM (SELECT deptno, SUM(sal) AS s FROM emp GROUP BY deptno) AS a "
        "JOIN (SELECT deptno, MAX(comm) AS m FROM emp AS e GROUP BY deptno) AS b ON a.deptno <=> b.deptno"
    )
    assert _rule(rules._merge_joined_aggregates, right) == (
        "SELECT a.kumosql_j0, a.kumosql_j1, a.kumosql_j3 FROM "
        "(SELECT deptno AS kumosql_j0, SUM(sal) AS kumosql_j1, deptno AS kumosql_j2, MAX(comm) AS kumosql_j3 FROM emp GROUP BY deptno) AS a"
    )
    assert _proven(left, right)
    # "=" drops the NULL group; different filters or keys are different groups
    assert _rule(rules._merge_joined_aggregates, right.replace("<=>", "=")) is None
    assert _rule(rules._merge_joined_aggregates, right.replace("FROM emp AS e GROUP", "FROM emp AS e WHERE sal > 1 GROUP")) is None
    assert _rule(rules._merge_joined_aggregates, right.replace("SELECT deptno, MAX(comm) AS m FROM emp AS e GROUP BY deptno", "SELECT deptno, MAX(comm) AS m FROM emp AS e GROUP BY deptno, mgr")) is None
    assert not _proven(left, right.replace("<=>", "="))
    globals_ = "SELECT a.s, b.m FROM (SELECT SUM(sal) AS s FROM emp) AS a JOIN (SELECT MAX(comm) AS m FROM emp) AS b ON TRUE"
    # (normalize now reads a single-row source as a scalar subquery before this rule sees it)
    assert _rule(rules._merge_joined_aggregates, globals_) == "SELECT a.kumosql_j0, a.kumosql_j1 FROM (SELECT SUM(sal) AS kumosql_j0, MAX(comm) AS kumosql_j1 FROM emp) AS a"


def test_filter_over_a_union_with_aggregating_branches_is_distributed():
    union = "(SELECT 1 AS c, deptno, name FROM dept UNION ALL SELECT COUNT(*) AS c, NULL AS deptno, name FROM dept GROUP BY name) AS t"
    out = _rule(rules._distribute_over_aggregating_branches, f"SELECT t.name, t.c FROM {union} WHERE t.name = 'x'")
    assert out.count("UNION ALL") == 1 and out.count("WHERE t.name = 'x'") == 2
    assert _rule(rules._distribute_over_aggregating_branches, f"SELECT COUNT(*) FROM {union}") is None


def test_projection_over_a_grouped_join_reads_the_join():
    grouped = "(SELECT deptno AS k, SUM(sal) AS s FROM emp GROUP BY deptno) AS g"
    out = _rule(rules._merge_projection_over_grouped_join, f"SELECT d.v * 2 FROM (SELECT g.s + 1 AS v FROM dept JOIN {grouped} ON dept.deptno = g.k) AS d")
    assert out is not None and "AS d" not in out and "(g.s + 1) * 2" in out
    # an outer join is left for the rules that first make a null-rejected one inner
    outer = f"SELECT d.v FROM (SELECT g.s AS v FROM dept LEFT JOIN {grouped} ON dept.deptno = g.k) AS d WHERE d.v > 0"
    assert _rule(rules._merge_projection_over_grouped_join, outer) is None
