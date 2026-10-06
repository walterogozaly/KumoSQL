"""Field-level lineage and impact for STRUCT columns (docs/pipeline-analysis.md, docs/evals/lineage-goldens-bench.md)."""

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target, path_overlap

SCHEMA = {
    "raw": {
        "k": "INT64",
        "a": "STRUCT<b STRUCT<c INT64, e INT64>, d INT64>",
        "arr": "ARRAY<STRUCT<f INT64>>",
    }
}


def build(**queries: str) -> Pipeline:
    models = {name: Model(Target(name=name), "table", sql) for name, sql in queries.items()}
    return Pipeline(models, {"raw": Target(name="raw")}, {name: dict(columns) for name, columns in SCHEMA.items()})


def sources(pipeline: Pipeline, model: str, column: str) -> set[str]:
    return {str(s) for s in pipeline.explain_lineage()[ColumnRef(model, column)].sources}


def reached(pipeline: Pipeline, kind: str, column: str) -> dict[str, str]:
    impact = pipeline.assess_change(kind, "raw", column)
    return {m.model: m.effect for m in impact.affected}


def test_column_ref_paths():
    ref = ColumnRef("t", "a", ("b", "c"))
    assert str(ref) == "t.a.b.c" and ref.dotted == "a.b.c" and ref.root == ColumnRef("t", "a")
    assert ColumnRef("t", "a").root is not None and str(ColumnRef("t", "a")) == "t.a"
    assert ref != ColumnRef("t", "a") and sorted([ref, ColumnRef("t", "a")])[0] == ColumnRef("t", "a")
    assert path_overlap((), ("b",)) and path_overlap(("b",), ("B", "c")) and not path_overlap(("b",), ("d",))
    assert ref.overlaps(ColumnRef("t", "A", ("b",))) and not ref.overlaps(ColumnRef("u", "a", ("b",)))


def test_a_field_read_traces_to_that_field_only():
    p = build(m="SELECT a.b.c AS c, a.d AS d, a.b.e + a.b.c AS both, a AS whole FROM raw")
    assert sources(p, "m", "c") == {"raw.a.b.c"}
    assert sources(p, "m", "d") == {"raw.a.d"}
    assert sources(p, "m", "both") == {"raw.a.b.c", "raw.a.b.e"}
    assert sources(p, "m", "whole") == {"raw.a"}
    assert {str(r) for r in p.consumed_columns()["m"]} == {"raw.a", "raw.a.b.c", "raw.a.b.e", "raw.a.d"}


def test_a_qualified_field_read_and_a_field_read_in_a_cte():
    p = build(m="WITH c AS (SELECT t.a.b.c AS x, t.a.d AS y FROM raw AS t) SELECT x, y FROM c")
    assert sources(p, "m", "x") == {"raw.a.b.c"} and sources(p, "m", "y") == {"raw.a.d"}


def test_a_read_the_lineage_cannot_resolve_stays_at_the_root_column():
    # A subscript ends the plain name chain, so the field is not guessed.
    p = build(m="SELECT arr[OFFSET(0)].f AS f, a.b AS whole_b FROM raw")
    assert sources(p, "m", "f") == {"raw.arr"}
    assert sources(p, "m", "whole_b") == {"raw.a.b"}


def test_an_unknown_root_column_is_still_unknown():
    p = build(m="SELECT nope.b AS x FROM raw")
    record = p.explain_lineage()[ColumnRef("m", "x")]
    assert record.status == "unknown" or not any(s.column == "nope" for s in record.sources)


def test_a_field_follows_a_plain_copy_of_the_struct_through_a_model():
    p = build(m1="SELECT a FROM raw", m2="SELECT a.b.c AS c, a.d AS d FROM m1", m3="SELECT c FROM m2")
    assert sources(p, "m2", "c") == {"m1.a.b.c"}
    trace = p.trace_column(ColumnRef("m3", "c"))
    assert trace.complete and trace.sources == {ColumnRef("raw", "a", ("b", "c"))}
    assert p.upstream_columns(ColumnRef("m2", "d")) == {ColumnRef("m1", "a", ("d",)), ColumnRef("raw", "a", ("d",))}


def test_a_field_does_not_narrow_through_a_changed_struct():
    p = build(m1="SELECT STRUCT(a.d AS d, k AS k) AS a FROM raw", m2="SELECT a.d AS d FROM m1")
    trace = p.trace_column(ColumnRef("m2", "d"))
    assert trace.complete and ColumnRef("raw", "a", ("d",)) in trace.sources


def test_impact_matches_by_field_prefix():
    p = build(
        reads_b="SELECT a.b AS x FROM raw",
        reads_bc="SELECT a.b.c AS x FROM raw",
        reads_whole="SELECT a AS x FROM raw",
        reads_d="SELECT a.d AS x FROM raw",
        reads_k="SELECT k FROM raw",
    )
    for kind in ("drop_column", "change_expression"):
        hit = reached(p, kind, "a.b")
        assert set(hit) == {"reads_b", "reads_bc", "reads_whole"}, (kind, hit)
        assert set(reached(p, kind, "a.b.c")) == {"reads_b", "reads_bc", "reads_whole"}
        assert set(reached(p, kind, "a.d")) == {"reads_d", "reads_whole"}
        assert set(reached(p, kind, "a")) == {"reads_b", "reads_bc", "reads_whole", "reads_d"}


def test_impact_of_a_field_through_a_filter_and_a_downstream_model():
    p = build(
        m1="SELECT a FROM raw",
        keeps_b="SELECT a.b.c AS c FROM m1",
        keeps_d="SELECT a.d AS d FROM m1",
        filtered="SELECT k FROM raw WHERE a.d > 1",
    )
    hit = reached(p, "change_expression", "a.b.c")
    assert hit.get("m1") == "values_change" and hit.get("keeps_b") == "values_change"
    assert "keeps_d" not in hit and "filtered" not in hit
    assert reached(p, "change_expression", "a.d")["filtered"] == "behavior_may_change"


def test_a_dotted_column_that_is_a_stored_name_stays_whole():
    p = Pipeline(
        {"m": Model(Target(name="m"), "table", "SELECT x FROM raw")},
        {"raw": Target(name="raw")},
        {"raw": {"x": "INT64", "odd.name": "INT64"}},
    )
    impact = p.assess_change("drop_column", "raw", "odd.name")
    assert impact.target_known and impact.affected == []
    assert {m.model for m in p.assess_change("drop_column", "raw", "x").affected} == {"m"}


@pytest.mark.parametrize("column", ["a", "a.b", "a.b.c"])
def test_downstream_columns_by_field(column):
    p = build(m="SELECT a.b.c AS c FROM raw")
    path = tuple(column.split(".")[1:])
    assert p.downstream_columns(ColumnRef("raw", "a", path)) == {ColumnRef("m", "c")}
    assert p.downstream_columns(ColumnRef("raw", "a", ("d",))) == set()  # a sibling field feeds nothing in m
