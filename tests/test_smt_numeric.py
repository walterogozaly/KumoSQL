"""Scaled NUMERIC and BIGNUMERIC arithmetic in the SMT prover (``kumosql.smt_numeric``)."""

from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import random

import pytest

z3 = pytest.importorskip("z3")

from kumosql import smt_numeric, smt_values as S  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["x", "y", "f", "n", "m", "c"]}
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "n": "NUMERIC", "m": "NUMERIC", "c": "BOOL"}}


def _evaluate(expression) -> Fraction:
    value = z3.simplify(expression)
    return Fraction(value.numerator_as_long(), value.denominator_as_long())


def _prove(left: str, right: str, timeout_ms: int = 8000):
    return prove_equivalent_smt(left, right, schema=SCHEMA, types=TYPES, timeout_ms=timeout_ms)


def _cases(rng, count, places):
    for _ in range(count):
        digits = rng.choice([1, 2, 5, 9, 12])
        yield Fraction(rng.randint(-10**digits, 10**digits), 2 * 10 ** rng.randint(0, places + 3))


# --- the encoding against the reference arithmetic ---------------------------------------------


def test_round_half_away_is_numeric_from_and_bignumeric_from():
    rng = random.Random(484)
    for value in _cases(rng, 400, 9):
        assert _evaluate(smt_numeric.round_half_away(z3.RealVal(f"{value.numerator}/{value.denominator}"), 9)) == S.numeric_from(value)
    for value in _cases(rng, 200, 38):
        assert _evaluate(smt_numeric.round_half_away(z3.RealVal(f"{value.numerator}/{value.denominator}"), 38)) == S.bignumeric_from(value)


@pytest.mark.parametrize("places", [-3, -1, 0, 1, 2, 5, 9, 12])
def test_round_half_away_rounds_ties_away_from_zero_at_any_place(places):
    rng = random.Random(places)
    for value in _cases(rng, 150, 4):
        expected = Decimal(value.numerator) / Decimal(value.denominator)
        expected = expected.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        got = _evaluate(smt_numeric.round_half_away(z3.RealVal(f"{value.numerator}/{value.denominator}"), places))
        assert got == Fraction(expected), (value, places)


def test_the_ties_that_decide_the_direction():
    half = Fraction(1, 2 * 10**9)
    assert _evaluate(smt_numeric.round_half_away(z3.RealVal(f"{half.numerator}/{half.denominator}"), 9)) == Fraction(1, 10**9)
    assert _evaluate(smt_numeric.round_half_away(z3.RealVal(f"{-half.numerator}/{half.denominator}"), 9)) == Fraction(-1, 10**9)


def test_the_range_is_29_integer_digits_for_numeric():
    limit = Fraction(10**29)
    one = Fraction(1, 10**9)

    def over(cls, value):
        return z3.is_true(z3.simplify(smt_numeric.out_of_range(cls, z3.RealVal(f"{value.numerator}/{value.denominator}"))))

    assert not over(S.NUMERIC, limit - one) and not over(S.NUMERIC, -(limit - one))
    assert over(S.NUMERIC, limit) and over(S.NUMERIC, -limit)
    edge = Fraction(S.BIGNUMERIC_LIMIT, 10**38)
    assert over(S.BIGNUMERIC, edge) and over(S.BIGNUMERIC, -edge - 1) and not over(S.BIGNUMERIC, edge - 1)


def test_only_plain_decimal_text_is_a_compiled_literal():
    assert smt_numeric.literal_decimal("1.50", S.NUMERIC) == Fraction(3, 2)
    assert smt_numeric.literal_decimal("-0.0000000005", S.NUMERIC) == Fraction(-1, 10**9)
    assert smt_numeric.literal_decimal("0.0000000004", S.NUMERIC) == 0
    for text in ("1e3", ".5", "5.", " 1", "1_0", "NaN", "Infinity", "", "100000000000000000000000000000", "1" * 100):
        assert smt_numeric.literal_decimal(text, S.NUMERIC) is None, text


# --- what the prover now decides ----------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT n FROM t WHERE n = NUMERIC '1.50'", "SELECT n FROM t WHERE n = NUMERIC '1.5'"),
        ("SELECT n FROM t WHERE n = NUMERIC '0.0000000005'", "SELECT n FROM t WHERE n = NUMERIC '0.000000001'"),
        ("SELECT n FROM t WHERE n = CAST('2.5' AS NUMERIC)", "SELECT n FROM t WHERE n = NUMERIC '2.50'"),
        ("SELECT n * m AS v FROM t", "SELECT m * n AS v FROM t"),
        ("SELECT n * 2 AS v FROM t", "SELECT n + n AS v FROM t"),
        ("SELECT n * x AS v FROM t", "SELECT x * n AS v FROM t"),
        ("SELECT ROUND(n * m, 9) AS v FROM t", "SELECT n * m AS v FROM t"),
        ("SELECT n * m + n * m AS v FROM t", "SELECT 2 * (n * m) AS v FROM t"),
        ("SELECT n / m AS v FROM t", "SELECT ROUND(n / m, 9) AS v FROM t"),
        ("SELECT ROUND(n, 2) AS v FROM t", "SELECT ROUND(ROUND(n, 2), 2) AS v FROM t"),
        ("SELECT ROUND(n, -1) AS v FROM t", "SELECT ROUND(ROUND(n, -1), -1) AS v FROM t"),
        ("SELECT ROUND(n, 0) AS v FROM t", "SELECT ROUND(n) AS v FROM t"),
        ("SELECT CAST(n AS NUMERIC) AS v FROM t", "SELECT n AS v FROM t"),
        ("SELECT CAST(CAST(n AS BIGNUMERIC) AS NUMERIC) AS v FROM t", "SELECT n AS v FROM t"),
        ("SELECT CAST(x AS NUMERIC) AS v FROM t", "SELECT CAST(x AS BIGNUMERIC) AS v FROM t WHERE TRUE"),
    ],
)
def test_pairs_that_hold_by_the_scaled_rules_are_proved(left, right):
    result = _prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def _dec(value) -> Fraction:
    return Fraction(repr(value))


def _round(value: Fraction, places: int) -> Fraction:
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return Fraction(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


REFUTED = [
    # (left, right, how each side evaluates on the counterexample's n and m by the reference arithmetic)
    (
        "SELECT ROUND(n, 1) AS v FROM t",
        "SELECT ROUND(ROUND(n, 2), 1) AS v FROM t",
        lambda n, m: (_round(n, 1), _round(_round(n, 2), 1)),
    ),
    (
        "SELECT n * (m + m) AS v FROM t",
        "SELECT n * m + n * m AS v FROM t",
        lambda n, m: (S.numeric_from(n * (m + m)), S.numeric_from(n * m) + S.numeric_from(n * m)),
    ),
    (
        "SELECT n / 3 * 3 AS v FROM t",
        "SELECT n AS v FROM t",
        lambda n, m: (S.numeric_from(S.numeric_from(n / 3) * 3), n),
    ),
    (
        "SELECT CAST(n * m AS BIGNUMERIC) AS v FROM t",
        "SELECT CAST(n AS BIGNUMERIC) * CAST(m AS BIGNUMERIC) AS v FROM t",
        lambda n, m: (S.bignumeric_from(S.numeric_from(n * m)), S.bignumeric_from(n * m)),
    ),
    (
        "SELECT n FROM t WHERE n = NUMERIC '0.0000000004'",
        "SELECT n FROM t WHERE n = NUMERIC '0.000000001'",
        None,
    ),
]


@pytest.mark.parametrize("left,right,reference", REFUTED, ids=range(len(REFUTED)))
def test_a_refutation_is_a_database_the_reference_arithmetic_agrees_with(left, right, reference):
    result = _prove(left, right)
    assert result.status is SmtStatus.NOT_EQUIVALENT, result.reason
    row = result.counterexample.tables["t"][0]
    if reference is not None:
        n, m = _dec(row.get("n", 0)), _dec(row.get("m", 0))
        mine, other = reference(n, m)
        assert mine != other, (row, mine, other)


def test_a_refutation_row_is_a_legal_numeric_value():
    result = _prove("SELECT n * (m + m) AS v FROM t", "SELECT n * m + n * m AS v FROM t")
    for row in result.counterexample.tables["t"]:
        for name in ("n", "m"):
            if name in row:
                assert S.numeric_from(_dec(row[name])) == _dec(row[name])


# --- where the scale is not known the answer is unknown, never a refutation -----------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT ROUND(SUM(n), 9) AS v FROM t", "SELECT SUM(n) AS v FROM t"),
        ("SELECT ROUND(AVG(n), 9) AS v FROM t", "SELECT AVG(n) AS v FROM t"),
        ("SELECT ROUND(CASE WHEN c THEN n END, 9) AS v FROM t", "SELECT CASE WHEN c THEN n END AS v FROM t"),
        ("SELECT CAST(f AS NUMERIC) AS v FROM t", "SELECT CAST(ROUND(f, 9) AS NUMERIC) AS v FROM t"),
        ("SELECT ROUND(n * 1.5, 2) AS v FROM t", "SELECT n * 1.5 AS v FROM t"),
        ("SELECT ROUND(x, 2) AS v FROM t", "SELECT x AS v FROM t"),
        ("SELECT x / y AS v FROM t", "SELECT ROUND(x / y, 9) AS v FROM t"),
        ("SELECT CAST(n AS NUMERIC(10, 2)) AS v FROM t", "SELECT n AS v FROM t"),
        ("SELECT ROUND(n, 2, 'ROUND_HALF_EVEN') AS v FROM t", "SELECT ROUND(n, 2) AS v FROM t"),
    ],
)
def test_an_unbounded_scale_is_never_decided_wrongly(left, right):
    result = _prove(left, right, timeout_ms=3000)
    assert result.status is not SmtStatus.NOT_EQUIVALENT, result.counterexample
    assert result.status is SmtStatus.NOT_PROVEN


# --- the overflow of a NUMERIC result is an error site -----------------------------------------------------


def test_a_dropped_guard_on_a_numeric_product_introduces_an_overflow():
    guard = "n < 10000000000000 AND n > -10000000000000"
    left = f"SELECT IF({guard}, n * 1000000000000000, NULL) AS v FROM t WHERE {guard}"
    right = f"SELECT n * 1000000000000000 AS v FROM t WHERE {guard}"
    result = _prove(left, right)
    assert result.errors.verdict == "introduces"
    assert result.status is SmtStatus.NOT_PROVEN
    assert any("NUMERIC overflow" in site for site in result.errors.sites)


def test_a_guard_added_to_a_numeric_product_refines():
    guard = "n < 10000000000000 AND n > -10000000000000"
    result = _prove(
        f"SELECT n * 1000000000000000 AS v FROM t WHERE {guard}",
        f"SELECT IF({guard}, n * 1000000000000000, NULL) AS v FROM t WHERE {guard}",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert result.errors.verdict == "refines"


def test_a_sum_that_stays_in_range_is_not_an_error_site():
    result = _prove("SELECT n + 1 AS v FROM t WHERE n < 100 AND n > -100", "SELECT 1 + n AS v FROM t WHERE n < 100 AND n > -100")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert result.errors.verdict == "same"


def test_a_cast_of_a_big_bignumeric_to_numeric_is_an_overflow_site():
    guard = "n < 10000000000000 AND n > -10000000000000"
    left = f"SELECT IF({guard}, CAST(CAST(n AS BIGNUMERIC) * 1000000000000000 AS NUMERIC), NULL) AS v FROM t WHERE {guard}"
    right = f"SELECT CAST(CAST(n AS BIGNUMERIC) * 1000000000000000 AS NUMERIC) AS v FROM t WHERE {guard}"
    result = _prove(left, right)
    assert result.errors.verdict == "introduces"
    assert any("NUMERIC overflow" in site for site in result.errors.sites)
