"""Calcite's eager SUM through a join (after AggregateReduceFunctionsRule), read back into the plain SUM.

``SUM(x)`` over a join is written as ``CASE WHEN n = 0 THEN NULL ELSE s END`` over a join of
per-key groups, with ``s`` the sum of one side's ``COALESCE(SUM(x), 0)`` times the other side's
``COUNT(*)`` and ``n`` the sum of the two counts' products. That is ``SUM(x)`` only when ``x`` is
never NULL (rows whose ``x`` are all NULL give 0, not NULL). Every pair that differs carries a
witness database, re-run here on DuckDB with its optimizer off; the provers must never prove those.
"""

import random
from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"emp": ["empno", "ename", "job", "sal", "mgr"], "dept": ["deptno", "name", "loc"]}
TYPES = {
    "emp": {"empno": "INTEGER", "ename": "VARCHAR", "job": "VARCHAR", "sal": "INTEGER", "mgr": "INTEGER"},
    "dept": {"deptno": "INTEGER", "name": "VARCHAR", "loc": "VARCHAR"},
}
# STRICT: sal, job and name are NOT NULL (Calcite's EMP/DEPT); LOOSE: only the keys are.
STRICT = {
    "emp": TableConstraints(keys=(("empno",),), not_null=frozenset({"empno", "ename", "job", "sal"})),
    "dept": TableConstraints(keys=(("deptno",),), not_null=frozenset({"deptno", "name"})),
}
LOOSE = {
    "emp": TableConstraints(keys=(("empno",),), not_null=frozenset({"empno"})),
    "dept": TableConstraints(keys=(("deptno",),), not_null=frozenset({"deptno"})),
}


def _ddl(constraints):
    def column(table, name, kind):
        return f"{name} {kind}{' NOT NULL' if name in constraints[table].not_null else ''}"

    return {
        "emp": ", ".join(column("emp", n, k) for n, k in (("empno", "INT"), ("ename", "VARCHAR"), ("job", "VARCHAR"), ("sal", "INT"), ("mgr", "INT"))),
        "dept": ", ".join(column("dept", n, k) for n, k in (("deptno", "INT"), ("name", "VARCHAR"), ("loc", "VARCHAR"))),
    }


PLAIN = "SELECT SUM(e.sal) AS s FROM emp AS e JOIN dept AS d ON e.job = d.name WHERE e.ename = 'A'"


def eager(*, emp_count="COUNT(*)", dept_count="COUNT(*)", factor="q.c", join="JOIN", on="p.job = q.name", guard=True):
    inner = (
        f"SELECT COALESCE(SUM(p.s * {factor}), 0) AS s, COALESCE(SUM(p.c * {factor}), 0) AS n "
        f"FROM (SELECT job, COALESCE(SUM(sal), 0) AS s, {emp_count} AS c FROM emp WHERE ename = 'A' GROUP BY job) AS p "
        f"{join} (SELECT name, {dept_count} AS c FROM dept GROUP BY name) AS q ON {on}"
    )
    value = "CASE WHEN t.n = 0 THEN NULL ELSE t.s END" if guard else "t.s"
    return f"SELECT {value} AS s FROM ({inner}) AS t"


# Calcite's own spelling (RelOptRulesTest.testPushAggregateSumThroughJoinAfterAggregateReduce, renamed columns).
CALCITE = (
    "SELECT (CASE WHEN (t9.c1 = 0) THEN CAST(NULL AS INTEGER) ELSE t9.c0 END) AS s FROM (SELECT COALESCE(SUM(t8.c0), 0) AS c0, "
    "COALESCE(SUM(t8.c1), 0) AS c1 FROM (SELECT CAST((CAST(t7.c1 AS BIGINT) * t7.c4) AS INTEGER) AS c0, (t7.c2 * t7.c4) AS c1 "
    "FROM (SELECT t5.c0 AS c0, t5.c1 AS c1, t5.c2 AS c2, t6.c0 AS c3, t6.c1 AS c4 FROM (SELECT t3.job AS c0, "
    "COALESCE(SUM(t3.sal), 0) AS c1, COUNT(*) AS c2 FROM (SELECT * FROM emp WHERE ename = 'A') AS t3 GROUP BY t3.job) AS t5 "
    "INNER JOIN (SELECT t4.name AS c0, COUNT(*) AS c1 FROM dept AS t4 GROUP BY t4.name) AS t6 ON (t5.c0 = t6.c0)) AS t7) AS t8) AS t9"
)

REDUCED = "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp"

# (id, left, right, constraints) for pairs that are equivalent and proved
PROVED = [
    ("eager-sum-through-join", PLAIN, eager(), STRICT),
    ("calcite-spelling", PLAIN, CALCITE, STRICT),
    ("reduced-sum-not-null", "SELECT SUM(sal) AS s FROM emp", REDUCED, STRICT),
    ("reduced-sum-count-of-same-column", "SELECT SUM(sal) AS s FROM emp",
     "SELECT CASE WHEN COUNT(sal) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp", LOOSE),
    ("reduced-sum-grouped", "SELECT job, SUM(sal) AS s FROM emp GROUP BY job",
     "SELECT job, CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp GROUP BY job", STRICT),
    ("reduced-sum-derived", "SELECT SUM(sal) AS s FROM emp",
     "SELECT CASE WHEN COALESCE(t.n, 0) = 0 THEN NULL ELSE COALESCE(t.s, 0) END AS s FROM (SELECT COUNT(*) AS n, SUM(sal) AS s FROM emp) AS t", STRICT),
    ("sum-of-group-counts-is-never-zero", "SELECT NULLIF(COUNT(*), 0) AS c FROM emp",
     "SELECT SUM(g.c) AS c FROM (SELECT job, COUNT(*) AS c FROM emp GROUP BY job) AS g", LOOSE),
]

# (id, left, right, constraints, witness rows) for pairs that differ
DIFFERENT = [
    # SUM over joined rows whose sal are all NULL is NULL; the eager form gives 0
    ("nullable-sal", PLAIN, eager(), LOOSE, {"emp": [(1, "A", "x", None, None)], "dept": [(1, "x", None)]}),
    ("reduced-nullable-sal", "SELECT SUM(sal) AS s FROM emp", REDUCED, LOOSE, {"emp": [(1, "A", "x", None, None)]}),
    ("reduced-grouped-nullable-sal", "SELECT job, SUM(sal) AS s FROM emp GROUP BY job",
     "SELECT job, CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp GROUP BY job", LOOSE,
     {"emp": [(1, "A", "x", None, None)]}),
    # COUNT(mgr) misses the rows whose mgr is NULL
    ("count-of-another-column", PLAIN, eager(emp_count="COUNT(mgr)"), STRICT, {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("dept-count-of-a-column", PLAIN, eager(dept_count="COUNT(loc)"), STRICT, {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("reduced-count-of-another-column", "SELECT SUM(sal) AS s FROM emp",
     "SELECT CASE WHEN COUNT(mgr) = 0 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    # no CASE: 0 where the plain SUM over no rows is NULL
    ("case-missing", PLAIN, eager(guard=False), STRICT, {}),
    # each EMP group counted by its own size instead of DEPT's
    ("wrong-side-count", PLAIN, eager(factor="p.c"), STRICT,
     {"emp": [(1, "A", "x", 5, None), (2, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    # the eager form drops the EMP rows no DEPT row matches
    ("left-join", "SELECT SUM(e.sal) AS s FROM emp AS e LEFT JOIN dept AS d ON e.job = d.name WHERE e.ename = 'A'",
     eager(join="LEFT JOIN"), STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    # NULL keys meet under IS NOT DISTINCT FROM, never under =
    ("null-safe-keys", PLAIN, eager(on="p.job IS NOT DISTINCT FROM q.name"), LOOSE,
     {"emp": [(1, "A", None, 5, None)], "dept": [(1, None, None)]}),
    ("inverted-case", REDUCED.replace("THEN NULL ELSE COALESCE(SUM(sal), 0) END", "THEN COALESCE(SUM(sal), 0) ELSE NULL END"),
     "SELECT SUM(sal) AS s FROM emp", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    ("count-not-zero-test", "SELECT SUM(sal) AS s FROM emp",
     "SELECT CASE WHEN COUNT(*) = 1 THEN NULL ELSE COALESCE(SUM(sal), 0) END AS s FROM emp", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    # a LEFT JOIN pads the aggregate table: its count is NULL there, and the CASE gives 0
    ("padded-derived-count", "SELECT d.name, t.s FROM dept AS d LEFT JOIN (SELECT job, SUM(sal) AS s FROM emp GROUP BY job) AS t ON d.name = t.job",
     "SELECT d.name, CASE WHEN t.n = 0 THEN NULL ELSE COALESCE(t.s, 0) END AS s FROM dept AS d "
     "LEFT JOIN (SELECT job, COUNT(*) AS n, SUM(sal) AS s FROM emp GROUP BY job) AS t ON d.name = t.job", STRICT, {"dept": [(1, "x", None)]}),
    ("padded-total", "SELECT d.name, t.s FROM dept AS d LEFT JOIN (SELECT COUNT(*) AS n, SUM(sal) AS s FROM emp) AS t ON d.loc = 'l'",
     "SELECT d.name, CASE WHEN t.n = 0 THEN NULL ELSE COALESCE(t.s, 0) END AS s FROM dept AS d "
     "LEFT JOIN (SELECT COUNT(*) AS n, SUM(sal) AS s FROM emp) AS t ON d.loc = 'l'", STRICT, {"dept": [(1, "x", None)]}),
    ("padded-group-sum", "SELECT SUM(COALESCE(p.s, 0)) AS s FROM dept AS d LEFT JOIN (SELECT job, SUM(sal) AS s FROM emp GROUP BY job) AS p ON d.name = p.job",
     "SELECT SUM(p.s) AS s FROM dept AS d LEFT JOIN (SELECT job, SUM(COALESCE(sal, 0)) AS s FROM emp GROUP BY job) AS p ON d.name = p.job", LOOSE,
     {"dept": [(1, "x", None)]}),
    ("sum-of-group-counts-over-no-rows", "SELECT COUNT(*) AS c FROM emp",
     "SELECT SUM(g.c) AS c FROM (SELECT job, COUNT(*) AS c FROM emp GROUP BY job) AS g", STRICT, {}),
    # COALESCE(g.s, 0) is g.s only when g has a row per group: a global SUM over no rows is NULL, so is a GROUP BY () one
    ("coalesce-of-global-derived-sum", "SELECT COALESCE(t.s, 0) AS s FROM (SELECT SUM(sal) AS s FROM emp) AS t",
     "SELECT t.s AS s FROM (SELECT SUM(sal) AS s FROM emp) AS t", STRICT, {}),
    ("coalesce-of-global-derived-sum-joined", "SELECT SUM(COALESCE(g.s, 0)) AS s FROM (SELECT SUM(sal) AS s FROM emp) AS g JOIN dept AS d ON d.loc = 'l'",
     "SELECT SUM(g.s) AS s FROM (SELECT SUM(sal) AS s FROM emp) AS g JOIN dept AS d ON d.loc = 'l'", STRICT, {"dept": [(1, "x", "l")]}),
    ("coalesce-of-group-by-empty-tuple", "SELECT SUM(COALESCE(g.s, 0)) AS s FROM (SELECT SUM(sal) AS s FROM emp GROUP BY ()) AS g",
     "SELECT SUM(g.s) AS s FROM (SELECT SUM(sal) AS s FROM emp GROUP BY ()) AS g", STRICT, {}),
    ("coalesce-of-rollup-sum", "SELECT SUM(COALESCE(g.s, 0)) AS s FROM (SELECT job, SUM(sal) AS s FROM emp GROUP BY ROLLUP(job)) AS g",
     "SELECT SUM(g.s) AS s FROM (SELECT job, SUM(sal) AS s FROM emp GROUP BY ROLLUP(job)) AS g", STRICT, {}),
    # a global SUM of group counts is never 0 only for COUNT(*) of unpadded plain groups
    ("sum-of-group-count-column", "SELECT NULLIF(COUNT(mgr), 0) AS c FROM emp",
     "SELECT SUM(g.c) AS c FROM (SELECT job, COUNT(mgr) AS c FROM emp GROUP BY job) AS g", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    ("sum-of-rollup-counts", "SELECT NULLIF(COUNT(*), 0) AS c FROM emp",
     "SELECT SUM(g.c) AS c FROM (SELECT job, COUNT(*) AS c FROM emp GROUP BY ROLLUP(job)) AS g", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    ("sum-of-padded-group-counts", "SELECT SUM(COALESCE(g.c, 0)) AS c FROM dept AS d LEFT JOIN (SELECT job, COUNT(*) AS c FROM emp GROUP BY job) AS g ON d.name = g.job",
     "SELECT SUM(g.c) AS c FROM dept AS d LEFT JOIN (SELECT job, COUNT(*) AS c FROM emp GROUP BY job) AS g ON d.name = g.job", STRICT, {"dept": [(1, "x", None)]}),
    # near misses of the CASE read-back and the eager form
    ("case-count-of-a-dept-column", "SELECT SUM(e.sal) AS s FROM emp AS e JOIN dept AS d ON e.job = d.name",
     "SELECT CASE WHEN COUNT(d.loc) = 0 THEN NULL ELSE COALESCE(SUM(e.sal), 0) END AS s FROM emp AS e JOIN dept AS d ON e.job = d.name", STRICT,
     {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("case-over-outer-padded-column", "SELECT SUM(d.deptno) AS s FROM emp AS e LEFT JOIN dept AS d ON e.job = d.name",
     "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(d.deptno), 0) END AS s FROM emp AS e LEFT JOIN dept AS d ON e.job = d.name", STRICT,
     {"emp": [(1, "A", "x", 5, None)]}),
    ("case-sum-of-a-nullable-expression", "SELECT SUM(sal + mgr) AS s FROM emp",
     "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(sal + mgr), 0) END AS s FROM emp", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    ("case-sum-of-another-column", "SELECT SUM(sal) AS s FROM emp",
     "SELECT CASE WHEN COUNT(*) = 0 THEN NULL ELSE COALESCE(SUM(mgr), 0) END AS s FROM emp", STRICT, {"emp": [(1, "A", "x", 5, None)]}),
    ("eager-dept-groups-not-counted", PLAIN, eager(dept_count="1"), STRICT, {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None), (2, "x", None)]}),
    ("eager-dept-side-filtered", PLAIN, eager().replace("FROM dept GROUP BY name", "FROM dept WHERE loc = 'l' GROUP BY name"), STRICT,
     {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("eager-emp-group-having", PLAIN, eager().replace("GROUP BY job", "GROUP BY job HAVING COUNT(*) > 1"), STRICT,
     {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("eager-sum-times-own-count", PLAIN, eager().replace("SUM(p.s * q.c)", "SUM(p.s * p.c)"), STRICT,
     {"emp": [(1, "A", "x", 5, None), (2, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
    ("eager-other-filter", PLAIN, eager().replace("WHERE ename = 'A'", "WHERE ename = 'B'"), STRICT,
     {"emp": [(1, "A", "x", 5, None)], "dept": [(1, "x", None)]}),
]

# equivalent, whether proved or not: the random databases must agree
AGREE = [
    ("hidden-group-key", PLAIN,
     "SELECT CASE WHEN t.n = 0 THEN NULL ELSE t.s END AS s FROM (SELECT COALESCE(SUM(p.s * q.c), 0) AS s, COALESCE(SUM(p.c * q.c), 0) AS n "
     "FROM (SELECT job, COALESCE(SUM(sal), 0) AS s, COUNT(*) AS c FROM emp WHERE ename = 'A' GROUP BY job, mgr) AS p "
     "JOIN (SELECT name, COUNT(*) AS c FROM dept GROUP BY name) AS q ON p.job = q.name) AS t", STRICT),
    ("left-join-of-groups", PLAIN, eager(join="LEFT JOIN"), STRICT),
]


def _database(constraints, rows):
    db = duckdb.connect()
    for table, ddl in _ddl(constraints).items():
        db.execute(f"CREATE TABLE {table} ({ddl})")
        insert_rows(db, table, rows.get(table, []))
    return db


def _random_rows(rng, constraints):
    def maybe(table, column, value):
        return None if column not in constraints[table].not_null and rng.random() < 0.3 else value

    emps = [
        (k, maybe("emp", "ename", rng.choice(["A", "A", "B"])), maybe("emp", "job", rng.choice(["x", "y", "z"])),
         maybe("emp", "sal", rng.choice([0, 1, 5, -3])), maybe("emp", "mgr", rng.choice([1, 2])))
        for k in rng.sample(range(1, 8), rng.randint(0, 6))
    ]
    depts = [(k, maybe("dept", "name", rng.choice(["x", "y", "w"])), maybe("dept", "loc", "l")) for k in rng.sample(range(1, 6), rng.randint(0, 4))]
    return {"emp": emps, "dept": depts}


def _bag(rows):
    return Counter(tuple(int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() else v for v in row) for row in rows)


def _prove(left, right, constraints, dialect="mysql"):
    extra = {"exact_arithmetic": True} if dialect == "mysql" else {}
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, constraints=constraints, compare_names=False, dialect=dialect, **extra)


@pytest.mark.parametrize("name, left, right, constraints", PROVED, ids=[p[0] for p in PROVED])
def test_equivalent_pair_is_proved(name, left, right, constraints):
    result = _prove(left, right, constraints)
    assert result.proven, result.reason


@pytest.mark.parametrize("name, left, right, constraints", PROVED + AGREE, ids=[p[0] for p in PROVED + AGREE])
def test_equivalent_pair_agrees_on_random_databases(name, left, right, constraints):
    rng = random.Random(name)
    for _ in range(40):
        rows = _random_rows(rng, constraints)
        first, second = run_unoptimized(_database(constraints, rows), left, right)
        assert _bag(first) == _bag(second), (rows, first, second)


@pytest.mark.parametrize("name, left, right, constraints, witness", DIFFERENT, ids=[p[0] for p in DIFFERENT])
def test_witness_separates_the_pair(name, left, right, constraints, witness):
    first, second = run_unoptimized(_database(constraints, witness), left, right)
    assert _bag(first) != _bag(second)


@pytest.mark.parametrize("name, left, right, constraints, witness", DIFFERENT, ids=[p[0] for p in DIFFERENT])
@pytest.mark.parametrize("dialect", ["mysql", "duckdb"])
def test_different_pair_is_never_proved(name, left, right, constraints, witness, dialect):
    assert not _prove(left, right, constraints, dialect).proven
    assert not _prove(right, left, constraints, dialect).proven


def test_eager_form_reads_back_as_the_plain_sum():
    not_null = {t: c.not_null for t, c in STRICT.items()}
    normalized = normalize(eager(), schema=SCHEMA, not_null=not_null)
    assert normalized.startswith("SELECT SUM(") and "CASE" not in normalized and "GROUP BY" not in normalized, normalized


def test_nullable_sum_keeps_its_zero():
    """With sal nullable the eager form is SUM(COALESCE(sal, 0)) over the join, not SUM(sal)."""

    not_null = {t: c.not_null for t, c in LOOSE.items()}
    normalized = normalize(eager(), schema=SCHEMA, not_null=not_null)
    assert "COALESCE(" in normalized and "GROUP BY" not in normalized, normalized


def test_volatile_argument_is_not_read_as_one_value():
    """COUNT(sal + RANDOM()) and SUM(sal + RANDOM()) are two draws, not aggregates of the same values."""

    from sqlglot import parse_one

    from kumosql.eager_sum_readback import read_back_eager_sums

    select = parse_one(
        "SELECT CASE WHEN COUNT(sal + RANDOM()) = 0 THEN NULL ELSE COALESCE(SUM(sal + RANDOM()), 0) END AS s FROM emp", read="duckdb"
    )
    assert read_back_eager_sums(select, {"emp": frozenset({"sal"})}) is None


def test_group_by_empty_tuple_is_a_global_aggregate():
    from sqlglot import parse_one

    from kumosql.eager_sum_readback import read_back_eager_sums

    select = parse_one("SELECT SUM(COALESCE(g.s, 0)) AS s FROM (SELECT SUM(sal) AS s FROM emp GROUP BY ()) AS g", read="duckdb")
    assert read_back_eager_sums(select, {"emp": frozenset({"sal"})}) is None
