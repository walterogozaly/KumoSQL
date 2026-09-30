import json

import pytest

from kumosql import load_compiled_graph
from kumosql.cli import pipeline_main
from kumosql.scopes import Scope

SCHEMA = {"p.raw.a": {"id": "INT64", "x": "INT64", "flag": "BOOL"}, "p.raw.b": {"id": "INT64", "y": "INT64"}}


def build(schema=SCHEMA, **queries):
    tables = [{"target": {"database": "p", "schema": "m", "name": n}, "query": q} for n, q in queries.items()]
    return load_compiled_graph({"tables": tables}, source_schema=schema)


def by_model(impact):
    return {a.model: a for a in impact.affected}


def test_drop_finds_output_filter_and_join_only_readers():
    pipeline = build(
        base="SELECT id, x, flag FROM `p.raw.a`",
        out_reader="SELECT id, x AS x2 FROM `p.m.base`",
        filter_reader="SELECT id FROM `p.m.base` WHERE x > 1",
        join_reader="SELECT b.id FROM `p.raw.b` AS b JOIN `p.m.base` AS t ON b.y = t.x",
        other="SELECT id FROM `p.m.base`",
        later="SELECT id FROM `p.m.filter_reader`",
    )
    impact = pipeline.assess_change("drop_column", "p.m.base", "X")
    found = by_model(impact)
    assert set(found) == {"p.m.out_reader", "p.m.filter_reader", "p.m.join_reader", "p.m.later"}
    assert found["p.m.out_reader"].via == "output_column"
    assert found["p.m.filter_reader"].via == "condition_only"
    assert found["p.m.join_reader"].via == "condition_only"
    assert found["p.m.filter_reader"].effect == "breaks"
    assert found["p.m.later"].effect == "indirect" and found["p.m.later"].depth == 2
    assert impact.unknown == []
    assert impact.safe_to_delete == "unknown"


def test_rename_matches_drop_and_expression_change_follows_lineage():
    pipeline = build(
        base="SELECT id, x, flag FROM `p.raw.a`",
        derived="SELECT id, x + 1 AS y FROM `p.m.base`",
        deeper="SELECT y * 2 AS z FROM `p.m.derived`",
        filtered="SELECT id FROM `p.m.derived` WHERE y > 3",
    )
    drop = pipeline.assess_change("drop_column", "p.m.base", "x")
    rename = pipeline.assess_change("rename_column", "p.m.base", "x")
    assert by_model(drop).keys() == by_model(rename).keys()
    change = by_model(pipeline.assess_change("change_expression", "p.m.base", "x"))
    assert change["p.m.derived"].effect == "values_change"
    assert change["p.m.deeper"].effect == "values_change"
    assert change["p.m.filtered"].effect == "behavior_may_change"
    assert all(a.effect != "breaks" for a in change.values())
    assert not pipeline.assess_change("change_expression", "p.m.base", "flag").affected


def test_star_reader_without_schema_is_unknown_with_its_readers():
    pipeline = build(
        schema={},
        star="SELECT * FROM `p.ext.t`",
        after="SELECT id FROM `p.m.star`",
        plain="SELECT x FROM `p.ext.t`",
    )
    impact = pipeline.assess_change("drop_column", "p.ext.t", "x")
    assert "p.m.plain" in by_model(impact)
    unknown = {u.model: u.reason for u in impact.unknown}
    assert unknown == {"p.m.star": "unexpanded_star", "p.m.after": "downstream_of_unknown_reader"}
    assert not impact.complete


def test_star_reader_with_schema_counts_as_reader():
    pipeline = build(base="SELECT id, x FROM `p.raw.a`", star="SELECT * FROM `p.m.base`")
    impact = pipeline.assess_change("drop_column", "p.m.base", "x")
    assert "p.m.star" in by_model(impact)
    assert impact.unknown == []


def test_unparseable_reader_is_unknown_not_dropped():
    pipeline = build(base="SELECT id, x FROM `p.raw.a`", broken="SELECT FROM WHERE ((")
    pipeline.models["p.m.broken"].declared_dependencies = (pipeline.models["p.m.base"].target,)
    impact = pipeline.assess_change("drop_column", "p.m.base", "x")
    assert "p.m.broken" in {u.model for u in impact.unknown}
    assert not impact.complete
    assert "unknown_readers" in impact.incomplete_reasons


def test_terminal_and_missing_targets_are_not_complete_or_safe():
    pipeline = build(base="SELECT id, x FROM `p.raw.a`", leaf="SELECT id FROM `p.m.base`")
    leaf = pipeline.assess_change("drop_table", "p.m.leaf")
    assert leaf.terminal and not leaf.complete and leaf.affected == []
    assert leaf.safe_to_delete == "unknown"
    missing = pipeline.assess_change("drop_column", "p.m.base", "nope")
    assert not missing.target_known and not missing.complete
    table = pipeline.assess_change("drop_table", "p.m.base")
    assert by_model(table).keys() == {"p.m.leaf"}


def test_cycle_terminates_and_is_incomplete():
    pipeline = build(a="SELECT x FROM `p.m.b`", b="SELECT x FROM `p.m.a`")
    impact = pipeline.assess_change("drop_column", "p.m.a", "x")
    assert "p.m.b" in by_model(impact)
    assert not impact.complete
    assert "analysis_gaps" in impact.incomplete_reasons
    assert pipeline.assess_change("change_expression", "p.m.a", "x").affected is not None


def test_scope_limits_listing_and_counts_the_rest():
    pipeline = build(
        base="SELECT id, x FROM `p.raw.a`",
        one="SELECT x FROM `p.m.base`",
        two="SELECT x FROM `p.m.base`",
    )
    scope = Scope(name="only one", fields={"name": ("one",)})
    impact = pipeline.assess_change("drop_column", "p.m.base", "x", scope=scope)
    assert [a.model for a in impact.affected] == ["p.m.one"]
    assert impact.out_of_scope == 1 and impact.scope == "only one"


def test_bad_arguments():
    pipeline = build(base="SELECT id FROM `p.raw.a`")
    with pytest.raises(ValueError):
        pipeline.assess_change("explode", "p.m.base")
    with pytest.raises(ValueError):
        pipeline.assess_change("drop_column", "p.m.base")


def test_cli_assess(tmp_path, capsys):
    graph = {"tables": [
        {"target": {"database": "p", "schema": "m", "name": "base"}, "query": "SELECT id, x FROM `p.raw.a`"},
        {"target": {"database": "p", "schema": "m", "name": "r"}, "query": "SELECT x FROM `p.m.base`"},
    ]}
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    args = [str(path), "--source-schema", str(schema), "--assess", "drop_column", "--target", "p.m.base.x"]
    assert pipeline_main(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["affected"][0]["model"] == "p.m.r"
    assert data["safe_to_delete"] == "unknown"
