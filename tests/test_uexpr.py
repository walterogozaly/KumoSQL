"""The bag-equivalence backend (``kumosql.uexpr``) on hand-written pairs.

Pairs it must prove, pairs it must not prove (not equivalent, or equivalent only under an assumption it does
not make), constructs outside its fragment (not proven, with ``unsupported`` as the reason) and how a top-level
``ORDER BY .. LIMIT`` is compared. The published SQLSolver suites and their floors are in
``tests/test_uexpr_benchmarks.py``. The backend is not wired into the combined prover yet, so these tests call
``kumosql.uexpr.prove_bag_equivalent`` directly.
"""

import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import TIE_ASSUMPTION, TableConstraints, SmtStatus
from kumosql.uexpr import DECLARED_CONSTRAINTS_ASSUMPTION, prove_bag_equivalent

SCHEMA = {"emp": ["id", "dept", "sal"], "t": ["a", "b"], "u": ["a", "c"]}
CONSTRAINTS = {"emp": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
TYPES = {
    "emp": {"id": "INT", "dept": "INT", "sal": "INT"},
    "t": {"a": "INT", "b": "INT"},
    "u": {"a": "INT", "c": "INT"},
}


def prove(left: str, right: str, **options):
    settings = dict(schema=SCHEMA, constraints=CONSTRAINTS, types=TYPES, dialect="mysql", exact_arithmetic=True, compare_names=False)
    settings.update(options)
    return prove_bag_equivalent(left, right, **settings)


PROVEN = [
    # filters and projections
    ("SELECT a FROM t WHERE a > 1 AND b > 2", "SELECT a FROM t WHERE b > 2 AND a > 1"),
    ("SELECT a FROM t WHERE NOT (a > 1)", "SELECT a FROM t WHERE a <= 1"),
    ("SELECT a FROM t WHERE 1 = 0", "SELECT a FROM t WHERE a > 1 AND a < 1"),
    ("SELECT * FROM t", "SELECT a, b FROM t"),
    # UNION ALL is +, a join is *, both commute
    ("SELECT a FROM t UNION ALL SELECT a FROM u", "SELECT a FROM u UNION ALL SELECT a FROM t"),
    ("SELECT t.a FROM t JOIN u ON t.a = u.a", "SELECT t.a FROM u JOIN t ON u.a = t.a"),
    # DISTINCT is squash
    ("SELECT DISTINCT a FROM t", "SELECT a FROM t GROUP BY a"),
    # IN and EXISTS over a subquery
    ("SELECT a FROM t WHERE a IN (SELECT a FROM u)", "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)"),
    # a LEFT JOIN is the inner join plus the unmatched rows padded with NULL; USING reads the left column
    ("SELECT a FROM t LEFT JOIN u USING (a)", "SELECT t.a FROM t LEFT JOIN u ON t.a = u.a"),
    # grouping
    ("SELECT a, COUNT(*) FROM t GROUP BY a", "SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 0"),
    # SUM skips NULL
    ("SELECT SUM(a) FROM t", "SELECT SUM(a) FROM t WHERE a IS NOT NULL"),
    # a table with no declared columns or constraints is just an opaque relation
    ("SELECT x FROM nosuch WHERE x > 1", "SELECT x FROM nosuch WHERE 1 < x"),
]

NOT_PROVEN = [
    # bag against set
    ("SELECT a FROM t", "SELECT DISTINCT a FROM t"),
    ("SELECT a FROM t UNION ALL SELECT a FROM u", "SELECT a FROM t UNION SELECT a FROM u"),
    ("SELECT a FROM t EXCEPT ALL SELECT a FROM u", "SELECT a FROM t EXCEPT SELECT a FROM u"),
    # a join repeats rows, IN does not; a LEFT JOIN keeps unmatched rows, an inner join drops them
    ("SELECT t.a FROM t JOIN u ON t.a = u.a", "SELECT a FROM t WHERE a IN (SELECT a FROM u)"),
    ("SELECT t.a FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.a FROM t JOIN u ON t.a = u.a"),
    # a self join on a column that is not a key repeats rows
    ("SELECT t1.a FROM t t1 JOIN t t2 ON t1.a = t2.a", "SELECT a FROM t"),
    # boundary values and different columns
    ("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a >= 1"),
    ("SELECT a FROM t", "SELECT b FROM t"),
    # NULL: a = a drops the rows where a is NULL; IS NULL is not IS NOT NULL
    ("SELECT a FROM t WHERE a = a", "SELECT a FROM t"),
    ("SELECT a FROM t WHERE b IS NULL", "SELECT a FROM t WHERE b IS NOT NULL"),
    # NOT IN is not NOT EXISTS when the subquery returns a NULL
    ("SELECT a FROM t WHERE a NOT IN (SELECT a FROM u)", "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a)"),
    # aggregates
    ("SELECT COUNT(a) FROM t", "SELECT COUNT(*) FROM t"),
    ("SELECT SUM(b) FROM t", "SELECT SUM(DISTINCT b) FROM t"),
    # a keyed self join does not rescue an unkeyed column
    ("SELECT e.dept FROM emp e JOIN emp f ON e.dept = f.dept", "SELECT dept FROM emp"),
]


@pytest.mark.parametrize("left,right", PROVEN)
def test_proves_equivalent_pairs(left, right):
    for first, second in ((left, right), (right, left)):
        result = prove(first, second)
        assert result.proven, result.reason
        assert result.status == SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("left,right", NOT_PROVEN)
def test_does_not_prove_other_pairs(left, right):
    for first, second in ((left, right), (right, left)):
        result = prove(first, second)
        assert not result.proven
        assert result.status == SmtStatus.NOT_PROVEN
        assert result.reason.startswith("bag procedure:")


def test_never_refutes():
    """Not proven means unknown: the backend has no way to answer "different"."""

    assert {prove(a, b).status for a, b in NOT_PROVEN} == {SmtStatus.NOT_PROVEN}


def test_declared_key_is_an_axiom_and_is_reported():
    left = "SELECT e.id FROM emp e JOIN emp f ON e.id = f.id"
    right = "SELECT id FROM emp"
    result = prove(left, right)
    assert result.proven and DECLARED_CONSTRAINTS_ASSUMPTION in result.assumptions
    without = prove(left, right, constraints={})
    assert not without.proven, "without the key, the self join repeats a row once per duplicate"


def test_not_null_axiom():
    left = "SELECT id FROM emp WHERE id IS NOT NULL"
    right = "SELECT id FROM emp"
    assert prove(left, right).proven
    assert not prove(left, right, constraints={}).proven


@pytest.mark.parametrize(
    "sql,reason",
    [
        ("SELECT a, ROW_NUMBER() OVER (ORDER BY b) FROM t", "unsupported"),
        ("SELECT a, SUM(b) FROM t GROUP BY ROLLUP (a)", "unsupported"),
        ("SELECT a, COUNT(DISTINCT b, a) FROM t GROUP BY a", "unsupported"),
        ("SELECT a FROM t, UNNEST(ARRAY[1, 2]) AS x", "unsupported"),
        ("WITH RECURSIVE x AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM x WHERE n < 3) SELECT n FROM x", "unsupported"),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a LIMIT 2) AS x", "unsupported"),
    ],
)
def test_unsupported_constructs_are_not_proven(sql, reason):
    """A query outside the fragment is never proven, not even against itself."""

    result = prove(sql, sql)
    assert not result.proven
    assert reason in result.reason


def test_column_count_and_names():
    assert "different numbers of columns" in prove("SELECT a FROM t", "SELECT a, b FROM t").reason
    renamed = prove("SELECT a AS x FROM t", "SELECT a AS y FROM t", compare_names=True)
    assert not renamed.proven and "name their columns differently" in renamed.reason
    assert prove("SELECT a AS x FROM t", "SELECT a AS y FROM t").proven


# ORDER BY and LIMIT -------------------------------------------------------------------------------------------


def test_order_by_without_limit_does_not_change_the_bag():
    assert prove("SELECT a FROM t ORDER BY a", "SELECT a FROM t").proven
    assert prove("SELECT a FROM t ORDER BY a", "SELECT a FROM t WHERE 1 < 2 ORDER BY a").proven
    assert not prove("SELECT a FROM t ORDER BY a", "SELECT a FROM t WHERE a > 1 ORDER BY a").proven


def test_same_cut_over_equivalent_queries():
    left = "SELECT a FROM t WHERE a > 1 ORDER BY a LIMIT 3 OFFSET 1"
    right = "SELECT a FROM t WHERE 1 < a ORDER BY a LIMIT 3 OFFSET 1"
    result = prove(left, right)
    assert result.proven and TIE_ASSUMPTION not in result.assumptions, "ordering by the only output column leaves no ties"
    union = prove(
        "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY a LIMIT 3",
        "SELECT a FROM u UNION ALL SELECT a FROM t ORDER BY a LIMIT 3",
    )
    assert union.proven


def test_ties_in_the_ordering_are_an_assumption():
    result = prove("SELECT a FROM t ORDER BY b LIMIT 2", "SELECT a FROM t ORDER BY b LIMIT 2")
    assert result.proven and TIE_ASSUMPTION in result.assumptions


def test_cut_over_different_queries_is_not_proven():
    assert not prove("SELECT a FROM t WHERE a > 1 ORDER BY a LIMIT 3", "SELECT a FROM t ORDER BY a LIMIT 3").proven


@pytest.mark.parametrize(
    "left,right,reason",
    [
        ("SELECT a FROM t ORDER BY a LIMIT 3", "SELECT a FROM t ORDER BY a LIMIT 4", "different ORDER BY .. LIMIT"),
        ("SELECT a FROM t ORDER BY a LIMIT 3", "SELECT a FROM t ORDER BY a DESC LIMIT 3", "different ORDER BY .. LIMIT"),
        ("SELECT a FROM t ORDER BY a LIMIT 3", "SELECT a FROM t", "different ORDER BY .. LIMIT"),
        ("SELECT a FROM t LIMIT 3", "SELECT a FROM t LIMIT 3", "arbitrary rows"),
    ],
)
def test_cuts_that_differ_or_have_no_order_are_not_proven(left, right, reason):
    result = prove(left, right)
    assert not result.proven and reason in result.reason


# The pairs that must stay unproven ------------------------------------------------------------------------------


def test_spark_pairs_that_must_stay_unproven_are_not_proven():
    """The Spark pairs in ``tests/fixtures/sqlsolver/not_provable.json`` hold only with a fixed tie-break for
    ``LIMIT`` without ``ORDER BY``, or only on some strings; a proof of one is a wrong proof."""

    import importlib.util
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parent.parent / "tools"
    spec = importlib.util.spec_from_file_location("uexpr_bench", tools / "uexpr_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["uexpr_bench"] = module
    spec.loader.exec_module(module)
    bench = module.bench
    indices = sorted(bench.must_not_prove("spark"))
    assert indices == [44, 50, 60, 61]
    tables = bench.load_schema(bench.FIXTURES / bench.SUITES["spark"][1])
    pairs = bench.load_pairs(bench.FIXTURES / bench.SUITES["spark"][0])
    for index in indices:
        left, right = pairs[index]
        assert not module.prove(left, right, tables), f"spark[{index}] must stay unproven"
