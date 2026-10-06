"""Scalar functions of the GoogleSQL evaluator: math, arrays, errors and the function registry."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
import sqlglot
from sqlglot import exp

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T
from kumosql.gsql_eval import values as V
from kumosql.gsql_eval.functions import NODE_MAP, REGISTRY

# --- the registry: every name parses to the node its mapper reads ------------------------------------------------------

# NAME -> (SQL of one call, number of argument expressions the mapper must return)
SAMPLES = {
    "ABS": ("ABS(x)", 1),
    "ACOS": ("ACOS(x)", 1),
    "ACOSH": ("ACOSH(x)", 1),
    "ARRAY_CONCAT": ("ARRAY_CONCAT(x, y, z)", 3),
    "ARRAY_FIRST": ("ARRAY_FIRST(x)", 1),
    "ARRAY_INCLUDES": ("ARRAY_INCLUDES(x, y)", 2),
    "ARRAY_INCLUDES_ALL": ("ARRAY_INCLUDES_ALL(x, y)", 2),
    "ARRAY_INCLUDES_ANY": ("ARRAY_INCLUDES_ANY(x, y)", 2),
    "ARRAY_IS_DISTINCT": ("ARRAY_IS_DISTINCT(x)", 1),
    "ARRAY_LAST": ("ARRAY_LAST(x)", 1),
    "ARRAY_LENGTH": ("ARRAY_LENGTH(x)", 1),
    "ARRAY_REVERSE": ("ARRAY_REVERSE(x)", 1),
    "ARRAY_SLICE": ("ARRAY_SLICE(x, 1, 2)", 3),
    "ARRAY_TO_STRING": ("ARRAY_TO_STRING(x, ',', 'n')", 3),
    "ASIN": ("ASIN(x)", 1),
    "ASINH": ("ASINH(x)", 1),
    "ATAN": ("ATAN(x)", 1),
    "ATAN2": ("ATAN2(x, y)", 2),
    "ATANH": ("ATANH(x)", 1),
    "BIT_COUNT": ("BIT_COUNT(x)", 1),
    "CBRT": ("CBRT(x)", 1),
    "CEIL": ("CEIL(x)", 1),
    "CEILING": ("CEILING(x)", 1),  # sqlglot reads CEILING as CEIL; the name below is what the mapper returns
    "COS": ("COS(x)", 1),
    "COSH": ("COSH(x)", 1),
    "COT": ("COT(x)", 1),
    "COTH": ("COTH(x)", 1),
    "CSC": ("CSC(x)", 1),
    "CSCH": ("CSCH(x)", 1),
    "DIV": ("DIV(x, y)", 2),
    "ERROR": ("ERROR('m')", 1),
    "EUCLIDEAN_DISTANCE": ("EUCLIDEAN_DISTANCE(x, y)", 2),
    "EXP": ("EXP(x)", 1),
    "FLOOR": ("FLOOR(x)", 1),
    "GENERATE_ARRAY": ("GENERATE_ARRAY(1, 5, 2)", 3),
    "GENERATE_DATE_ARRAY": ("GENERATE_DATE_ARRAY(x, y, INTERVAL 1 DAY)", 3),
    "GENERATE_TIMESTAMP_ARRAY": ("GENERATE_TIMESTAMP_ARRAY(x, y, INTERVAL 1 DAY)", 3),
    "GENERATE_UUID": ("GENERATE_UUID()", 0),
    "GREATEST": ("GREATEST(x, y, z)", 3),
    "IEEE_DIVIDE": ("IEEE_DIVIDE(x, y)", 2),
    "IFERROR": ("IFERROR(x, y)", 2),
    "ISERROR": ("ISERROR(x)", 1),
    "IS_INF": ("IS_INF(x)", 1),
    "IS_NAN": ("IS_NAN(x)", 1),
    "LEAST": ("LEAST(x, y, z)", 3),
    "LN": ("LN(x)", 1),
    "LOG": ("LOG(x, y)", 2),
    "MD5": ("MD5(x)", 1),
    "MOD": ("MOD(x, y)", 2),
    "NULLIFERROR": ("NULLIFERROR(x)", 1),
    "PI": ("PI()", 0),
    "POW": ("POW(x, y)", 2),
    "POWER": ("POWER(x, y)", 2),
    "RAND": ("RAND()", 0),
    "RANGE_BUCKET": ("RANGE_BUCKET(x, y)", 2),
    "ROUND": ("ROUND(x, 1, 'ROUND_HALF_EVEN')", 3),
    "SAFE_ADD": ("SAFE_ADD(x, y)", 2),
    "SAFE_DIVIDE": ("SAFE_DIVIDE(x, y)", 2),
    "SAFE_MULTIPLY": ("SAFE_MULTIPLY(x, y)", 2),
    "SAFE_NEGATE": ("SAFE_NEGATE(x)", 1),
    "SAFE_SUBTRACT": ("SAFE_SUBTRACT(x, y)", 2),
    "SEC": ("SEC(x)", 1),
    "SECH": ("SECH(x)", 1),
    "SHA1": ("SHA1(x)", 1),
    "SHA256": ("SHA256(x)", 1),
    "SHA512": ("SHA512(x)", 1),
    "SIGN": ("SIGN(x)", 1),
    "SIN": ("SIN(x)", 1),
    "SINH": ("SINH(x)", 1),
    "SQRT": ("SQRT(x)", 1),
    "TAN": ("TAN(x)", 1),
    "TANH": ("TANH(x)", 1),
    "TO_JSON_STRING": ("TO_JSON_STRING(x, TRUE)", 2),
    "TRUNC": ("TRUNC(x, 2)", 2),
}
# names sqlglot reaches through another spelling: the mapper's NAME differs from the SQL's
SPELLINGS = {"CEILING": "CEIL", "POWER": "POW"}
# registry names with no spelling of their own in BigQuery (the mapper returns them for an optimised node)
SYNTHETIC = {"<TO_HEX_MD5>": "TO_HEX(MD5(x))"}
MY_MODULES = ("fn_math", "fn_array", "fn_misc")


def _mine() -> set[str]:
    return {name for name, handler in REGISTRY.items() if handler.__module__.rsplit(".", 1)[-1] in MY_MODULES}


def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one("SELECT " + sql, read="bigquery").expressions[0]


def _mapped(node: exp.Expression) -> tuple[str, list]:
    mapper = NODE_MAP.get(type(node))
    if mapper is not None:
        return mapper(node)
    assert isinstance(node, exp.Anonymous), f"no mapper for {type(node).__name__}"
    return str(node.this).upper(), list(node.expressions)


def test_every_registered_name_has_a_sample():
    assert _mine() - set(SYNTHETIC) <= set(SAMPLES), sorted(_mine() - set(SAMPLES) - set(SYNTHETIC))


def _skip_unless_mine(name: str) -> None:
    if name in REGISTRY and name not in _mine():
        pytest.skip(f"{name} is registered by another family")


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_call_maps_to_name_and_arguments(name):
    _skip_unless_mine(name)
    sql, count = SAMPLES[name]
    node = _parse(sql)
    mapped_name, args = _mapped(node)
    assert mapped_name == SPELLINGS.get(name, name)
    assert mapped_name in REGISTRY
    assert len(args) == count
    assert all(isinstance(a, exp.Expression) for a in args)


def test_synthetic_names_map():
    for name, sql in SYNTHETIC.items():
        mapped_name, args = _mapped(_parse(sql))
        assert mapped_name == name and name in REGISTRY and len(args) == 1


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_flagged_variant_is_unsupported(name):
    """A mapper reads every argument of its node, so an argument it does not know raises Unsupported."""

    _skip_unless_mine(name)
    node = _parse(SAMPLES[name][0])
    if type(node) not in NODE_MAP:
        pytest.skip("an anonymous call carries only expressions")
    node.set("unknown_flag", exp.true())
    with pytest.raises(Unsupported):
        NODE_MAP[type(node)](node)


def test_argument_order_of_log():
    # LOG(value, base) parses as Log(this=base, expression=value); LOG10(x) as Log(this=10, expression=x)
    name, args = _mapped(_parse("LOG(x, 2)"))
    assert name == "LOG" and [a.sql() for a in args] == ["x", "2"]
    name, args = _mapped(_parse("LOG10(x)"))
    assert name == "LOG" and [a.sql() for a in args] == ["x", "10"]


def test_ignore_nulls_flag_is_refused():
    node = _parse("GREATEST(x, y)")
    node.set("ignore_nulls", True)
    with pytest.raises(Unsupported):
        NODE_MAP[exp.Greatest](node)


# --- evaluation ------------------------------------------------------------------------------------------------------


def value(expr: str, **kwargs):
    result = evaluate(f"SELECT {expr}", **kwargs)
    assert len(result.rows) == 1 and len(result.rows[0]) == 1
    return result.rows[0][0]


def err(expr: str):
    with pytest.raises(EvalError):
        value(expr)


def same(expr: str, expected):
    got = value(expr)
    assert got == expected and type(got) is type(expected), (expr, got, expected)


def test_abs_sign():
    same("ABS(-3)", 3)
    same("ABS(-2.5)", 2.5)
    same("ABS(NUMERIC '-1.5')", Decimal("1.5"))
    assert value("ABS(NULL)") is None
    err("ABS(-9223372036854775807 - 1)")
    same("SIGN(-4)", -1)
    same("SIGN(0)", 0)
    same("SIGN(2.5)", 1.0)
    assert math.isnan(value("SIGN(IEEE_DIVIDE(0, 0))"))
    same("SIGN(NUMERIC '-3')", Decimal(-1))


def test_ceil_floor_return_float64_for_integers():
    same("CEIL(1.2)", 2.0)
    same("FLOOR(-1.2)", -2.0)
    same("CEIL(5)", 5.0)
    same("FLOOR(NUMERIC '-1.5')", Decimal(-2))
    same("CEILING(NUMERIC '1.0000001')", Decimal(2))
    assert math.copysign(1, value("CEIL(-0.5)")) == -1  # -0
    assert value("FLOOR(CAST(NULL AS FLOAT64))") is None


def test_round_half_away_from_zero():
    same("ROUND(2.5)", 3.0)
    same("ROUND(-2.5)", -3.0)
    same("ROUND(0.49999999999999994)", 0.0)
    same("ROUND(5)", 5.0)
    same("ROUND(NUMERIC '2.45', 1)", Decimal("2.5"))
    same("ROUND(NUMERIC '-2.45', 1)", Decimal("-2.5"))
    same("ROUND(NUMERIC '1234.5', -2)", Decimal("1200"))
    same("ROUND(BIGNUMERIC '1321232.41325', 4)", Decimal("1321232.4133"))
    same("ROUND(NUMERIC '2.45', 5, 'ROUND_HALF_EVEN')", Decimal("2.45"))
    same("ROUND(NUMERIC '2.45', 1, 'ROUND_HALF_EVEN')", Decimal("2.4"))
    same("ROUND(NUMERIC '2.45', 1, 'ROUND_HALF_AWAY_FROM_ZERO')", Decimal("2.5"))
    assert value("ROUND(NUMERIC '2.45', 1, NULL)") is None
    assert value("ROUND(NUMERIC '2.45', NULL)") is None


def test_round_with_digits_on_floats_is_flagged_inexact():
    result = evaluate("SELECT ROUND(1.2345, 2)")
    assert result.rows == [(1.23,)] and result.inexact
    assert not evaluate("SELECT ROUND(1.5)").inexact


def test_round_modes_that_are_not_certain_are_unsupported():
    with pytest.raises(Unsupported):
        value("ROUND(2.5, 0, 'ROUND_HALF_EVEN')")
    with pytest.raises(Unsupported):
        value("ROUND(2.5, 0, 'round_half_even')")


def test_trunc():
    same("TRUNC(-1.7)", -1.0)
    same("TRUNC(NUMERIC '-1.79', 1)", Decimal("-1.7"))
    same("TRUNC(1234.5, -2)", 1200.0)
    same("TRUNC(7)", 7.0)


def test_mod_and_div():
    same("MOD(-7, 3)", -1)
    same("MOD(7, -3)", 1)
    same("MOD(NUMERIC '7.5', 2)", Decimal("1.5"))
    same("DIV(-7, 2)", -3)
    same("DIV(7, -2)", -3)
    same("DIV(NUMERIC '7.9', NUMERIC '2')", Decimal(3))
    same("-7 MOD 3" if False else "MOD(-9223372036854775807 - 1, -1)", 0)
    err("MOD(1, 0)")
    err("DIV(1, 0)")
    err("DIV(-9223372036854775807 - 1, -1)")
    assert value("MOD(NULL, 2)") is None
    with pytest.raises(AnalysisError):
        value("MOD(1.5, 2)")


def test_ieee_divide():
    same("IEEE_DIVIDE(1, 0)", math.inf)
    same("IEEE_DIVIDE(-1, 0)", -math.inf)
    same("IEEE_DIVIDE(1, -0.0)", -math.inf)
    assert math.isnan(value("IEEE_DIVIDE(0, 0)"))
    same("IEEE_DIVIDE(1e300, 1e-300)", math.inf)
    same("IEEE_DIVIDE(6, 4)", 1.5)


def test_safe_arithmetic_returns_null_on_error():
    assert value("SAFE_DIVIDE(1, 0)") is None
    assert value("SAFE_DIVIDE(1e300, 1e-300)") is None
    same("SAFE_DIVIDE(1, 2)", 0.5)
    assert value("SAFE_ADD(9223372036854775807, 1)") is None
    same("SAFE_SUBTRACT(5, 7)", -2)
    assert value("SAFE_MULTIPLY(9223372036854775807, 2)") is None
    assert value("SAFE_NEGATE(-9223372036854775807 - 1)") is None
    same("SAFE_NEGATE(5)", -5)
    assert value("SAFE_DIVIDE(NULL, 1)") is None


def test_safe_prefix_does_not_hide_errors_in_arguments():
    assert value("SAFE.LOG(-1)") is None
    assert value("SAFE.CSC(0)") is None
    err("SAFE.LOG(1 / 0)")
    err("SAFE_DIVIDE(1 / 0, 1)")
    err("SAFE.ERROR(ERROR('custom'))")
    assert value("SAFE.ERROR('message')") is None
    assert value("SAFE.NULLIFERROR(1 / 0)") is None


def test_sqrt_exp_ln_log():
    same("SQRT(16)", 4.0)
    err("SQRT(-1)")
    same("SQRT(-0.0)", -0.0)
    same("EXP(0)", 1.0)
    err("EXP(1000)")
    same("EXP(IEEE_DIVIDE(-1, 0))", 0.0)
    same("LN(1)", 0.0)
    err("LN(0)")
    err("LN(-1)")
    same("LOG(1)", 0.0)
    same("LOG(8, 2)", 3.0)
    same("LOG(IEEE_DIVIDE(1, 0), 1.1)", math.inf)
    same("LOG(IEEE_DIVIDE(1, 0), 0.1)", -math.inf)
    assert math.isnan(value("LOG(IEEE_DIVIDE(-1, 0), 1)"))
    assert math.isnan(value("LOG(1, IEEE_DIVIDE(1, 0))"))
    err("LOG(8, 1)")
    err("LOG(0, 2)")
    err("LOG(8, 0)")
    same("LOG10(1000)", 3.0)
    assert evaluate("SELECT LOG(10, 3)").inexact


def test_numeric_versions_of_transcendentals_are_unsupported():
    for call in ("SQRT(NUMERIC '4')", "EXP(NUMERIC '1')", "LN(NUMERIC '1')", "POW(NUMERIC '2', 3)", "LOG(NUMERIC '8', 2)"):
        with pytest.raises(Unsupported):
            value(call)


def test_pow():
    same("POW(2, 10)", 1024.0)
    same("POWER(2, -1)", 0.5)
    same("POW(2, 0.5) > 1.41", True)
    err("POW(-8, 1.0 / 3)")
    err("POW(0, -1)")
    err("POW(10, 400)")
    same("POW(10, -400)", 0.0)
    same("POW(IEEE_DIVIDE(1, 0), 2)", math.inf)
    same("POW(IEEE_DIVIDE(-1, 0), 3)", -math.inf)
    same("POW(0.1, IEEE_DIVIDE(-1, 0))", math.inf)
    same("POW(1.1, IEEE_DIVIDE(1, 0))", math.inf)
    same("POW(1, IEEE_DIVIDE(0, 0))", 1.0)


def test_trigonometry():
    assert math.isnan(value("TAN(IEEE_DIVIDE(1, 0))"))
    assert math.isnan(value("SIN(IEEE_DIVIDE(-1, 0))"))
    assert math.isnan(value("TAN(TAN(IEEE_DIVIDE(1, 0)))"))
    same("SIN(0)", 0.0)
    same("COS(0)", 1.0)
    same("ATAN2(0, 1)", 0.0)
    err("ACOS(2)")
    err("ASIN(-1.5)")
    err("ACOSH(0.5)")
    err("ATANH(2)")
    same("ATANH(1)", math.inf)
    err("SINH(1000)")
    same("TANH(1000)", 1.0)
    same("SEC(0)", 1.0)
    same("SECH(0)", 1.0)
    err("CSC(0)")
    err("COT(0)")
    err("CSCH(0)")
    err("COTH(0)")
    assert value("CSC(1)") == pytest.approx(1.1883951057781212, rel=1e-15)
    assert value("COT(1)") == pytest.approx(0.64209261593433065, rel=1e-15)
    assert evaluate("SELECT SIN(1)").inexact
    assert not evaluate("SELECT SIN(0)").inexact


def test_cbrt_is_correctly_rounded():
    same("CBRT(27.0)", 3.0)
    same("CBRT(-8.0)", -2.0)
    same("CBRT(0.0)", 0.0)


def test_is_inf_is_nan():
    same("IS_INF(IEEE_DIVIDE(1, 0))", True)
    same("IS_INF(1)", False)
    same("IS_NAN(IEEE_DIVIDE(0, 0))", True)
    same("IS_NAN(1.5)", False)
    assert value("IS_NAN(CAST(NULL AS FLOAT64))") is None


def test_greatest_least():
    same("GREATEST(1, 2.5, 2)", 2.5)
    same("LEAST(3, 1, 2)", 1)
    same("LEAST('b', 'a')", "a")
    assert value("GREATEST(1, NULL, 3)") is None
    assert math.isnan(value("GREATEST(1.0, IEEE_DIVIDE(0, 0), 3.0)"))
    assert math.isnan(value("LEAST(1.0, IEEE_DIVIDE(0, 0))"))
    same("GREATEST(DATE '2020-01-01', DATE '2021-01-01')", __import__("datetime").date(2021, 1, 1))
    same("LEAST(TRUE, FALSE)", False)
    with pytest.raises(AnalysisError):
        value("GREATEST([1], [2])")


def test_range_bucket():
    same("RANGE_BUCKET(20, [0, 10, 20, 30, 40])", 3)
    same("RANGE_BUCKET(-1, [0, 10])", 0)
    same("RANGE_BUCKET(99, [0, 10])", 2)
    same("RANGE_BUCKET(5, ARRAY<INT64>[])", 0)
    assert value("RANGE_BUCKET(NULL, [1])") is None
    with pytest.raises(Unsupported):
        value("RANGE_BUCKET(5, [10, 1])")


def test_bit_count_and_hashes():
    same("BIT_COUNT(7)", 3)
    same("BIT_COUNT(-1)", 64)
    same("BIT_COUNT(b'\\x01\\xff')", 9)
    assert value("MD5('')") == bytes.fromhex("d41d8cd98f00b204e9800998ecf8427e")
    assert value("SHA1(b'')") == bytes.fromhex("da39a3ee5e6b4b0d3255bfef95601890afd80709")
    assert value("SHA256('')").hex().startswith("e3b0c44298fc1c14")
    assert len(value("SHA512('a')")) == 64
    same("TO_HEX(MD5(''))", "d41d8cd98f00b204e9800998ecf8427e")
    assert value("MD5(CAST(NULL AS STRING))") is None
    with pytest.raises(AnalysisError):
        value("MD5(1)")


# --- arrays ----------------------------------------------------------------------------------------------------------


def test_array_length_reverse_first_last():
    same("ARRAY_LENGTH([1, NULL, 3])", 3)
    same("ARRAY_LENGTH(ARRAY<STRING>[])", 0)
    assert value("ARRAY_LENGTH(CAST(NULL AS ARRAY<INT64>))") is None
    same("ARRAY_REVERSE([1, 2, 3])", (3, 2, 1))
    same("ARRAY_FIRST([4, 5])", 4)
    same("ARRAY_LAST([4, 5])", 5)
    err("ARRAY_FIRST(ARRAY<INT64>[])")
    err("ARRAY_LAST(ARRAY<INT64>[])")
    assert value("SAFE.ARRAY_FIRST(ARRAY<INT64>[])") is None
    assert value("ARRAY_FIRST(CAST(NULL AS ARRAY<INT64>))") is None


def test_array_slice():
    same("ARRAY_SLICE([1, 2, 3, 4, 5], 1, 3)", (2, 3, 4))
    same("ARRAY_SLICE([1, 2, 3, 4, 5], -2, -1)", (4, 5))
    same("ARRAY_SLICE([1, 2, 3], 2, 1)", ())
    same("ARRAY_SLICE([1, 2, 3], -10, 10)", (1, 2, 3))
    same("ARRAY_SLICE([1, 2], -8869911617074265235, -4)", ())
    assert value("ARRAY_SLICE([1, 2], NULL, 1)") is None


def test_array_to_string():
    same("ARRAY_TO_STRING(['a', NULL, 'b'], ',')", "a,b")
    same("ARRAY_TO_STRING(['a', NULL, 'b'], ',', 'x')", "a,x,b")
    same("ARRAY_TO_STRING(ARRAY<STRING>[], ',')", "")
    assert value("ARRAY_TO_STRING(['a'], NULL)") is None
    same("ARRAY_TO_STRING([b'a', b'b'], b'-')", b"a-b")
    with pytest.raises(AnalysisError):
        value("ARRAY_TO_STRING([1, 2], ',')")


def test_array_concat_and_operator():
    same("ARRAY_CONCAT([1, 2], [3])", (1, 2, 3))
    same("[1] || [2] || [3, 4]", (1, 2, 3, 4))
    same("ARRAY_CONCAT([1], [2.5])", (1.0, 2.5))
    assert value("ARRAY_CONCAT([1], CAST(NULL AS ARRAY<INT64>))") is None
    assert value("[1] || NULL") is None
    same("ARRAY_CONCAT(['a', NULL], ARRAY<STRING>[])", ("a", None))
    with pytest.raises(AnalysisError):
        value("ARRAY_CONCAT(['a'], [TRUE])")
    with pytest.raises(AnalysisError):
        value("ARRAY_CONCAT(1)")
    # an untyped [] reads as ARRAY<INT64> unless the compiler marks it, so this is either right or declined, never wrong
    try:
        assert value("ARRAY_CONCAT(['a'], [])") == ("a",)
    except Unsupported:
        pass


def test_array_concat_is_unordered_if_any_input_is():
    result = evaluate("SELECT ARRAY_CONCAT([1], ARRAY(SELECT x FROM UNNEST([2, 3]) AS x))")
    assert isinstance(result.rows[0][0], V.UnorderedArray)
    assert isinstance(evaluate("SELECT ARRAY_CONCAT([1], [2])").rows[0][0], tuple)
    assert not isinstance(evaluate("SELECT ARRAY_CONCAT([1], [2])").rows[0][0], V.UnorderedArray)


def test_order_dependent_functions_flag_unordered_arrays():
    unordered = "ARRAY(SELECT x FROM UNNEST([1, 2, 3]) AS x)"
    assert not evaluate(f"SELECT ARRAY_FIRST({unordered})").deterministic
    assert not evaluate(f"SELECT ARRAY_TO_STRING(ARRAY(SELECT CAST(x AS STRING) FROM UNNEST([1, 2]) AS x), ',')").deterministic
    assert evaluate(f"SELECT ARRAY_LENGTH({unordered})").deterministic
    assert evaluate("SELECT ARRAY_FIRST(ARRAY(SELECT x FROM UNNEST([1, 2, 3]) AS x ORDER BY x))").deterministic
    assert isinstance(evaluate(f"SELECT ARRAY_REVERSE({unordered})").rows[0][0], V.UnorderedArray)
    assert isinstance(evaluate(f"SELECT ARRAY_SLICE({unordered}, 0, 1)").rows[0][0], V.UnorderedArray)


def test_array_is_distinct():
    same("ARRAY_IS_DISTINCT([1, 2, 3])", True)
    same("ARRAY_IS_DISTINCT([1, 2, 1])", False)
    same("ARRAY_IS_DISTINCT([NULL])", True)
    same("ARRAY_IS_DISTINCT([NULL, NULL])", False)
    same("ARRAY_IS_DISTINCT([1.0, 1.0])", False)
    same("ARRAY_IS_DISTINCT([STRUCT(1), STRUCT(1)])", False)
    assert value("ARRAY_IS_DISTINCT(NULL)") is None


def test_array_includes():
    same("ARRAY_INCLUDES([1, 2, 3], 2)", True)
    same("ARRAY_INCLUDES([1, 2, 3], 4)", False)
    same("ARRAY_INCLUDES([NULL], 1)", False)
    assert value("ARRAY_INCLUDES([1, 2, 3], NULL)") is None
    assert value("ARRAY_INCLUDES(CAST(NULL AS ARRAY<INT64>), 1)") is None
    same("ARRAY_INCLUDES([1, 2, 3], e -> e > 2)", True)
    same("ARRAY_INCLUDES([1, 2, 3], e -> e > 3)", False)
    same("ARRAY_INCLUDES([NULL, 1], e -> e IS NULL)", True)
    same("ARRAY_INCLUDES([NULL], e -> e > 0)", False)


def test_array_includes_with_an_untyped_null_array():
    assert value("ARRAY_INCLUDES(NULL, 'a')") is None
    assert value("ARRAY_INCLUDES_ANY(NULL, ['a'])") is None
    assert value("ARRAY_INCLUDES_ALL(['a'], NULL)") is None


def test_array_includes_any_all():
    same("ARRAY_INCLUDES_ANY([1, 2, 3], [0, 3])", True)
    same("ARRAY_INCLUDES_ANY([1, 2, 3], [4])", False)
    same("ARRAY_INCLUDES_ANY([NULL, 1], [NULL])", False)
    same("ARRAY_INCLUDES_ALL([1, 2, 2, 3], [2, 3])", True)
    same("ARRAY_INCLUDES_ALL([1, 2, 3], [2, 6])", False)
    same("ARRAY_INCLUDES_ALL([NULL, 1], [NULL, 1])", False)
    assert value("ARRAY_INCLUDES_ALL(NULL, [1])") is None
    assert value("ARRAY_INCLUDES_ANY([1], NULL)") is None


def test_lambda_sees_the_row():
    table = Table([("n", T.INT64)], [(1,), (2,), (3,)])
    result = evaluate("SELECT n, ARRAY_INCLUDES([1, 2, 3], e -> e > n) AS big FROM t ORDER BY n", Database({"t": table}))
    assert result.rows == [(1, True), (2, True), (3, False)]


def test_lambda_is_unsupported_for_other_functions():
    with pytest.raises(Unsupported):
        value("ARRAY_LENGTH(e -> e)")


def test_generate_array():
    same("GENERATE_ARRAY(1, 5)", (1, 2, 3, 4, 5))
    same("GENERATE_ARRAY(1, 10, 3)", (1, 4, 7, 10))
    same("GENERATE_ARRAY(5, 1, -2)", (5, 3, 1))
    same("GENERATE_ARRAY(3, 1)", ())
    same("GENERATE_ARRAY(1.0, 3.0)", (1.0, 2.0, 3.0))
    same("GENERATE_ARRAY(NUMERIC '0.5', NUMERIC '2', NUMERIC '0.5')", (Decimal("0.5"), Decimal("1.0"), Decimal("1.5"), Decimal("2.0")))
    assert value("GENERATE_ARRAY(1, NULL)") is None
    err("GENERATE_ARRAY(1, 5, 0)")
    with pytest.raises(Unsupported):
        value("GENERATE_ARRAY(0, 1, 0.1)")


def test_generate_date_and_timestamp_array():
    import datetime

    d = datetime.date
    same("GENERATE_DATE_ARRAY('2016-09-28', '2016-09-30')", (d(2016, 9, 28), d(2016, 9, 29), d(2016, 9, 30)))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2016-03-01', INTERVAL 1 MONTH)", (d(2016, 1, 1), d(2016, 2, 1), d(2016, 3, 1)))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2016-01-20', INTERVAL 1 WEEK)", (d(2016, 1, 1), d(2016, 1, 8), d(2016, 1, 15)))
    same("GENERATE_DATE_ARRAY('2016-01-05', '2016-01-01')", ())
    assert value("GENERATE_DATE_ARRAY(NULL, DATE '2016-01-01')") is None
    err("GENERATE_DATE_ARRAY('2016-01-01', '2017-01-01', INTERVAL 0 DAY)")
    with pytest.raises(AnalysisError):
        value("GENERATE_DATE_ARRAY('2016-01-01', '2017-01-01', INTERVAL 1 HOUR)")
    same("ARRAY_LENGTH(GENERATE_TIMESTAMP_ARRAY('2016-01-01', '2016-01-03', INTERVAL 1 DAY))", 3)
    same("ARRAY_LENGTH(GENERATE_TIMESTAMP_ARRAY('2016-01-01 00:00:00', '2016-01-01 02:00:00', INTERVAL 30 MINUTE))", 5)
    err("GENERATE_TIMESTAMP_ARRAY('2016-01-01', '2017-01-01', INTERVAL 0 DAY)")
    with pytest.raises(AnalysisError):
        value("GENERATE_TIMESTAMP_ARRAY('2016-01-01', '2017-01-01', INTERVAL 1 WEEK)")


def test_euclidean_distance():
    same("EUCLIDEAN_DISTANCE(ARRAY<FLOAT64>[0, 0], ARRAY<FLOAT64>[3, 4])", 5.0)
    same("EUCLIDEAN_DISTANCE(ARRAY[('a', 3.0)], ARRAY[('b', 4.0)])", 5.0)
    same("EUCLIDEAN_DISTANCE(ARRAY[(1, 3.0), (2, 1.0)], ARRAY[(2, 1.0), (1, 3.0)])", 0.0)
    assert value("EUCLIDEAN_DISTANCE(ARRAY<FLOAT64>[1], CAST(NULL AS ARRAY<FLOAT64>))") is None
    assert evaluate("SELECT EUCLIDEAN_DISTANCE(ARRAY<FLOAT64>[0], ARRAY<FLOAT64>[1])").inexact
    with pytest.raises(Unsupported):
        value("EUCLIDEAN_DISTANCE(ARRAY<FLOAT64>[1, 2], ARRAY<FLOAT64>[1])")
    with pytest.raises(Unsupported):
        value("EUCLIDEAN_DISTANCE([1, 2], [1, 2])")


# --- errors ----------------------------------------------------------------------------------------------------------


def test_error_is_raised_only_when_evaluated():
    err("ERROR('boom')")
    same("IF(FALSE, ERROR('boom'), 'fine')", "fine")
    err("IF(TRUE, ERROR('boom'), 'fine')")
    err("CAST(ERROR('boom') AS BYTES)")
    assert evaluate("SELECT ERROR('boom') FROM UNNEST(ARRAY<INT64>[])").rows == []
    same("COALESCE('a', ERROR('boom'))", "a")


def test_error_message_is_kept():
    with pytest.raises(EvalError, match="boom"):
        value("ERROR('boom')")


def test_nulliferror_iferror_iserror():
    assert value("NULLIFERROR(1 / 0)") is None
    same("NULLIFERROR(7)", 7)
    same("IFERROR(1 / 0, 5)", 5.0)
    same("IFERROR(3, 1 / 0)", 3.0)
    same("ISERROR(1 / 0)", True)
    same("ISERROR(1)", False)
    assert value("NULLIFERROR(NULLIFERROR(CAST('inner' AS DATE)))") is None
    err("IFERROR(1 / 0, 1 / 0)")


def test_nondeterministic_functions_are_marked():
    result = evaluate("SELECT GENERATE_UUID() AS u, RAND() AS r")
    assert not result.deterministic
    uuid_text, rand_value = result.rows[0]
    assert len(uuid_text) == 36 and uuid_text == uuid_text.lower()
    assert 0.0 <= rand_value < 1.0
    assert evaluate("SELECT 1").deterministic


def test_safe_convert_bytes_to_string_replaces_invalid_utf8():
    same("SAFE_CONVERT_BYTES_TO_STRING(b'\\xe2\\x28\\xa1')", "�(�")
    same("SAFE_CONVERT_BYTES_TO_STRING(b'abc')", "abc")


def test_to_json_string():
    same("TO_JSON_STRING(STRUCT(1 AS a, 'x\"y' AS b, [TRUE, NULL] AS c))", '{"a":1,"b":"x\\"y","c":[true,null]}')
    same("TO_JSON_STRING(NULL)", "null")
    same("TO_JSON_STRING('a\\nb')", '"a\\nb"')
    with pytest.raises(Unsupported):
        value("TO_JSON_STRING(1.5)")
    with pytest.raises(Unsupported):
        value("TO_JSON_STRING(STRUCT(1 AS a), TRUE)")


def test_unimplemented_functions_are_unsupported_not_guessed():
    for call in ("FARM_FINGERPRINT('a')", "TYPEOF(1)", "NOT_A_FUNCTION(1)"):
        with pytest.raises(Unsupported):
            value(call)
