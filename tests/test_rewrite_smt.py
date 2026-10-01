from kumosql import VerificationStatus, verify_rewrite

import pytest

from kumosql import prover_context, smt_equivalence

_SMT = pytest.importorskip("z3")


@pytest.fixture(autouse=True)
def predicate_only_solver(monkeypatch):
    """These tests describe the original policy (SMT for predicate-only changes); the solver has its own tests."""

    monkeypatch.setattr(prover_context, "settings", lambda: {"enabled": False, "timeout_ms": 5000})


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


def test_solver_proves_a_change_beyond_predicates(monkeypatch):
    monkeypatch.setattr(prover_context, "settings", lambda: {"enabled": True, "timeout_ms": 5000})
    before = "SELECT d.name FROM dept d WHERE d.deptno IN (SELECT e.deptno FROM emp e WHERE e.sal > 1)"
    after = "SELECT d.name FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.sal > 1 AND e.deptno = d.deptno)"
    result = verify_rewrite(before, after)

    assert result.status is VerificationStatus.PROVEN
    (check,) = _smt_check(result)
    assert check.outcome == "passed" and "solver" in check.detail


def test_solver_uses_declared_keys(monkeypatch):
    from kumosql.prover_schema import from_bigquery

    monkeypatch.setattr(prover_context, "settings", lambda: {"enabled": True, "timeout_ms": 5000})
    schema = from_bigquery(
        [("p", "d", "users", {"schema": [{"name": "id", "mode": "REQUIRED"}, {"name": "n", "mode": "NULLABLE"}],
                              "constraints": {"primaryKey": {"columns": ["id"]}}})]
    )
    monkeypatch.setattr(prover_context, "current_schema", lambda: schema)
    self_join = "SELECT a.n FROM `p.d.users` a JOIN `p.d.users` b ON a.id = b.id"
    plain = "SELECT n FROM `p.d.users`"
    result = verify_rewrite(self_join, plain)

    assert result.status is VerificationStatus.PROVEN
    assumptions = dict(_smt_check(result)[0].evidence)["assumptions"]
    assert any("declared keys" in a for a in assumptions)
    # Without the declared key the same rewrite is not provable.
    monkeypatch.setattr(prover_context, "current_schema", lambda: from_bigquery([]))
    assert verify_rewrite(self_join, plain).status is VerificationStatus.UNPROVEN
