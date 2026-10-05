"""``model_reuse._prove``: a candidate that fails under declared keys is retried with only NOT NULL declared.

Declared keys and foreign keys let the prover drop joins and DISTINCTs. On some pairs it drops them from one
side only and then cannot line the two sides up, although they line up without those rewrites. A proof that
assumes fewer constraints holds on every database the declared ones allow, so the retry is sound; it is made
only for the prover's shape-mismatch reasons, never for a difference it found or a timeout.
"""

import pytest

pytest.importorskip("z3")

from kumosql import algebraic_equivalence, model_reuse  # noqa: E402
from kumosql.model_reuse import _SHAPE_MISMATCH, _prove, rewrite_over_model  # noqa: E402
from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus, TableConstraints  # noqa: E402

SCHEMA = {"emps": ["empid", "deptno", "name"], "depts": ["deptno", "name"]}
NOT_NULL = {"emps": frozenset({"empid", "deptno"}), "depts": frozenset({"deptno"})}
KEYED = {
    "emps": TableConstraints(not_null=NOT_NULL["emps"], foreign_keys=((("deptno",), "depts", ("deptno",)),)),
    "depts": TableConstraints(not_null=NOT_NULL["depts"], keys=(("deptno",),)),
}
UNKEYED = {t: TableConstraints(not_null=nn) for t, nn in NOT_NULL.items()}


class FakeProver:
    """Stands in for the algebraic prover: answers by the constraints it is given and records them."""

    def __init__(self, keyed, plain):
        self.answers = {True: keyed, False: plain}
        self.calls = []

    def __call__(self, left, right, **kwargs):
        constraints = kwargs["constraints"]
        keyed = any(c.keys or c.foreign_keys for c in constraints.values())
        self.calls.append(constraints)
        return self.answers[keyed]


def answer(status, reason=""):
    return SmtEquivalenceResult(status, reason)


JOINED = "SELECT e.empid FROM emps e JOIN depts d ON e.deptno = d.deptno"


def prove_with(monkeypatch, prover, constraints, query=JOINED, replacement="SELECT empid FROM emps"):
    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", prover)
    return _prove(query, replacement, SCHEMA, constraints, None, 1000, "postgres", False)


@pytest.mark.parametrize("reason", _SHAPE_MISMATCH)
def test_a_shape_mismatch_is_retried_with_only_not_null(monkeypatch, reason):
    prover = FakeProver(answer(SmtStatus.NOT_PROVEN, reason), answer(SmtStatus.PROVEN_EQUIVALENT))
    result = prove_with(monkeypatch, prover, KEYED)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert len(prover.calls) == 2
    retry = prover.calls[1]
    assert all(not c.keys and not c.foreign_keys for c in retry.values())
    assert {t: c.not_null for t, c in retry.items()} == NOT_NULL  # NOT NULL is kept: it is part of the schema


def test_the_retry_failing_keeps_the_first_verdict(monkeypatch):
    first = answer(SmtStatus.NOT_PROVEN, _SHAPE_MISMATCH[0])
    prover = FakeProver(first, answer(SmtStatus.NOT_PROVEN, "something else"))
    assert prove_with(monkeypatch, prover, KEYED) is first


@pytest.mark.parametrize(
    "first",
    [
        answer(SmtStatus.PROVEN_EQUIVALENT),
        answer(SmtStatus.NOT_EQUIVALENT, "the queries differ on the attached database"),
        answer(SmtStatus.NOT_PROVEN, "different column counts (1 vs 2)"),
        answer(SmtStatus.NOT_PROVEN, "the solver timed out"),
    ],
)
def test_other_verdicts_are_not_retried(monkeypatch, first):
    prover = FakeProver(first, answer(SmtStatus.PROVEN_EQUIVALENT))
    assert prove_with(monkeypatch, prover, KEYED) is first
    assert len(prover.calls) == 1


def test_a_schema_without_keys_is_not_retried(monkeypatch):
    first = answer(SmtStatus.NOT_PROVEN, _SHAPE_MISMATCH[0])
    prover = FakeProver(answer(SmtStatus.PROVEN_EQUIVALENT), first)  # a second attempt would be a keyed one
    assert prove_with(monkeypatch, prover, UNKEYED) is first
    assert len(prover.calls) == 1


@pytest.mark.parametrize(
    "query, replacement, retried",
    [
        ("SELECT e.empid FROM emps e JOIN depts d ON e.deptno = d.deptno", "SELECT empid FROM emps", True),  # a foreign key's child and parent
        ("SELECT empid FROM emps", "SELECT DISTINCT deptno FROM depts", True),  # a keyed table
        ("SELECT empid FROM emps", "SELECT empid, name FROM emps", False),  # a foreign key's child alone: nothing to drop
        ("SELECT empid FROM emps WHERE deptno = 1", "SELECT empid FROM emps", False),
        ("SELECT FROM WHERE", "SELECT empid FROM emps", True),  # unreadable text keeps the retry
    ],
)
def test_the_retry_is_made_only_where_a_key_could_have_mattered(monkeypatch, query, replacement, retried):
    first = answer(SmtStatus.NOT_PROVEN, _SHAPE_MISMATCH[0])
    prover = FakeProver(first, answer(SmtStatus.PROVEN_EQUIVALENT))
    result = prove_with(monkeypatch, prover, KEYED, query, replacement)
    assert len(prover.calls) == (2 if retried else 1)
    assert (result.status is SmtStatus.PROVEN_EQUIVALENT) == retried


def test_a_prover_crash_is_never_a_proof(monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(algebraic_equivalence, "prove_equivalent_algebraic", crash)
    assert _prove(JOINED, JOINED, SCHEMA, KEYED, None, 1000, "postgres", False) is None


# Calcite's ``testJoinOnCalcToJoin2`` under its UK/FK schema, answered from the join model: with the declared keys
# the prover drops the join on one side only, so the match is found only by the retry.
MODEL = "SELECT emps.empid, emps.deptno, depts.deptno FROM emps JOIN depts ON emps.deptno = depts.deptno"
QUERY = "SELECT * FROM (SELECT empid, deptno FROM emps WHERE empid > 10) AS a JOIN (SELECT deptno FROM depts WHERE deptno > 10) AS b ON a.deptno = b.deptno"


def test_a_join_over_filtered_children_is_answered_from_the_join_model():
    reuse = rewrite_over_model(QUERY, MODEL, schema=SCHEMA, constraints=KEYED)
    assert reuse.rewritten, reuse.reason
    assert "mv0" in reuse.sql and "> 10" in reuse.sql


def test_that_answer_needs_the_retry(monkeypatch):
    monkeypatch.setattr(model_reuse, "_SHAPE_MISMATCH", ())
    assert not rewrite_over_model(QUERY, MODEL, schema=SCHEMA, constraints=KEYED).rewritten
