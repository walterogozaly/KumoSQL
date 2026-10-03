"""Concurrent first use of the Z3-backed provers (determinism and robustness audit, DR-04).

Z3 objects in the shared default context are not thread-safe: four threads making their first SMT proof at once
all failed with ``Context mismatch``, and the process stayed broken afterwards. The provers now hold one lock
(``kumosql.solver_lock``). Each check runs in a fresh interpreter, since only a cold start shows the failure.
"""

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
    done = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    lines = done.stdout.split()
    assert lines and set(lines) <= {"proven_equivalent", "bounded_equivalent"}, done.stdout
