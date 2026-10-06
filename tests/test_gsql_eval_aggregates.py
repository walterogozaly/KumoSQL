"""Aggregate functions of the GoogleSQL evaluator (``kumosql.gsql_eval.aggregates``).

Expected values come from BigQuery's documented semantics; cases whose BigQuery answer is not pinned down must
raise ``Unsupported``. Only operators, literals, UNNEST and CASE are used, so these tests do not depend on the
scalar function modules.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T
from kumosql.gsql_eval import values as V

N = Decimal


def table(*columns, rows):
    return Table(list(columns), list(rows))


DB = Database(
    {
        "t": table(
            ("g", T.STRING), ("a", T.INT64), ("f", T.FLOAT64), ("b", T.BOOL),
            rows=[
                ("x", 1, 1.5, True),
                ("x", 2, 2.5, False),
                ("y", 2, None, None),
                ("y", None, 4.0, True),
                ("z", None, None, None),
            ],
        ),
        "nums": table(("v", T.NUMERIC), rows=[(N("0.1"),), (N("0.2"),), (None,)]),
        "empty": table(("a", T.INT64), ("s", T.STRING), rows=[]),
    }
)


def rows(sql, db=DB):
    return evaluate(sql, db).rows


def one(sql, db=DB):
    result = evaluate(sql, db).rows
    assert len(result) == 1 and len(result[0]) == 1, result
    return result[0][0]


# --- COUNT, COUNTIF -----------------------------------------------------------------------------


def test_count_forms():
    assert rows("SELECT COUNT(*), COUNT(a), COUNT(1), COUNT(NULL), COUNT(g) FROM t") == [(5, 3, 5, 0, 5)]
    assert rows("SELECT COUNT(DISTINCT a), COUNT(DISTINCT g) FROM t") == [(2, 3)]


def test_count_over_no_rows_is_zero_and_other_aggregates_are_null():
    assert rows("SELECT COUNT(*), COUNT(a), SUM(a), AVG(a), MIN(a), MAX(a), ANY_VALUE(a), ARRAY_AGG(a), "
                "STRING_AGG(s), LOGICAL_AND(a > 1), BIT_OR(a), STDDEV(a) FROM empty") == [
        (0, 0, None, None, None, None, None, None, None, None, None, None)
    ]


def test_group_by_over_no_rows_has_no_groups():
    assert rows("SELECT a, COUNT(*) FROM empty GROUP BY a") == []


def test_count_distinct_treats_nan_as_one_value_and_zeros_as_equal():
    assert one("SELECT COUNT(DISTINCT x) FROM UNNEST([CAST('nan' AS FLOAT64), CAST('nan' AS FLOAT64), 0.0, -0.0, 1.0, NULL]) x") == 3


def test_count_argument_errors():
    with pytest.raises(AnalysisError):
        evaluate("SELECT COUNT(DISTINCT a, g) FROM t", DB)
    with pytest.raises(AnalysisError):
        evaluate("SELECT COUNT() FROM t", DB)
    with pytest.raises(AnalysisError):
        evaluate("SELECT COUNT(a, g) FROM t", DB)


def test_countif():
    assert rows("SELECT COUNTIF(a > 1), COUNTIF(b), COUNTIF(NULL) FROM t") == [(2, 2, 0)]
    assert one("SELECT COUNTIF(x > 5) FROM UNNEST([1, 2]) x") == 0
    with pytest.raises(AnalysisError):
        evaluate("SELECT COUNTIF(a) FROM t", DB)


# --- SUM and AVG -----------------------------------------------------------------------------


def test_sum_ints_and_nulls():
    assert rows("SELECT SUM(a), SUM(DISTINCT a), SUM(a * 2) FROM t") == [(5, 3, 10)]
    assert rows("SELECT g, SUM(a) FROM t GROUP BY g ORDER BY g") == [("x", 3), ("y", 2), ("z", None)]
    assert one("SELECT SUM(NULL) FROM t") is None


def test_sum_int64_overflow_is_an_error():
    with pytest.raises(EvalError):
        evaluate("SELECT SUM(x) FROM UNNEST([9223372036854775807, 1]) x")
    with pytest.raises(EvalError):
        evaluate("SELECT SUM(x) FROM UNNEST([-9223372036854775807, -2]) x")
    assert one("SELECT SUM(x) FROM UNNEST([9223372036854775807, 0, NULL]) x") == 2**63 - 1


def test_sum_whose_partial_sums_overflow_but_total_does_not_is_unsupported():
    with pytest.raises(Unsupported):
        evaluate("SELECT SUM(x) FROM UNNEST([9223372036854775807, 1, -1]) x")


def test_sum_float_is_inexact_and_follows_float_rules():
    result = evaluate("SELECT SUM(f) FROM t", DB)
    assert result.rows == [(8.0,)]
    assert result.inexact
    result = evaluate("SELECT SUM(x) FROM UNNEST([0.1, 0.2]) x")
    assert result.rows == [(0.1 + 0.2,)] and result.inexact
    assert not evaluate("SELECT SUM(x) FROM UNNEST([1.0, 2.0]) x").inexact  # whole numbers add exactly
    assert not evaluate("SELECT SUM(x) FROM UNNEST([0.1]) x").inexact
    assert math.isnan(one("SELECT SUM(x) FROM UNNEST([CAST('inf' AS FLOAT64), CAST('-inf' AS FLOAT64)]) x"))
    assert one("SELECT SUM(x) FROM UNNEST([CAST('inf' AS FLOAT64), 1.0]) x") == math.inf


def test_sum_float_overflow_to_infinity_is_unsupported():
    with pytest.raises(Unsupported):
        evaluate("SELECT SUM(x) FROM UNNEST([1.7e308, 1.7e308]) x")


def test_sum_numeric_is_exact_and_ranged():
    assert rows("SELECT SUM(v) FROM nums") == [(N("0.3"),)]
    assert evaluate("SELECT SUM(v) FROM nums", DB).columns[0][1] == T.NUMERIC
    big = Database({"b": table(("v", T.NUMERIC), rows=[(N("99999999999999999999999999999.999999999"),), (N("1"),)])})
    with pytest.raises(EvalError):
        evaluate("SELECT SUM(v) FROM b", big)
    cancel = Database({"b": table(("v", T.NUMERIC), rows=[(N("99999999999999999999999999999.999999999"),), (N("1"),), (N("-1"),)])})
    with pytest.raises(Unsupported):
        evaluate("SELECT SUM(v) FROM b", cancel)


def test_sum_bignumeric_keeps_38_digits():
    db = Database({"b": table(("v", T.BIGNUMERIC), rows=[(N("0.00000000000000000000000000000000000001"),)] * 3)})
    result = evaluate("SELECT SUM(v) FROM b", db)
    assert result.rows == [(N("0.00000000000000000000000000000000000003"),)]
    assert result.columns[0][1] == T.BIGNUMERIC


def test_sum_rejects_other_types():
    with pytest.raises(AnalysisError):
        evaluate("SELECT SUM(g) FROM t", DB)
    with pytest.raises(AnalysisError):
        evaluate("SELECT SUM(b) FROM t", DB)


def test_sum_distinct_floats_merges_nans_and_zeros():
    assert one("SELECT SUM(DISTINCT x) FROM UNNEST([1.0, 1.0, 2.0, NULL]) x") == 3.0


def test_avg_int64_is_float():
    assert rows("SELECT AVG(a), AVG(DISTINCT a) FROM t") == [(5 / 3, 1.5)]
    assert evaluate("SELECT AVG(a) FROM t", DB).columns[0][1] == T.FLOAT64
    assert one("SELECT AVG(x) FROM UNNEST([1, 2]) x") == 1.5


def test_avg_int64_with_a_huge_sum_is_unsupported():
    with pytest.raises(Unsupported):
        evaluate("SELECT AVG(x) FROM UNNEST([9223372036854775807, 9223372036854775807]) x")


def test_avg_float():
    assert one("SELECT AVG(f) FROM t") == pytest.approx(8.0 / 3)
    assert one("SELECT AVG(x) FROM UNNEST([1.0, 2.0]) x") == 1.5


def test_avg_numeric_rounds_half_away_from_zero_to_nine_digits():
    db = Database({"nums": table(("v", T.NUMERIC), rows=[(N("0.000000001"),), (N("0.000000002"),)])})
    assert evaluate("SELECT AVG(v) FROM nums", db).rows == [(N("0.000000002"),)]  # 1.5e-9 rounds away from zero
    neg = Database({"nums": table(("v", T.NUMERIC), rows=[(N("-0.000000001"),), (N("-0.000000002"),)])})
    assert evaluate("SELECT AVG(v) FROM nums", neg).rows == [(N("-0.000000002"),)]
    third = Database({"nums": table(("v", T.NUMERIC), rows=[(N("1"),), (N("1"),), (N("2"),)])})
    assert evaluate("SELECT AVG(v) FROM nums", third).rows == [(N("1.333333333"),)]
    assert evaluate("SELECT AVG(v) FROM nums", DB).columns[0][1] == T.NUMERIC
    assert evaluate("SELECT AVG(v) FROM nums", DB).rows == [(N("0.15"),)]


# --- MIN, MAX ------------------------------------------------------------------------------------


def test_min_max_basic_types():
    assert rows("SELECT MIN(a), MAX(a), MIN(g), MAX(g), MIN(b), MAX(b), MIN(f), MAX(f) FROM t") == [
        (1, 2, "x", "z", False, True, 1.5, 4.0)
    ]
    assert rows("SELECT MIN(v), MAX(v) FROM nums") == [(N("0.1"), N("0.2"))]
    assert one("SELECT MAX(x) FROM UNNEST([DATE '2020-01-01', DATE '2021-06-01', NULL]) x").isoformat() == "2021-06-01"
    assert one("SELECT MIN(x) FROM UNNEST([b'b', b'a']) x") == b"a"


def test_min_max_ignore_nulls_and_empty():
    assert one("SELECT MAX(x) FROM UNNEST([CAST(NULL AS INT64)]) x") is None
    assert one("SELECT MIN(a) FROM empty") is None


def test_min_max_float_nan_rules():
    assert math.isnan(one("SELECT MAX(x) FROM UNNEST([CAST('nan' AS FLOAT64), NULL]) x"))
    with pytest.raises(Unsupported):
        evaluate("SELECT MAX(x) FROM UNNEST([CAST('nan' AS FLOAT64), 1.0]) x")
    assert one("SELECT MAX(x) FROM UNNEST([CAST('-inf' AS FLOAT64), 1.0, CAST('inf' AS FLOAT64)]) x") == math.inf
    with pytest.raises(Unsupported):
        evaluate("SELECT MAX(x) FROM UNNEST([0.0, -0.0]) x")


def test_min_max_type_errors():
    with pytest.raises(AnalysisError):
        evaluate("SELECT MAX(x) FROM UNNEST([[1], [2]]) x")
    with pytest.raises(AnalysisError):
        evaluate("SELECT MAX(1, 2)")


def test_min_max_distinct_is_redundant():
    result = evaluate("SELECT MIN(DISTINCT x), MAX(DISTINCT x) FROM UNNEST([5, 5, NULL, 2]) x")
    assert result.rows == [(2, 5)] and result.deterministic
    with pytest.raises(AnalysisError):
        evaluate("SELECT MIN(DISTINCT x) FROM UNNEST([[1], [2]]) x")


# --- ANY_VALUE, MAX_BY, MIN_BY --------------------------------------------------------------------


def test_any_value_is_deterministic_only_when_the_value_cannot_vary():
    result = evaluate("SELECT ANY_VALUE(g) FROM t WHERE a = 1", DB)
    assert result.rows == [("x",)] and result.deterministic
    result = evaluate("SELECT ANY_VALUE(x) FROM UNNEST([5, 5, NULL, 5]) x")
    assert result.rows == [(5,)] and result.deterministic
    result = evaluate("SELECT ANY_VALUE(g) FROM t", DB)
    assert not result.deterministic
    assert result.rows[0][0] in {"x", "y", "z"}
    assert evaluate("SELECT ANY_VALUE(x) FROM UNNEST([CAST(NULL AS INT64)]) x").rows == [(None,)]


def test_any_value_distinct_keeps_unique_value_determinism():
    result = evaluate("SELECT ANY_VALUE(DISTINCT x) FROM UNNEST([5, 5, NULL, 5]) x")
    assert result.rows == [(5,)] and result.deterministic
    result = evaluate("SELECT ANY_VALUE(DISTINCT x) FROM UNNEST([5, 5, 7, NULL]) x")
    assert result.rows == [(5,)] and not result.deterministic


def test_any_value_having_max_min():
    sql = "SELECT ANY_VALUE(g HAVING MAX a), ANY_VALUE(g HAVING MIN a) FROM t WHERE a IS NOT NULL AND g = 'x'"
    result = evaluate(sql, DB)
    assert result.rows == [("x", "x")] and result.deterministic
    sql = "SELECT ANY_VALUE(r.s HAVING MAX r.k), ANY_VALUE(r.s HAVING MIN r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 3), STRUCT('r', 2)]) AS r"
    assert evaluate(sql).rows == [("q", "p")]
    ties = evaluate("SELECT ANY_VALUE(r.s HAVING MAX r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 1)]) AS r")
    assert not ties.deterministic
    same = evaluate("SELECT ANY_VALUE(r.s HAVING MAX r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('p', 1)]) AS r")
    assert same.rows == [("p",)] and same.deterministic


def test_any_value_having_ignores_rows_with_null_ordering_value():
    sql = "SELECT ANY_VALUE(r.s HAVING MAX r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', CAST(NULL AS INT64))]) AS r"
    assert evaluate(sql).rows == [("p",)]
    sql = "SELECT ANY_VALUE(r.s HAVING MAX r.k) FROM UNNEST([STRUCT('p' AS s, CAST(NULL AS INT64) AS k)]) AS r"
    assert evaluate(sql).rows == [(None,)]


def test_any_value_having_with_a_null_value_at_the_extreme_is_unsupported():
    sql = "SELECT ANY_VALUE(r.s HAVING MAX r.k) FROM UNNEST([STRUCT(CAST(NULL AS STRING) AS s, 9 AS k), STRUCT('q', 1)]) AS r"
    with pytest.raises(Unsupported):
        evaluate(sql)


def test_max_by_min_by():
    sql = "SELECT MAX_BY(r.s, r.k), MIN_BY(r.s, r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 3), STRUCT('r', 2)]) AS r"
    assert evaluate(sql).rows == [("q", "p")]


# --- ARRAY_AGG ----------------------------------------------------------------------------------


def test_array_agg_order_by_and_nulls():
    assert one("SELECT ARRAY_AGG(a ORDER BY a) FROM t") == (None, None, 1, 2, 2)
    assert one("SELECT ARRAY_AGG(a ORDER BY a DESC) FROM t") == (2, 2, 1, None, None)
    assert one("SELECT ARRAY_AGG(a ORDER BY a DESC NULLS FIRST) FROM t") == (None, None, 2, 2, 1)
    assert one("SELECT ARRAY_AGG(a IGNORE NULLS ORDER BY a) FROM t") == (1, 2, 2)
    assert one("SELECT ARRAY_AGG(a RESPECT NULLS ORDER BY a) FROM t") == (None, None, 1, 2, 2)


def test_array_agg_orders_by_another_column():
    assert one("SELECT ARRAY_AGG(g ORDER BY f DESC) FROM t WHERE f IS NOT NULL") == ("y", "x", "x")
    assert one("SELECT ARRAY_AGG(g ORDER BY a, g DESC) FROM t WHERE a IS NOT NULL") == ("x", "y", "x")


def test_array_agg_type_and_empty_group():
    result = evaluate("SELECT ARRAY_AGG(a) FROM t", DB)
    assert result.columns[0][1] == T.array(T.INT64)
    assert one("SELECT ARRAY_AGG(a) FROM empty") is None
    assert rows("SELECT g, ARRAY_AGG(a IGNORE NULLS ORDER BY a) FROM t WHERE g <> 'z' GROUP BY g ORDER BY g") == [
        ("x", (1, 2)), ("y", (2,))
    ]


def test_array_agg_distinct():
    assert one("SELECT ARRAY_AGG(DISTINCT a ORDER BY a) FROM t") == (None, 1, 2)
    assert one("SELECT ARRAY_AGG(DISTINCT a IGNORE NULLS ORDER BY a DESC) FROM t") == (2, 1)
    assert one("SELECT ARRAY_AGG(DISTINCT x ORDER BY x) FROM UNNEST([CAST('nan' AS FLOAT64), CAST('nan' AS FLOAT64), 1.0]) x")[1:] == (1.0,)
    with pytest.raises(AnalysisError):
        evaluate("SELECT ARRAY_AGG(DISTINCT a ORDER BY f) FROM t", DB)


def test_array_agg_limit():
    assert one("SELECT ARRAY_AGG(a IGNORE NULLS ORDER BY a DESC LIMIT 2) FROM t") == (2, 2)
    result = evaluate("SELECT ARRAY_AGG(g ORDER BY f LIMIT 1) FROM t WHERE f IS NOT NULL", DB)
    assert result.rows == [(("x",),)] and result.deterministic
    assert one("SELECT ARRAY_AGG(x ORDER BY x LIMIT 10) FROM UNNEST([3, 1, 2]) x") == (1, 2, 3)
    with pytest.raises(AnalysisError):
        evaluate("SELECT ARRAY_AGG(x ORDER BY x LIMIT -1) FROM UNNEST([3, 1, 2]) x")
    with pytest.raises(Unsupported):
        evaluate("SELECT ARRAY_AGG(x ORDER BY x LIMIT 0) FROM UNNEST([3, 1, 2]) x")


def test_array_agg_limit_cut_through_tied_rows_is_nondeterministic():
    sql = "SELECT ARRAY_AGG(r.s ORDER BY r.k LIMIT 1) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 1)]) AS r"
    assert not evaluate(sql).deterministic
    sql = "SELECT ARRAY_AGG(r.s ORDER BY r.k LIMIT 2) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 1)]) AS r"
    assert evaluate(sql).deterministic
    sql = "SELECT ARRAY_AGG(r.s LIMIT 1) FROM UNNEST([STRUCT('p' AS s), STRUCT('q')]) AS r"
    assert not evaluate(sql).deterministic


def test_array_agg_without_order_is_unordered():
    value = one("SELECT ARRAY_AGG(a) FROM t")
    assert isinstance(value, V.UnorderedArray) and sorted(x for x in value if x is not None) == [1, 2, 2]
    assert not isinstance(one("SELECT ARRAY_AGG(x) FROM UNNEST([7]) x"), V.UnorderedArray)
    assert V.ordered_kind(one("SELECT ARRAY_AGG(x) FROM UNNEST([7, 7]) x"))


def test_array_agg_element_access_of_unordered_result_is_nondeterministic():
    result = evaluate("SELECT ARRAY_AGG(x)[OFFSET(0)] FROM UNNEST([1, 2]) x")
    assert not result.deterministic
    assert evaluate("SELECT ARRAY_AGG(x ORDER BY x)[OFFSET(0)] FROM UNNEST([2, 1]) x").rows == [(1,)]
    assert evaluate("SELECT ARRAY_AGG(x)[OFFSET(0)] FROM UNNEST([2, 2]) x").deterministic


def test_array_agg_ties_in_the_order_make_the_array_unordered():
    value = one("SELECT ARRAY_AGG(r.s ORDER BY r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 1), STRUCT('r', 0)]) AS r")
    assert isinstance(value, V.UnorderedArray)
    value = one("SELECT ARRAY_AGG(r.s ORDER BY r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('p', 1), STRUCT('r', 0)]) AS r")
    assert value == ("r", "p", "p") and not isinstance(value, V.UnorderedArray)


def test_array_agg_of_structs_and_arrays_in_structs():
    value = one("SELECT ARRAY_AGG(STRUCT(a, g) ORDER BY a, g) FROM t WHERE a IS NOT NULL")
    assert value == ((1, "x"), (2, "x"), (2, "y"))
    with pytest.raises(AnalysisError):
        evaluate("SELECT ARRAY_AGG(x) FROM UNNEST([[1], [2]]) x")


def test_array_agg_ignore_nulls_over_only_nulls_is_unsupported():
    with pytest.raises(Unsupported):
        evaluate("SELECT ARRAY_AGG(a IGNORE NULLS) FROM t WHERE g = 'z'", DB)


def test_array_agg_order_key_must_be_orderable():
    with pytest.raises(AnalysisError):
        evaluate("SELECT ARRAY_AGG(x ORDER BY [x]) FROM UNNEST([1, 2]) x")


# --- STRING_AGG ----------------------------------------------------------------------------------


def test_string_agg_separators_and_order():
    assert one("SELECT STRING_AGG(g ORDER BY g) FROM t") == "x,x,y,y,z"
    assert one("SELECT STRING_AGG(g, '-' ORDER BY a, g) FROM t WHERE a IS NOT NULL") == "x-x-y"
    assert one("SELECT STRING_AGG(g, '' ORDER BY g DESC) FROM t") == "zyyxx"
    assert one("SELECT STRING_AGG(DISTINCT g, ';' ORDER BY g) FROM t") == "x;y;z"
    assert one("SELECT STRING_AGG(g, ', ' ORDER BY g LIMIT 2) FROM t") == "x, x"
    assert one("SELECT STRING_AGG(DISTINCT t.g, ',' ORDER BY g) FROM t") == "x,y,z"
    with pytest.raises(AnalysisError):
        evaluate("SELECT STRING_AGG(DISTINCT g, ',' ORDER BY a) FROM t", DB)


def test_string_agg_skips_nulls_and_returns_null_when_nothing_is_left():
    assert one("SELECT STRING_AGG(x, '/' ORDER BY x) FROM UNNEST(['a', NULL, 'b']) x") == "a/b"
    assert one("SELECT STRING_AGG(x) FROM UNNEST([CAST(NULL AS STRING)]) x") is None
    assert one("SELECT STRING_AGG(s) FROM empty") is None
    assert one("SELECT STRING_AGG(x, '/' ORDER BY x) FROM UNNEST(['', 'a']) x") == "/a"


def test_string_agg_without_order_is_nondeterministic_unless_values_agree():
    assert not evaluate("SELECT STRING_AGG(g) FROM t", DB).deterministic
    result = evaluate("SELECT STRING_AGG(g) FROM t WHERE g = 'x'", DB)
    assert result.rows == [("x,x",)] and result.deterministic
    result = evaluate("SELECT STRING_AGG(g) FROM t WHERE a = 1", DB)
    assert result.rows == [("x",)] and result.deterministic


def test_string_agg_bytes():
    assert one("SELECT STRING_AGG(x, b';' ORDER BY x) FROM UNNEST([b'b', b'a']) x") == b"a;b"
    assert one("SELECT STRING_AGG(x ORDER BY x) FROM UNNEST([b'b', b'a']) x") == b"a,b"
    with pytest.raises(AnalysisError):
        evaluate("SELECT STRING_AGG(x, ';') FROM UNNEST([b'b', b'a']) x")


def test_string_agg_rejects_other_types_and_null_separator():
    with pytest.raises(AnalysisError):
        evaluate("SELECT STRING_AGG(a) FROM t", DB)
    with pytest.raises(Unsupported):
        evaluate("SELECT STRING_AGG(g, NULL) FROM t", DB)


def test_string_agg_ties_make_the_result_nondeterministic():
    sql = "SELECT STRING_AGG(r.s ORDER BY r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 1)]) AS r"
    assert not evaluate(sql).deterministic
    sql = "SELECT STRING_AGG(r.s ORDER BY r.k) FROM UNNEST([STRUCT('p' AS s, 1 AS k), STRUCT('q', 2)]) AS r"
    assert evaluate(sql).deterministic


# --- ARRAY_CONCAT_AGG ---------------------------------------------------------------------------


ARRAYS = Database(
    {
        "arr": table(
            ("k", T.INT64), ("x", T.array(T.INT64)),
            rows=[(2, (1, 2)), (1, (3,)), (0, None), (5, ())],
        ),
    }
)


def test_array_concat_agg():
    assert one("SELECT ARRAY_CONCAT_AGG(x ORDER BY k) FROM arr", ARRAYS) == (3, 1, 2)
    assert one("SELECT ARRAY_CONCAT_AGG(x ORDER BY k DESC) FROM arr", ARRAYS) == (1, 2, 3)
    value = one("SELECT ARRAY_CONCAT_AGG(x) FROM arr WHERE k < 5", ARRAYS)
    assert isinstance(value, V.UnorderedArray) and sorted(value) == [1, 2, 3]
    assert one("SELECT ARRAY_CONCAT_AGG(x) FROM arr WHERE k = 2", ARRAYS) == (1, 2)
    assert one("SELECT ARRAY_CONCAT_AGG(x) FROM arr WHERE k IN (2, 5)", ARRAYS) == (1, 2)  # one non-empty array
    assert one("SELECT ARRAY_CONCAT_AGG(x) FROM arr WHERE k = 5", ARRAYS) == ()
    assert one("SELECT ARRAY_CONCAT_AGG(x) FROM arr WHERE k = 0", ARRAYS) is None
    assert evaluate("SELECT ARRAY_CONCAT_AGG(x) FROM arr", ARRAYS).columns[0][1] == T.array(T.INT64)
    with pytest.raises(AnalysisError):
        evaluate("SELECT ARRAY_CONCAT_AGG(a) FROM t", DB)
    with pytest.raises(Unsupported):
        evaluate("SELECT ARRAY_CONCAT_AGG(x LIMIT 1) FROM arr", ARRAYS)
    with pytest.raises(Unsupported):
        evaluate("SELECT ARRAY_CONCAT_AGG(DISTINCT x) FROM arr", ARRAYS)


# --- LOGICAL_*, BIT_* ----------------------------------------------------------------------------


def test_logical_and_or():
    assert rows("SELECT LOGICAL_AND(b), LOGICAL_OR(b) FROM t") == [(False, True)]
    assert rows("SELECT g, LOGICAL_AND(b), LOGICAL_OR(b) FROM t GROUP BY g ORDER BY g") == [
        ("x", False, True), ("y", True, True), ("z", None, None)
    ]
    assert one("SELECT LOGICAL_AND(x > 0) FROM UNNEST([1, NULL, 2]) x") is True
    assert one("SELECT LOGICAL_OR(NULL) FROM t") is None
    with pytest.raises(AnalysisError):
        evaluate("SELECT LOGICAL_AND(a) FROM t", DB)


def test_bit_aggregates():
    assert rows("SELECT BIT_AND(x), BIT_OR(x), BIT_XOR(x) FROM UNNEST([12, 10, 6, NULL]) x") == [(0, 14, 0)]
    assert rows("SELECT BIT_AND(x), BIT_OR(x), BIT_XOR(x) FROM UNNEST([-1, 5]) x") == [(5, -1, -6)]
    assert rows("SELECT BIT_AND(x), BIT_OR(x), BIT_XOR(x) FROM UNNEST([CAST(NULL AS INT64)]) x") == [(None, None, None)]
    with pytest.raises(AnalysisError):
        evaluate("SELECT BIT_AND(g) FROM t", DB)
    with pytest.raises(Unsupported):
        evaluate("SELECT BIT_XOR(DISTINCT a) FROM t", DB)


# --- statistics ----------------------------------------------------------------------------------


def test_variance_and_stddev():
    sql = "SELECT VAR_POP(x), VAR_SAMP(x), VARIANCE(x), STDDEV_POP(x), STDDEV_SAMP(x), STDDEV(x) FROM UNNEST([1, 2, 3, 4]) x"
    result = evaluate(sql)
    assert result.inexact
    pop, samp, var, spop, ssamp, sd = result.rows[0]
    assert pop == 1.25 and samp == pytest.approx(5 / 3) and var == samp
    assert spop == pytest.approx(math.sqrt(1.25)) and ssamp == pytest.approx(math.sqrt(5 / 3)) and sd == ssamp
    assert evaluate("SELECT VAR_POP(x), STDDEV_POP(x) FROM UNNEST([3.5, 3.5, 3.5]) x").rows == [(0.0, 0.0)]


def test_variance_edge_counts():
    assert rows("SELECT VAR_POP(x), VAR_SAMP(x) FROM UNNEST([5]) x") == [(0.0, None)]
    assert rows("SELECT VAR_POP(x), VAR_SAMP(x), STDDEV(x) FROM UNNEST([CAST(NULL AS INT64)]) x") == [(None, None, None)]
    assert rows("SELECT VAR_POP(x), VAR_SAMP(x) FROM UNNEST([1, NULL, 3]) x") == [(1.0, 2.0)]


def test_variance_of_non_finite_inputs_is_nan():
    for sql in (
        "SELECT VAR_POP(x), STDDEV_POP(x), VAR_SAMP(x), STDDEV_SAMP(x) FROM UNNEST([1.0, CAST('nan' AS FLOAT64)]) x",
        "SELECT VAR_POP(x), STDDEV_POP(x), VAR_SAMP(x), STDDEV_SAMP(x) FROM UNNEST([1.0, CAST('inf' AS FLOAT64)]) x",
    ):
        assert all(math.isnan(v) for v in evaluate(sql).rows[0])
    row = evaluate("SELECT VAR_POP(x), VAR_SAMP(x) FROM UNNEST([CAST('inf' AS FLOAT64)]) x").rows[0]
    assert math.isnan(row[0]) and row[1] is None


def test_variance_is_exact_and_overflows_only_in_the_final_conversion():
    sql = "SELECT VAR_POP(x), STDDEV_POP(x), VAR_SAMP(x), STDDEV_SAMP(x) FROM UNNEST([1.0, 2.2e304, -2.2e304]) x"
    var_pop, sd_pop, var_samp, sd_samp = evaluate(sql).rows[0]
    assert var_pop == math.inf and var_samp == math.inf  # the variance does not fit a double
    assert sd_pop == pytest.approx(2.2e304 * math.sqrt(2 / 3), rel=1e-14) and sd_samp == pytest.approx(2.2e304, rel=1e-14)
    sql = "SELECT COVAR_POP(x, x), COVAR_SAMP(x, x) FROM UNNEST([1.7e308, 8.5e307]) x"
    assert evaluate(sql).rows == [(math.inf, math.inf)]
    assert evaluate("SELECT CORR(x, -x) FROM UNNEST([1.0, 2.2e154]) x").rows == [(-1.0,)]


def test_variance_and_covariance_of_numeric():
    db = Database({"v": table(("x", T.NUMERIC), ("y", T.NUMERIC),
                              rows=[(N("1.1"), N("2.2")), (N("2.2"), N("4.4")), (N("3.3"), N("3.3")), (None, N("1"))])})
    result = evaluate("SELECT VAR_POP(x), VAR_SAMP(x), STDDEV_POP(x), COVAR_POP(x, y), CORR(x, y) FROM v", db)
    assert result.columns[0][1] == T.FLOAT64
    var_pop, var_samp, sd_pop, cov, corr = result.rows[0]
    assert var_pop == pytest.approx(0.8066666666666667, rel=1e-14) and var_samp == pytest.approx(1.21, rel=1e-14)
    assert sd_pop == pytest.approx(math.sqrt(var_pop)) and cov == pytest.approx(0.4033333333333333, rel=1e-14)
    assert 0 < corr < 1
    big = Database({"v": table(("x", T.NUMERIC), rows=[(N("1000000000000000000000000.1"),), (N("1000000000000000000000000.2"),)])})
    assert evaluate("SELECT VAR_POP(x), STDDEV_POP(x) FROM v", big).rows[0] == pytest.approx((0.0025, 0.05), rel=1e-12)


def test_variance_having_max_min_keeps_only_the_extreme_rows():
    data = "FROM UNNEST([STRUCT(1 AS x, 1 AS k), STRUCT(3, 2), STRUCT(5, 2), STRUCT(9, NULL)]) AS r"
    assert evaluate("SELECT VAR_POP(r.x HAVING MAX r.k), VAR_SAMP(r.x HAVING MAX r.k) " + data).rows == [(1.0, 2.0)]
    assert evaluate("SELECT VAR_POP(r.x HAVING MIN r.k), VAR_SAMP(r.x HAVING MIN r.k) " + data).rows == [(0.0, None)]
    assert evaluate("SELECT COVAR_POP(r.x, r.x HAVING MAX r.k), CORR(r.x, r.x HAVING MAX r.k) " + data).rows == [(1.0, 1.0)]


def test_variance_rejects_other_types():
    with pytest.raises(AnalysisError):
        evaluate("SELECT STDDEV(g) FROM t", DB)


def test_covariance_and_correlation():
    sql = ("SELECT COVAR_POP(r.x, r.y), COVAR_SAMP(r.x, r.y), CORR(r.x, r.y) "
           "FROM UNNEST([STRUCT(1 AS x, 2 AS y), STRUCT(2, 4), STRUCT(3, 6), STRUCT(4, 9)]) AS r")
    pop, samp, corr = evaluate(sql).rows[0]
    assert pop == pytest.approx(sum((x - 2.5) * (y - 5.25) for x, y in [(1, 2), (2, 4), (3, 6), (4, 9)]) / 4)
    assert samp == pytest.approx(pop * 4 / 3)
    assert 0.98 < corr < 1.0
    sql = "SELECT CORR(r.x, r.y) FROM UNNEST([STRUCT(1.0 AS x, -1.0 AS y), STRUCT(2.0, -2.0), STRUCT(3.0, -3.0)]) AS r"
    assert evaluate(sql).rows[0][0] == pytest.approx(-1.0)


def test_covariance_skips_pairs_with_a_null_and_handles_small_inputs():
    sql = "SELECT COVAR_POP(r.x, r.y), COVAR_SAMP(r.x, r.y), CORR(r.x, r.y) FROM UNNEST([STRUCT(1 AS x, CAST(NULL AS INT64) AS y), STRUCT(2, 3)]) AS r"
    assert evaluate(sql).rows == [(0.0, None, None)]
    sql = "SELECT COVAR_POP(r.x, r.y), COVAR_SAMP(r.x, r.y), CORR(r.x, r.y) FROM UNNEST([STRUCT(CAST(NULL AS INT64) AS x, 1 AS y)]) AS r"
    assert evaluate(sql).rows == [(None, None, None)]


def test_correlation_of_a_column_without_variance_is_nan():
    assert math.isnan(one("SELECT CORR(x, y) FROM UNNEST([STRUCT(1 AS x, 2 AS y), STRUCT(1, 3)]) AS r".replace("(x, y)", "(r.x, r.y)")))


# --- where aggregates appear ---------------------------------------------------------------------


def test_aggregates_in_having_order_by_and_expressions():
    assert rows("SELECT g, COUNT(*) AS c FROM t GROUP BY g HAVING SUM(a) >= 2 ORDER BY SUM(a) DESC, g") == [("x", 2), ("y", 2)]
    assert rows("SELECT g, COUNT(*) + SUM(a) FROM t GROUP BY g ORDER BY g") == [("x", 5), ("y", 4), ("z", None)]
    assert rows("SELECT CASE WHEN COUNT(a) > 2 THEN 'many' ELSE 'few' END FROM t") == [("many",)]
    assert rows("SELECT g, SUM(CASE WHEN a > 1 THEN 1 ELSE 0 END) FROM t GROUP BY g ORDER BY g") == [("x", 1), ("y", 1), ("z", 0)]


def test_aggregate_arguments_may_use_group_columns_and_outer_names():
    assert rows("SELECT g, MAX(g) FROM t GROUP BY g ORDER BY g") == [("x", "x"), ("y", "y"), ("z", "z")]
    assert rows("SELECT (SELECT SUM(x + t.a) FROM UNNEST([1, 2]) x) FROM t WHERE a = 1") == [(5,)]


def test_aggregate_of_aggregate_and_misplaced_aggregates_are_errors():
    with pytest.raises(AnalysisError):
        evaluate("SELECT SUM(COUNT(a)) FROM t", DB)
    with pytest.raises(AnalysisError):
        evaluate("SELECT g FROM t WHERE COUNT(*) > 1", DB)


def test_ungrouped_column_next_to_an_aggregate_is_an_error():
    with pytest.raises(AnalysisError):
        evaluate("SELECT g, COUNT(*) FROM t", DB)


def test_unnest_source():
    assert rows("SELECT SUM(x), COUNT(x), AVG(x), MIN(x), MAX(x) FROM UNNEST([4, 1, 3]) x") == [(8, 3, 8 / 3, 1, 4)]


# --- GROUPING ------------------------------------------------------------------------------------


def test_grouping_with_rollup():
    assert rows("SELECT g, GROUPING(g), COUNT(*) FROM t GROUP BY ROLLUP(g) ORDER BY g NULLS LAST") == [
        ("x", 0, 2), ("y", 0, 2), ("z", 0, 1), (None, 1, 5)
    ]


def test_grouping_with_cube_and_two_keys():
    sql = "SELECT g, a, GROUPING(g) AS gg, GROUPING(a) AS ga, COUNT(*) FROM t WHERE g <> 'z' GROUP BY CUBE(g, a) ORDER BY gg, ga, g, a"
    assert rows(sql) == [
        ("x", 1, 0, 0, 1), ("x", 2, 0, 0, 1), ("y", None, 0, 0, 1), ("y", 2, 0, 0, 1),
        ("x", None, 0, 1, 2), ("y", None, 0, 1, 2),
        (None, None, 1, 0, 1), (None, 1, 1, 0, 1), (None, 2, 1, 0, 2),
        (None, None, 1, 1, 4),
    ]


def test_grouping_in_having_and_distinguishes_a_null_key_from_a_rolled_up_one():
    sql = "SELECT a, COUNT(*) FROM t GROUP BY ROLLUP(a) HAVING GROUPING(a) = 1"
    assert rows(sql) == [(None, 5)]
    sql = "SELECT a, GROUPING(a) FROM t GROUP BY ROLLUP(a) ORDER BY GROUPING(a), a"
    assert rows(sql) == [(None, 0), (1, 0), (2, 0), (None, 1)]


def test_grouping_needs_a_grouped_query_and_a_grouped_argument():
    with pytest.raises(AnalysisError):
        evaluate("SELECT GROUPING(g) FROM t", DB)
    with pytest.raises(AnalysisError):
        evaluate("SELECT GROUPING(a) FROM t GROUP BY g", DB)


# --- unsupported aggregates and modifiers -------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT APPROX_COUNT_DISTINCT(a) FROM t",
        "SELECT APPROX_QUANTILES(a, 2) FROM t",
        "SELECT APPROX_TOP_COUNT(a, 1) FROM t",
        "SELECT APPROX_TOP_SUM(g, a, 1) FROM t",
        "SELECT SUM(a IGNORE NULLS) FROM t",
        "SELECT MAX(a RESPECT NULLS) FROM t",
        "SELECT ANY_VALUE(a IGNORE NULLS) FROM t",
        "SELECT STRING_AGG(g IGNORE NULLS) FROM t",
        "SELECT ARRAY_AGG(a HAVING MAX f) FROM t",
        "SELECT SUM(DISTINCT a ORDER BY a) FROM t",
        "SELECT COUNTIF(DISTINCT b) FROM t",
        "SELECT SUM(INTERVAL 1 DAY) FROM t",
    ],
)
def test_unsupported_or_rejected_forms_never_return_a_value(sql):
    with pytest.raises((Unsupported, AnalysisError)):
        evaluate(sql, DB)
