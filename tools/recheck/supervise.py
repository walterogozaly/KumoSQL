"""Run each re-check job in a child process the parent can kill, so no pair can stall a whole run.

``multiprocessing.Pool`` waits forever for the result of a task whose worker died: a DuckDB segmentation
fault (an IEJoin over empty or NULL-heavy tables is one, for example SQLFluff's ST09 fixtures with several
inequality joins) or a native z3 call that ignores the soft alarm leaves ``imap_unordered`` blocked, and the
run sits idle for hours. Here every job is its own forked child:

* the child sends its record down a pipe and exits;
* a child still running after ``deadline`` seconds is killed and its record is ``timeout``;
* a child that dies without a record (a signal) is run again, up to ``retries`` times, with the variant of the
  job ``reseed`` returns (another seed visits other databases, and a crash that depends on the sequence of
  databases then does not recur); when every attempt dies the record is ``search-error`` naming the signal.
  A record produced by a retry carries ``crash_retry`` so the report can say so.

Nothing here changes what a pair is proven or compared with.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import faulthandler
import json
import multiprocessing
from multiprocessing.connection import wait
import signal
import time


def _signal_name(code: int | None) -> str:
    if code is None:
        return "no exit code"
    if code >= 0:
        return f"exit code {code}"
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f"signal {-code}"


def _child(fn: Callable, job, conn) -> None:
    faulthandler.disable()  # a native crash is expected here and handled by the parent; keep its traceback out of the output
    try:
        record = fn(job)
        conn.send(json.dumps(record, default=str))
    finally:
        conn.close()


class _Running:
    def __init__(self, context, fn: Callable, job, attempt: int, original) -> None:
        self.job, self.attempt, self.original = job, attempt, original
        self.parent, child = context.Pipe(duplex=False)
        self.process = context.Process(target=_child, args=(fn, job, child), daemon=True)
        self.process.start()
        child.close()
        self.started = time.time()
        self.payload: str | None = None


def imap_unordered(
    fn: Callable,
    jobs: Iterable,
    processes: int,
    deadline: float,
    *,
    retries: int = 1,
    reseed: Callable | None = None,
    describe: Callable | None = None,
) -> Iterator[dict]:
    """Yield ``fn(job)`` records as children finish, at most ``processes`` at a time.

    ``describe(job) -> dict`` gives the fields (``eval``, ``pair``) of a record made without a result from the
    child (timeout or crash); ``reseed(job, attempt)`` gives the job for a crash retry (default: the same job).
    """

    context = multiprocessing.get_context("fork")
    queue = list(jobs)[::-1]
    running: list[_Running] = []
    retry_queue: list[tuple] = []
    describe = describe or (lambda job: {})

    def launch(job, attempt: int, original) -> None:
        running.append(_Running(context, fn, job, attempt, original))

    try:
        while queue or running or retry_queue:
            while len(running) < processes and (retry_queue or queue):
                if retry_queue:
                    job, attempt, original = retry_queue.pop()
                else:
                    job = queue.pop()
                    attempt, original = 0, job
                launch(job, attempt, original)
            ready = wait([r.parent for r in running] + [r.process.sentinel for r in running], timeout=5)
            now = time.time()
            for r in list(running):
                finished = None
                if r.parent in ready or r.parent.poll():
                    try:
                        r.payload = r.parent.recv()
                    except (EOFError, OSError):
                        r.payload = None
                    finished = True
                elif r.process.sentinel in ready:
                    finished = True
                elif now - r.started > deadline:
                    r.process.kill()
                    r.process.join()
                    running.remove(r)
                    r.parent.close()
                    yield {**describe(r.original), "verdict": "timeout", "seconds": round(now - r.started, 2),
                           "error": f"killed after {deadline:.0f}s (hard limit)"}
                    continue
                if not finished:
                    continue
                r.process.join(10)
                if r.process.is_alive():
                    r.process.kill()
                    r.process.join()
                running.remove(r)
                r.parent.close()
                if r.payload is not None:
                    record = json.loads(r.payload)
                    if r.attempt:
                        record["crash_retry"] = r.attempt
                    yield record
                elif r.attempt < retries:
                    next_job = reseed(r.original, r.attempt + 1) if reseed else r.original
                    retry_queue.append((next_job, r.attempt + 1, r.original))
                else:
                    yield {**describe(r.original), "verdict": "search-error",
                           "error": f"worker died without a result ({_signal_name(r.process.exitcode)}); "
                                    f"DuckDB or z3 crashed on this pair, {r.attempt + 1} attempt(s)"}
    finally:
        for r in running:
            if r.process.is_alive():
                r.process.kill()
                r.process.join()


def reconnect_every(count: int) -> None:
    """Make every ``engine.Runner`` open a fresh DuckDB connection after each ``count`` databases it loads.

    A crash of DuckDB that depends on what the connection ran before (SQLFluff's ST09 inequality joins crash
    one after about 1,400 databases) does not recur on a younger connection. Used for the retry of a pair whose
    worker died; the queries and databases are unchanged. Patches the class in this process only.
    """

    from recheck import engine

    original = engine.Runner.load
    loaded = {"n": 0}

    def load(self, data):
        loaded["n"] += 1
        if loaded["n"] % count == 0:
            self.close()
            self.__init__(self.case, self.query_seconds)
        return original(self, data)

    engine.Runner.load = load
