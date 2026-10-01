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


@contextmanager
def stage(name: str, **detail: object) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        seconds = time.perf_counter() - start
        record(name, seconds, **detail)


def record(name: str, seconds: float, **detail: object) -> None:
    with _LOCK:
        _RECENT.append({"stage": name, "seconds": round(seconds, 3), **detail})
    if os.environ.get("KUMOSQL_TIMING", "1") != "0":
        extra = ", ".join(f"{k} {v}" for k, v in detail.items())
        try:
            print(f"[kumosql] {name}: {seconds:.2f}s{f' ({extra})' if extra else ''}", file=sys.stderr, flush=True)
        except (OSError, ValueError):  # a closed console must never break an analysis
            pass


def recent() -> list[dict]:
    with _LOCK:
        return list(_RECENT)
