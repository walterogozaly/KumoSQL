"""The SMT prover's work per proof: its OR-split case search makes each check once, a counterexample
model is worked out only when one is read, and ``_subst`` is z3's substitution (src/kumosql/smt_equivalence.py)."""

import collections

import pytest

z3 = pytest.importorskip("z3")

from kumosql import smt_equivalence  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402

SCHEMA = {"visits": ["guest", "door", "arrive", "leave"]}
# Guests seen at a different door while one of their own visits was open: the right side is proven
# by splitting its OR into the two directions, then matching one direction with the aliases swapped.
OVERLAP = (
    "SELECT DISTINCT g1.guest FROM visits AS g1 JOIN visits AS g2 ON g1.guest = g2.guest AND g1.door <> g2.door "
    "AND g2.arrive BETWEEN g1.arrive AND g1.leave"
)
EITHER_DIRECTION = (
    "SELECT DISTINCT p.guest FROM visits AS p JOIN visits AS q ON p.guest = q.guest AND p.door <> q.door "
    "WHERE p.arrive BETWEEN q.arrive AND q.leave OR q.arrive BETWEEN p.arrive AND p.leave"
)


def _count(monkeypatch, name):
    calls = collections.Counter()
    original = getattr(smt_equivalence._Prover, name)

    def counted(self, *args):
        calls[tuple(_describe(arg) for arg in args)] += 1
        return original(self, *args)

    monkeypatch.setattr(smt_equivalence._Prover, name, counted)
    return calls


def _describe(block):
    if hasattr(block, "cond"):
        return block.cond.t.sexpr(), tuple(o.table for o in block.occs)
    return id(block)


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql", timeout_ms=4000).proven


def test_the_or_split_search_checks_each_case_against_each_target_once(monkeypatch):
    checks = _count(monkeypatch, "branch_set_contained")
    assert _proven(OVERLAP, EITHER_DIRECTION)
    assert checks
    assert [key for key, times in checks.items() if times > 1] == []


def test_a_proof_works_out_no_counterexample_model(monkeypatch):
    models = _count(monkeypatch, "_nice_model")
    assert _proven(OVERLAP, EITHER_DIRECTION)
    assert sum(models.values()) == 0


def test_a_refutation_still_gets_an_integral_counterexample(monkeypatch):
    models = _count(monkeypatch, "_nice_model")
    result = prove_equivalent_smt("SELECT a FROM t WHERE a > 5", "SELECT a FROM t WHERE a > 3")
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert sum(models.values()) >= 1
    values = [row["a"] for row in result.counterexample.tables["t"]]
    assert any(isinstance(v, int) and 3 < v <= 5 for v in values)


def test_subst_is_z3_substitute():
    x, y, z = z3.Ints("x y z")
    b = z3.Bool("b")
    term = z3.And(x + 2 * y > x, b)
    pairs = [(x, z), (b, z3.BoolVal(True))]
    assert smt_equivalence._subst(term, pairs).eq(z3.substitute(term, *pairs))
    assert smt_equivalence._subst(term, []) is term
    with pytest.raises(z3.Z3Exception):
        smt_equivalence._subst(term, [(x, b)])
