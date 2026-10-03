"""The pytest-xdist scheduler ``tests/conftest.py`` uses for ``--dist loadgroup`` (what ``tools/run_tests.py`` runs).

pytest-xdist's own ``loadgroup`` keeps up to three tests queued on each worker, which is right for tests that take
milliseconds and wrong for the benchmarks that take minutes: near the end of a run two or three long tests could wait
in one worker's queue, one after the other, while the other workers had nothing left to do. It also moves groups of
tests (``xdist_group``) ahead of the order ``tests/conftest.py`` chose.

``DurationScheduling`` is ``loadgroup`` with two changes:

* it hands out work in the order the workers collected it, which ``tests/conftest.py`` sorted (the longest tests of
  the run, then tests that failed before, then the fast tests, then the slow tests longest-first, from
  ``tests/order.json``);
* a worker takes more work only while everything it holds is fast. A worker starts a test only once it also holds
  the test it will run next (pytest-xdist needs that to tear fixtures down), so a worker holding a slow test (one
  that ``tests/order.json`` times at 3 seconds or more) gets the shortest work left as its next test, never the
  next slow test in line; the slow tests go to whichever worker is free first.

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

    def _seconds(self, unit) -> float:
        return sum(self.slow.get(nodeid, 0.0) for nodeid in unit)

    def _shortest(self) -> str:
        """The first unit of work with no slow test in it, else the quickest one left."""

        for scope, unit in self.workqueue.items():
            if not any(nodeid in self.slow for nodeid in unit):
                return scope
        return min(self.workqueue, key=lambda scope: self._seconds(self.workqueue[scope]))

    def _reschedule(self, node) -> None:
        if node.shutting_down:
            return
        if not self.workqueue:
            node.shutdown()
            return
        held = self._held(node)
        holds_slow = any(nodeid in self.slow for nodeid in held)
        if len(held) >= 2 and (holds_slow or len(held) > PREFETCH):
            return
        if holds_slow:
            # the worker is waiting for its next test before it starts the slow one: give it something short
            self.workqueue.move_to_end(self._shortest(), last=False)
        self._assign_work_unit(node)
