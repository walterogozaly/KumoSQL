"""BigQuery ``UNION ... BY NAME`` / ``CORRESPONDING`` match columns by name; lineage and the provers must too."""

import pytest

from kumosql import ColumnRef, load_compiled_graph
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.set_operations import positionalize
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt
import sqlglot

SCHEMA = {
    "p.raw.src_a": {"k": "INT64", "x": "STRING", "y": "STRING"},
    "p.raw.src_b": {"k": "INT64", "y": "STRING"},
}
A, B = "`p.raw.src_a`", "`p.raw.src_b`"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def trace(sql, schema=SCHEMA):
    graph = {"tables": [{"target": {"database": "p", "schema": "m", "name": "t"}, "query": sql}]}
    pipeline = load_compiled_graph(graph, source_schema=schema)
    records = pipeline.explain_lineage()
    return {ref.column: rec for ref, rec in records.items()}, pipeline


def leaves(rec):
    return {str(s) for s in rec.sources}


def test_branches_are_matched_by_name_not_position():
    records, _ = trace(
        f"SELECT k, x, y FROM {A} UNION ALL BY NAME SELECT y, x, k FROM (SELECT k, y, 'b' AS x FROM {B})"
    )
    assert records["k"].status == "traced" and leaves(records["k"]) == {"p.raw.src_a.k", "p.raw.src_b.k"}
    assert leaves(records["y"]) == {"p.raw.src_a.y", "p.raw.src_b.y"}
    assert leaves(records["x"]) == {"p.raw.src_a.x"}


def test_full_outer_missing_column_is_null_not_another_column():
    records, _ = trace(f"SELECT k, x, y FROM {A} FULL OUTER UNION ALL BY NAME SELECT k, y FROM {B}")
    assert [r.status for r in records.values()] == ["traced"] * 3
    assert leaves(records["x"]) == {"p.raw.src_a.x"}  # the other branch contributes NULL
    assert leaves(records["y"]) == {"p.raw.src_a.y", "p.raw.src_b.y"}


def test_full_outer_adds_columns_only_the_right_branch_has():
    records, _ = trace(f"SELECT k, y FROM {B} FULL OUTER UNION DISTINCT BY NAME SELECT x, k FROM {A}")
    assert list(records) == ["k", "y", "x"]
    assert leaves(records["x"]) == {"p.raw.src_a.x"}
    assert leaves(records["k"]) == {"p.raw.src_a.k", "p.raw.src_b.k"}


def test_left_outer_keeps_left_columns_but_still_reads_the_dropped_ones():
    records, pipeline = trace(f"SELECT k, x FROM {A} LEFT OUTER UNION ALL BY NAME SELECT y, k, x FROM {A}")
    assert list(records) == ["k", "x"]
    assert {c.column for c in pipeline.consumed_columns()["p.m.t"]} == {"k", "x", "y"}


def test_inner_corresponding_keeps_common_columns_only():
    records, _ = trace(f"SELECT k, x, y FROM {A} INNER UNION ALL CORRESPONDING SELECT y, k FROM {B}")
    assert list(records) == ["k", "y"]
    assert leaves(records["k"]) == {"p.raw.src_a.k", "p.raw.src_b.k"}


def test_strict_corresponding_and_star_expansion_follow_names():
    records, _ = trace(f"SELECT * FROM {B} UNION ALL STRICT CORRESPONDING SELECT y, k FROM {A}")
    assert leaves(records["k"]) == {"p.raw.src_a.k", "p.raw.src_b.k"}
    assert leaves(records["y"]) == {"p.raw.src_a.y", "p.raw.src_b.y"}


def test_chained_by_name_operations():
    records, _ = trace(
        f"SELECT k FROM {B} FULL OUTER UNION ALL BY NAME SELECT x, k FROM {A} FULL OUTER UNION ALL BY NAME SELECT y FROM {B}"
    )
    assert list(records) == ["k", "x", "y"]
    assert leaves(records["y"]) == {"p.raw.src_b.y"}
    assert leaves(records["x"]) == {"p.raw.src_a.x"}


def test_plain_by_name_with_different_columns_is_unknown_not_traced():
    records, _ = trace(f"SELECT k, x FROM {A} UNION ALL BY NAME SELECT k, y FROM {B}")
    assert {r.status for r in records.values()} == {"unknown"}
    assert {r.reason for r in records.values()} == {"by_name_set_operation"}


def test_unexpanded_star_in_a_by_name_branch_is_unknown():
    records, _ = trace(f"SELECT k FROM {A} FULL OUTER UNION ALL BY NAME SELECT * FROM `p.raw.nowhere`")
    assert all(r.status == "unknown" for r in records.values())


def test_positional_union_is_unchanged():
    records, _ = trace(f"SELECT k, y FROM {B} UNION ALL SELECT k, y FROM {A}")
    assert leaves(records["k"]) == {"p.raw.src_a.k", "p.raw.src_b.k"}


def test_positionalize_leaves_what_it_cannot_resolve_and_says_so():
    tree, problems = positionalize(sqlglot.parse_one("SELECT a FROM t UNION ALL BY NAME SELECT * FROM u", read="bigquery"))
    assert problems and tree.args.get("by_name")


def test_provers_read_by_name_unions_by_name():
    by_name = "SELECT a, b FROM t UNION ALL BY NAME SELECT b, a FROM u"
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        assert prove(by_name, "SELECT a, b FROM t UNION ALL SELECT a, b FROM u").status is SmtStatus.PROVEN_EQUIVALENT
        assert prove(by_name, "SELECT a, b FROM t UNION ALL SELECT b, a FROM u").status is not SmtStatus.PROVEN_EQUIVALENT
        unknown = prove("SELECT a FROM t UNION ALL BY NAME SELECT * FROM u", "SELECT a FROM t UNION ALL SELECT a FROM u")
        assert unknown.status is SmtStatus.NOT_PROVEN


def test_table_profile_does_not_pair_by_name_columns_by_position():
    from kumosql.table_profile import profile_pipeline

    graph = {"tables": [{"target": {"database": "p", "schema": "m", "name": "t"},
                         "query": f"SELECT k, y FROM {B} FULL OUTER UNION ALL BY NAME SELECT y, k FROM {B}"}]}
    pipeline = load_compiled_graph(graph, source_schema=SCHEMA)
    text = repr(profile_pipeline(pipeline)["p.m.t"])
    assert "union(" not in text


def _chain():
    def t(name, query):
        return {"target": {"database": "p", "schema": "m", "name": name}, "query": query}

    graph = {"tables": [
        t("u", f"SELECT k, x, y FROM {A} FULL OUTER UNION ALL BY NAME SELECT y, k FROM {B}"),
        t("swapped", f"SELECT k, x, y FROM {A} UNION ALL SELECT k, y, y FROM {B}"),
        t("v", "SELECT k, x FROM `p.m.u`"),
        t("w", "SELECT x AS xx FROM `p.m.v`"),
    ]}
    return load_compiled_graph(graph, source_schema=SCHEMA)


def test_impact_through_a_by_name_model_follows_names():
    from kumosql.impact import assess_change

    pipeline = _chain()
    # b has no x, so dropping b.k reaches u and what reads k; nothing reads b.y through v's x.
    assert {a.model for a in assess_change(pipeline, "change_expression", "p.raw.src_b", "k").affected} >= {"p.m.u", "p.m.v"}
    assert "p.m.w" not in {a.model for a in assess_change(pipeline, "change_expression", "p.raw.src_b", "k").affected}
    assert {a.model for a in assess_change(pipeline, "change_expression", "p.raw.src_a", "x").affected} >= {"p.m.u", "p.m.v", "p.m.w"}


def test_by_name_models_never_get_a_meaning_that_could_match_another_model():
    from kumosql.overlap import find_overlaps
    from kumosql.rollups import find_rollups
    from kumosql.table_profile import profile_pipeline

    pipeline = _chain()
    meanings = {a.column: a for a in profile_pipeline(pipeline)["p.m.u"].attributes}
    assert all(a.status == "unknown" and a.meaning is None for a in meanings.values())
    for match in find_overlaps(pipeline, model="p.m.u").matches:
        assert match.table != "p.m.swapped" or match.kind != "same_meaning"
    assert not [r for r in find_rollups(pipeline, model="p.m.u").rollups if r.derivability == "derivable_exact"]
