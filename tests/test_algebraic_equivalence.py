import random
import sqlite3
from collections import Counter

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, TableConstraints

U = "(SELECT a, k FROM A UNION ALL SELECT a, k FROM B)"

PROVEN = [
    pytest.param(
        f"SELECT u.a, c.x FROM {U} AS u JOIN C AS c ON u.k = c.k",
        "SELECT a.a, c.x FROM A AS a JOIN C AS c ON a.k = c.k UNION ALL SELECT b.a, c.x FROM B AS b JOIN C AS c ON b.k = c.k",
        id="join-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT u.a FROM {U} AS u WHERE u.a > 1",
        "SELECT a FROM A WHERE a > 1 UNION ALL SELECT a FROM B WHERE a > 1",
        id="filter-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A UNION ALL SELECT SUM(a) AS s FROM B) AS v",
        id="sum-of-union-all-is-sum-of-partial-sums",
    ),
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT SUM(n) AS n FROM (SELECT COUNT(*) AS n FROM B UNION ALL SELECT COUNT(*) AS n FROM A) AS v",
        id="count-of-union-all-is-sum-of-partial-counts",
    ),
    pytest.param(
        f"SELECT MAX(u.a) AS m FROM {U} AS u",
        "SELECT MAX(m) AS m FROM (SELECT MAX(a) AS m FROM A UNION ALL SELECT MAX(a) AS m FROM B) AS v",
        id="max-of-union-all",
    ),
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n FROM {U} AS u GROUP BY u.k",
        "SELECT v.k, SUM(v.n) AS n FROM (SELECT k, COUNT(*) AS n FROM A GROUP BY k "
        "UNION ALL SELECT k, COUNT(*) AS n FROM B GROUP BY k) AS v GROUP BY v.k",
        id="grouped-count-of-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u WHERE u.a > 1",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A WHERE a > 1 UNION ALL SELECT SUM(a) AS s FROM B WHERE a > 1) AS v",
        id="filtered-sum-of-union-all",
    ),
]

NOT_PROVEN = [
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT COUNT(*) AS n FROM A UNION ALL SELECT COUNT(*) AS n FROM B",
        id="count-of-union-is-not-union-of-counts",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(a) AS s FROM A",
        id="dropping-a-branch",
    ),
    pytest.param(
        f"SELECT AVG(u.a) AS s FROM {U} AS u",
        "SELECT AVG(s) AS s FROM (SELECT AVG(a) AS s FROM A UNION ALL SELECT AVG(a) AS s FROM B) AS v",
        id="average-of-averages",
    ),
    pytest.param(
        "SELECT DISTINCT u.a FROM (SELECT a FROM A UNION ALL SELECT a FROM B) AS u",
        "SELECT DISTINCT a FROM A UNION ALL SELECT DISTINCT a FROM B",
        id="distinct-does-not-distribute",
    ),
]


@pytest.mark.parametrize("left,right", PROVEN)
def test_algebraic_identities_are_proven(left, right):
    assert prove_equivalent_algebraic(left, right).proven


@pytest.mark.parametrize("left,right", NOT_PROVEN)
def test_non_identities_are_not_proven(left, right):
    assert not prove_equivalent_algebraic(left, right).proven


def test_normalize_is_idempotent():
    for case in PROVEN:
        for sql in case.values:
            once = normalize(sql)
            assert normalize(once) == once


# Union branches that name their columns differently: copies of the outer select
# must still read the first branch's names positionally.
MISALIGNED = [
    "SELECT u.a FROM (SELECT a, k FROM A UNION ALL SELECT k, a FROM A) AS u",
    "SELECT u.a, COUNT(*) AS n FROM (SELECT a, k FROM A UNION ALL SELECT k AS b, a AS c FROM B) AS u GROUP BY u.a",
    "SELECT SUM(u.k) AS s FROM (SELECT a, k FROM A UNION ALL SELECT k, a FROM B) AS u WHERE u.a > 0",
    "SELECT u.x, c.x FROM (SELECT a AS x, k FROM A UNION ALL SELECT k, a FROM B) AS u JOIN C AS c ON u.k = c.k",
]


@pytest.mark.parametrize(
    "sql", [v for case in PROVEN + NOT_PROVEN for v in case.values] + MISALIGNED
)
def test_normalization_preserves_results_on_random_databases(sql):
    rng = random.Random(7)
    normalized = normalize(sql)
    for _ in range(40):
        db = sqlite3.connect(":memory:")
        for table in ("A", "B", "C"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 0, 1, 3, 5])):
                row = [rng.choice([None, 0, 1, 2, 3]) for _ in range(3)]
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_distinct_aggregate_regrouping():
    schema = {"emp": ["deptno", "sal", "comm"]}
    flat = "SELECT deptno, SUM(comm), SUM(DISTINCT sal), COUNT(DISTINCT sal) FROM emp GROUP BY deptno"
    staged = (
        "SELECT deptno, SUM(s), SUM(sal), COUNT(sal) FROM "
        "(SELECT deptno, sal, SUM(comm) AS s FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    )
    assert prove_equivalent_algebraic(flat, staged, schema=schema, compare_names=False).proven
    # COUNT(*) of the staged rows counts NULL values of sal as a group: not COUNT(DISTINCT sal).
    wrong = "SELECT deptno, COUNT(*) FROM (SELECT deptno, sal FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    count = "SELECT deptno, COUNT(DISTINCT sal) FROM emp GROUP BY deptno"
    assert not prove_equivalent_algebraic(count, wrong, schema=schema, compare_names=False).proven
    # SUM of a staged MIN is not a SUM.
    bad = (
        "SELECT deptno, SUM(s) FROM (SELECT deptno, sal, MIN(comm) AS s FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    )
    assert not prove_equivalent_algebraic(
        "SELECT deptno, SUM(comm) FROM emp GROUP BY deptno", bad, schema=schema, compare_names=False
    ).proven


def test_aggregates_of_group_keys():
    schema = {"emp": ["deptno", "sal"]}
    left = "SELECT sal, MIN(sal), SUM(DISTINCT sal) FROM emp GROUP BY sal"
    right = "SELECT sal, sal, sal FROM emp GROUP BY sal"
    assert prove_equivalent_algebraic(left, right, schema=schema, compare_names=False).proven
    # SUM(sal) without DISTINCT adds each row's value: not the key.
    assert not prove_equivalent_algebraic(
        "SELECT sal, SUM(sal) FROM emp GROUP BY sal", right.replace("sal, sal, sal", "sal, sal"), schema=schema, compare_names=False
    ).proven


def test_constant_dates_are_folded():
    plain = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-01'"
    spark = "SELECT a FROM t WHERE d >= date('1994-09-01 +08') AND d < date('1994-09-01 +08') + INTERVAL '3' MONTH"
    clamped = "SELECT a FROM t WHERE d < DATE_ADD(DATE '2020-01-31', INTERVAL 1 MONTH)"
    leap = "SELECT a FROM t WHERE d < DATE '2020-02-29'"
    schema = {"t": ["a", "d"]}
    assert prove_equivalent_algebraic(plain, spark, schema=schema, dialect="mysql").proven
    assert prove_equivalent_algebraic(clamped, leap, schema=schema).proven
    other = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-02'"
    assert not prove_equivalent_algebraic(plain, other, schema=schema).proven


def test_string_and_date_ordering_is_fast_and_exact():
    schema = {"t": ["a", "d"]}
    base = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-01'"
    assert prove_equivalent_algebraic(
        base, "SELECT a FROM t WHERE NOT (d < DATE '1994-09-01') AND NOT (d >= DATE '1994-12-01')", schema=schema
    ).proven
    # Strings exist between '1994-08-31' and '1994-09-01', so these differ.
    assert not prove_equivalent_algebraic(
        base, "SELECT a FROM t WHERE d > DATE '1994-08-31' AND d < DATE '1994-12-01'", schema=schema
    ).proven


# Pre-aggregated tables joined and re-aggregated: the flat form must give the same rows.
EAGER = [
    "SELECT COALESCE(SUM(p.c * q.c), 0) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k",
    "SELECT p.k, SUM(p.s * q.c) AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k GROUP BY p.k",
    "SELECT q.k, MIN(p.m) AS lo, MAX(p.m) AS hi, SUM(p.c) AS n FROM "
    "(SELECT k, MIN(x) AS m, COUNT(*) AS c FROM A WHERE a > 0 GROUP BY k) AS p JOIN C AS q ON p.k = q.k GROUP BY q.k",
    "SELECT q.a, SUM(p.s) AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k GROUP BY q.a",
    # A join of grouped tables read off by arithmetic.
    "SELECT p.k, q.k AS k2, p.s * q.c AS t, p.c * q.c AS n, p.m FROM "
    "(SELECT k, SUM(x) AS s, COUNT(*) AS c, MIN(x) AS m FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k <= q.k",
    "SELECT q.k, p.s, p.c FROM (SELECT k, SUM(x) AS s, COUNT(*) AS c FROM A GROUP BY k) AS p "
    "JOIN (SELECT k FROM B GROUP BY k) AS q ON p.k = q.k",
    "SELECT p.k, p.s + 1 AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k",
    # Not rewritable: these would change how many times a group is counted.
    "SELECT COUNT(*) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k",
    "SELECT SUM(q.x) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k",
    "SELECT p.k, SUM(p.s + 1) AS n FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p GROUP BY p.k",
]


@pytest.mark.parametrize("sql", EAGER)
def test_eager_aggregation_unnesting_preserves_results(sql):
    rng = random.Random(11)
    normalized = normalize(sql)
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        for table in ("A", "B", "C"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 1, 2, 4, 6])):
                row = [rng.choice([None, 0, 1, 2]) for _ in range(3)]
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_eager_aggregation_is_proved_equal_to_the_flat_join():
    schema = {"a": ["a", "k", "x"], "b": ["a", "k", "x"]}
    eager = (
        "SELECT p.k, SUM(p.s * q.c) AS t FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k) AS p "
        "JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k GROUP BY p.k"
    )
    flat = "SELECT a.k, SUM(a.x) AS t FROM a JOIN b ON a.k = b.k GROUP BY a.k"
    assert prove_equivalent_algebraic(eager, flat, schema=schema).proven
    # Summing the count instead of the sum is a different query.
    other = eager.replace("SUM(p.s * q.c)", "SUM(q.c)")
    assert not prove_equivalent_algebraic(other, flat, schema=schema).proven


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT * FROM dept WHERE (SELECT 1) = 1", "SELECT * FROM dept WHERE 1 = 1"),
        ("SELECT * FROM dept WHERE deptno IN (SELECT deptno FROM dept WHERE FALSE)", "SELECT * FROM dept WHERE FALSE"),
        ("SELECT * FROM dept WHERE NOT EXISTS (SELECT 1 FROM dept LIMIT 0)", "SELECT * FROM dept"),
        ("SELECT COUNT(DISTINCT deptno) FILTER(WHERE 3 > 2) FROM dept", "SELECT COUNT(DISTINCT deptno) FROM dept"),
        ("SELECT SUM(deptno) FILTER(WHERE deptno > 2) FROM dept", "SELECT SUM(CASE WHEN deptno > 2 THEN deptno END) FROM dept"),
    ],
)
def test_trivial_identities_are_proven(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_filter_on_a_different_condition_is_not_proven():
    result = prove_equivalent_algebraic(
        "SELECT SUM(deptno) FILTER(WHERE deptno > 2) FROM dept",
        "SELECT SUM(deptno) FILTER(WHERE deptno > 3) FROM dept",
        schema={"dept": ["deptno"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


SCHEMA = {"emp": ["empno", "deptno", "sal"], "dept": ["deptno", "name"]}


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql")


def test_the_same_scalar_subquery_on_both_sides_is_one_value():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3) AND deptno = 1",
        "SELECT empno FROM emp WHERE deptno = 1 AND (SELECT MAX(sal) FROM emp WHERE deptno > 3) = sal",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert "scalar subqueries return at most one row" in " ".join(result.assumptions)


def test_differently_written_equivalent_scalar_subqueries_are_matched():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3)",
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(x.sal) FROM (SELECT sal FROM emp WHERE deptno > 3 AND deptno IS NOT NULL) AS x)",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_different_scalar_subqueries_are_not_equated():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3)",
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 4)",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_correlated_scalar_subquery_is_never_replaced_by_a_shared_value():
    # The same text reads a different outer column on each side: it must not be equated.
    result = _prove(
        "SELECT e.empno FROM emp e WHERE e.sal = (SELECT MAX(sal) FROM emp WHERE deptno = e.deptno)",
        "SELECT e.empno FROM emp e WHERE e.sal = (SELECT MAX(sal) FROM emp WHERE deptno = e.empno)",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_an_unresolvable_column_counts_as_correlated():
    from kumosql import scalar_subqueries
    import sqlglot

    tree = sqlglot.parse_one("SELECT 1 FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > mystery)")
    node = next(tree.find_all(sqlglot.exp.Subquery))
    assert not scalar_subqueries.is_uncorrelated(node, SCHEMA)
    assert not scalar_subqueries.is_uncorrelated(node, None)


@pytest.mark.parametrize(
    "left, right",
    [
        # column names are case-insensitive
        ("SELECT T.EMPNO FROM EMP AS T WHERE T.SAL > 1", "SELECT t.empno FROM EMP AS t WHERE t.sal > 1"),
        # a UNION ALL source is read only for the columns the query uses, in any order
        (
            "SELECT t.ename, AVG(t.empno) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.ename",
            "SELECT t.ename, AVG(t.empno) FROM (SELECT ename, empno FROM emp UNION ALL SELECT ename, empno FROM emp) AS t GROUP BY t.ename",
        ),
        # a HAVING conjunct on the group key is a WHERE, whatever else the HAVING holds
        (
            "SELECT name FROM dept WHERE name > 'b' GROUP BY name HAVING name > 'c' AND (COUNT(*) > 3 OR name < 'z')",
            "SELECT name FROM dept WHERE name > 'b' AND name > 'c' GROUP BY name HAVING COUNT(*) > 3 OR name < 'z'",
        ),
        # GROUP BY TRUE over constants is an existence test
        (
            "SELECT e.empno FROM emp e WHERE EXISTS (SELECT 1 FROM emp WHERE empno < 20)",
            "SELECT e.empno FROM emp e, (SELECT 1 AS i FROM emp WHERE empno < 20 GROUP BY TRUE) AS t",
        ),
        # the same derived relation under different aliases is one relation
        (
            "SELECT d.deptno FROM dept d RIGHT JOIN (SELECT x.deptno FROM emp x WHERE x.sal > 1 GROUP BY x.deptno) t ON d.deptno = t.deptno",
            "SELECT d0.deptno FROM dept d0 RIGHT JOIN (SELECT y.deptno FROM emp y WHERE y.sal > 1 GROUP BY y.deptno) u ON d0.deptno = u.deptno",
        ),
    ],
)
def test_more_shapes_are_proven(left, right):
    result = prove_equivalent_algebraic(
        left, right, schema={"emp": ["empno", "ename", "sal", "deptno"], "dept": ["deptno", "name"]}, compare_names=False, dialect="mysql"
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_a_constant_set_source_still_needs_the_same_filter():
    result = prove_equivalent_algebraic(
        "SELECT e.empno FROM emp e WHERE EXISTS (SELECT 1 FROM emp WHERE empno < 20)",
        "SELECT e.empno FROM emp e, (SELECT 1 AS i FROM emp WHERE empno < 21 GROUP BY TRUE) AS t",
        schema={"emp": ["empno"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


OJ_SCHEMA = {"emp": ["ename", "sal", "deptno"], "bonus": ["ename"]}


@pytest.mark.parametrize(
    "left, right",
    [
        (
            "SELECT COUNT(*), CASE WHEN emp.sal < 11 THEN -1 * emp.sal ELSE emp.sal END FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY CASE WHEN emp.sal < 11 THEN -1 * emp.sal ELSE emp.sal END",
            "SELECT COUNT(*), t3.e FROM (SELECT b.ename FROM bonus b) t2 LEFT JOIN (SELECT x.ename, CASE WHEN x.sal < 11 THEN -1 * x.sal ELSE x.sal END AS e FROM emp x) t3 ON t2.ename = t3.ename GROUP BY t3.e",
        ),
        (
            "SELECT COUNT(*), emp.deptno FROM emp FULL JOIN bonus ON emp.ename = bonus.ename GROUP BY emp.deptno",
            "SELECT COUNT(*), a.deptno FROM emp a FULL JOIN bonus b ON a.ename = b.ename GROUP BY a.deptno",
        ),
    ],
)
def test_aggregates_over_outer_joins(left, right):
    result = prove_equivalent_algebraic(left, right, schema=OJ_SCHEMA, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_outer_join_and_inner_join_aggregates_differ():
    result = prove_equivalent_algebraic(
        "SELECT COUNT(*), emp.deptno FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        "SELECT COUNT(*), emp.deptno FROM bonus JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        schema=OJ_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_left_join_is_not_a_right_join_in_an_aggregate():
    result = prove_equivalent_algebraic(
        "SELECT COUNT(*), emp.deptno FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        "SELECT COUNT(*), emp.deptno FROM bonus RIGHT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        schema=OJ_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT COUNT(NULL) FROM dept", "SELECT 0"),
        ("SELECT COUNT(DISTINCT (deptno = NULL)) FROM dept", "SELECT 0"),
        ("SELECT SUM(NULL) FROM dept", "SELECT NULL"),
        ("SELECT UPPER(LOWER(name)) FROM dept", "SELECT UPPER(name) FROM dept"),
        ("SELECT POSITIVE(deptno) FROM dept", "SELECT deptno FROM dept"),
        ('SELECT CONCAT("a", CONCAT("b", "c")) AS c1 FROM dept', 'SELECT CONCAT("a", "b", "c") AS c1 FROM dept'),
    ],
)
def test_constant_aggregates_and_function_identities(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT COUNT(NULL) FROM dept", "SELECT 1"),
        ("SELECT COUNT(NULL) FROM dept", "SELECT 0 FROM dept"),  # one row versus one per department
        ("SELECT LOWER(UPPER(name)) FROM dept", "SELECT UPPER(name) FROM dept"),
        ("SELECT deptno FROM dept WHERE (deptno, name) IN (SELECT deptno, name FROM dept WHERE deptno > 1)", "SELECT deptno FROM dept WHERE deptno IN (SELECT deptno FROM dept WHERE deptno > 1)"),
    ],
)
def test_constant_aggregate_and_tuple_in_negatives(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_tuple_in_is_an_existence_test_on_every_column():
    result = prove_equivalent_algebraic(
        "SELECT deptno FROM dept WHERE (deptno, name) IN (SELECT deptno, name FROM dept WHERE deptno > 1)",
        "SELECT deptno FROM dept WHERE EXISTS (SELECT 1 FROM dept d WHERE d.deptno = dept.deptno AND d.name = dept.name AND d.deptno > 1)",
        schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


LINEITEM = {"lineitem": ["l_suppkey", "l_extendedprice", "l_shipdate"]}


def test_aggregate_over_grouped_derived_table_in_a_scalar_subquery_keeps_its_grouping():
    # MAX over per-supplier totals is not a flat aggregate: the derived table must stay grouped.
    result = prove_equivalent_algebraic(
        "SELECT (SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM lineitem GROUP BY l_suppkey) AS d) AS m",
        "SELECT (SELECT MAX(l_extendedprice) FROM lineitem) AS m",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_filtering_derived_table_under_a_grouping_is_folded_in():
    result = prove_equivalent_algebraic(
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM lineitem WHERE l_shipdate >= 5 GROUP BY l_suppkey) AS r",
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM (SELECT l_suppkey, l_extendedprice FROM lineitem WHERE l_shipdate >= 5) AS z GROUP BY l_suppkey) AS q",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_null_guards_and_trailing_zeros_do_not_change_a_derived_relation():
    left = "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice * (1 - l_suppkey)) AS t FROM lineitem WHERE l_shipdate >= 5 GROUP BY l_suppkey) AS r"
    right = (
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice * (1.00 - l_suppkey)) AS t FROM lineitem "
        "WHERE l_shipdate IS NOT NULL AND (l_shipdate >= 5) AND l_suppkey IS NOT NULL GROUP BY l_suppkey) AS q"
    )
    constraints = {"lineitem": TableConstraints(not_null=frozenset({"l_suppkey"}))}
    result = prove_equivalent_algebraic(left, right, schema=LINEITEM, constraints=constraints, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # Without the declaration the guard on l_suppkey filters rows, so the queries differ.
    result = prove_equivalent_algebraic(left, right, schema=LINEITEM, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_null_guard_removal_keeps_or_precedence():
    result = prove_equivalent_algebraic(
        "SELECT l_suppkey FROM lineitem WHERE l_shipdate IS NOT NULL AND (l_shipdate = 1 OR l_suppkey = 2) AND l_extendedprice > 0",
        "SELECT l_suppkey FROM lineitem WHERE l_shipdate = 1 OR l_suppkey = 2 AND l_extendedprice > 0",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
