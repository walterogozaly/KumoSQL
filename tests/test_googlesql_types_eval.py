"""GoogleSQL type inference: the dev split of the compliance-test labels must never be wrong, and its exact count only grows.

``python tools/googlesql_types_eval.py`` prints the full score. Each labelled output column of a compliance query is
exact (KumoSQL's type text equals the label), unknown (KumoSQL gave no type) or WRONG (a different type or width); WRONG
must stay at 0 because the typer answers "unknown" whenever GoogleSQL's rules do not fix a type. The held-out split is
never scored here: only its case count is pinned, so a changed fixture shows up without anyone reading its cases.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "googlesql_types_eval", Path(__file__).parent.parent / "tools" / "googlesql_types_eval.py"
)
ev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ev  # dataclasses and typing look their module up here
SPEC.loader.exec_module(ev)

# BigQuery-typed dev columns whose type KumoSQL gives exactly. Raise it with the score, never lower it.
FLOOR_EXACT = 11945
# Cases in each split: 285 compliance files, a file held out when sha256(stem) % 4 == 0 (see the eval's SPLIT_RULE).
DEV_CASES = 7878
HELDOUT_CASES = 2602


@pytest.fixture(scope="module")
def dev_counts():
    return ev.score(ev.load(ev.DEV))


def test_dev_split_has_no_wrong_column(dev_counts):
    assert dev_counts.get("wrong", 0) == 0
    assert dev_counts.get("bigquery wrong", 0) == 0


def test_dev_split_never_crashes_the_typer(dev_counts):
    assert dev_counts.get("crashed queries", 0) == 0
    assert dev_counts["queries"] == DEV_CASES


def test_dev_split_exact_floor(dev_counts):
    exact = dev_counts.get("bigquery exact", 0)
    assert exact >= FLOOR_EXACT, f"{exact} BigQuery columns exact, floor {FLOOR_EXACT}"
    # a column is exact, unknown or wrong, so the three account for every labelled one
    labelled = dev_counts["labelled columns"]
    assert dev_counts.get("exact", 0) + dev_counts.get("unknown", 0) + dev_counts.get("wrong", 0) == labelled
    assert dev_counts["bigquery columns"] <= labelled


def test_split_rule_and_case_counts_are_unchanged():
    """Only counts are read from the held-out file, never its cases."""

    dev = ev.load(ev.DEV)
    assert len(dev["cases"]) == DEV_CASES
    assert all(not ev.held_out(case["file"]) for case in dev["cases"])
    assert len(ev.load(ev.HELDOUT)["cases"]) == HELDOUT_CASES
