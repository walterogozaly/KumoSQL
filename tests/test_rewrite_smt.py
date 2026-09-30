from kumosql import VerificationStatus, verify_rewrite

import pytest

from kumosql import smt_equivalence

_SMT = pytest.importorskip("z3")


def _smt_check(result):
    return [check for check in result.checks if check.kind == "smt_proof"]


def test_redundant_conjunct_is_proven_by_smt():
    result = verify_rewrite(
        "SELECT a FROM t WHERE a > 1 AND a > 0", "SELECT a FROM t WHERE a > 1"
    )

    assert result.status is VerificationStatus.PROVEN
    (check,) = _smt_check(result)
    assert check.outcome == "passed"
    assert dict(check.evidence)["assumptions"]


def test_weakened_predicate_stays_unproven_with_reason():
    result = verify_rewrite("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a > 0")

    assert result.status is VerificationStatus.UNPROVEN
    assert any("SMT found a counterexample" in detail for detail in result.details)
    assert [check.outcome for check in _smt_check(result)] == ["refuted"]


def test_unsupported_construct_is_unproven_with_reason():
    result = verify_rewrite(
        "SELECT a FROM t LEFT JOIN u ON t.a = u.a AND t.a > 1 AND t.a > 0",
        "SELECT a FROM t LEFT JOIN u ON t.a = u.a AND t.a > 1",
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert any("unsupported:" in detail for detail in result.details)


def test_non_predicate_change_does_not_use_smt():
    result = verify_rewrite("SELECT a FROM t WHERE a > 1", "SELECT a FROM t2 WHERE a > 1")

    assert result.status is VerificationStatus.UNPROVEN
    assert _smt_check(result) == []


def test_order_by_change_is_not_sent_to_smt():
    result = verify_rewrite(
        "SELECT a FROM t WHERE a > 1 AND a > 0 ORDER BY a",
        "SELECT a FROM t WHERE a > 1 ORDER BY a DESC",
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert _smt_check(result) == []


def test_missing_z3_degrades_to_unproven(monkeypatch):
    monkeypatch.setattr(smt_equivalence, "z3", None)
    result = verify_rewrite(
        "SELECT a FROM t WHERE a > 1 AND a > 0", "SELECT a FROM t WHERE a > 1"
    )

    assert result.status is VerificationStatus.UNPROVEN
    assert any("z3-solver is not installed" in detail for detail in result.details)


def test_smt_applies_inside_create_and_sqlx():
    sql = verify_rewrite(
        "CREATE TABLE x AS SELECT a FROM t WHERE a > 1 OR a > 1",
        "CREATE TABLE x AS SELECT a FROM t WHERE a > 1",
    )
    sqlx = verify_rewrite(
        "config { type: 'view' }\nSELECT a FROM ${ref('t')} WHERE a > 1 AND a > 0",
        "config { type: 'view' }\nSELECT a FROM ${ref('t')} WHERE a > 1",
    )

    assert sql.status is VerificationStatus.PROVEN
    assert sqlx.status is VerificationStatus.PROVEN
