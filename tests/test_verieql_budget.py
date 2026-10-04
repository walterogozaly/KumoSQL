"""Synthetic cases exercise VeriEQL's spawn budget without corpus fixtures."""

import os
from pathlib import Path
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import verieql_bench as bench


def _case(right="SELECT 1"):
    return {"index": 7, "schema": {}, "pair": ["SELECT 1", right]}


OPTIONS = {"trials": 2, "recheck_trials": 2, "more_trials": 2, "wide_trials": 0, "budget": 30}


def _search_then_wait(case, options, sender):
    sender.send(("searched", None))
    time.sleep(30)


def _crash(case, options, sender):
    os._exit(7)


def _send_then_wait(case, options, sender):
    sender.send(("searched", None))
    sender.send(("verdict", bench.Verdict(case["index"], bench.EQUIVALENT, "proof")))
    time.sleep(30)


def _nested_case(right):
    return bench._budgeted_case(_case(right), OPTIONS)


@pytest.mark.parametrize("right,status", [("SELECT 1", bench.EQUIVALENT), ("SELECT 2", bench.DIFFERENT)])
def test_spawn_budget_keeps_real_verdicts(right, status):
    result = bench._budgeted_case(_case(right), OPTIONS)
    assert result.status == status, result


def test_unavailable_alarm_uses_spawn(monkeypatch):
    monkeypatch.delattr(bench.signal, "SIGALRM", raising=False)
    assert bench.decide(_case(), **OPTIONS).status == bench.EQUIVALENT


def test_timeout_before_search_is_unknown():
    result = bench._budgeted_case(_case(), {**OPTIONS, "budget": 0.001})
    assert (result.status, result.detail) == (bench.UNKNOWN, "time budget")


def test_timeout_after_search_keeps_only_dataset_agreement(monkeypatch):
    monkeypatch.setattr(bench, "_budget_worker", _search_then_wait)
    result = bench._budgeted_case(_case(), {**OPTIONS, "budget": 4})
    assert (result.status, result.detail) == (bench.AGREES, "time budget")


def test_child_crash_is_unknown(monkeypatch):
    monkeypatch.setattr(bench, "_budget_worker", _crash)
    result = bench._budgeted_case(_case(), OPTIONS)
    assert (result.status, result.detail) == (bench.UNKNOWN, "worker failed")


def test_a_result_from_a_worker_that_does_not_exit_is_not_a_proof(monkeypatch):
    monkeypatch.setattr(bench, "_budget_worker", _send_then_wait)
    result = bench._budgeted_case(_case(), {**OPTIONS, "budget": 4})
    assert (result.status, result.detail) == (bench.AGREES, "time budget")


def test_pool_workers_can_start_a_budget_process():
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_nested_case, ["SELECT 1", "SELECT 2"]))
    assert [v.status for v in results] == [bench.EQUIVALENT, bench.DIFFERENT]
