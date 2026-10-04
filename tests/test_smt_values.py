"""The reference arithmetic of the SMT prover's value layer: BigQuery's INT64, FLOAT64 and NUMERIC rules.

These are the rules the prover's literal folding and error exposure rest on. Rules the author could not
confirm in the GoogleSQL documentation are named in ``smt_values``'s docstring, not asserted here.
"""

from fractions import Fraction
import math

import pytest

from kumosql import smt_values as v


def test_int64_arithmetic_is_exact_and_overflow_is_an_error():
    assert v.int64_add(2**62, 2**62 - 1) == v.INT64_MAX
    with pytest.raises(v.BigQueryError):
        v.int64_add(v.INT64_MAX, 1)
    with pytest.raises(v.BigQueryError):
        v.int64_mul(2**32, 2**31)
    with pytest.raises(v.BigQueryError):
        v.int64_neg(v.INT64_MIN)
    with pytest.raises(v.BigQueryError):
        v.int64_abs(v.INT64_MIN)


def test_div_truncates_toward_zero_and_mod_takes_the_sign_of_the_dividend():
    assert v.int64_div(7, 2) == 3
    assert v.int64_div(-7, 2) == -3
    assert v.int64_div(7, -2) == -3
    assert v.int64_mod(-7, 3) == -1
    assert v.int64_mod(7, -3) == 1
    with pytest.raises(v.BigQueryError):
        v.int64_div(1, 0)
    with pytest.raises(v.BigQueryError):
        v.int64_mod(1, 0)
    with pytest.raises(v.BigQueryError):
        v.int64_div(v.INT64_MIN, -1)


def test_slash_is_float_division_and_a_zero_divisor_is_an_error():
    assert v.div(1, 2) == 0.5
    with pytest.raises(v.BigQueryError):
        v.div(1.0, 0.0)
    with pytest.raises(v.BigQueryError):
        v.div(0.0, 0.0)
    assert math.isnan(v.div(math.nan, 2.0))


def test_ieee_divide_gives_infinities_and_nan_and_safe_divide_gives_null():
    assert v.ieee_divide(1.0, 0.0) == math.inf
    assert v.ieee_divide(-1.0, 0.0) == -math.inf
    assert v.ieee_divide(1.0, -0.0) == -math.inf
    assert math.isnan(v.ieee_divide(0.0, 0.0))
    assert v.safe_divide(1.0, 0.0) is None
    assert v.safe_divide(1.0, 4.0) == 0.25


def test_float64_overflow_is_an_error_but_infinity_in_is_infinity_out():
    with pytest.raises(v.BigQueryError):
        v.float64_mul(1e200, 1e200)
    with pytest.raises(v.BigQueryError):
        v.float64_add(1.7e308, 1.7e308)
    assert v.float64_add(math.inf, 1.0) == math.inf


def test_cast_float_to_int64_rounds_half_away_from_zero():
    assert v.float64_to_int64(0.5) == 1
    assert v.float64_to_int64(-0.5) == -1
    assert v.float64_to_int64(1.5) == 2
    assert v.float64_to_int64(2.5) == 3
    assert v.float64_to_int64(-2.5) == -3
    for bad in (math.nan, math.inf, 2.0**63, -(2.0**64)):
        with pytest.raises(v.BigQueryError):
            v.float64_to_int64(bad)


def test_nan_groups_together_sorts_first_and_is_unequal_to_itself():
    assert v.float_key(math.nan) == v.float_key(float("nan"))
    assert v.float_key(-0.0) == v.float_key(0.0)
    assert not v.float_equal(math.nan, math.nan)
    assert v.float_equal(-0.0, 0.0)
    assert v.float_order_key(math.nan) < v.float_order_key(-math.inf)


def test_float_literals_are_the_nearest_double_and_integer_literals_are_exact():
    assert v.float_literal("0.1") == Fraction(0.1)
    assert v.float_literal("0.1") != Fraction(1, 10)
    assert v.float_literal("0.1") == v.float_literal("0.10000000000000001")
    assert v.integer_literal("9007199254740993") == 2**53 + 1
    assert v.float_literal("9007199254740993") == 2**53  # past 2**53 a FLOAT64 cannot hold it
    assert v.is_negative_zero_literal("0.0", True)
    assert not v.is_negative_zero_literal("0.0", False)


def test_numeric_has_nine_decimals_and_rounds_half_away_from_zero():
    assert v.numeric_from("0.0000000005") == Fraction(1, 10**9)
    assert v.numeric_from("-0.0000000005") == Fraction(-1, 10**9)
    assert v.numeric_from("0.0000000004") == 0
    assert v.numeric_from(2) == 2
    with pytest.raises(v.BigQueryError):
        v.numeric_from(10**29)
    with pytest.raises(v.BigQueryError):
        v.numeric_from(math.inf)


def test_numeric_from_float_uses_the_exact_binary_value():
    # 1.5e-9 is not exactly 1.5e-9 as a double: it lies just below the half, so it rounds down to 1e-9 here
    assert v.numeric_from(1.5e-9) == Fraction(1, 10**9)
    assert v.numeric_from("0.0000000015") == Fraction(2, 10**9)


def test_bignumeric_keeps_38_decimals():
    assert v.bignumeric_from("0.1") == Fraction(1, 10)
    assert v.bignumeric_from(Fraction(1, 3)) == Fraction(33333333333333333333333333333333333333, 10**38)


def test_folding_types_literals_the_way_bigquery_does():
    import sqlglot

    def fold(sql):
        return v.fold_literals(sqlglot.parse_one(sql, read="bigquery"))

    assert fold("1 + 2") == 3
    assert fold("1 / 2") == Fraction(1, 2)  # `/` is FLOAT64 even for integers
    assert fold("0.1 + 0.2") == Fraction(0.30000000000000004)
    assert fold("0.1 + 0.2") != fold("0.3")
    assert fold("9223372036854775807 + 1") is None  # an overflow is not folded away
    assert fold("1 / 0") is None
    assert fold("-9223372036854775808") == v.INT64_MIN
