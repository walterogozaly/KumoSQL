"""Runtime errors as an outcome of the SMT prover: what a proof says about an operation that can fail.

BigQuery promises no order between WHERE and the select list, so a division or CAST is exposed to every row
of its FROM; only CASE/IF/COALESCE branches and the SAFE_ functions guard one.
"""

import pytest

pytest.importorskip("z3")

from kumosql import smt_errors
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

SCHEMA = {"t": ["x", "y", "f"]}
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64"}}
PROVEN = (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY)


def _verdict(left, right, types=TYPES):
    result = prove_equivalent_smt(left, right, schema=SCHEMA, types=types, timeout_ms=5000)
    if result.errors is not None and result.errors.verdict == smt_errors.INTRODUCES:
        assert result.status is SmtStatus.NOT_PROVEN  # the same rows, but the rewrite can fail: not an equivalence
    else:
        assert result.status in PROVEN, result.reason
    return result.errors.verdict if result.errors is not None else None


def test_queries_without_a_fallible_operation_have_no_error_exposure():
    assert _verdict("SELECT x FROM t WHERE x > 1", "SELECT x FROM t WHERE x >= 2") == smt_errors.NONE


def test_a_filter_does_not_guard_a_division():
    # the division may run on the row the filter drops, in both queries alike
    assert _verdict("SELECT x FROM t WHERE y <> 0 AND x / y > 1", "SELECT x FROM t WHERE x / y > 1 AND y <> 0") == smt_errors.SAME


def test_an_if_guard_dropped_for_a_safe_function_changes_nothing():
    assert _verdict("SELECT IF(y <> 0, x / y, NULL) FROM t", "SELECT SAFE_DIVIDE(x, y) FROM t") == smt_errors.NONE


def test_a_cast_replaced_by_safe_cast_refines_the_original():
    assert _verdict("SELECT CAST(f AS INT64) FROM t", "SELECT SAFE_CAST(f AS INT64) FROM t") == smt_errors.REFINES


def test_a_safe_cast_replaced_by_cast_introduces_an_error():
    assert _verdict("SELECT SAFE_CAST(f AS INT64) FROM t", "SELECT CAST(f AS INT64) FROM t") == smt_errors.INTRODUCES


def test_an_overflow_site_moved_out_of_its_case_guard_introduces_an_error():
    guarded = "SELECT CASE WHEN x < 9223372036854775807 THEN x + 1 END AS v FROM t WHERE x < 9223372036854775807"
    unguarded = "SELECT x + 1 AS v FROM t WHERE x < 9223372036854775807"
    assert _verdict(guarded, unguarded) == smt_errors.INTRODUCES


def test_the_same_operation_under_an_equivalent_filter_is_the_same_exposure():
    assert _verdict("SELECT x + 1 AS v FROM t WHERE x < 100", "SELECT x + 1 AS v FROM t WHERE NOT (x >= 100)") == smt_errors.SAME


def test_a_proof_with_an_error_verdict_lists_it_as_an_assumption():
    result = prove_equivalent_smt(
        "SELECT CAST(f AS INT64) FROM t", "SELECT SAFE_CAST(f AS INT64) FROM t", schema=SCHEMA, types=TYPES, timeout_ms=5000
    )
    assert any(a.startswith("the rewrite") or "runtime error" in a for a in result.assumptions)


def test_no_witness_is_claimed_when_an_uninterpreted_function_stands_in_for_the_failing_value():
    assert smt_errors.uninterpreted_free(__import__("z3").Int("a") > 0)
    z3 = __import__("z3")
    f = z3.Function("f", z3.IntSort(), z3.IntSort())
    assert not smt_errors.uninterpreted_free(f(z3.Int("a")) > 0)


def test_a_query_over_whole_numbers_drops_the_float_assumptions():
    def listed(left, right, types=TYPES):
        result = prove_equivalent_smt(left, right, schema=SCHEMA, types=types, timeout_ms=5000)
        assert result.status in PROVEN
        return " | ".join(result.assumptions)

    whole = listed("SELECT SUM(x + 1) AS s FROM t", "SELECT SUM(1 + x) AS s FROM t")
    assert "NaN" not in whole and "SUM and AVG" not in whole
    # A declared FLOAT64 column is modeled with NaN, so only the order assumption for its sum remains.
    floating = listed("SELECT SUM(f) AS s FROM t", "SELECT SUM(f) AS s FROM t WHERE TRUE")
    assert "NaN" not in floating and "SUM and AVG" in floating
    identical = listed("SELECT SUM(f) AS s FROM t", "SELECT SUM(f) AS s FROM t")  # the same plan on both sides: see test_float_sum_order.py
    assert "NaN" not in identical and "SUM and AVG" not in identical and "identical FLOAT64 SUM" in identical
    untyped = listed("SELECT SUM(x + 1) AS s FROM t", "SELECT SUM(1 + x) AS s FROM t", types=None)
    assert "NaN" in untyped and "SUM and AVG" in untyped


def test_a_number_compared_with_a_bool_is_declined_rather_than_read_as_never_equal():
    # With x declared INT64, `x = TRUE` read as always false made a LEFT JOIN equal to an INNER JOIN.
    left = "SELECT COUNT(*) AS n FROM t AS a LEFT JOIN t AS b ON b.y = a.x WHERE a.x = TRUE OR b.y = 1"
    right = "SELECT COUNT(*) AS n FROM t AS a JOIN t AS b ON b.y = a.x WHERE a.x = TRUE OR b.y = 1"
    result = prove_equivalent_smt(left, right, schema=SCHEMA, types=TYPES, timeout_ms=5000)
    assert result.status is SmtStatus.NOT_PROVEN

