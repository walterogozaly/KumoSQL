"""Concurrent first use of the Z3-backed provers (determinism and robustness audit, DR-04).

Z3 objects in the shared default context are not thread-safe: four threads making their first SMT proof at once
all failed with ``Context mismatch``, and the process stayed broken afterwards. The provers now hold one lock
(``kumosql.solver_lock``). Each check runs in a fresh interpreter, since only a cold start shows the failure.
"""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("z3")

SCRIPT = textwrap.dedent(
    """
    import threading
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.bounded_equivalence import check_bounded, schema_from_prover
    from kumosql.smt_equivalence import prove_equivalent_smt

    bounded_schema = schema_from_prover({"t": ["a"]}, types={"t": {"a": "INT64"}})

    def smt(i):
        return prove_equivalent_smt(f"SELECT a FROM t WHERE a > {i}", f"SELECT a FROM t WHERE {i} < a").status.value

    def algebraic(i):
        sql = f"SELECT a FROM t WHERE a > {i} AND a IN (SELECT a FROM t WHERE a < {i + 9})"
        return prove_equivalent_algebraic(sql, sql, schema={"t": ["a"]}).status.value

    def bounded(i):
        return check_bounded(f"SELECT a FROM t WHERE a > {i}", f"SELECT a FROM t WHERE {i} < a", bounded_schema).status.value

    jobs = [smt, algebraic, bounded] * 3
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def run(index):
        barrier.wait()
        try:
            results[index] = jobs[index](index)
        except Exception as error:
            results[index] = f"{type(error).__name__}: {error}"

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results.append(smt(99))
    print("\\n".join(results))
    """
)


@pytest.mark.parametrize("attempt", range(2))
def test_concurrent_cold_start_proves_in_every_thread(attempt):
    # A shared editable venv can point at another checkout: subprocesses must
    # exercise this exact candidate's prover, not the venv's original checkout.
    source = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")]))}
    done = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=300, env=env)
    assert done.returncode == 0, done.stderr[-2000:]
    lines = done.stdout.split()
    assert lines and set(lines) <= {"proven_equivalent", "bounded_equivalent"}, done.stdout


def test_solver_lock_preserves_gc_state_on_nesting_and_failure():
    import gc
    from kumosql.solver_lock import serialized
    original = gc.isenabled()
    try:
        @serialized
        def inner():
            assert not gc.isenabled()
            raise ValueError("proof failure")
        @serialized
        def outer():
            assert not gc.isenabled()
            inner()
        for enabled in (True, False):
            gc.enable() if enabled else gc.disable()
            with pytest.raises(ValueError, match="proof failure"):
                outer()
            assert gc.isenabled() is enabled
    finally:
        gc.enable() if original else gc.disable()


def test_pending_cycles_are_finalized_under_solver_lock(monkeypatch):
    import gc
    from kumosql.solver_lock import serialized, SOLVER_LOCK
    seen = []
    class Probe:
        def __del__(self):
            seen.append((gc.isenabled(), SOLVER_LOCK._is_owned()))
    @serialized
    def make_cycle():
        item = Probe()
        item.cycle = item
    monkeypatch.setattr(gc, "get_count", lambda: (gc.get_threshold()[0] + 1, 0, 0))
    make_cycle()
    assert seen == [(False, True)]
