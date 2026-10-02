"""View mining and the MV-benchmark workloads: zero wrong rewrites, rewrite-coverage floors.

The unit tests run on a tiny inline schema. The workload tests read the edx-h MV benchmark files, which are
downloaded on first use (see tools/mv_workload_bench.py) and never stored in the repository; they skip when the
download is not possible. ``FLOORS`` only ever go up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql import view_candidates as vc  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402

_path = Path(__file__).resolve().parent.parent / "tools" / "mv_workload_bench.py"
_spec = importlib.util.spec_from_file_location("mv_workload_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["mv_workload_bench"] = bench
_spec.loader.exec_module(bench)

SCHEMA = {"t": ["id", "x", "y"], "u": ["id", "t_id", "z"], "w": ["id", "u_id", "v"]}
QUERIES = {
    "q1": "SELECT MIN(t.x) FROM t, u WHERE t.id = u.t_id AND u.z > 5",
    "q2": "SELECT MIN(a.y) FROM t AS a, u AS b WHERE b.t_id = a.id AND a.x = 1",
    "q3": "SELECT MIN(t.x) FROM t, u, w WHERE t.id = u.t_id AND u.id = w.u_id AND w.v = 'k'",
    "q4": "SELECT MIN(w.v) FROM w, u WHERE u.id = w.u_id",
}


def test_shapes_do_not_depend_on_aliases():
    shapes = [vc.shapes_of(vc.graph_of(QUERIES[q], SCHEMA)) for q in ("q1", "q2")]
    assert set(shapes[0]) == set(shapes[1]) and shapes[0]


def test_mining_counts_the_queries_that_share_a_join():
    candidates = {c.shape.tables: c for c in vc.mine(QUERIES, SCHEMA)}
    assert candidates[("t", "u")].queries == {"q1", "q2", "q3"}
    assert candidates[("u", "w")].queries == {"q3", "q4"}
    assert ("t", "u", "w") not in candidates  # only q3 joins all three: below the support threshold


def test_selection_prefers_the_view_that_saves_more_joins():
    chosen = vc.select(vc.mine(QUERIES, SCHEMA), budget=1)
    assert [c.shape.tables for c in chosen] == [("t", "u")]


def test_a_mined_view_answers_the_queries_that_share_it():
    view = {c.shape.tables: c for c in vc.mine(QUERIES, SCHEMA)}[("t", "u")].sql()
    for name in ("q1", "q2"):
        reuse = rewrite_over_model(QUERIES[name], view, schema=SCHEMA)
        assert reuse.rewritten, (name, reuse.reason)
        assert "mv0" in reuse.sql


def test_a_view_that_lacks_a_column_is_not_used():
    view = "SELECT t.id AS t_id, u.t_id AS u_t_id FROM t, u WHERE t.id = u.t_id"
    assert not rewrite_over_model(QUERIES["q1"], view, schema=SCHEMA).rewritten  # q1 reads u.z and t.x


FLOORS = {"job_sample_rewritten": 20, "sample": 24}


def _run(tracks, sample=FLOORS["sample"]):
    try:
        return bench.run_workload("job", None, sample, 6, 1, False, tracks)
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")


def test_job_sample_rewrites_are_proven_verified_and_never_wrong():
    report = _run(["mined", "poisoned"])
    mined, poisoned = report["tracks"]["mined"]["all"], report["tracks"]["poisoned"]["all"]
    assert mined["wrong"] == 0 and mined["unchecked"] == 0, mined
    assert mined["rewritten"] >= FLOORS["job_sample_rewritten"], mined
    assert poisoned["rewritten"] == 0 and poisoned["wrong"] == 0, poisoned  # no query can be answered from an empty view


@pytest.mark.slow
def test_job_full_workload():
    report = bench.run_workload("job", None, None, bench.BUDGET, 2, False, ["mined", "given", "poisoned"])
    for track, summary in report["tracks"].items():
        assert summary["all"]["wrong"] == 0, (track, summary["all"])
    assert report["tracks"]["mined"]["all"]["rewritten"] >= 108
    assert report["tracks"]["mined"]["held_out"]["rewritten"] >= 12
    assert report["tracks"]["given"]["all"]["rewritten"] >= 90
