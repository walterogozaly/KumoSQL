"""The bag-equivalence backend as a last resort in ``prover_context.prove``."""

import pytest

from kumosql import prover_context
from kumosql.prover_schema import ProverSchema
from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus
from kumosql.uexpr import hook

FACTS = ProverSchema(columns={"t": ["a", "b"]}, table_count=1, sources={"given"})
LEFT = "SELECT a FROM t WHERE a > 1 AND b = 2"
RIGHT = "SELECT a FROM t WHERE b = 2 AND a > 1"
DIFFERENT = "SELECT a FROM t WHERE a > 2 AND b = 2"


def not_proven(*_args, **_kwargs):
    return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "no proof")


def run(left=LEFT, right=RIGHT, **kwargs):
    return prover_context.prove(left, right, schema=FACTS, equivalences_enabled=False, timeout_ms=2000, **kwargs)


@pytest.fixture
def calls(monkeypatch):
    """Record every call of the hook and answer "no proof"."""

    seen = []

    def fake(left, right, **kwargs):
        seen.append((left, right, kwargs))
        return None

    monkeypatch.setattr(hook, "prove_last_resort", fake)
    return seen


def test_hook_is_not_reached_when_the_earlier_provers_prove(calls):
    assert run().proven
    assert calls == []


def test_hook_is_not_reached_on_a_refutation_or_conditional_proof(monkeypatch, calls):
    from kumosql import algebraic_equivalence

    for status in (SmtStatus.NOT_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        monkeypatch.setattr(
            algebraic_equivalence, "prove_equivalent_algebraic", lambda *a, _s=status, **k: SmtEquivalenceResult(_s, "x")
        )
        assert run().status is status
    assert calls == []


def test_hook_is_reached_once_after_the_earlier_provers_fail(monkeypatch, calls):
    from kumosql import algebraic_equivalence

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    result = run()
    assert result.status is SmtStatus.NOT_PROVEN and result.reason == "no proof"
    assert len(calls) == 1
    left, right, kwargs = calls[0]
    assert (left, right) == (LEFT, RIGHT)
    assert kwargs["schema"] == {"t": ["a", "b"]} and kwargs["timeout_ms"] == 2000 and kwargs["dialect"] == "bigquery"


def test_a_pair_only_the_backend_proves_is_proven_through_prove(monkeypatch):
    from kumosql import algebraic_equivalence

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    result = run()
    assert result.proven
    assert "bag procedure" in result.reason


def test_a_not_equivalent_pair_stays_unproven(monkeypatch):
    from kumosql import algebraic_equivalence

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    assert not run(LEFT, DIFFERENT).proven
    assert not run("SELECT a FROM t", "SELECT a FROM t WHERE a > 1").proven


def test_an_error_in_the_backend_keeps_the_earlier_result(monkeypatch):
    import kumosql.uexpr as uexpr
    from kumosql import algebraic_equivalence

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    monkeypatch.setattr(uexpr, "prove_bag_equivalent", boom)
    assert hook.prove_last_resort(LEFT, RIGHT, schema={"t": ["a", "b"]}) is None
    result = run()
    assert result.status is SmtStatus.NOT_PROVEN and result.reason == "no proof"


def test_an_error_in_the_hook_itself_keeps_the_earlier_result(monkeypatch):
    from kumosql import algebraic_equivalence

    def boom(*_a, **_k):
        raise MemoryError("boom")

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    monkeypatch.setattr(hook, "prove_last_resort", boom)
    assert run().reason == "no proof"


def test_a_timeout_or_unsupported_query_is_not_a_proof(monkeypatch):
    from kumosql import algebraic_equivalence
    from kumosql.uexpr import decide

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    assert hook.prove_last_resort("SELECT a FROM t WHERE", RIGHT, schema={"t": ["a", "b"]}) is None
    monkeypatch.setattr(decide.Prover, "bag_equal", lambda *_a, **_k: (_ for _ in ()).throw(decide.Timeout()))
    assert hook.prove_last_resort(LEFT, RIGHT, schema={"t": ["a", "b"]}) is None


def test_hook_can_be_switched_off(monkeypatch):
    from kumosql import algebraic_equivalence

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", not_proven)
    monkeypatch.setattr(hook, "ENABLED", False)
    assert not run().proven


def test_backend_time_is_bounded_by_the_prover_limit(monkeypatch):
    import kumosql.uexpr as uexpr

    seen = {}

    def fake(left, right, **kwargs):
        seen.update(kwargs)
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "x")

    monkeypatch.setattr(uexpr, "prove_bag_equivalent", fake)
    hook.prove_last_resort(LEFT, RIGHT, schema={"t": ["a", "b"]}, timeout_ms=1000)
    assert seen["timeout_ms"] == 1000
    assert seen["budget_s"] == hook.BUDGET_FACTOR * 1.0


def test_the_front_end_checks_still_apply(monkeypatch):
    import kumosql.uexpr as uexpr

    def forbidden(*_a, **_k):
        raise AssertionError("the backend must not run on a query BigQuery rejects")

    monkeypatch.setattr(uexpr, "prove_bag_equivalent", forbidden)
    left = "SELECT CAST(a AS FLOAT) FROM t"
    assert hook.prove_last_resort(left, left, schema={"t": ["a", "b"]}) is None


@pytest.mark.parametrize("target", ["UNSIGNED", "TINYINT", "SMALLINT"])
def test_a_cast_to_a_narrow_or_unsigned_integer_is_not_proven_equal_to_a_signed_one(target):
    # CAST(-2 AS UNSIGNED) is 18446744073709551614 in MySQL, so 5 % it is 5, not 1.
    left = f"SELECT 5 % CAST(-2 AS {target})"
    right = "SELECT 5 % CAST(-2 AS SIGNED)"
    assert hook.prove_last_resort(left, right, dialect="mysql", compare_names=False) is None
    assert hook.prove_last_resort(right, right, dialect="mysql", compare_names=False) is not None
