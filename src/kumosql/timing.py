"""Per-stage timings for loading and analysing a project.

``stage(name)`` times a block as a :func:`kumosql.console.task` (console and ``ui.log``,
redacted), so a slow load can be diagnosed from numbers: ``analyse: finished in 8.8s (models 5,000)``.
Set ``KUMOSQL_TIMING=0`` to keep quick stages out of the console. ``recent()`` returns the
latest timings for the UI and tests. Only counts and durations are recorded,
never names or SQL.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from typing import Iterator
import os
import threading
import time

_LOCK = threading.Lock()
_RECENT: deque = deque(maxlen=200)
_LOCAL = threading.local()
_PROGRESS: dict[str, dict] = {}


def _quiet() -> bool:
    return os.environ.get("KUMOSQL_TIMING", "1") == "0"


def _open_loops() -> list:
    if not hasattr(_LOCAL, "loops"):
        _LOCAL.loops = []
    return _LOCAL.loops


@contextmanager
def stage(name: str, **detail: object) -> Iterator[None]:
    """Time a block as a :func:`kumosql.console.task`; the start is printed too, so a long stage never looks like a stall."""

    from . import console

    loops = _open_loops()
    mark = len(loops)
    begin = time.perf_counter()
    try:
        with console.task(name, quiet=_quiet(), **detail):
            try:
                yield
            finally:
                for loop in reversed(loops[mark:]):  # a loop left open by an error must not keep the log stack tangled
                    loop._abandon()
    finally:
        record(name, time.perf_counter() - begin, **detail)


class Progress:
    """Progress and the slowest items of a long loop: call ``step(model_key)`` at the top of each iteration and ``finish()`` after it.

    It is a nested console task, so the log shows ``analyse > trace columns: started``, a
    "still running ... 407/2,400" line every few seconds and a finish line. Any item slower than
    ``slow`` seconds is reported at once, and the slowest few at the end. Items are named by
    redacted placeholder (``model#417``), never by their real name.
    """

    def __init__(self, name: str, total: int, *, every: float = 5.0, slow: float = 3.0) -> None:
        from . import console

        self.name, self.total, self.slow = name, total, slow
        self.done = 0
        self.started = time.perf_counter()
        self.slowest: list[tuple[float, str]] = []
        self._open: tuple[str, float] | None = None
        self._console = console
        self._context = console.task(name, quiet=_quiet(), warn_after=None, heartbeat_after=every, items=total)
        self._task = self._context.__enter__()
        self._task.progress(0, total)
        _PROGRESS[name] = {"done": 0, "total": total}
        _open_loops().append(self)

    def step(self, label: str = "") -> None:
        """Call at the top of each loop iteration: it closes the previous item and opens this one."""

        self._close()
        shown = self._console.ref("model", label) if label else f"{self.done + 1:,} of {self.total:,}"
        self._open = (shown, time.perf_counter())

    def _close(self) -> None:
        if self._open is None:
            return
        label, begin = self._open
        self._open = None
        took = time.perf_counter() - begin
        self.done += 1
        _PROGRESS[self.name]["done"] = self.done
        self._task.progress(self.done, self.total)
        self.slowest = sorted([*self.slowest, (took, label)], reverse=True)[:5]
        if took >= self.slow:
            self._console.say(f"{self.name}: slow item {label} took {took:.1f}s", console=not _quiet(), level="WARN")

    def _leave(self, error: BaseException | None) -> None:
        loops = _open_loops()
        if self in loops:
            loops.remove(self)
        _PROGRESS.pop(self.name, None)
        try:
            if error is None:
                self._context.__exit__(None, None, None)
            else:
                self._context.__exit__(type(error), error, error.__traceback__)
        except BaseException:  # noqa: BLE001  the task re-raises what it was given; the caller already has it
            pass

    def _abandon(self) -> None:
        self._close()
        self._leave(RuntimeError("loop did not finish"))

    def finish(self) -> None:
        self._close()
        top = ", ".join(f"{label} {took:.1f}s" for took, label in self.slowest if took >= 0.5)
        if top:
            self._console.say(f"{self.name}: slowest {top}", console=not _quiet())
        self._task.note(done=self.done)
        self._leave(None)
        record(self.name, time.perf_counter() - self.started, items=self.total)


def current_progress() -> list[dict]:
    """Loops running now, as ``{"name", "done", "total"}``."""

    return [{"name": name, **value} for name, value in list(_PROGRESS.items())]


def record(name: str, seconds: float, **detail: object) -> None:
    """Keep a finished stage's timing for the UI and tests (the log line comes from the console task)."""

    with _LOCK:
        _RECENT.append({"stage": name, "seconds": round(seconds, 3), **detail})


def recent() -> list[dict]:
    with _LOCK:
        return list(_RECENT)
