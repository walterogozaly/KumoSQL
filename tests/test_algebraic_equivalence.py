import random
import sqlite3
from collections import Counter

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus

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
