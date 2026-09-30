"""Offline checks for the BigQuery testbed generator (no gcloud needed)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import sqlglot

_DIR = Path(__file__).resolve().parents[1] / "examples" / "bq_testbed"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("build_testbed", _DIR / "build_testbed.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

PROJECT, DATASET = "my-proj", "ds"


def _plan(rounds: int = 1):
    return bt.plan(PROJECT, DATASET, 10, rounds)


def test_every_statement_parses_as_bigquery():
    build, jobs = _plan()
    for _, sql in build + jobs:
        sqlglot.parse_one(sql, read="bigquery")


def test_steps_only_reference_earlier_steps():
    seen: set[str] = set()
    for step in bt.models.STEPS:
        body = step.sql.format(p=PROJECT, d=DATASET, src=bt.models.SOURCE, pct=10)
        refs = set(re.findall(rf"`{PROJECT}\.{DATASET}\.(\w+)`", body))
        assert refs <= seen, f"{step.name} reads {refs - seen} before it exists"
        assert step.name not in seen
        seen.add(step.name)


def test_workload_reads_only_defined_models():
    names = {s.name for s in bt.models.STEPS}
    _, jobs = _plan()
    for _, sql in jobs:
        refs = set(re.findall(rf"`{PROJECT}\.{DATASET}\.(\w+)`", sql))
        assert refs and refs <= names


def test_no_dml_and_tables_expire():
    build, _ = _plan()
    for kind, sql in build:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", sql)
        if kind in ("raw", "table"):
            assert "expiration_timestamp" in sql
    assert "default_table_expiration_days = 60" in build[0][1]


def test_workload_is_deterministic_and_repeats():
    assert _plan(2)[1] == _plan(2)[1]
    assert len(_plan(2)[1]) == 2 * len(_plan(1)[1])


def test_messy_shapes_are_present():
    names = {s.name for s in bt.models.STEPS}
    assert {"agg_sales_by_state", "mart_state_revenue", "tbl_sales_by_state"} <= names
    assert {"dim_customer", "dim_customers"} <= names
    assert sum(n.startswith("chain_") for n in names) >= 8


class _FakeRunner(bt.Runner):
    def __init__(self, sizes, **kw):
        super().__init__("p", **kw)
        self.sizes = iter(sizes)

    def estimate(self, sql):
        return next(self.sizes)

    def _run(self, cmd, sql):
        return ""


def test_guard_stops_oversized_job_and_total():
    with pytest.raises(bt.GuardError):
        _FakeRunner([200], max_job_bytes=100, max_total_bytes=1000).execute("select 1", "adhoc")
    runner = _FakeRunner([60, 60], max_job_bytes=100, max_total_bytes=100)
    runner.execute("select 1", "adhoc")
    with pytest.raises(bt.GuardError):
        runner.execute("select 1", "adhoc")
