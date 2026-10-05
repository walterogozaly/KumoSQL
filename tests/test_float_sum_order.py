"""The row-order assumption of SUM and AVG: dropped, narrowed to an identical plan, or kept (float_sum_order.py)."""

import pytest

pytest.importorskip("z3")

from kumosql.float_sum_order import SAME_PLAN_ASSUMPTION  # noqa: E402
from kumosql.smt_equivalence import BASE_ASSUMPTIONS, SmtStatus, prove_equivalent_smt  # noqa: E402

ORDER = BASE_ASSUMPTIONS[2]
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "g": "FLOAT64", "n": "NUMERIC", "c": "BOOL"}}
SCHEMA = {"t": list(TYPES["t"])}


def prove(left, right, types=TYPES):
    return prove_equivalent_smt(left, right, schema=SCHEMA, types=types, timeout_ms=5000)


def order_assumptions(result):
    return [a for a in result.assumptions if a in (ORDER, SAME_PLAN_ASSUMPTION)]


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT SUM(f) AS v FROM t", "select sum(f) as v from t"),
        ("SELECT x, SUM(f) AS v FROM t GROUP BY x", "select x, sum(f) as v from t group by x"),
        ("SELECT AVG(f) AS v FROM t", "select avg(f) as v from t"),
        ("SELECT SUM(DISTINCT f) AS v FROM t", "select sum(distinct f) as v from t"),
        ("SELECT x FROM t GROUP BY x HAVING SUM(f) > 1", "select x from t group by x having sum(f) > 1"),
        ("WITH d AS (SELECT f FROM t WHERE x > 1) SELECT SUM(f) AS v FROM d", "WITH d AS (SELECT f FROM t WHERE x > 1) SELECT SUM(f) AS v FROM d"),
    ],
)
def test_an_identical_float_sum_replaces_the_order_assumption_with_the_same_plan_one(left, right):
    result = prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert order_assumptions(result) == [SAME_PLAN_ASSUMPTION]


@pytest.mark.parametrize(
    "left, right",
    [
        # the same rows reached by another plan
        ("SELECT SUM(f) AS v FROM t WHERE y > 0", "SELECT SUM(f) AS v FROM t WHERE y >= 1"),
        ("WITH d AS (SELECT f FROM t) SELECT SUM(f) AS v FROM d", "SELECT SUM(f) AS v FROM (SELECT f FROM t)"),
        ("SELECT SUM(f) AS v FROM t", "SELECT SUM(f) AS v FROM t WHERE TRUE"),
        ("SELECT SUM(v) AS s FROM (SELECT f AS v FROM t UNION ALL SELECT g AS v FROM t)", "SELECT SUM(v) AS s FROM (SELECT g AS v FROM t UNION ALL SELECT f AS v FROM t)"),
        # AVG written as its sum over its count
        ("SELECT AVG(f) AS v FROM t", "SELECT SUM(f) / COUNT(f) AS v FROM t"),
        # a window or a correlated subquery is not compared by text
        ("SELECT x, SUM(f) OVER (PARTITION BY y) AS v FROM t", "select x, sum(f) over (partition by y) as v from t"),
        ("SELECT x, (SELECT SUM(u.f) FROM t u WHERE u.x = o.x) AS v FROM t o", "select x, (select sum(u.f) from t u where u.x = o.x) as v from t o"),
    ],
)
def test_the_order_assumption_stays_where_the_plans_can_differ(left, right):
    result = prove(left, right)
    assert ORDER in result.assumptions
    assert SAME_PLAN_ASSUMPTION not in result.assumptions


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT SUM(x) AS v FROM t WHERE f > 0", "SELECT SUM(x) AS v FROM t WHERE 0 < f"),
        ("SELECT SUM(n) AS v FROM t", "SELECT SUM(n) AS v FROM t WHERE TRUE"),
        ("SELECT SUM(x) AS v FROM t GROUP BY f", "SELECT SUM(x) AS v FROM t WHERE f IS NOT NULL OR f IS NULL GROUP BY f"),
    ],
)
def test_an_exact_sum_needs_no_order_assumption_even_beside_a_float_column(left, right):
    result = prove(left, right)
    assert order_assumptions(result) == []


def test_a_sum_with_no_declared_type_is_not_exact():
    result = prove("SELECT SUM(x) AS v FROM t WHERE f > 0", "SELECT SUM(x) AS v FROM t WHERE 0 < f", types=None)
    assert ORDER in result.assumptions


def test_one_sided_aggregates_keep_the_assumption():
    # an identical SUM on one side only, and a different one on the other
    result = prove("SELECT SUM(f) AS v, COUNT(*) AS c FROM t", "SELECT SUM(f) AS v, COUNT(*) AS c FROM t WHERE TRUE")
    assert ORDER in result.assumptions


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT SUM(f) AS v FROM t", "SELECT SUM(s) AS v FROM (SELECT SUM(f) AS s FROM t GROUP BY x)"),
        ("SELECT SUM(f) + SUM(g) AS v FROM t", "SELECT SUM(f + g) AS v FROM t"),
        ("SELECT SUM(f) AS v FROM t", "SELECT SUM(g) AS v FROM t"),
    ],
)
def test_regrouped_float_additions_are_never_proven(left, right):
    assert prove(left, right).status is not SmtStatus.PROVEN_EQUIVALENT


def test_other_dialects_keep_the_assumption_unchanged():
    result = prove_equivalent_smt("SELECT SUM(a) FROM t", "select sum(a) from t", dialect="mysql", timeout_ms=5000)
    assert ORDER in result.assumptions
