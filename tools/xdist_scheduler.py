"""The pytest-xdist scheduler ``tests/conftest.py`` uses for ``--dist loadgroup`` (what ``tools/run_tests.py`` runs).

pytest-xdist's own ``loadgroup`` keeps up to three tests queued on each worker, which is right for tests that take
milliseconds and wrong for the benchmarks that take minutes: near the end of a run two or three long tests could wait
in one worker's queue, one after the other, while the other workers had nothing left to do. It also moves groups of
tests (``xdist_group``) ahead of the order ``tests/conftest.py`` chose.

``DurationScheduling`` is ``loadgroup`` with two changes:

* it hands out work in the order the workers collected it, which ``tests/conftest.py`` sorted (tests that failed
  before, then the fast tests, then the slow tests longest-first, from ``tests/order.json``);
* a worker takes more work only while everything it holds is fast: a worker running a slow test (one that
  ``tests/order.json`` times at 3 seconds or more) gets its next test when that one finishes, so the slow tests go
  to whichever worker is free first.

Which tests run, and where each group of tests runs together, is unchanged.
"""

from __future__ import annotations

from xdist.scheduler import LoadGroupScheduling

PREFETCH = 2  # fast tests a worker may hold beyond the one it is running, as pytest-xdist's loadgroup does


class DurationScheduling(LoadGroupScheduling):
    def __init__(self, config, log=None, slow: dict[str, float] | None = None):
        super().__init__(config, log)
        self.slow = slow or {}

    def schedule(self) -> None:
        self.config.option.loadscopereorder = False  # keep the order tests/conftest.py chose
        super().schedule()

    def _held(self, node) -> list[str]:
        return [nodeid for unit in self.assigned_work[node].values() for nodeid, done in unit.items() if not done]

    def _reschedule(self, node) -> None:
        if node.shutting_down:
            return
        if not self.workqueue:
            node.shutdown()
            return
        held = self._held(node)
        if len(held) > PREFETCH or any(nodeid in self.slow for nodeid in held):
            return
        self._assign_work_unit(node)
