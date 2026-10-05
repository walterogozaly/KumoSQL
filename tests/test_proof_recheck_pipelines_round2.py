"""Round two of the proof re-check for the pipeline, fix and property evals: the supervisor that keeps one
crashing or hanging pair from stalling a run (tools/recheck/supervise.py) and the adapters' extra items."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from recheck import pipelines_refactors as pr  # noqa: E402
from recheck import supervise  # noqa: E402


def _job(job: tuple) -> dict:
    """A stand-in for ``proof_recheck._work``: the job names what the child does."""

    action, name, flag = job
    if action == "ok":
        return {"eval": "demo", "pair": name, "verdict": "survived"}
    if action == "segfault":  # a native crash, as DuckDB's IEJoin gives on SQLFluff's ST09 fixtures
        os.kill(os.getpid(), signal.SIGSEGV)
    if action == "crash-once":  # the first attempt (no marker file yet) crashes, the retry succeeds
        if not os.path.exists(flag):
            Path(flag).write_text("x")
            os.kill(os.getpid(), signal.SIGSEGV)
        return {"eval": "demo", "pair": name, "verdict": "survived"}
    if action == "hang":
        time.sleep(600)
    raise AssertionError(action)


def _describe(job: tuple) -> dict:
    return {"eval": "demo", "pair": job[1]}


def _run(jobs, **options) -> dict[str, dict]:
    records = supervise.imap_unordered(_job, jobs, 2, options.pop("deadline", 30), describe=_describe, **options)
    return {r["pair"]: r for r in records}


def test_a_crashing_pair_is_recorded_and_the_others_still_finish():
    out = _run([("ok", "a", ""), ("segfault", "b", ""), ("ok", "c", "")], retries=0)
    assert out["a"]["verdict"] == out["c"]["verdict"] == "survived"
    assert out["b"]["verdict"] == "search-error" and "SIGSEGV" in out["b"]["error"]


def test_a_crashed_pair_is_retried_and_the_retry_is_marked(tmp_path):
    out = _run([("crash-once", "a", str(tmp_path / "flag"))], retries=1)
    assert out["a"]["verdict"] == "survived" and out["a"]["crash_retry"] == 1


def test_a_hanging_pair_is_killed_at_the_deadline_and_recorded_as_timeout():
    start = time.time()
    out = _run([("hang", "a", ""), ("ok", "b", "")], deadline=3)
    assert out["a"]["verdict"] == "timeout" and out["b"]["verdict"] == "survived"
    assert time.time() - start < 30


def test_sqlfluff_items_add_the_adapted_forms_the_eval_proves_statement_by_statement():
    items = pr.ADAPTERS["sqlfluff-semantic-fixes"].items()
    adapted = [i for i in items if "#adapted" in i["pair"]]
    assert adapted and all(i["pair"].split("#")[0] in {j["pair"] for j in items} for i in adapted)


def test_reconnecting_runner_opens_a_younger_connection():
    from recheck import engine
    from recheck.engine import Case, Column, Table

    tables = {"t": Table("t", [Column("x", "int")])}
    case = Case("demo", "p", "SELECT x FROM t", "SELECT x FROM t", tables)
    original = engine.Runner.load
    try:
        supervise.reconnect_every(2)
        runner = engine.Runner(case)
        first = runner.db
        for _ in range(3):
            runner.load({"t": [(1,)]})
        assert runner.db is not first  # reopened on the second load
        assert runner.run("SELECT x FROM t") == [(1,)]
    finally:
        engine.Runner.load = original
