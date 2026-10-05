"""Round-two proof re-check of the rewrite evals: the DLBench known dialect gap (tools/recheck/dialect_rewrites.py).

SQLite's LENGTH counts characters and ClickHouse's length() counts bytes, so a proof that reads both as LENGTH holds
for ASCII text only. The adapter marks the pair as a known dialect gap and keeps the databases ASCII, so that it no
longer reads as a false proof; without the restriction the search still finds the difference. Every test builds its
own case and connection.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
import sys

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "src"))

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import engine  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

PAIR = "BIRDTrans/clickhouse/2"


@pytest.fixture(autouse=True)
def clean_engine():
    yield
    dr.uninstall()


def _item(adapter: str) -> dict:
    return next(i for i in dr.ADAPTERS[adapter].items() if i["pair"] == PAIR)


def test_dlbench_target_marks_clickhouse_byte_length_as_a_known_dialect_gap():
    case = dr.ADAPTERS["dlbench-target"].case(_item("dlbench-target"))
    assert case is not None and case.meta["engines"] == ("sqlite", "duckdb")
    assert "strlen" in case.right.lower()  # ClickHouse's length counts bytes
    assert "known_dialect_gap" in case.meta
    assert case.legal is not None
    assert case.legal({"conference": [(1, "a", "K", "x")]}) is False
    assert case.legal({"conference": [(1, "a", "abc", None)]}) is True
    assert engine.recheck(case, budget=600, seconds=30)["verdict"] == "survived"


def test_without_the_restriction_the_search_still_finds_the_gap():
    case = dr.ADAPTERS["dlbench-target"].case(_item("dlbench-target"))
    record = engine.recheck(dataclasses.replace(case, legal=None), budget=600, seconds=30)
    assert record["verdict"] == "differs"
    assert record["witness"]["left"] != record["witness"]["right"]


def test_the_plain_dlbench_adapter_needs_no_marker():
    case = dr.ADAPTERS["dlbench"].case(_item("dlbench"))
    assert case is not None and case.legal is None and "known_dialect_gap" not in case.meta
