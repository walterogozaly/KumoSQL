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
from kumosql import view_generalize as vg  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402
from kumosql.random_check import find_difference  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

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


# -- LEFT JOIN generalization of mined views ------------------------------------------------------

CONSTRAINTS = {
    "t": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),)),
    "u": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),)),
    "w": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),)),
}


def _general(queries=QUERIES, constraints=CONSTRAINTS):
    graphs = {q: vc.graph_of(sql, SCHEMA) for q, sql in queries.items()}
    return vg.generalize(vc.mine(queries, SCHEMA), constraints, graphs), graphs


def test_a_keyed_table_becomes_an_extra_of_the_core():
    views, _ = _general()
    by_core = {v.core.tables: v for v in views}
    view = by_core[("u",)]  # t.id is a key: u LEFT JOIN t ON t.id = u.t_id
    assert [e.table for e in view.extras] == ["t"] and view.extras[0].key == "id" and view.extras[0].via_column == "t_id"
    assert "LEFT JOIN t AS t1 ON t1.id = t0.t_id" in view.sql()
    assert ("u", "w") in by_core or ("w",) in by_core  # w.u_id is not unique, so w is never stripped as an extra of u


def test_no_declared_key_means_no_generalization():
    views, _ = _general(constraints={})
    assert all(not v.extras for v in views)


def test_slices_list_the_core_alone_and_with_the_extra():
    view = {v.core.tables: v for v in _general()[0]}[("u",)]
    def graph(sql):
        return vc.graph_of(sql, SCHEMA)

    assert view.slices_for(graph("SELECT MIN(a.z) FROM u AS a")) == [frozenset()]
    assert view.slices_for(graph("SELECT MIN(a.z) FROM u AS a, t AS b WHERE b.id = a.t_id")) == [frozenset({0}), frozenset()]
    assert view.slices_for(graph("SELECT MIN(b.x) FROM t AS b")) == []
    # both tables are in the query but it does not join them on the view's columns: only the core fits
    assert view.slices_for(graph("SELECT MIN(a.z) FROM u AS a, t AS b WHERE b.id = a.id")) == [frozenset()]


def test_the_prover_shows_a_slice_equals_the_stored_view_only_under_the_key():
    view = {v.core.tables: v for v in _general()[0]}[("u",)]
    for chosen in (frozenset(), frozenset({0})):
        assert view.lemma(chosen, SCHEMA, CONSTRAINTS)
    assert not view.lemma(frozenset(), SCHEMA, {})  # without the key the LEFT JOIN may repeat rows


def _slice_schema():
    int_column = lambda name, null=False: bench.Column(name, "int", null)  # noqa: E731
    return bench.Schema(
        [
            bench.Table("t", [int_column("id", True), int_column("x"), int_column("y")], [("id",)]),
            bench.Table("u", [int_column("id", True), int_column("t_id"), int_column("z")], [("id",)]),
        ]
    )


_SLICE_QUERIES = {
    "a": "SELECT COUNT(*) FROM u, t WHERE t.id = u.t_id AND u.z > 3",
    "b": "SELECT COUNT(*) FROM u, t WHERE t.id = u.t_id AND t.x = 1",
    "c": "SELECT COUNT(*) FROM u WHERE u.z < 7",
}


def _slice_view(schema, cons):
    graphs = {q: vc.graph_of(sql, schema.columns) for q, sql in _SLICE_QUERIES.items()}
    return {v.core.tables: v for v in vg.generalize(vc.mine(_SLICE_QUERIES, schema.columns), cons, graphs)}[("u",)]


def test_a_query_over_the_core_alone_reads_the_general_view_and_is_verified():
    schema = _slice_schema()
    cons = bench.constraints_of(schema)
    record = bench.view_record(_slice_view(schema, cons), "view0")
    for name, chosen in (("a", frozenset({0})), ("b", frozenset({0})), ("c", frozenset())):
        reuse = bench.rewrite_over_slice(_SLICE_QUERIES[name], record, chosen, schema, cons)
        assert reuse.rewritten, (name, reuse.reason)
        assert find_difference(schema, reuse.query_sql, reuse.inlined_sql, mode="bag", trials=20) is None
    # the joined query needs the presence test on t
    assert "IS NOT NULL" in bench.rewrite_over_slice(_SLICE_QUERIES["a"], record, frozenset({0}), schema, cons).inlined_sql


def test_a_general_view_with_a_contradiction_answers_nothing():
    schema = _slice_schema()
    cons = bench.constraints_of(schema)
    record = bench.poisoned(bench.view_record(_slice_view(schema, cons), "view0"))
    for name in _SLICE_QUERIES:
        assert not bench.rewrite_over_slice(_SLICE_QUERIES[name], record, frozenset(), schema, cons).rewritten


# These workload floors score development queries only; held-out queries are reserved for the final workstream run.
FLOORS = {"scale_dev_sample_rewritten": 16, "sample": 24}


def _run(tracks, sample=FLOORS["sample"]):
    try:
        # The development sample exercises LEFT JOIN generalization without reading held-out query results.
        return bench.run_workload("scale", None, sample, 12, 1, False, tracks, dev_only=True)
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")


def test_scale_dev_sample_rewrites_are_proven_verified_and_never_wrong():
    report = _run(["mined", "poisoned"])
    mined, poisoned = report["tracks"]["mined"]["dev"], report["tracks"]["poisoned"]["dev"]
    assert mined["queries"] == report["tracks"]["mined"]["all"]["queries"]
    assert report["tracks"]["mined"]["held_out"]["queries"] == 0
    assert mined["wrong"] == 0 and mined["unchecked"] == 0, mined
    assert mined["rewritten"] >= FLOORS["scale_dev_sample_rewritten"], mined
    assert poisoned["rewritten"] == 0 and poisoned["wrong"] == 0, poisoned  # no query can be answered from an empty view


@pytest.mark.slow
def test_job_full_workload():
    report = bench.run_workload("job", None, None, bench.BUDGET, 2, False, ["mined", "given", "poisoned"], dev_only=True)
    for track, summary in report["tracks"].items():
        assert summary["dev"]["wrong"] == 0, (track, summary["dev"])
    assert report["tracks"]["mined"]["dev"]["rewritten"] >= 96
    assert report["tracks"]["given"]["dev"]["rewritten"] >= 77
