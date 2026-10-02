"""Regressions found by the lineage, change-impact and Dataform-preservation suites (docs/lineage-bench.md)."""

from kumosql import apply_rule, load_sqlx_project
from kumosql.impact import assess_change
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target
from kumosql.sqlx import mask_sqlx_interpolations, restore_sqlx_interpolations

RAW = Target("p", "d", "raw")
SCHEMA = {"p.d.raw": {"a": "INT64", "b": "INT64", "c": "INT64", "z": "INT64"}}


def pipeline(**queries: str) -> Pipeline:
    models = {f"p.d.{name}": Model(Target("p", "d", name), "table", sql) for name, sql in queries.items()}
    return Pipeline(models, {RAW.key: RAW}, SCHEMA)


def affected(pl: Pipeline, table: str, column: str) -> set[str]:
    return {a.model for a in assess_change(pl, "drop_column", table, column).affected}


def test_a_column_named_in_a_cte_is_read_even_when_nothing_uses_it():
    pl = pipeline(m="WITH s1 AS (SELECT a, b, c FROM `p.d.raw`), s2 AS (SELECT a + 1 AS a1 FROM s1) SELECT a1 FROM s2")
    assert {ref.column for ref in pl.consumed_columns()["p.d.m"]} == {"a", "b", "c"}
    assert affected(pl, "p.d.raw", "c") == {"p.d.m"}


def test_a_star_in_a_cte_only_reads_what_the_outer_query_uses():
    pl = pipeline(m="WITH s AS (SELECT * FROM `p.d.raw`) SELECT a FROM s")
    assert {ref.column for ref in pl.consumed_columns()["p.d.m"]} == {"a"}
    assert affected(pl, "p.d.raw", "z") == set()


def test_columns_named_in_star_except_are_read():
    pl = pipeline(m="SELECT * EXCEPT (z) FROM `p.d.raw`", t="SELECT t.* EXCEPT (b) FROM `p.d.raw` AS t")
    assert ColumnRef("p.d.raw", "z") in pl.consumed_columns()["p.d.m"]
    assert ColumnRef("p.d.raw", "b") in pl.consumed_columns()["p.d.t"]
    assert affected(pl, "p.d.raw", "z") >= {"p.d.m"}


def test_a_cycle_does_not_make_the_columns_it_reads_look_dead():
    pl = pipeline(x="SELECT id FROM `p.d.y`", y="SELECT id FROM `p.d.x`")
    assert pl.dead_columns() == {}
    impact = assess_change(pl, "drop_column", "p.d.x", "id")
    assert {u.model for u in impact.unknown} >= {"p.d.y"}


def test_cte_before_insert_resolves_to_its_source_tables():
    pl = pipeline(t="WITH w AS (SELECT a FROM `p.d.raw`) INSERT INTO `p.d.t` SELECT w.a FROM w")
    assert pl.column_lineage()[ColumnRef("p.d.t", "a")] == {ColumnRef("p.d.raw", "a")}
    assert pl.upstream["p.d.t"] == {"p.d.raw"} and pl.table_reads()["p.d.t"] == {"p.d.raw"}  # the CTE name is not a table


def test_target_column_list_names_the_outputs_by_position():
    pl = pipeline(t="INSERT INTO `p.d.t` (x, y) SELECT a, b + 1 FROM `p.d.raw`", v="CREATE VIEW `p.d.v` (k) AS SELECT c FROM `p.d.raw`")
    assert pl.output_columns("p.d.t") == ("x", "y")
    assert pl.column_lineage()[ColumnRef("p.d.t", "y")] == {ColumnRef("p.d.raw", "b")}
    assert pl.output_columns("p.d.v") == ("k",)


def test_an_unaliased_cast_is_not_named_after_its_column():
    pl = pipeline(t="SELECT CAST(a AS STRING), SAFE_CAST(b AS INT64) AS b FROM `p.d.raw`")
    assert pl.output_columns("p.d.t")[0] != "a" and pl.output_columns("p.d.t")[1] == "b"


def test_tables_read_by_earlier_statements_of_a_script_are_dependencies():
    other = Target("p", "d", "other")
    models = {"p.d.s": Model(Target("p", "d", "s"), "table", "CREATE TEMP TABLE x AS SELECT a FROM `p.d.other`; SELECT a FROM `p.d.raw`")}
    pl = Pipeline(models, {RAW.key: RAW, other.key: other}, SCHEMA)
    assert pl.upstream["p.d.s"] == {"p.d.raw", "p.d.other"}
    assert any(d.code == "skipped_statements" for d in pl.all_diagnostics())


def test_dml_that_is_not_traced_is_reported_not_ignored():
    pl = pipeline(m="UPDATE `p.d.raw` SET a = 1 WHERE b = 2; SELECT a FROM `p.d.raw`")
    assert any(d.code == "skipped_statements" for d in pl.all_diagnostics())


def test_ref_in_pre_and_post_operations_is_a_dependency(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (tmp_path / "definitions" / "other.sqlx").write_text('config { type: "table" }\nselect 1 as id')
    (tmp_path / "definitions" / "m.sqlx").write_text(
        'config { type: "table" }\npre_operations { delete from ${self()} where id in (select id from ${ref("other")}) }\nselect 1 as id\n'
    )
    assert load_sqlx_project(tmp_path).upstream["p.d.m"] == {"p.d.other"}


def test_a_table_named_by_an_unresolved_template_is_flagged_and_never_dead(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (tmp_path / "definitions" / "a.sqlx").write_text('config { type: "table" }\nselect 1 as id, 2 as unused')
    (tmp_path / "definitions" / "b.sqlx").write_text('config { type: "table" }\nselect id from ${ref("a")}')
    (tmp_path / "definitions" / "c.sqlx").write_text('config { type: "table" }\njs { const t = "x"; }\nselect id from ${t}')
    pl = load_sqlx_project(tmp_path)
    codes = {(d.model, d.code) for d in pl.all_diagnostics()}
    assert ("p.d.c", "unresolved_template") in codes
    assert not any("__sqlx_token" in d.message for d in pl.all_diagnostics())
    assert pl.dead_columns() == {}
    assert "p.d.c" in {u.model for u in assess_change(pl, "drop_column", "p.d.a", "id").unknown}


def test_when_clause_that_continues_a_condition_is_rewritten_around():
    source = (
        'config { type: "incremental" }\n'
        'SELECT c.id\nFROM (SELECT id FROM ${ref("a")}) AS c\nWHERE 1 = 1 AND c.id > 0 '
        "${when(incremental(), `AND c.id > (SELECT MAX(id) FROM ${self()})`, ``)}\n"
    )
    masked, restorations = mask_sqlx_interpolations("SELECT 1 WHERE x > 0 ${when(incremental(), `AND y > 1`)}")
    assert "AND __sqlx_token_000__" in masked
    assert restore_sqlx_interpolations(masked, restorations) == "SELECT 1 WHERE x > 0 ${when(incremental(), `AND y > 1`)}"
    result = apply_rule("lift_subqueries", source)
    assert "${when(incremental(), `AND c.id > (SELECT MAX(id) FROM ${self()})`, ``)}" in result.sql
    assert "FROM (SELECT id" not in result.sql
