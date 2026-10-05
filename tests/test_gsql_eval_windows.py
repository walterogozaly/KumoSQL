"""Window (analytic) functions of the GoogleSQL reference evaluator (``kumosql.gsql_eval.windows``)."""

from __future__ import annotations

import math
import sys
from decimal import Decimal

import pytest
from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T

I, F, S, N = T.INT64, T.FLOAT64, T.STRING, T.NUMERIC

# g, a, x: partition p has a = 1, 2, 3, 4 (x NULL at a=2); partition q has a = 5, 7; all (g, a) are distinct
T1 = Table(
    [("g", S), ("a", I), ("x", I)],
    [("p", 1, 10), ("p", 2, None), ("p", 3, 30), ("q", 5, 50), ("p", 4, 40), ("q", 7, 7)],
)
# ties on a inside partition p (x differs) and inside q (identical rows)
TIES = Table(
    [("g", S), ("a", I), ("x", I)],
    [("p", 1, 10), ("p", 2, 20), ("p", 2, 30), ("p", 3, 40), ("q", 5, 50), ("q", 5, 50), ("q", 6, 60)],
)
NUMS = Table([("v", I), ("f", F), ("d", N)], [(1, 1.0, Decimal("1")), (2, 2.5, Decimal("2")), (4, None, None), (7, 7.0, Decimal("7"))])


def run(sql, table=T1, name="t", params=None, **kw):
    return evaluate(sql, Database({name: table}), params=params, **kw)


def rows(sql, table=T1, **kw):
    return run(sql, table, **kw).rows


def by_key(result, key=0):
    return {row[key]: row for row in result.rows}


# --- numbering ---------------------------------------------------------------------------------------------


def test_row_number_rank_dense_rank_without_ties():
    result = run("SELECT a, ROW_NUMBER() OVER (PARTITION BY g ORDER BY a), RANK() OVER (PARTITION BY g ORDER BY a), "
                 "DENSE_RANK() OVER (PARTITION BY g ORDER BY a) FROM t")
    assert result.deterministic
    assert by_key(result) == {1: (1, 1, 1, 1), 2: (2, 2, 2, 2), 3: (3, 3, 3, 3), 4: (4, 4, 4, 4), 5: (5, 1, 1, 1), 7: (7, 2, 2, 2)}
    assert [t for _, t in result.columns] == [I, I, I, I]


def test_rank_family_with_ties_is_deterministic():
    result = run("SELECT x, RANK() OVER (ORDER BY a), DENSE_RANK() OVER (ORDER BY a), PERCENT_RANK() OVER (ORDER BY a), "
                 "CUME_DIST() OVER (ORDER BY a) FROM t WHERE g = 'p'", TIES)
    assert result.deterministic
    # a: 1, 2, 2, 3
    assert by_key(result) == {
        10: (10, 1, 1, 0.0, 0.25),
        20: (20, 2, 2, 1 / 3, 0.75),
        30: (30, 2, 2, 1 / 3, 0.75),
        40: (40, 4, 3, 1.0, 1.0),
    }
    assert [t for _, t in result.columns][3:] == [F, F]


def test_percent_rank_of_single_row_partition_is_zero():
    assert rows("SELECT PERCENT_RANK() OVER (ORDER BY a), CUME_DIST() OVER (ORDER BY a) FROM t WHERE a = 1") == [(0.0, 1.0)]


def test_row_number_over_ties_is_nondeterministic_unless_rows_are_identical():
    result = run("SELECT x, ROW_NUMBER() OVER (ORDER BY a) FROM t WHERE g = 'p'", TIES)
    assert not result.deterministic and "ROW_NUMBER" in result.reasons[0]
    same = run("SELECT g, a, x, ROW_NUMBER() OVER (ORDER BY a) FROM t WHERE g = 'q'", TIES)
    assert same.deterministic
    assert sorted(same.rows) == [("q", 5, 50, 1), ("q", 5, 50, 2), ("q", 6, 60, 3)]


def test_row_number_without_order_by():
    result = run("SELECT ROW_NUMBER() OVER () FROM t", T1)
    assert sorted(r[0] for r in result.rows) == [1, 2, 3, 4, 5, 6]
    assert not result.deterministic  # rows differ, so which one is 1 is undetermined
    single = run("SELECT ROW_NUMBER() OVER (PARTITION BY a) FROM t")
    assert single.deterministic and set(single.rows) == {(1,)}


def test_ntile_distribution():
    # 7 rows over 3 buckets: sizes 3, 2, 2; 7 rows over 10 buckets: one row each
    t = Table([("a", I)], [(i,) for i in range(1, 8)])
    assert [r[0] for r in rows("SELECT NTILE(3) OVER (ORDER BY a) FROM t ORDER BY a", t)] == [1, 1, 1, 2, 2, 3, 3]
    assert [r[0] for r in rows("SELECT NTILE(10) OVER (ORDER BY a) FROM t ORDER BY a", t)] == [1, 2, 3, 4, 5, 6, 7]
    assert [r[0] for r in rows("SELECT NTILE(1) OVER (ORDER BY a) FROM t ORDER BY a", t)] == [1] * 7


def test_ntile_over_ties_is_nondeterministic():
    assert not run("SELECT NTILE(2) OVER (ORDER BY a) FROM t WHERE g = 'p'", TIES).deterministic


def test_ntile_argument_is_checked():
    with pytest.raises(EvalError):  # BigQuery fails these while running, so not on an empty input
        run("SELECT NTILE(0) OVER (ORDER BY a) FROM t")
    with pytest.raises(EvalError):
        run("SELECT NTILE(NULL) OVER (ORDER BY a) FROM t")
    assert rows("SELECT NTILE(0) OVER (ORDER BY a) FROM t WHERE a > 100") == []
    with pytest.raises(AnalysisError):  # a column is not a constant
        run("SELECT NTILE(a) OVER (ORDER BY a) FROM t")
    assert rows("SELECT NTILE(@n) OVER (ORDER BY a) FROM t WHERE a = 1", params={"n": (I, 2)}) == [(1,)]


# --- ordering and NULLs ---------------------------------------------------------------------------------------


def test_null_ordering_defaults_and_overrides():
    t = Table([("a", I)], [(2,), (None,), (1,)])
    q = lambda spec: [r[0] for r in rows(f"SELECT ROW_NUMBER() OVER (ORDER BY a {spec}) FROM t", t)]
    assert q("") == [3, 1, 2]  # NULLS FIRST for ASC
    assert q("DESC") == [1, 3, 2]  # NULLS LAST for DESC
    assert q("NULLS LAST") == [2, 3, 1]
    assert q("DESC NULLS FIRST") == [2, 1, 3]


def test_nulls_are_one_peer_group_and_nan_sorts_first():
    t = Table([("f", F)], [(1.0,), (None,), (None,), (float("nan"),), (float("nan"),)])
    got = rows("SELECT f, RANK() OVER (ORDER BY f) FROM t", t)
    ranks = {("null" if f is None else "nan" if f != f else f): r for f, r in got}
    assert ranks == {"null": 1, "nan": 3, 1.0: 5}


def test_partition_by_groups_nulls_and_floats_together():
    t = Table([("k", F), ("a", I)], [(None, 1), (None, 2), (0.0, 1), (-0.0, 2)])
    got = sorted(rows("SELECT a, ROW_NUMBER() OVER (PARTITION BY k ORDER BY a) FROM t", t))
    assert got == [(1, 1), (1, 1), (2, 2), (2, 2)]


def test_window_order_by_multiple_keys_and_expressions():
    got = rows("SELECT g, a, ROW_NUMBER() OVER (ORDER BY g DESC, a) FROM t ORDER BY 3")
    assert got == [("q", 5, 1), ("q", 7, 2), ("p", 1, 3), ("p", 2, 4), ("p", 3, 5), ("p", 4, 6)]
    assert rows("SELECT a, ROW_NUMBER() OVER (ORDER BY -a) FROM t WHERE g = 'q' ORDER BY a") == [(5, 2), (7, 1)]


# --- LAG / LEAD ---------------------------------------------------------------------------------------------


def test_lag_lead_defaults_and_offsets():
    got = by_key(run(
        "SELECT a, LAG(x) OVER (ORDER BY a), LAG(x, 2) OVER (ORDER BY a), LEAD(x) OVER (ORDER BY a), "
        "LEAD(x, 2, -1) OVER (ORDER BY a), LAG(x, 0) OVER (ORDER BY a) FROM t WHERE g = 'p'"))
    assert got == {
        1: (1, None, None, None, 30, 10),
        2: (2, 10, None, 30, 40, None),
        3: (3, None, 10, 40, -1, 30),
        4: (4, 30, None, None, -1, 40),
    }


def test_lag_lead_respect_and_ignore_nulls():
    q = "SELECT a, {f} FROM t WHERE g = 'p' ORDER BY a"
    assert rows(q.format(f="LAG(x) OVER (ORDER BY a)")) == [(1, None), (2, 10), (3, None), (4, 30)]
    assert rows(q.format(f="LAG(x RESPECT NULLS) OVER (ORDER BY a)")) == [(1, None), (2, 10), (3, None), (4, 30)]
    assert rows(q.format(f="LAG(x IGNORE NULLS) OVER (ORDER BY a)")) == [(1, None), (2, 10), (3, 10), (4, 30)]
    assert rows(q.format(f="LEAD(x IGNORE NULLS) OVER (ORDER BY a)")) == [(1, 30), (2, 30), (3, 40), (4, None)]
    assert rows(q.format(f="LEAD(x, 2, 99) IGNORE NULLS OVER (ORDER BY a)")) == [(1, 40), (2, 40), (3, 99), (4, 99)]


def test_lag_default_is_coerced_and_checked():
    got = run("SELECT LAG(f, 1, 7) OVER (ORDER BY v) FROM t", NUMS)
    assert got.columns[0][1] == F
    assert [r[0] for r in got.rows] == [7.0, 1.0, 2.5, None]
    with pytest.raises(AnalysisError):
        run("SELECT LAG(v, 1, 'a') OVER (ORDER BY v) FROM t", NUMS)
    with pytest.raises(AnalysisError):
        run("SELECT LAG(v, 1, 1.5) OVER (ORDER BY v) FROM t", NUMS)
    with pytest.raises(AnalysisError):  # the default must be constant
        run("SELECT LAG(v, 1, v) OVER (ORDER BY v) FROM t", NUMS)
    got = run("SELECT LAG(f, 1, CAST(7 AS FLOAT64) + 1) OVER (ORDER BY v) FROM t", NUMS)  # constant, but not a literal
    assert [r[0] for r in got.rows] == [8.0, 1.0, 2.5, None]
    arrays = Table([("a", T.array(I))], [((1,),), ((2,),)])
    assert [r[0] for r in rows("SELECT LAG(a, 1, [-1]) OVER (ORDER BY a[OFFSET(0)]) FROM t", arrays)] == [(-1,), (1,)]


def test_lag_argument_and_clause_errors():
    for sql in ("SELECT LAG(x, -1) OVER (ORDER BY a) FROM t", "SELECT LEAD(x, NULL) OVER (ORDER BY a) FROM t"):
        with pytest.raises(EvalError):
            run(sql)
    for sql in (
        "SELECT LAG(x) OVER (PARTITION BY g) FROM t",  # ORDER BY is required
        "SELECT LEAD(x) OVER () FROM t",
        "SELECT LAG(x) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",  # no frame
    ):
        with pytest.raises(AnalysisError):
            run(sql)
    with pytest.raises(Unsupported):
        run("SELECT LAG(x, 0) IGNORE NULLS OVER (ORDER BY a) FROM t")


def test_lag_over_ties():
    assert not run("SELECT LAG(x) OVER (ORDER BY a) FROM t WHERE g = 'p'", TIES).deterministic
    # tied rows that agree on the value read leave LAG's multiset of answers alone only when the rows are identical
    assert run("SELECT g, a, x, LAG(x) OVER (ORDER BY a) FROM t WHERE g = 'q'", TIES).deterministic


# --- FIRST_VALUE / LAST_VALUE / NTH_VALUE -----------------------------------------------------------------------


def test_first_last_value_default_frame():
    got = rows("SELECT a, FIRST_VALUE(x) OVER (ORDER BY a), LAST_VALUE(x) OVER (ORDER BY a) FROM t WHERE g = 'p' ORDER BY a")
    # the default frame ends at the current row
    assert got == [(1, 10, 10), (2, 10, None), (3, 10, 30), (4, 10, 40)]


def test_first_last_value_without_order_by_see_whole_partition():
    got = rows("SELECT a, LAST_VALUE(x) OVER (PARTITION BY g) FROM t ORDER BY a")
    assert not run("SELECT a, LAST_VALUE(x) OVER (PARTITION BY g) FROM t").deterministic
    assert {r[1] for r in got if r[0] in (5, 7)} <= {50, 7}


def test_ignore_nulls_in_value_functions():
    q = "SELECT a, {f} FROM t WHERE g = 'p' ORDER BY a"
    assert rows(q.format(f="FIRST_VALUE(x) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)")) == [
        (1, None), (2, 30), (3, 40), (4, None)]
    assert rows(q.format(f="FIRST_VALUE(x IGNORE NULLS) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)")) == [
        (1, 30), (2, 30), (3, 40), (4, None)]
    assert rows(q.format(f="LAST_VALUE(x IGNORE NULLS) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND 1 FOLLOWING)")) == [
        (1, 10), (2, 30), (3, 40), (4, 40)]
    assert rows(q.format(f="LAST_VALUE(x) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND 1 FOLLOWING)")) == [
        (1, None), (2, 30), (3, 40), (4, 40)]


def test_nth_value():
    q = "SELECT a, {f} FROM t WHERE g = 'p' ORDER BY a"
    full = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
    assert rows(q.format(f=f"NTH_VALUE(x, 2) OVER (ORDER BY a {full})")) == [(a, None) for a in (1, 2, 3, 4)]
    assert rows(q.format(f=f"NTH_VALUE(x, 2) IGNORE NULLS OVER (ORDER BY a {full})")) == [(a, 30) for a in (1, 2, 3, 4)]
    assert rows(q.format(f=f"NTH_VALUE(x, 4) RESPECT NULLS OVER (ORDER BY a {full})")) == [(a, 40) for a in (1, 2, 3, 4)]
    assert rows(q.format(f=f"NTH_VALUE(x, 5) OVER (ORDER BY a {full})")) == [(a, None) for a in (1, 2, 3, 4)]
    assert rows(q.format(f="NTH_VALUE(x, 2) OVER (ORDER BY a)")) == [(1, None), (2, None), (3, None), (4, None)]
    with pytest.raises(EvalError):
        run("SELECT NTH_VALUE(x, 0) OVER (ORDER BY a) FROM t")


def test_value_functions_keep_the_value_type():
    got = run("SELECT FIRST_VALUE(f) OVER (ORDER BY v), LAST_VALUE(d) OVER (ORDER BY v) FROM t", NUMS)
    assert [t for _, t in got.columns] == [F, N]


def test_value_function_over_ties_is_nondeterministic_only_when_the_tie_matters():
    # TIES, partition p: a = 1, 2, 2, 3 with x = 10, 20, 30, 40
    q = "SELECT FIRST_VALUE(x) OVER (ORDER BY a {frame}) FROM t WHERE g = 'p'"
    assert run(q.format(frame=""), TIES).deterministic  # the first row is never a tied one
    assert not run(q.format(frame="ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING"), TIES).deterministic
    assert run(q.format(frame="ROWS BETWEEN CURRENT ROW AND CURRENT ROW"), TIES).deterministic  # every row sees itself
    # peers share the default frame, but which of them is its last row is undetermined
    assert not run("SELECT LAST_VALUE(x) OVER (ORDER BY a) FROM t WHERE g = 'p'", TIES).deterministic


# --- frames -------------------------------------------------------------------------------------------------


V = Table([("a", I), ("v", I)], [(1, 1), (2, 2), (2, 3), (4, 4), (7, 5), (None, 6), (None, 7)])
W = Table([("a", I), ("v", I)], [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])


def q_sum(frame, table=W, order="ORDER BY a"):
    return [r[0] for r in rows(f"SELECT SUM(v) OVER ({order} {frame}) FROM t ORDER BY a", table)]


def test_rows_frames():
    assert q_sum("ROWS BETWEEN 1 PRECEDING AND CURRENT ROW") == [1, 3, 5, 7, 9]
    assert q_sum("ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING") == [3, 6, 9, 12, 9]
    assert q_sum("ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW") == [1, 3, 6, 10, 15]
    assert q_sum("ROWS UNBOUNDED PRECEDING") == [1, 3, 6, 10, 15]
    assert q_sum("ROWS 2 PRECEDING") == [1, 3, 6, 9, 12]
    assert q_sum("ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING") == [15, 14, 12, 9, 5]
    assert q_sum("ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING") == [5, 7, 9, 5, None]
    assert q_sum("ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING") == [None, 1, 3, 5, 7]
    assert q_sum("ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING") == [15] * 5
    assert q_sum("ROWS BETWEEN 0 PRECEDING AND 0 FOLLOWING") == [1, 2, 3, 4, 5]
    assert q_sum("ROWS BETWEEN 10 PRECEDING AND 10 FOLLOWING") == [15] * 5
    assert q_sum("ROWS BETWEEN 3 FOLLOWING AND 1 FOLLOWING") == [None] * 5  # an empty frame
    assert q_sum("ROWS BETWEEN 1 PRECEDING AND CURRENT ROW", order="ORDER BY a DESC") == [3, 5, 7, 9, 5]


def test_rows_frame_offset_parameter():
    got = rows("SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN @n PRECEDING AND CURRENT ROW) FROM t ORDER BY a", W, params={"n": (I, 1)})
    assert [r[0] for r in got] == [1, 3, 5, 7, 9]


def test_default_frames_and_peers():
    # ORDER BY: RANGE UNBOUNDED PRECEDING .. CURRENT ROW, so peers share the sum and NULL keys are peers
    got = rows("SELECT a, SUM(v) OVER (ORDER BY a), COUNT(*) OVER (ORDER BY a) FROM t ORDER BY a, v", V)
    assert got == [(None, 13, 2), (None, 13, 2), (1, 14, 3), (2, 19, 5), (2, 19, 5), (4, 23, 6), (7, 28, 7)]
    # no ORDER BY: the whole partition
    assert {r for r in rows("SELECT SUM(v) OVER () FROM t", V)} == {(28,)}
    assert {r for r in rows("SELECT SUM(v) OVER (PARTITION BY a) FROM t WHERE a IS NOT NULL", V)} == {(1,), (5,), (4,), (5,)}


def test_aggregate_window_over_ties_is_deterministic_for_range_and_not_for_rows():
    assert run("SELECT SUM(x) OVER (ORDER BY a) FROM t", TIES).deterministic  # peers share a RANGE frame
    assert not run("SELECT SUM(x) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t WHERE g = 'p'", TIES).deterministic
    # tied rows that differ only in a column the answer ignores still make a ROWS frame order-dependent
    assert run("SELECT SUM(x) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t WHERE g = 'q'", TIES).deterministic


def test_range_frames_without_offsets():
    got = rows("SELECT a, SUM(v) OVER (ORDER BY a RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) FROM t ORDER BY a, v", V)
    assert [r[1] for r in got] == [28, 28, 15, 14, 14, 9, 5]
    got = rows("SELECT a, SUM(v) OVER (ORDER BY a RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM t", V)
    assert {r[1] for r in got} == {28}
    got = rows("SELECT a, SUM(v) OVER (ORDER BY a RANGE BETWEEN CURRENT ROW AND CURRENT ROW) FROM t ORDER BY a, v", V)
    assert [r[1] for r in got] == [13, 13, 1, 5, 5, 4, 5]


def test_range_frames_with_offsets():
    # keys 1, 2, 2, 4, 7 and two NULLs (values 6, 7); rows come back ordered like the window
    q = "SELECT SUM(v) OVER (ORDER BY a {order} RANGE BETWEEN {s} AND {e}) FROM t ORDER BY a {order}, v"

    def frame(order, start, end):
        return [r[0] for r in rows(q.format(order=order, s=start, e=end), V)]

    assert frame("", "1 PRECEDING", "1 FOLLOWING") == [13, 13, 6, 6, 6, 4, 5]
    assert frame("", "2 PRECEDING", "CURRENT ROW") == [13, 13, 1, 6, 6, 9, 5]
    assert frame("", "UNBOUNDED PRECEDING", "1 PRECEDING") == [13, 13, 13, 14, 14, 19, 23]
    assert frame("", "1 FOLLOWING", "UNBOUNDED FOLLOWING") == [28, 28, 14, 9, 9, 5, None]
    # DESC: PRECEDING looks at larger keys, NULLs sort last
    assert frame("DESC", "1 PRECEDING", "CURRENT ROW") == [5, 4, 5, 5, 6, 13, 13]


def test_range_offset_rows_exact():
    # ASC with NULLS FIRST: NULL rows only ever see their NULL peers through an offset
    got = rows("SELECT a, v, SUM(v) OVER (ORDER BY a RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t", V)
    assert sorted(got, key=lambda r: r[1]) == [
        (1, 1, 6),   # keys 1, 2, 2
        (2, 2, 6),   # keys 1, 2, 2 (3 is absent)
        (2, 3, 6),
        (4, 4, 4),   # keys in [3, 5]
        (7, 5, 5),
        (None, 6, 13),
        (None, 7, 13),
    ]
    got = rows("SELECT a, v, SUM(v) OVER (ORDER BY a DESC RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t", V)
    # DESC puts NULLs last; PRECEDING means larger keys; the frame ends at the last peer of the current row
    assert sorted(got, key=lambda r: r[1]) == [
        (1, 1, 6),   # keys 2, 2, 1
        (2, 2, 5),   # keys in [2, 3]: 2, 2
        (2, 3, 5),
        (4, 4, 4),
        (7, 5, 5),
        (None, 6, 13),
        (None, 7, 13),
    ]


def test_range_offset_on_unbounded_side_includes_null_rows():
    got = rows("SELECT a, v, COUNT(*) OVER (ORDER BY a RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) FROM t", V)
    by_v = {r[1]: r[2] for r in got}
    # NULLs come first, so UNBOUNDED PRECEDING includes them for every non-NULL key; for a NULL key the end is the NULL peers
    assert by_v == {1: 2, 2: 3, 3: 3, 4: 5, 5: 6, 6: 2, 7: 2}


def test_range_offsets_over_other_numeric_types():
    got = rows("SELECT v, COUNT(*) OVER (ORDER BY f RANGE BETWEEN 1.5 PRECEDING AND CURRENT ROW) FROM t ORDER BY v", NUMS)
    assert got == [(1, 1), (2, 2), (4, 1), (7, 1)]  # f = 1.0, 2.5, NULL (first, a peer group of one), 7.0
    got = rows("SELECT v, COUNT(*) OVER (ORDER BY d RANGE BETWEEN 5 PRECEDING AND 0 FOLLOWING) FROM t ORDER BY v", NUMS)
    assert got == [(1, 1), (2, 2), (4, 1), (7, 2)]
    got = rows("SELECT v, COUNT(*) OVER (ORDER BY v RANGE BETWEEN 2 PRECEDING AND 2 FOLLOWING) FROM t ORDER BY v", NUMS)
    assert got == [(1, 2), (2, 3), (4, 2), (7, 1)]


def test_range_offset_errors():
    for sql in (
        "SELECT SUM(v) OVER (ORDER BY a, v RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a RANGE BETWEEN 1.5 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN 1.5 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED FOLLOWING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN CURRENT ROW AND UNBOUNDED PRECEDING) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS 1 FOLLOWING) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN a PRECEDING AND CURRENT ROW) FROM t",  # not a constant
    ):
        with pytest.raises(AnalysisError):
            run(sql, W)
    for sql in (
        "SELECT SUM(v) OVER (ORDER BY a RANGE BETWEEN INTERVAL 1 DAY PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND CURRENT ROW) FROM t",
    ):
        with pytest.raises(Unsupported):
            run(sql, W)
    for sql in (
        "SELECT SUM(v) OVER (ORDER BY a RANGE BETWEEN -1 PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN CAST(NULL AS INT64) PRECEDING AND CURRENT ROW) FROM t",
        "SELECT SUM(v) OVER (ORDER BY a ROWS BETWEEN -1 PRECEDING AND CURRENT ROW) FROM t",
    ):
        with pytest.raises(EvalError):
            run(sql, W)
    dates = Table([("d", T.DATE), ("v", I)], [])
    with pytest.raises(Unsupported):
        run("SELECT SUM(v) OVER (ORDER BY d RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t", dates)
    with pytest.raises(AnalysisError):
        run("SELECT SUM(v) OVER (ORDER BY g RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t", T1)


def test_range_offsets_over_nan_infinite_and_null_keys():
    inf, nan = float("inf"), float("nan")
    t = Table([("f", F)], [(nan,), (nan,), (-inf,), (1.0,), (2.0,), (inf,), (None,)])
    got = rows("SELECT f, COUNT(*) OVER (ORDER BY f RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t", t)
    counts = {("null" if f is None else "nan" if f != f else f): c for f, c in got}
    # NULL and NaN keys only see their own peers; infinities only themselves; NaN is not a number for the others
    assert counts == {"null": 1, "nan": 2, -inf: 1, 1.0: 2, 2.0: 2, inf: 1}
    got = rows("SELECT f, COUNT(*) OVER (ORDER BY f RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) FROM t", t)
    counts = {("null" if f is None else "nan" if f != f else f): c for f, c in got}
    # an unbounded side still counts every earlier row: NULLs, then NaNs, then -inf ... (-inf - 1 is -inf: it counts itself)
    assert counts == {"null": 1, "nan": 3, -inf: 4, 1.0: 4, 2.0: 5, inf: 7}


def test_range_offset_overflow_stops_at_the_largest_double():
    big, inf = sys.float_info.max, float("inf")
    t = Table([("f", F)], [(1.0,), (big,), (inf,)])
    got = rows(f"SELECT f, COUNT(*) OVER (ORDER BY f RANGE BETWEEN CURRENT ROW AND {big!r} FOLLOWING) FROM t", t)
    assert dict(got) == {1.0: 2, big: 1, inf: 1}


def test_range_offsets_over_numeric_keys_are_exact_beyond_28_digits():
    top = Decimal("99999999999999999999999999999.999999999")
    t = Table([("d", N)], [(top,), (Decimal("99999999999999999999999999998.999999999"),), (Decimal("99999999999999999999999999996.999999999"),)])
    got = rows("SELECT d, COUNT(*) OVER (ORDER BY d RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) FROM t", t)
    assert sorted(c for _, c in got) == [1, 2, 2]


def test_frame_clause_rules():
    for fn in ("ROW_NUMBER()", "RANK()", "DENSE_RANK()", "PERCENT_RANK()", "CUME_DIST()", "NTILE(2)", "LAG(a)", "LEAD(a)"):
        with pytest.raises(AnalysisError):
            run(f"SELECT {fn} OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t")
    for fn in ("RANK()", "DENSE_RANK()", "PERCENT_RANK()", "CUME_DIST()", "NTILE(2)"):
        with pytest.raises(AnalysisError):
            run(f"SELECT {fn} OVER () FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT PERCENTILE_CONT(a, 0.5) OVER (PARTITION BY g ORDER BY a) FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT PERCENTILE_DISC(a, 0.5) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t")


def test_aggregate_distinct_forbids_order_and_frame():
    with pytest.raises(AnalysisError):
        run("SELECT COUNT(DISTINCT v) OVER (ORDER BY a) FROM t", W)
    with pytest.raises(AnalysisError):
        run("SELECT COUNT(DISTINCT v) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t", W)


# --- percentiles ---------------------------------------------------------------------------------------------


def test_percentile_cont_interpolates_and_ignores_nulls():
    got = run("SELECT PERCENTILE_CONT(v, 0.5) OVER (), PERCENTILE_CONT(v, 0) OVER (), PERCENTILE_CONT(v, 1) OVER (), "
              "PERCENTILE_CONT(v, 0.25) OVER () FROM t WHERE v < 8", NUMS)
    assert got.rows[0] == (3.0, 1.0, 7.0, 1.75)  # values 1, 2, 4, 7
    assert [t for _, t in got.columns] == [F, F, F, F]
    assert got.inexact
    f = run("SELECT PERCENTILE_CONT(f, 0.5) OVER () FROM t", NUMS)  # 1.0, 2.5, 7.0 (NULL ignored)
    assert f.rows[0] == (2.5,) and not f.inexact


def test_percentile_disc():
    got = rows("SELECT PERCENTILE_DISC(v, 0) OVER (), PERCENTILE_DISC(v, 0.25) OVER (), PERCENTILE_DISC(v, 0.26) OVER (), "
               "PERCENTILE_DISC(v, 0.5) OVER (), PERCENTILE_DISC(v, 1) OVER () FROM t", NUMS)
    assert got[0] == (1, 1, 2, 2, 7)
    assert [t for _, t in run("SELECT PERCENTILE_DISC(d, 0.5) OVER (), PERCENTILE_DISC(a, 0.5) OVER () FROM t", NUMS.__class__(
        NUMS.columns + [("a", S)], [r + ("x",) for r in NUMS.rows])).columns] == [N, S]
    assert rows("SELECT PERCENTILE_DISC(f, 0.5) OVER () FROM t", NUMS)[0] == (2.5,)


def test_percentile_partitions_and_empty_input():
    got = by_key(run("SELECT g, PERCENTILE_CONT(a, 0.5) OVER (PARTITION BY g) FROM t"))
    assert got["p"][1] == 2.5 and got["q"][1] == 6.0
    t = Table([("v", I)], [(None,), (None,)])
    assert rows("SELECT PERCENTILE_CONT(v, 0.5) OVER (), PERCENTILE_DISC(v, 0.5) OVER () FROM t", t) == [(None, None)] * 2


def test_percentile_numeric_results_stay_exact():
    got = run("SELECT PERCENTILE_CONT(d, 0.5) OVER () FROM t WHERE v < 3", NUMS)  # 1, 2
    assert got.rows[0] == (Decimal("1.5"),) and got.columns[0][1] == N
    # results round half away from zero at 9 digits: values 1, 2, 7 and position 0.1 * 2 = 0.2, and NUMERIC percentiles
    assert rows("SELECT PERCENTILE_CONT(d, 0.1) OVER () FROM t", NUMS)[0] == (Decimal("1.2"),)
    assert rows("SELECT PERCENTILE_CONT(d, NUMERIC '0.333333333') OVER () FROM t", NUMS)[0] == (Decimal("1.666666666"),)
    assert rows("SELECT PERCENTILE_DISC(d, NUMERIC '0.666666667') OVER () FROM t", NUMS)[0] == (Decimal("7"),)


def test_percentile_respect_nulls_nan_and_infinity():
    # f is 1.0, 2.5, NULL, 7.0: RESPECT NULLS makes the NULL the smallest value
    q = "SELECT PERCENTILE_CONT(f, {p} RESPECT NULLS) OVER (), PERCENTILE_DISC(f, {p} RESPECT NULLS) OVER () FROM t"
    assert rows(q.format(p=0), NUMS)[0] == (None, None)
    assert rows(q.format(p=0.25), NUMS)[0] == (1.0, None)  # CONT between a NULL and 1.0 is the value; DISC picks the NULL
    assert rows(q.format(p=0.5), NUMS)[0] == (1.75, 1.0)
    inf, nan = float("inf"), float("nan")
    t = Table([("f", F)], [(nan,), (-inf,), (1.0,), (3.0,), (inf,)])  # NaN sorts first
    got = rows("SELECT PERCENTILE_CONT(f, 0.125) OVER (), PERCENTILE_CONT(f, 0.25) OVER (), PERCENTILE_CONT(f, 0.3) OVER (), "
               "PERCENTILE_CONT(f, 0.5) OVER (), PERCENTILE_CONT(f, 0.9) OVER (), PERCENTILE_CONT(f, 1) OVER () FROM t", t)[0]
    assert got[0] != got[0] and got[1] == -inf and got[2] == -inf and got[3] == 1.0 and got[4] == inf and got[5] == inf
    big = sys.float_info.max
    t = Table([("f", F)], [(big,), (-big,)])
    assert rows("SELECT PERCENTILE_CONT(f, 0.5) OVER () FROM t", t)[0] == (0.0,)  # no overflow in the interpolation


def test_percentile_errors():
    with pytest.raises(EvalError):
        run("SELECT PERCENTILE_CONT(v, 1.5) OVER () FROM t", NUMS)
    with pytest.raises(EvalError):
        run("SELECT PERCENTILE_DISC(v, -0.1) OVER () FROM t", NUMS)
    with pytest.raises(EvalError):
        run("SELECT PERCENTILE_CONT(v, NULL) OVER () FROM t", NUMS)
    with pytest.raises(AnalysisError):
        run("SELECT PERCENTILE_CONT(g, 0.5) OVER () FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT PERCENTILE_CONT(v, v) OVER () FROM t", NUMS)  # the percentile must be constant


# --- named windows -------------------------------------------------------------------------------------------


def test_named_windows_and_inheritance():
    expected = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 1), (7, 2)]
    assert rows("SELECT a, ROW_NUMBER() OVER w FROM t WINDOW w AS (PARTITION BY g ORDER BY a) ORDER BY a") == expected
    assert rows("SELECT a, ROW_NUMBER() OVER (w) FROM t WINDOW w AS (PARTITION BY g ORDER BY a) ORDER BY a") == expected
    assert rows("SELECT a, ROW_NUMBER() OVER (w ORDER BY a) FROM t WINDOW w AS (PARTITION BY g) ORDER BY a") == expected
    assert rows("SELECT a, ROW_NUMBER() OVER w2 FROM t WINDOW w AS (PARTITION BY g), w2 AS (w ORDER BY a) ORDER BY a") == expected
    assert rows("SELECT a, ROW_NUMBER() OVER w3 FROM t WINDOW w AS (PARTITION BY g), w2 AS (w), w3 AS (w2 ORDER BY a) ORDER BY a") == expected
    assert rows("SELECT a, RANK() OVER W FROM t WINDOW w AS (ORDER BY a) ORDER BY a")[:2] == [(1, 1), (2, 2)]  # names are case-insensitive


def test_named_window_frames():
    got = rows("SELECT SUM(v) OVER w FROM t WINDOW w AS (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) ORDER BY a", W)
    assert [r[0] for r in got] == [1, 3, 5, 7, 9]
    got = rows("SELECT SUM(v) OVER (w ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t WINDOW w AS (ORDER BY a) ORDER BY a", W)
    assert [r[0] for r in got] == [1, 3, 5, 7, 9]


def test_named_window_errors():
    for sql in (
        "SELECT ROW_NUMBER() OVER w FROM t",
        "SELECT ROW_NUMBER() OVER (w ORDER BY a) FROM t WINDOW w AS (ORDER BY a)",
        "SELECT ROW_NUMBER() OVER (w PARTITION BY g) FROM t WINDOW w AS (ORDER BY a)",
        "SELECT SUM(a) OVER (w ROWS 1 PRECEDING) FROM t WINDOW w AS (ORDER BY a ROWS 2 PRECEDING)",
        "SELECT ROW_NUMBER() OVER w FROM t WINDOW w AS (v ORDER BY a)",
    ):
        with pytest.raises(AnalysisError):
            run(sql)
    with pytest.raises(Unsupported):  # forward reference
        run("SELECT ROW_NUMBER() OVER w1 FROM t WINDOW w1 AS (w2 ORDER BY a), w2 AS (PARTITION BY g)")


# --- context ---------------------------------------------------------------------------------------------------


def test_windows_in_qualify_order_by_and_nested_selects():
    got = rows("SELECT g, a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY a DESC) = 1 ORDER BY g")
    assert got == [("p", 4), ("q", 7)]
    got = rows("SELECT a FROM t ORDER BY ROW_NUMBER() OVER (ORDER BY -a)")
    assert got == [(7,), (5,), (4,), (3,), (2,), (1,)]
    got = rows("SELECT a, rn FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a) rn FROM t) WHERE rn <= 2 ORDER BY a")
    assert got == [(1, 1), (2, 2)]
    got = rows("SELECT a, ROW_NUMBER() OVER (ORDER BY a) + RANK() OVER (ORDER BY a DESC) FROM t ORDER BY a")
    assert got == [(1, 7), (2, 7), (3, 7), (4, 7), (5, 7), (7, 7)]


def test_two_windows_do_not_disturb_each_other():
    got = rows("SELECT a, ROW_NUMBER() OVER (ORDER BY a DESC), ROW_NUMBER() OVER (PARTITION BY g ORDER BY a) FROM t ORDER BY a")
    assert got == [(1, 6, 1), (2, 5, 2), (3, 4, 3), (4, 3, 4), (5, 2, 1), (7, 1, 2)]


def test_window_on_empty_input():
    assert rows("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t WHERE a > 100") == []


def test_unsupported_and_invalid_window_forms():
    # a constant ORDER BY key is a constant (not a column position): everything is a peer
    assert rows("SELECT RANK() OVER (ORDER BY 1) FROM t") == [(1,)] * 6
    with pytest.raises(AnalysisError):
        run("SELECT ROW_NUMBER() OVER (ORDER BY ARRAY[a]) FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT RANK() OVER (ORDER BY a) FROM t WHERE RANK() OVER (ORDER BY a) = 1")
    with pytest.raises(Unsupported):
        run("SELECT RANK(1) OVER (ORDER BY a) FROM t")
    with pytest.raises(Unsupported):
        run("SELECT RANK() IGNORE NULLS OVER (ORDER BY a) FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT ROW_NUMBER(a) OVER () FROM t")


# --- aggregates as window functions ---------------------------------------------------------------------------


def test_real_aggregates_as_window_functions():
    got = rows("SELECT a, SUM(v) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW), COUNT(*) OVER (), "
               "MAX(v) OVER (ORDER BY a), MIN(v) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING), "
               "AVG(v) OVER (ORDER BY a) FROM t ORDER BY a", W)
    assert got == [
        (1, 1, 5, 1, 2, 1.0),
        (2, 3, 5, 2, 3, 1.5),
        (3, 5, 5, 3, 4, 2.0),
        (4, 7, 5, 4, 5, 2.5),
        (5, 9, 5, 5, None, 3.0),
    ]


def test_real_aggregates_over_grouped_input():
    got = rows("SELECT g, SUM(a) s, SUM(SUM(a)) OVER (ORDER BY g) FROM t GROUP BY g ORDER BY g")
    assert got == [("p", 10, 10), ("q", 12, 22)]
    assert math.isclose(rows("SELECT COUNT(*) OVER () / 4 FROM t LIMIT 1")[0][0], 1.5)


def test_real_aggregates_distinct_array_and_string_agg():
    got = by_key(run("SELECT a, COUNT(DISTINCT x) OVER (PARTITION BY g), ARRAY_AGG(x IGNORE NULLS) OVER (ORDER BY a "
                     "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t WHERE g = 'p' OR a = 5"))
    assert got[1][1] == 3 and got[5][1] == 1  # partition p has x = 10, NULL, 30, 40; partition q has 50
    assert got[2][2] == (10,) and got[3][2] == (30,) and got[5][2] == (40, 50)
    ties = run("SELECT STRING_AGG(CAST(x AS STRING), ',') OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) "
               "FROM t WHERE g = 'p'", TIES)
    assert not ties.deterministic


def test_real_aggregate_window_rejects_unsupported_wrappers():
    with pytest.raises(Unsupported):
        run("SELECT SUM(a) FILTER (WHERE a > 1) OVER () FROM t")
    with pytest.raises(AnalysisError):
        run("SELECT SUM(SUM(a) OVER ()) OVER () FROM t")


def test_is_first_is_last_are_googlesql_only():
    sql = "SELECT a, IS_FIRST(1) OVER (PARTITION BY g ORDER BY a), IS_LAST(2) OVER (PARTITION BY g ORDER BY a) FROM t ORDER BY a"
    with pytest.raises(Unsupported):
        run(sql)
    got = rows(sql, mode="googlesql")
    assert got == [(1, True, False), (2, False, False), (3, False, True), (4, False, True), (5, True, True), (7, False, True)]
    with pytest.raises(EvalError):
        run("SELECT IS_FIRST(-1) OVER (ORDER BY a) FROM t", mode="googlesql")
    assert rows("SELECT SAFE.IS_LAST(CAST(NULL AS INT64)) OVER (ORDER BY a) FROM t WHERE a < 3", mode="googlesql") == [(None,)] * 2


def test_safe_numbering_functions():
    assert rows("SELECT SAFE.ROW_NUMBER() OVER (ORDER BY a) FROM t WHERE a < 3 ORDER BY 1") == [(1,), (2,)]
    with pytest.raises(EvalError):  # SAFE. does not catch a bad NTILE argument
        run("SELECT SAFE.NTILE(-1) OVER (ORDER BY a) FROM t")
    with pytest.raises(Unsupported):
        run("SELECT SAFE.LAG(x) OVER (ORDER BY a) FROM t")
