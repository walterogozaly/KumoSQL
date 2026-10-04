"""The SQLFluff execution oracle stays isolated on platforms without fork."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sqlfluff_fixtures_bench as bench


def _crash():
    import os
    os._exit(7)


def _hang():
    import time
    time.sleep(30)


@pytest.fixture
def spawn_only(monkeypatch):
    import multiprocessing
    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])


def test_spawn_returns_the_child_result(spawn_only):
    assert bench._isolated(abs, -3, timeout=10) == 3


def test_spawn_keeps_an_empty_result_distinct_from_failure(spawn_only):
    assert bench._isolated(dict, timeout=10) == {}


def test_large_result_does_not_block_the_child_pipe(spawn_only):
    result = bench._isolated(bytes, 1024 * 1024, timeout=10)
    assert len(result) == 1024 * 1024


def test_crashed_child_is_not_checkable(spawn_only):
    assert bench._isolated(_crash, timeout=10) is False


def test_timed_out_child_is_not_checkable(spawn_only):
    assert bench._isolated(_hang, timeout=0.1) is False


def test_spawn_runs_the_execution_oracle(spawn_only):
    witness = bench.execute_difference("SELECT 1 AS a", "SELECT 2 AS a", "ansi", {}, {}, 1)
    assert isinstance(witness, dict)
