"""Column lineage and change impact never confidently miss a dependency (docs/pipeline-analysis.md).

The parametrized cases come from an external audit (``tests/fixtures/lineage_s013/cases.json``):
each output column must trace to exactly the expected sources (or say unknown, where allowed), the
model must read every expected column, and dropping or changing any of them must reach the model and
the models downstream of it, or list them as unknown.
"""

import json
from pathlib import Path

import pytest

from kumosql.graph import build_query_graph
from kumosql.pipeline import Pipeline
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.pipeline_types import ColumnRef, Model, Target

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lineage_s013" / "cases.json").read_text())
SCHEMAS = FIXTURE["schemas"]
CASES = {case["id"]: case for case in FIXTURE["cases"]}


def build(case: dict, root: Path) -> tuple[Pipeline, str, str | None]:
    """The case's pipeline, the key of the model under test, and the key of its downstream reader (if any)."""

    if case.get("dataform"):
        definitions = root / "definitions"
        definitions.mkdir()
        (root / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
        if case.get("modeled_parent"):
            (definitions / "raw.sqlx").write_text('config { type: "declaration" }\n')
            (definitions / "src.sqlx").write_text('config { type: "table" }\nSELECT k, v, w, g, flag FROM ${ref("raw")}\n')
            schema = {"p.d.raw": dict(SCHEMAS["src"])}
        else:
            (definitions / "src.sqlx").write_text('config { type: "declaration" }\n')
            schema = {"p.d.src": dict(SCHEMAS["src"])}
        (definitions / "model.sqlx").write_text('config { type: "incremental" }\n' + case["sql"] + "\n")
        final = None
        if case.get("chain"):
            (definitions / "final.sqlx").write_text('config { type: "table" }\nSELECT `out` FROM ${ref("model")}\n')
            final = "p.d.final"
        return load_sqlx_project(root, source_schema=schema), "p.d.model", final
    key = case.get("model", "model")
    schema = {name: dict(columns) for name, columns in SCHEMAS.items()}
    schema.update(case.get("extra_schema", {}))
    models = {key: Model(Target(name=key), "table", case["sql"])}
    final = None
    if case.get("chain"):
        models["final"] = Model(Target(name="final"), "table", case.get("chain_query", "SELECT `out` FROM `model`"))
        final = "final"
    sources = {}
    for name in schema:
        if name not in models:
            parts = name.split(".")
            target = Target(*([""] * (3 - len(parts)) + parts))
            sources[target.key] = target
    return Pipeline(models, sources, schema), key, final


def split(ref: str) -> tuple[str, str]:
    table, _, column = ref.rpartition(".")
    return table, column


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_lineage_and_impact_never_confidently_miss(case_id, tmp_path):
    case = CASES[case_id]
    pl, key, final = build(case, tmp_path)
    tolerant = case.get("allow_unknown") or case.get("require_unknown")
    records = {ref.column: record for ref, record in pl.explain_lineage().items() if ref.table == key}
    for column, expected in case["expected_edges"].items():
        record = records.get(column)
        if record is None or record.status == "unknown":
            assert tolerant, f"{column} is not traced"
            continue
        assert {str(source) for source in record.sources} == set(expected), column
    for column, record in records.items():
        assert column in case["expected_edges"] or record.status == "unknown", f"unexpected output {column}"

    consumed = {str(ref) for ref in pl.consumed_columns().get(key, ())}
    unknown_reader = False
    probes = sorted(set(case["expected_consumed"]) | set(case.get("probes", [])))
    if case.get("require_unknown"):
        probes = ["src.v"]
    for probe in probes:
        table, column = split(probe)
        expect = {key}
        if final and probe in case.get("chain_refs", [probe]):
            expect.add(final)
        for kind in ("drop_column", "change_expression"):
            impact = pl.assess_change(kind, table, column)
            reached = {a.model for a in impact.affected}
            unknown = {u.model for u in impact.unknown}
            assert expect <= reached | unknown, (kind, probe)
            if expect & unknown:
                unknown_reader = True
                assert not impact.complete
        if probe in case["expected_consumed"] and probe not in consumed:
            assert tolerant, f"{probe} is not read"
    if case.get("require_unknown"):
        assert unknown_reader


def test_the_audited_kinds_of_effect():
    # The opposite side of a USING join, an unselected STRUCT field and an EXISTS select list are read but feed no value.
    expected = {
        "C21": ("other.k", "behavior_may_change"),
        "C24": ("src.w", "behavior_may_change"),
        "C34": ("other.k", "behavior_may_change"),
        "C35": ("src.k", "behavior_may_change"),
        "C41": ("other.v", "behavior_may_change"),
        "C37": ("src.k", "values_change"),
    }
    for case_id, (probe, effect) in expected.items():
        pl, key, _ = build(CASES[case_id], Path("."))
        impact = pl.assess_change("change_expression", *split(probe))
        assert [(a.model, a.effect) for a in impact.affected if a.model == key] == [(key, effect)], case_id


def pipeline(schema: dict | None = None, **queries: str) -> Pipeline:
    models = {name: Model(Target(name=name), "table", sql) for name, sql in queries.items()}
    sources = {name: Target(name=name) for name in ("src", "other")}
    return Pipeline(models, sources, {"src": dict(SCHEMAS["src"]), "other": dict(SCHEMAS["other"]), **(schema or {})})


def test_an_insert_into_a_table_with_unknown_columns_is_unknown_not_renamed():
    pl = pipeline(dst="INSERT INTO dst SELECT w, v FROM src", final="SELECT a FROM dst")
    assert pl.output_columns("dst") == ("*",)
    assert pl.explain_lineage()[ColumnRef("dst", "*")].reason == "insert_target_columns"
    for kind in ("drop_column", "change_expression"):
        impact = pl.assess_change(kind, "src", "w")
        assert {u.model: u.reason for u in impact.unknown} == {
            "dst": "insert_target_columns",
            "final": "downstream_of_unknown_reader",
        }
        assert not impact.complete


def test_an_insert_with_no_column_list_takes_the_target_columns_in_order():
    pl = pipeline({"dst": {"a": "INT64", "b": "INT64"}}, dst="INSERT INTO dst SELECT * EXCEPT (w) FROM other")
    assert pl.column_lineage()[ColumnRef("dst", "b")] == {ColumnRef("other", "v")}
    # Three SELECT columns for two target columns: unknown, not guessed.
    pl = pipeline({"dst": {"a": "INT64", "b": "INT64"}}, dst="INSERT INTO dst SELECT * FROM other")
    assert pl.output_columns("dst") == ("*",)


def test_a_change_to_a_filter_column_reaches_every_downstream_model():
    pl = pipeline(model="SELECT v AS out FROM src WHERE flag", mid="SELECT `out` FROM `model`", last="SELECT COUNT(*) AS n FROM mid")
    affected = {a.model: (a.effect, a.via) for a in pl.assess_change("change_expression", "src", "flag").affected}
    assert affected == {
        "model": ("behavior_may_change", "condition_only"),
        "mid": ("behavior_may_change", "model_dependency"),
        "last": ("behavior_may_change", "model_dependency"),
    }


def test_a_column_that_is_both_a_value_and_a_filter_changes_the_rows_too():
    pl = pipeline(
        model="WITH c AS (SELECT k, v FROM src) SELECT k, v FROM c WHERE k > 0",
        only_v="SELECT v FROM model",
    )
    affected = {a.model for a in pl.assess_change("change_expression", "src", "k").affected}
    assert affected == {"model", "only_v"}
    # A value-only change does not reach a reader of other columns.
    assert {a.model for a in pl.assess_change("change_expression", "src", "v").affected} == {"model", "only_v"}
    pl = pipeline(model="SELECT k, v FROM src", only_v="SELECT v FROM model")
    assert {a.model for a in pl.assess_change("change_expression", "src", "k").affected} == {"model"}


def test_group_by_all_keys_change_the_rows():
    pl = pipeline(model="SELECT g, SUM(v) AS total FROM src GROUP BY ALL", totals="SELECT total FROM model")
    assert {a.model for a in pl.assess_change("change_expression", "src", "g").affected} == {"model", "totals"}


def test_a_nested_subquery_predicate_is_unknown_not_constant():
    pl = pipeline(m="SELECT EXISTS(SELECT 1 FROM other o WHERE o.k IN (SELECT k FROM src)) AS e FROM src")
    record = pl.explain_lineage()[ColumnRef("m", "e")]
    assert (record.status, record.reason) == ("unknown", "subquery_predicate")


def test_a_correlated_count_is_not_constant():
    pl = pipeline(m="SELECT (SELECT COUNT(*) FROM other o WHERE o.k = s.k) AS n FROM src s")
    assert pl.column_lineage()[ColumnRef("m", "n")] == {ColumnRef("other", "k"), ColumnRef("src", "k")}


def test_no_lateral_alias_over_a_table_with_unknown_columns():
    # Some schema is known (src), so sqlglot would treat t as having no columns and read x as the alias.
    pl = pipeline(m="SELECT a AS x, x + 1 AS y FROM t WHERE x > 0")
    assert pl.column_lineage()[ColumnRef("m", "y")] == {ColumnRef("t", "x")}
    assert pl.consumed_columns()["m"] == {ColumnRef("t", "a"), ColumnRef("t", "x")}


def test_template_branches_over_a_source_with_unknown_columns_are_unknown(tmp_path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (definitions / "src.sqlx").write_text('config { type: "declaration" }\n')
    (definitions / "model.sqlx").write_text(
        'config { type: "incremental" }\nSELECT v AS out FROM ${ref("src")} WHERE ${when(incremental(), `w > 0`, `flag`)}\n'
    )
    pl = load_sqlx_project(tmp_path)
    impact = pl.assess_change("drop_column", "p.d.src", "w")
    assert {u.model: u.reason for u in impact.unknown} == {"p.d.model": "template_columns"}
    assert not impact.complete
    assert "p.d.src" not in pl.dead_columns()


def test_a_wildcard_reader_is_in_the_graph_of_each_known_table_it_matches():
    shard = Target("p", "d", "events_20261002")
    pl = Pipeline(
        {"model": Model(Target(name="model"), "table", "SELECT v AS out FROM `p.d.events_*`")},
        {shard.key: shard, "p.d.other_1": Target("p", "d", "other_1")},
        {"p.d.events_*": {"v": "INT64"}, shard.key: {"v": "INT64"}},
    )
    assert pl.upstream["model"] == {shard.key}
    edges = {(e["upstream_id"], e["downstream_id"]) for e in build_query_graph(pl).to_json()["edges"]}
    assert ("table:p.d.events_20261002", "table:model") in edges
    assert {a.model for a in pl.assess_change("drop_table", shard.key).affected} == {"model"}
