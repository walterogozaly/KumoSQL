"""Column lineage lists the conditions that limit which rows reach each column (docs/pipeline-analysis.md)."""

import pytest

from kumosql.live_graph import _share_row_filters
from kumosql.pipeline import Pipeline, _column_filters
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.pipeline_types import ColumnFilter, ColumnRef, Model, Target

SCHEMA = {
    "p.d.a": {"id": "INT64", "x": "STRING", "y": "STRING", "week": "INT64"},
    "p.d.b": {"id": "INT64", "week": "INT64", "flag": "BOOL"},
}


def pipeline(sql: str, **more: str) -> Pipeline:
    models = {"m": Model(Target(name="m"), "table", sql)}
    models.update({name: Model(Target(name=name), "table", text) for name, text in more.items()})
    sources = {name: Target(*name.split(".")) for name in SCHEMA}
    return Pipeline(models, sources, {name: dict(columns) for name, columns in SCHEMA.items()})


def filters(sql: str, column: str, **more: str) -> list[tuple[str, str, str, str]]:
    """``(kind, scope, effect, condition)`` of every filter on one output column of model ``m``."""

    found = pipeline(sql, **more).explain_lineage()[ColumnRef("m", column)].filters
    assert found is not None
    return [(item.kind, item.scope, item.effect, item.condition) for item in found]


def test_a_filter_on_a_query_that_reads_a_cte_reaches_the_cte_column():
    sql = """
        WITH c AS (SELECT id, COALESCE(x, y) AS product FROM p.d.a WHERE x IS NOT NULL OR y IS NOT NULL)
        SELECT c.product FROM c JOIN p.d.b AS b ON c.id = b.id WHERE b.week = EXTRACT(WEEK FROM CURRENT_DATE())
    """
    lineage = pipeline(sql).explain_lineage()[ColumnRef("m", "product")]
    assert lineage.sources == {ColumnRef("p.d.a", "x"), ColumnRef("p.d.a", "y")}
    assert [(f.kind, f.scope, f.effect) for f in lineage.filters] == [
        ("where", "main", "limits_rows"),
        ("join", "main", "limits_rows"),
        ("where", "c", "limits_rows"),
    ]
    week, join, inner = lineage.filters
    assert week.condition == "b.week = EXTRACT(WEEK FROM CURRENT_DATE)"
    assert week.columns == (ColumnRef("p.d.b", "week"),)
    assert join.columns == (ColumnRef("p.d.a", "id"), ColumnRef("p.d.b", "id"))
    assert inner.condition == "a.x IS NOT NULL OR a.y IS NOT NULL"
    assert inner.columns == (ColumnRef("p.d.a", "x"), ColumnRef("p.d.a", "y"))
    assert all(f.columns_complete for f in lineage.filters)


def test_a_query_without_conditions_has_an_empty_list_not_unknown():
    lineage = pipeline("SELECT x FROM p.d.a").explain_lineage()[ColumnRef("m", "x")]
    assert lineage.filters == ()
    assert "filters" in pipeline("SELECT x FROM p.d.a").lineage_report()[0]


def test_every_column_of_a_model_gets_the_same_row_filters():
    sql = "SELECT * FROM p.d.a WHERE week = 1"
    assert {tuple(filters(sql, name)) for name in ("id", "x", "y", "week")} == {
        (("where", "main", "limits_rows", "a.week = 1"),)
    }


def test_outer_joins_decide_matches_but_do_not_limit_the_preserved_side():
    left = "SELECT a.x, b.flag FROM p.d.a a LEFT JOIN p.d.b b ON a.id = b.id AND b.week = 3 WHERE a.week = 5"
    assert filters(left, "flag") == [
        ("where", "main", "limits_rows", "a.week = 5"),
        ("join", "main", "matches_only", "a.id = b.id AND b.week = 3"),
    ]
    right = "SELECT a.x, b.flag FROM p.d.a a RIGHT JOIN (SELECT * FROM p.d.b WHERE flag) b ON a.id = b.id"
    assert filters(right, "x") == [
        ("join", "main", "matches_only", "a.id = b.id"),
        ("where", "subquery b", "limits_rows", "b.flag"),
    ]


def test_a_filter_inside_the_null_supplying_input_does_not_limit_rows():
    sql = "WITH w AS (SELECT id FROM p.d.b WHERE week = 7) SELECT a.x FROM p.d.a a LEFT JOIN w ON a.id = w.id"
    assert filters(sql, "x") == [
        ("join", "main", "matches_only", "a.id = w.id"),
        ("where", "w", "matches_only", "b.week = 7"),
    ]


def test_inner_joined_input_filters_limit_rows_even_when_no_column_comes_from_it():
    sql = "WITH w AS (SELECT id FROM p.d.b WHERE week = 7) SELECT a.x FROM p.d.a a JOIN w ON a.id = w.id"
    assert ("where", "w", "limits_rows", "b.week = 7") in filters(sql, "x")


def test_set_operation_branches_keep_their_own_filters():
    union = "SELECT x AS v FROM p.d.a WHERE week = 1 UNION ALL SELECT y FROM p.d.a WHERE week = 2"
    assert filters(union, "v") == [
        ("where", "main branch 1", "limits_rows", "a.week = 1"),
        ("where", "main branch 2", "limits_rows", "a.week = 2"),
    ]
    minus = "SELECT x AS v FROM p.d.a WHERE week = 1 EXCEPT DISTINCT SELECT y FROM p.d.a WHERE week = 2"
    assert filters(minus, "v") == [
        ("where", "main branch 1", "limits_rows", "a.week = 1"),
        ("where", "main branch 2", "excludes_rows", "a.week = 2"),
    ]


def test_having_and_qualify_are_listed():
    sql = (
        "SELECT x, COUNT(*) AS n FROM p.d.a WHERE week > 1 GROUP BY x HAVING COUNT(*) > 2 "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY x) = 1"
    )
    assert [(kind, condition) for kind, _, _, condition in filters(sql, "x")] == [
        ("where", "a.week > 1"),
        ("having", "COUNT(*) > 2"),
        ("qualify", "ROW_NUMBER() OVER (ORDER BY a.x) = 1"),
    ]


def test_predicate_subqueries_are_part_of_the_condition_and_name_their_columns():
    exists = pipeline("SELECT a.x FROM p.d.a a WHERE EXISTS (SELECT 1 FROM p.d.b b WHERE b.id = a.id AND b.flag)")
    (found,) = exists.explain_lineage()[ColumnRef("m", "x")].filters
    assert found.condition == "EXISTS(SELECT 1 FROM p.d.b AS b WHERE b.id = a.id AND b.flag)"
    assert found.columns == (ColumnRef("p.d.a", "id"), ColumnRef("p.d.b", "flag"), ColumnRef("p.d.b", "id"))
    assert found.columns_complete


def test_a_scalar_subquery_filter_belongs_to_its_column_only():
    sql = "SELECT a.x, (SELECT MAX(b.week) FROM p.d.b b WHERE b.id = a.id AND b.flag) AS w FROM p.d.a a WHERE a.week = 1"
    assert filters(sql, "x") == [("where", "main", "limits_rows", "a.week = 1")]
    assert filters(sql, "w") == [
        ("where", "main", "limits_rows", "a.week = 1"),
        ("where", "subquery", "feeds_value", "b.id = a.id AND b.flag"),
    ]


def test_a_cte_read_twice_lists_its_filters_once():
    sql = "WITH c AS (SELECT id, x FROM p.d.a WHERE week = 1) SELECT c1.x FROM c c1 JOIN c c2 ON c1.id = c2.id"
    assert filters(sql, "x") == [
        ("join", "main", "limits_rows", "c1.id = c2.id"),
        ("where", "c", "limits_rows", "a.week = 1"),
    ]


def test_trace_column_collects_the_filters_of_every_model_upstream():
    first = "SELECT id, x FROM p.d.a WHERE week = 1"
    models = {
        "first": Model(Target(name="first"), "view", first),
        "m": Model(Target(name="m"), "view", "SELECT x FROM first WHERE id > 5"),
    }
    sources = {name: Target(*name.split(".")) for name in SCHEMA}
    found = Pipeline(models, sources, {name: dict(columns) for name, columns in SCHEMA.items()})
    trace = found.trace_column(ColumnRef("m", "x"))
    assert [(model, item.condition) for model, item in trace.filters] == [
        ("m", "first.id > 5"),
        ("first", "a.week = 1"),
    ]
    assert trace.filters_unknown == ()


def test_dataform_expressions_in_a_condition_are_shown_as_written(tmp_path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (definitions / "wk.sqlx").write_text('config { type: "declaration" }\n')
    (definitions / "a.sqlx").write_text(
        'config { type: "table" }\nSELECT w.id FROM ${ref("wk")} w WHERE w.week = ${constants.current_week}\n'
    )
    found = load_sqlx_project(tmp_path, source_schema={"p.d.wk": {"id": "INT64", "week": "INT64"}})
    (item,) = found.explain_lineage()[ColumnRef("p.d.a", "id")].filters
    assert item.condition == "w.week = ${constants.current_week}"
    assert item.columns == (ColumnRef("p.d.wk", "week"),)
    assert not item.columns_complete  # the expression itself is not analysed


def test_report_rows_carry_the_filters_as_json():
    sql = "SELECT x FROM p.d.a a LEFT JOIN p.d.b b ON a.id = b.id WHERE a.week = 1"
    (row,) = pipeline(sql).report()["column_lineage"]
    assert row["filters"][0] == {
        "kind": "where",
        "condition": "a.week = 1",
        "scope": "main",
        "effect": "limits_rows",
        "columns": [{"node": "p.d.a", "column": "week"}],
    }


def test_unknown_filters_are_reported_as_unknown_not_empty():
    assert _column_filters(None, "x", None) is None
    row = {"node": "m", "column": "x", "sources": [], "transform": "x", "status": "unknown", "filters_unknown": True}
    rows, shared = _share_row_filters([row])
    assert rows == [row] and shared == {}


def test_the_graph_payload_shares_a_models_filters_across_its_columns():
    found = ColumnFilter("where", "a.week = 1", "main", (ColumnRef("p.d.a", "week"),)).to_json()
    own = ColumnFilter("where", "b.flag", "subquery", (), "feeds_value").to_json()
    rows = [
        {"node": "m", "column": "x", "filters": [found]},
        {"node": "m", "column": "w", "filters": [found, own]},
    ]
    compact, shared = _share_row_filters(rows)
    assert shared == {"m": [found]}
    assert compact == [{"node": "m", "column": "x"}, {"node": "m", "column": "w", "filters": [own]}]


@pytest.mark.parametrize("sql", ["SELECT 1 AS one", "SELECT x FROM p.d.a"])
def test_models_without_a_where_clause_have_no_filters(sql):
    assert all(row["filters"] == [] for row in pipeline(sql).lineage_report())


def test_a_second_writer_of_a_table_adds_its_filters_under_its_own_scope():
    source, table = Target("p", "d", "source"), Target("p", "d", "target")
    schema = {source.key: {"id": "INT64", "v": "STRING", "week": "INT64"}, table.key: {"v": "STRING"}}
    models = {
        table.key: Model(table, "table", "SELECT v FROM p.d.source WHERE week = 1"),
        "p.d.op": Model(
            Target("p", "d", "op"), "operations", "INSERT INTO p.d.target (v) SELECT v FROM p.d.source WHERE week = 2"
        ),
    }
    found = Pipeline(models, {source.key: source}, schema).explain_lineage()[ColumnRef(table.key, "v")]
    assert [(f.scope, f.condition) for f in found.filters] == [("main", "source.week = 1"), ("p.d.op: main", "source.week = 2")]
