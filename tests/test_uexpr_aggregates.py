"""Aggregate decomposition in the bag-equivalence backend (``kumosql.uexpr``).

Provable pairs are also run on random databases (duckdb) so a wrong rule cannot hide behind the
prover; each must-not-prove pair is a near miss that really differs on some database, and the
backend has to leave it unproven.
"""

import random
from collections import Counter

import pytest

duckdb = pytest.importorskip("duckdb")

from kumosql.smt_equivalence import TableConstraints  # noqa: E402
from kumosql.uexpr import prove_bag_equivalent  # noqa: E402

SCHEMA = {
    "emp": ["empno", "ename", "mgr", "sal", "comm", "deptno"],
    "dept": ["deptno", "name"],
}
TYPES = {
    "emp": {"empno": "INTEGER", "ename": "VARCHAR(20)", "mgr": "INTEGER", "sal": "INTEGER", "comm": "INTEGER", "deptno": "INTEGER"},
    "dept": {"deptno": "INTEGER", "name": "VARCHAR(20)"},
}
CONSTRAINTS = {
    "emp": TableConstraints(not_null=frozenset({"empno", "sal", "deptno"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}


def prove(left: str, right: str) -> bool:
    return prove_bag_equivalent(
        left,
        right,
        schema=SCHEMA,
        constraints=CONSTRAINTS,
        types=TYPES,
        dialect="mysql",
        exact_arithmetic=True,
        compare_names=False,
        use_foreign_keys=False,
    ).proven


def _database(rng: random.Random):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE emp (empno INTEGER, ename VARCHAR, mgr INTEGER, sal INTEGER, comm INTEGER, deptno INTEGER)")
    con.execute("CREATE TABLE dept (deptno INTEGER, name VARCHAR)")
    pick = lambda values: rng.choice(values)  # noqa: E731
    for i in range(rng.randint(0, 7)):
        con.execute(
            "INSERT INTO emp VALUES (?, ?, ?, ?, ?, ?)",
            [i, pick(["a", "b", "c"]), pick([None, 1, 2]), pick([1, 2, 3]), pick([None, 5, 7, 9]), pick([1, 2])],
        )
    for i in range(rng.randint(0, 4)):
        con.execute("INSERT INTO dept VALUES (?, ?)", [i, pick(["a", "b", "c"])])
    return con


def _bag(con, sql: str) -> Counter:
    return Counter(tuple(None if x is None else round(float(x), 6) if not isinstance(x, str) else x for x in row) for row in con.execute(sql).fetchall())


def differs_somewhere(left: str, right: str, trials: int = 300) -> bool:
    rng = random.Random(7)
    for _ in range(trials):
        con = _database(rng)
        try:
            if _bag(con, left) != _bag(con, right):
                return True
        finally:
            con.close()
    return False


# Each pair is equivalent; the comment names the rule that proves it.
PROVABLE = {
    # COUNT over a UNION ALL is the SUM of the per-branch COUNTs: both sides become one linear sum, and the
    # existence of each branch's group is witnessed by the rows being counted.
    "count_of_union_vs_sum_of_counts": (
        "SELECT t.ename, COUNT(t.mgr) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.ename",
        "SELECT t.ename, SUM(c) FROM (SELECT ename, COUNT(mgr) AS c FROM emp GROUP BY ename UNION ALL "
        "SELECT ename, COUNT(mgr) AS c FROM emp GROUP BY ename) AS t GROUP BY t.ename",
    ),
    # SUM of per-group SUMs (and MIN of per-group MINs) over the partition by sal; SUM(DISTINCT sal) of a group
    # whose sal is one value is that value.
    "sum_min_over_preaggregated_groups": (
        "SELECT sal, SUM(comm), MIN(comm), SUM(DISTINCT sal) FROM emp GROUP BY sal",
        "SELECT t.sal, SUM(t.s), MIN(t.m), SUM(t.sal) FROM (SELECT sal, SUM(comm) AS s, MIN(comm) AS m FROM emp GROUP BY sal) AS t GROUP BY t.sal",
    ),
    # The partition is finer than the grouping: MIN of the per-(deptno, sal) minima is the MIN over the department,
    # and SUM(DISTINCT sal) sums each salary of the department once.
    "extrema_over_a_finer_partition": (
        "SELECT deptno, MIN(comm), MAX(comm) FROM emp GROUP BY deptno",
        "SELECT deptno, MIN(m), MAX(x) FROM (SELECT deptno, sal, MIN(comm) AS m, MAX(comm) AS x FROM emp GROUP BY deptno, sal) AS t GROUP BY deptno",
    ),
    "sums_over_a_finer_partition": (
        "SELECT deptno, SUM(comm), SUM(DISTINCT sal) FROM emp GROUP BY deptno",
        "SELECT deptno, SUM(s), SUM(sal) FROM (SELECT deptno, sal, SUM(comm) AS s FROM emp GROUP BY deptno, sal) AS t GROUP BY deptno",
    ),
    # MIN over a UNION ALL of per-branch minima.
    "min_of_union_of_minima": (
        "SELECT t.ename, MIN(t.empno) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.ename",
        "SELECT ename, MIN(m) FROM (SELECT ename, MIN(empno) AS m FROM emp GROUP BY ename UNION ALL "
        "SELECT ename, MIN(empno) AS m FROM emp GROUP BY ename) AS t GROUP BY ename",
    ),
    "max_of_union_of_maxima": (
        "SELECT t.deptno, MAX(t.mgr) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.deptno",
        "SELECT t.deptno, MAX(m) FROM (SELECT deptno, MAX(mgr) AS m FROM emp GROUP BY deptno UNION ALL "
        "SELECT deptno, MAX(mgr) AS m FROM emp GROUP BY deptno) AS t GROUP BY t.deptno",
    ),
    # The aggregate of a nullable value over a one-value group: the group's value, or NULL for the NULL group.
    "distinct_sum_over_one_value_group": (
        "SELECT comm, SUM(DISTINCT comm) FROM emp GROUP BY comm",
        "SELECT comm, comm FROM emp GROUP BY comm",
    ),
    "min_over_one_value_group": (
        "SELECT comm, MIN(comm), MAX(comm) FROM emp GROUP BY comm",
        "SELECT comm, comm, comm FROM emp GROUP BY comm",
    ),
    # Branches that fix the group key to different values: the equality the conditions force is taken out of each sum.
    "count_star_over_branches_with_different_filters": (
        "SELECT t.deptno, COUNT(*) FROM (SELECT * FROM emp WHERE deptno = 1 UNION ALL SELECT * FROM emp WHERE deptno > 1) AS t GROUP BY t.deptno",
        "SELECT t.deptno, SUM(c) FROM (SELECT deptno, COUNT(*) AS c FROM emp WHERE deptno = 1 GROUP BY deptno UNION ALL "
        "SELECT deptno, COUNT(*) AS c FROM emp WHERE deptno > 1 GROUP BY deptno) AS t GROUP BY t.deptno",
    ),
    # MIN(DISTINCT x) is MIN(x): only the set of values matters.
    "min_distinct_vs_min_of_groups": (
        "SELECT sal, MIN(DISTINCT comm) FROM emp GROUP BY sal",
        "SELECT sal, MIN(m) FROM (SELECT sal, MIN(comm) AS m FROM emp GROUP BY sal) AS t GROUP BY sal",
    ),
    # SUM of a constant-weighted union: the weights add.
    "sum_of_weighted_union": (
        "SELECT t.deptno, SUM(t.u) FROM (SELECT deptno, 2 AS u FROM emp UNION ALL SELECT deptno, 3 AS u FROM emp) AS t GROUP BY t.deptno",
        "SELECT deptno, SUM(s) FROM (SELECT deptno, SUM(2) AS s FROM emp GROUP BY deptno UNION ALL "
        "SELECT deptno, SUM(3) AS s FROM emp GROUP BY deptno) AS t GROUP BY deptno",
    ),
}

# Each pair differs on some database, and the backend must not prove it.
NEAR_MISSES = {
    # AVG of AVGs weighs every group the same however many rows it has.
    "avg_of_avgs": (
        "SELECT deptno, AVG(comm) FROM emp GROUP BY deptno",
        "SELECT deptno, AVG(a) FROM (SELECT deptno, sal, AVG(comm) AS a FROM emp GROUP BY deptno, sal) AS t GROUP BY deptno",
    ),
    # A join on a column that is not a key changes the multiplicity of the summed rows.
    "sum_over_a_multiplying_join": (
        "SELECT e.deptno, SUM(e.sal) FROM emp AS e GROUP BY e.deptno",
        "SELECT e.deptno, SUM(e.sal) FROM emp AS e JOIN dept AS d ON e.ename = d.name GROUP BY e.deptno",
    ),
    # MIN of per-group MAXes is not an extremum of the whole.
    "min_of_maxima": (
        "SELECT deptno, MIN(comm) FROM emp GROUP BY deptno",
        "SELECT deptno, MIN(x) FROM (SELECT deptno, sal, MAX(comm) AS x FROM emp GROUP BY deptno, sal) AS t GROUP BY deptno",
    ),
    # SUM(DISTINCT) of a one-value group is the value, but SUM is the value times the number of rows.
    "plain_sum_vs_distinct_sum": (
        "SELECT sal, SUM(DISTINCT sal) FROM emp GROUP BY sal",
        "SELECT sal, SUM(sal) FROM emp GROUP BY sal",
    ),
    # A HAVING on the groups drops groups, and so their minima.
    "extremum_of_filtered_groups": (
        "SELECT deptno, MIN(comm) FROM emp GROUP BY deptno",
        "SELECT deptno, MIN(m) FROM (SELECT deptno, sal, MIN(comm) AS m FROM emp GROUP BY deptno, sal HAVING COUNT(*) > 1) AS t GROUP BY deptno",
    ),
    # The number of groups is not the number of rows.
    "count_of_groups_vs_count_of_rows": (
        "SELECT COUNT(*) FROM emp",
        "SELECT COUNT(*) FROM (SELECT deptno FROM emp GROUP BY deptno) AS t",
    ),
    # COUNT(mgr) skips NULL managers; COUNT(*) does not, so the group's existence is not witnessed by a counted row.
    "count_column_vs_count_star_over_union": (
        "SELECT t.ename, COUNT(t.mgr) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.ename",
        "SELECT t.ename, SUM(c) FROM (SELECT ename, COUNT(*) AS c FROM emp GROUP BY ename UNION ALL "
        "SELECT ename, COUNT(*) AS c FROM emp GROUP BY ename) AS t GROUP BY t.ename",
    ),
    # An empty group sums to NULL, not 0: the pre-aggregated side turns it into 0.
    "sum_null_vs_zero": (
        "SELECT deptno, SUM(comm) FROM emp GROUP BY deptno",
        "SELECT deptno, SUM(COALESCE(s, 0)) FROM (SELECT deptno, SUM(comm) AS s FROM emp GROUP BY deptno) AS t GROUP BY deptno",
    ),
    # A row that has a manager is not witnessed by a row that has none: the existence test stays.
    "exists_witness_needs_its_conditions": (
        "SELECT e.empno FROM emp AS e WHERE EXISTS (SELECT 1 FROM emp AS x WHERE x.ename = e.ename AND x.mgr IS NOT NULL)",
        "SELECT e.empno FROM emp AS e",
    ),
    # Equal on every database where the group exists, but a global aggregate returns a row on an empty table.
    "group_key_constant_vs_global_aggregate": (
        "SELECT deptno, COUNT(*) FROM emp WHERE deptno = 1 GROUP BY deptno",
        "SELECT 1, COUNT(*) FROM emp WHERE deptno = 1",
    ),
    # The two groupings differ: grouping by sal is not grouping by deptno.
    "one_valued_group_of_another_key": (
        "SELECT deptno, SUM(DISTINCT sal) FROM emp GROUP BY deptno",
        "SELECT deptno, SUM(DISTINCT deptno) FROM emp GROUP BY deptno",
    ),
}


@pytest.mark.parametrize("name", sorted(PROVABLE))
def test_provable_pair_is_proved_and_holds_on_random_databases(name):
    left, right = PROVABLE[name]
    assert not differs_somewhere(left, right), "the pair is not equivalent: fix the test"
    assert prove(left, right)


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_miss_is_not_proved(name):
    left, right = NEAR_MISSES[name]
    assert differs_somewhere(left, right), "the pair is equivalent: it is not a near miss"
    assert not prove(left, right)
