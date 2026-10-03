"""One lock around every use of Z3's shared default context.

Z3 terms, sorts and solvers built without an explicit context share one global context, and Z3 objects
must not be used from two threads at once: concurrent first calls fail with ``Context mismatch`` and can
leave the context unusable for the rest of the process. The UI serves requests on threads and analyses
pipelines in the background, so each public entry point that builds Z3 terms runs under this lock.
It is re-entrant, so a prover that calls another (or a rule that uses Z3) does not deadlock.
"""

from __future__ import annotations

import functools
import threading

SOLVER_LOCK = threading.RLock()


def serialized(function):
    """Run ``function`` while holding :data:`SOLVER_LOCK`."""

    @functools.wraps(function)
    def locked(*args, **kwargs):
        with SOLVER_LOCK:
            return function(*args, **kwargs)

    return locked
