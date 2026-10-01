"""Per-stage timings for loading and analysing a project.

``stage(name)`` times a block and prints one line to the console (stderr), so a
slow load can be diagnosed from numbers: ``[kumosql] analyse: 8.82s (5000 models)``.
Set ``KUMOSQL_TIMING=0`` to silence the console lines. ``recent()`` returns the
latest timings for the UI and tests. Only counts and durations are recorded,
never names or SQL.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from typing import Iterator
import os
import sys
import threading
import time

_LOCK = threading.Lock()
_RECENT: deque = deque(maxlen=200)


def _print(line: str) -> None:
    if os.environ.get("KUMOSQL_TIMING", "1") == "0":
        return
    try:
        print(f"[kumosql] {line}", file=sys.stderr, flush=True)
    except (OSError, ValueError):  # a closed console must never break an analysis
        pass


def _detail(detail: dict) -> str:
    return ", ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}" for k, v in detail.items())


@contextmanager
def stage(name: str, **detail: object) -> Iterator[None]:
    """Time a block; the start is printed too, so a long stage never looks like a stall."""

    _print(f"{name}: started{f' ({_detail(detail)})' if detail else ''}")
    start = time.perf_counter()
    try:
        yield
    finally:
        seconds = time.perf_counter() - start
        record(name, seconds, **detail)


_PROGRESS: dict[str, dict] = {}


class Progress:
    """Progress and the slowest items of a long loop: call ``step()`` at the top of each iteration and ``finish()`` after it.

    Prints a line every ``every`` seconds, a line at once for any item slower than ``slow``
    seconds, and the slowest few at the end, all by position ("407 of 2,400"), never by name.
    """

    def __init__(self, name: str, total: int, *, every: float = 5.0, slow: float = 3.0) -> None:
        self.name, self.total, self.every, self.slow = name, total, every, slow
        self.done = 0
        self.started = self._last = time.perf_counter()
        self.slowest: list[tuple[float, str]] = []
        self._open: tuple[str, float] | None = None
        _PROGRESS[name] = {"done": 0, "total": total}
        _print(f"{name}: started ({total:,} items)")

    def step(self, label: str = "") -> None:
        """Call at the top of each loop iteration: it closes the previous item and opens this one."""

        self._close()
        # The label is accepted so callers can say what they are doing, but it is never printed:
        # items are identified by position only, so a log carries no model, table or file names.
        self._open = (f"{self.done + 1:,} of {self.total:,}", time.perf_counter())

    def _close(self) -> None:
        if self._open is None:
            return
        label, begin = self._open
        self._open = None
        now = time.perf_counter()
        took = now - begin
        self.done += 1
        _PROGRESS[self.name]["done"] = self.done
        self.slowest = sorted([*self.slowest, (took, label)], reverse=True)[:5]
        if took >= self.slow:
            _print(f"{self.name}: slow item {label} took {took:.1f}s")
        if now - self._last >= self.every:
            self._last = now
            _print(f"{self.name}: {self.done:,}/{self.total:,} after {now - self.started:.0f}s")

    def finish(self) -> None:
        self._close()
        _PROGRESS.pop(self.name, None)
        record(self.name, time.perf_counter() - self.started, items=self.total)
        top = ", ".join(f"{label} {took:.1f}s" for took, label in self.slowest if took >= 0.5)
        if top:
            _print(f"{self.name}: slowest {top}")


def current_progress() -> list[dict]:
    """Loops running now, as ``{"name", "done", "total"}``."""

    return [{"name": name, **value} for name, value in list(_PROGRESS.items())]


def record(name: str, seconds: float, **detail: object) -> None:
    with _LOCK:
        _RECENT.append({"stage": name, "seconds": round(seconds, 3), **detail})
    _print(f"{name}: {seconds:.2f}s{f' ({_detail(detail)})' if detail else ''}")


def recent() -> list[dict]:
    with _LOCK:
        return list(_RECENT)
