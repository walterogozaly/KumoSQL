"""Solver checks carry a deterministic work cap besides the wall clock (determinism audit, DR-02).

With only a wall-clock timeout, identical calls flipped between ``proven_equivalent`` and ``not_proven`` with
machine load. Each check now also gets Z3's ``rlimit``: a check that runs out of it stops at the same point on
any machine, and the result says which limit stopped it.
"""

import pytest

z3 = pytest.importorskip("z3")

from kumosql import solver_lock
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

IN_LIST = "SELECT a FROM t WHERE a IN (" + ", ".join(map(str, range(35))) + ")"
OR_CHAIN = "SELECT a FROM t WHERE " + " OR ".join(f"a = {i}" for i in range(35))


def test_bounded_solver_sets_a_work_cap_and_the_timeout():
    solver = solver_lock.bounded_solver(10)
    ys = [z3.Int(f"y{i}") for i in range(12)]
    solver.add(z3.Distinct(*ys), *[z3.And(y >= 0, y < 11) for y in ys])  # pigeonhole: a lot of search
    assert solver.check() == z3.unknown
    assert solver.reason_unknown() in ("canceled", "timeout")


def test_a_work_capped_check_gives_the_same_verdict_every_time(monkeypatch):
    monkeypatch.setattr(solver_lock, "WORK_PER_MS", 1)  # 5,000 units: far too few for this pair
    results = {(r.status, r.reason) for r in (prove_equivalent_smt(IN_LIST, OR_CHAIN, timeout_ms=5000) for _ in range(5))}
    assert len(results) == 1
    status, reason = results.pop()
    assert status is SmtStatus.NOT_PROVEN and "work budget" in reason


def test_the_default_cap_leaves_the_pair_proven():
    assert prove_equivalent_smt(IN_LIST, OR_CHAIN).proven
