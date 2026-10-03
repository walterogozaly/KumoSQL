"""Dataform loading must not turn config it cannot read, or text Dataform ignores, into a verdict.

Synthetic projects, one per case of an outside audit that compared the static reader with Dataform's own compiler:
a computed config type, built-in assertions, partitioning and clustering, and refs written inside SQL comments.
"""

import pytest

from kumosql import load_compiled_graph, load_sqlx_project
from kumosql.pipeline_equivalence import prove_models
from kumosql.pipeline_loading import _column_reads, _config_literal
from kumosql.sqlx import sql_comment_spans


def project(tmp_path, files, settings="defaultProject: p\ndefaultDataset: ds\nvars:\n  kind: incremental\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "workflow_settings.yaml").write_text(settings)
    for name, text in files.items():
        path = tmp_path / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


# ---------------------------------------------------------------- computed config type


def test_a_computed_type_is_unknown_and_never_proves_equal_to_a_table(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": "config { type: dataform.projectConfig.vars.kind }\nSELECT 1 AS id\n",
        "b.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    })
    assert pl.models["p.ds.a"].kind == "unknown"
    assert pl.models["p.ds.b"].kind == "table"
    assert [d.code for d in pl.diagnostics if d.model == "p.ds.a"] == ["dynamic_config"]
    result = prove_models(pl, "p.ds.a", "p.ds.b", declared=[])
    assert result.status == "unknown" and "may be incremental" in result.reason
    assert prove_models(pl, "p.ds.b", "p.ds.a", declared=[]).status == "unknown"


def test_the_literal_type_still_proves(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "view" }\nSELECT 1 AS id\n',
        "b.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    })
    assert prove_models(pl, "p.ds.a", "p.ds.b", declared=[]).status == "equivalent"


@pytest.mark.parametrize("value", ["kind", "dataform.projectConfig.vars.kind", '"incr" + "emental"', "`${kind}`", "pick()"])
def test_any_value_that_is_not_a_plain_string_is_computed(value):
    assert _config_literal("config { type: %s, name: \"x\" }" % value, "type") == (None, True)


def test_plain_and_missing_types():
    assert _config_literal('config { type: "incremental", name: "x" }', "type") == ("incremental", False)
    assert _config_literal("config { name: 'x' }", "type") == (None, False)
    # a type written inside a description or a nested block is not the action's own
    assert _config_literal('config { description: "type: a", bigquery: { type: b } }', "type") == (None, False)


def test_a_computed_type_keeps_the_query_analysed_but_blocks_stored_row_conclusions(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": "config { type: dataform.projectConfig.vars.kind }\nSELECT 1 AS id, 2 AS unused\n",
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.dead_columns() == {"p.ds.a": ("unused",)}  # what it reads does not depend on its type
    assert not pl.completeness()["complete"]
    assert all(pl.completeness()["views"].values())


# ------------------------------------------------------------ built-in assertions


def test_config_assertions_read_their_columns(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", assertions: { nonNull: ["id"], uniqueKey: ["id"], '
                  'rowConditions: ["unused > 0"] } }\nSELECT 1 AS id, 2 AS unused, 3 AS spare\n',
        "d.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
        "c.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("a")} WHERE id < 0\n',
    })
    assert pl.dead_columns() == {"p.ds.a": ("spare",)}
    assert pl.models["p.ds.a"].non_null == ("id",)  # the prover keeps its constraint metadata
    assert pl.models["p.ds.a"].unique_keys == (("id",),)


def test_a_column_only_a_non_null_assertion_names_is_used(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", assertions: { nonNull: ["flag"] } }\nSELECT 1 AS id, 2 AS flag\n',
        "d.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.dead_columns() == {}


def test_a_table_with_assertions_and_no_reader_stays_a_terminal_output(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", assertions: { rowConditions: ["id > 0"] } }\nSELECT 1 AS id, 2 AS other\n',
    })
    assert pl.dead_columns() == {}


def test_row_conditions_that_cannot_be_read_keep_every_column(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", assertions: { rowConditions: CONDITIONS } }\nSELECT 1 AS id, 2 AS unused\n',
        "d.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.models["p.ds.a"].config_reads_unread == ("rowConditions",)
    assert pl.dead_columns() == {}


def test_column_reads_from_config_text():
    words, unread = _column_reads(
        'config { assertions: { nonNull: ["a"], uniqueKeys: [["b", "c"], ["d"]], rowConditions: ["e > 0 AND `f g` IS NOT NULL"] },'
        ' bigquery: { partitionBy: "DATE(h)", clusterBy: ["i"], updatePartitionFilter: "j >= 1", requirePartitionFilter: true } }')
    assert unread == ()
    assert {"a", "b", "c", "d", "e", "f g", "h", "i", "j"} <= set(words)
    # a key named inside a string is not a key
    assert _column_reads('config { description: "partitionBy: x" }') == ((), ())


def test_column_reads_flag_computed_values():
    assert _column_reads("config { bigquery: { partitionBy: field, clusterBy: [\"a\", other] } }")[1] == ("partitionBy", "clusterBy")
    assert _column_reads('config { bigquery: { updatePartitionFilter: "ts >= " + start } }')[1] == ("updatePartitionFilter",)


# -------------------------------------------------------- partitioning and clustering


def test_partition_and_cluster_columns_are_used(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", bigquery: { partitionBy: "day", clusterBy: ["category"], requirePartitionFilter: true } }\n'
                  'SELECT DATE "2020-01-01" AS day, "x" AS category, 1 AS id, 2 AS spare\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.dead_columns() == {"p.ds.a": ("spare",)}


def test_a_partition_expression_names_its_column(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", bigquery: { partitionBy: "DATE(created_at)" } }\nSELECT TIMESTAMP "2020-01-01" AS created_at, 1 AS id\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.dead_columns() == {}


def test_an_incremental_unique_key_is_used(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "incremental", uniqueKey: ["k"] }\nSELECT 1 AS id, 2 AS k, 3 AS spare\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    assert pl.dead_columns() == {"p.ds.a": ("spare",)}


def test_compiled_graph_keeps_layout_and_assertion_columns():
    def table(name, **extra):
        return {"target": {"database": "p", "schema": "ds", "name": name}, **extra}

    graph = {"tables": [
        table("a", query="SELECT DATE '2020-01-01' AS day, 'x' AS category, 1 AS id, 2 AS unused, 3 AS spare",
              bigquery={"partitionBy": "day", "clusterBy": ["category"], "requirePartitionFilter": True},
              assertions={"rowConditions": ["unused > 0"]}),
        table("r", query="SELECT id FROM `p.ds.a`", dependencyTargets=[{"database": "p", "schema": "ds", "name": "a"}]),
    ]}
    pl = load_compiled_graph(graph)
    assert pl.dead_columns() == {"p.ds.a": ("spare",)}
    graph["tables"][0]["bigquery"]["partitionBy"] = {"computed": True}
    assert load_compiled_graph(graph).dead_columns() == {}


# --------------------------------------------------------------- refs in SQL comments


COMMENT_SQL = 'SELECT 2 AS other\n-- ${ref("base")}\n/* ${ref("base")} */\n# ${ref("base")}\n'


def test_refs_in_comments_are_not_dependencies(tmp_path):
    pl = project(tmp_path, {
        "base.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
        "reader.sqlx": "config { type: \"table\" }\n" + COMMENT_SQL,
    })
    assert pl.models["p.ds.reader"].declared_dependencies == ()
    assert pl.upstream["p.ds.reader"] == set()
    assert pl.dead_columns() == {}  # base is a terminal output, its only column stays


def test_a_ref_next_to_a_comment_still_counts(tmp_path):
    pl = project(tmp_path, {
        "base.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS unused\n',
        "reader.sqlx": 'config { type: "table" }\n-- ${ref("nothing")}\nSELECT id FROM ${ref("base")} /* ${ref("nothing")} */\n',
    })
    assert pl.upstream["p.ds.reader"] == {"p.ds.base"}
    assert pl.dead_columns() == {"p.ds.base": ("unused",)}


def test_comment_markers_inside_strings_and_expressions_do_not_hide_refs(tmp_path):
    pl = project(tmp_path, {
        "base.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
        "reader.sqlx": "config { type: \"table\" }\nSELECT '--', id, \"#\" AS hash FROM ${ref(\"base\")}\n",
    })
    assert pl.upstream["p.ds.reader"] == {"p.ds.base"}


def test_refs_in_comments_of_pre_and_post_operations_are_not_dependencies(tmp_path):
    pl = project(tmp_path, {
        "base.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
        "reader.sqlx": 'config { type: "table" }\npre_operations {\n  -- ${ref("base")}\n  SELECT 1\n}\nSELECT 2 AS other\n',
    })
    assert pl.models["p.ds.reader"].declared_dependencies == ()


def test_comment_spans():
    sql = "SELECT 'a--b' -- one\n, 2 /* two ${x} */ , `c#d`, \"e/*f\" # three\nFROM t -- four"
    found = [sql[start:end] for start, end in sql_comment_spans(sql)]
    assert found == ["-- one", "/* two ${x} */", "# three", "-- four"]
    for text, comment in (
        ('SELECT ${ref("a")} -- ${ref("b")}', '-- ${ref("b")}'),
        ("SELECT 1 /* unterminated", "/* unterminated"),
        ("SELECT '''a -- b''' -- c", "-- c"),
        ("SELECT 'it''s' -- ok", "-- ok"),
    ):
        assert [text[start:end] for start, end in sql_comment_spans(text)] == [comment]
