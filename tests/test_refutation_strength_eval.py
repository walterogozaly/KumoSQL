"""Refutation strength: pairs known to differ must be refuted with a database that replays, and never proved.

The pairs are pinned in tests/fixtures/refutation_strength and the other fixtures named in
tools/refutation_strength_bench.py. ``FLOORS`` only ever goes up. The VeriEQL pairs need the VeriEQL
download; without it they are skipped.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "refutation_strength_bench.py"
_spec = importlib.util.spec_from_file_location("refutation_strength_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["refutation_strength_bench"] = bench
_spec.loader.exec_module(bench)

# measured 2026-10-05: r024 41 of 41, optimizer-bugs 21 of 23, verieql 4 of 4 (9 of 70 refuted without the synthesized search)
FLOORS = {"r024": 41, "optimizer-bugs": 21, "verieql": 4, "total": 66}
JOBS = max(1, min(4, os.cpu_count() or 1))


def _by_source(rows):
    out = {}
    for row in rows:
        out.setdefault(row["source"].split("/")[0], []).append(row)
    return out


@pytest.fixture(scope="module")
def cases():
    return bench.load_cases()


@pytest.fixture(scope="module")
def main_rows(cases):
    return bench.run([c for c in cases if c.source.split("/")[0] in bench.MAIN], jobs=JOBS)


@pytest.fixture(scope="module")
def control_rows(cases):
    return bench.run([c for c in cases if c.source.split("/")[0] not in bench.MAIN], jobs=JOBS)


def test_every_pair_differs_on_its_own_witness(main_rows, control_rows):
    unconfirmed = [r["id"] for r in main_rows + control_rows if r["refutable"] and r["confirmed"] is False]
    assert not unconfirmed


def test_nothing_is_proved_and_nothing_is_wrong(main_rows, control_rows):
    assert not [r["id"] for r in main_rows + control_rows if r["outcome"] == "proven"]
    assert not [r["id"] for r in main_rows + control_rows if r["wrong"]]


def test_every_attached_counterexample_replays(main_rows, control_rows):
    assert not [r["id"] for r in main_rows + control_rows if r["outcome"] == "refuted" and r["replayed"] is not True]


def test_refutation_floors(main_rows):
    parts = _by_source(main_rows)
    for source in ("r024", "optimizer-bugs"):
        refuted = sum(r["outcome"] == "refuted" for r in parts[source] if r["refutable"])
        assert refuted >= FLOORS[source], source
    if "verieql" not in parts:
        pytest.skip("the VeriEQL download is unavailable")
    assert sum(r["outcome"] == "refuted" for r in parts["verieql"]) >= FLOORS["verieql"]
    assert sum(r["outcome"] == "refuted" for r in main_rows if r["refutable"]) >= FLOORS["total"]


def test_the_two_unrefutable_pairs_are_marked_and_the_rest_are_not(main_rows):
    assert {r["id"] for r in main_rows if not r["refutable"]} == {"R012b-18", "bug-025"}


def test_held_out_optimizer_bugs_are_never_proved(main_rows):
    held = [r for r in main_rows if r["held_out"]]
    assert len(held) == 5
    assert not [r["id"] for r in held if r["outcome"] == "proven"]
    assert sum(r["outcome"] == "refuted" for r in held) >= 4


def test_targeted_escapes_and_unsafe_controls_are_refuted(control_rows):
    parts = _by_source(control_rows)
    for source in ("targeted-escapes", "unsafe-controls"):
        refutable = [r for r in parts[source] if r["refutable"]]
        assert refutable and all(r["outcome"] == "refuted" for r in refutable), source
