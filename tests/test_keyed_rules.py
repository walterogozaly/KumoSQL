import sqlglot

from kumosql.keyed_rules import drop_keyed_distinct, exists_over_aggregate, remove_keyed_grouping

KEYS = {"emp": [("empno",)], "u": [("x",)]}
NOT_NULL = {"emp": frozenset({"empno"})}


def _rule(rule, sql):
    out = rule(sqlglot.parse_one(sql, read="mysql"), KEYS, NOT_NULL)
    return out.sql(dialect="mysql") if out is not None else None


def test_grouping_on_a_key_reads_each_row():
    out = _rule(remove_keyed_grouping, "SELECT empno, SUM(sal), COUNT(*), COUNT(DISTINCT mgr), GROUPING(deptno) FROM emp GROUP BY empno, deptno")
    assert out == "SELECT empno, sal, 1, CASE WHEN mgr IS NULL THEN 0 ELSE 1 END, 0 FROM emp"


def test_key_fixed_by_where_counts_as_grouped_and_having_moves_to_where():
    out = _rule(remove_keyed_grouping, "SELECT job, MAX(sal) FROM emp WHERE empno = 10 GROUP BY job HAVING job = 'a'")
    assert out == "SELECT job, sal FROM emp WHERE empno = 10 AND job = 'a'"


def test_nullable_key_or_join_or_unknown_aggregate_is_left_alone():
    assert _rule(remove_keyed_grouping, "SELECT x, SUM(y) FROM u GROUP BY x") is None
    assert _rule(remove_keyed_grouping, "SELECT empno, SUM(sal) FROM emp JOIN dept ON TRUE GROUP BY empno") is None
    assert _rule(remove_keyed_grouping, "SELECT empno, STDDEV(sal) FROM emp GROUP BY empno") is None
    assert _rule(remove_keyed_grouping, "SELECT deptno, SUM(sal) FROM emp GROUP BY deptno") is None


def test_distinct_with_a_key_is_dropped():
    assert _rule(drop_keyed_distinct, "SELECT DISTINCT empno, deptno FROM emp") == "SELECT empno, deptno FROM emp"
    assert _rule(drop_keyed_distinct, "SELECT DISTINCT deptno FROM emp") is None


def test_exists_over_a_global_aggregate_is_true():
    tree = exists_over_aggregate(sqlglot.parse_one("SELECT * FROM d WHERE EXISTS (SELECT COUNT(*) FROM e WHERE e.k = d.k)"))
    assert tree.sql() == "SELECT * FROM d WHERE TRUE"
    tree = exists_over_aggregate(sqlglot.parse_one("SELECT * FROM d WHERE EXISTS (SELECT COUNT(*) FROM e GROUP BY k)"))
    assert "EXISTS" in tree.sql()
