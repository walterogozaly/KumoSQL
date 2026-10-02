"""Statements that are not plain queries (MERGE, temp functions, INSERT VALUES, table-valued functions) are classified
by kind, so a single MERGE is not a "script of two queries", a function definition is never a skipped step, a constant
INSERT keeps its columns, and a statement sqlglot cannot parse still contributes its table reads."""

from __future__ import annotations

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target

SCHEMA = {
    "p.d.src": {"k": "INT64", "v": "STRING"},
    "p.d.src2": {"arr": "ARRAY<INT64>"},
    "p.d.lkp": {"x": "INT64"},
    "p.d.tgt": {"k": "INT64", "v": "STRING", "w": "STRING"},
}
SOURCES = {name: Target("p", "d", name.split(".")[2]) for name in SCHEMA if name != "p.d.tgt"}


def build(sql: str, kind: str = "table", name: str = "tgt", extra: dict | None = None) -> Pipeline:
    models = {f"p.d.{name}": Model(Target("p", "d", name), kind, sql)}
    for other, text in (extra or {}).items():
        models[f"p.d.{other}"] = Model(Target("p", "d", other), "operations", text)
    return Pipeline(models, SOURCES, SCHEMA)


def lineage(pl: Pipeline, model: str = "p.d.tgt") -> dict[str, set[str]]:
    return {ref.column: {str(s) for s in sources} for ref, sources in pl.column_lineage().items() if ref.table == model}


def codes(pl: Pipeline) -> list[str]:
    return [d.code for d in pl.all_diagnostics()]


GAPS = {"no_query", "unknown_reads", "skipped_statements", "parse_error", "unparsed_operation"}


@pytest.mark.parametrize("kind", ["table", "incremental", "operations"])
def test_merge_with_update_and_insert(kind):
    pl = build(
        "MERGE tgt T USING (SELECT k, v FROM src) S ON T.k = S.k "
        "WHEN MATCHED THEN UPDATE SET v = S.v WHEN NOT MATCHED THEN INSERT (k, v) VALUES (S.k, S.v)".replace("tgt", "`p.d.tgt`").replace("src", "`p.d.src`"),
        kind,
    )
    assert "p.d.src" in pl.upstream["p.d.tgt"]
    assert not GAPS & set(codes(pl))
    got = lineage(pl)
    assert got["k"] == {"p.d.src.k"} and got["v"] == {"p.d.src.v"}
    assert got.get("w", set()) <= {"p.d.tgt.w"}
    assert "p.d.src.k" in {str(c) for c in pl.consumed_columns()["p.d.tgt"]}


def test_delete_only_merge_keeps_both_tables_and_no_column_rows():
    pl = build("MERGE `p.d.tgt` T USING `p.d.src` S ON T.k = S.k WHEN MATCHED THEN DELETE")
    assert {"p.d.src", "p.d.tgt"} <= pl.upstream["p.d.tgt"] | {"p.d.tgt"}
    assert not {"parse_error", "no_query"} & set(codes(pl))
    assert not lineage(pl)


def test_insert_row_under_on_false_takes_source_columns_by_name():
    pl = build("MERGE `p.d.tgt` USING `p.d.src` ON FALSE WHEN NOT MATCHED THEN INSERT ROW")
    got = lineage(pl)
    assert got["k"] == {"p.d.src.k"} and got["v"] == {"p.d.src.v"}
    assert not got.get("w")


def test_scalar_subquery_temp_function_then_ctas_is_one_traced_statement():
    pl = build(
        "CREATE TEMP FUNCTION f(a ARRAY<INT64>) AS ((SELECT MAX(x) FROM UNNEST(a) x)); "
        "CREATE TABLE `p.d.tgt` AS SELECT f(arr) AS m FROM `p.d.src2`"
    )
    assert "skipped_statements" not in codes(pl)
    assert pl.upstream["p.d.tgt"] == {"p.d.src2"}
    assert lineage(pl)["m"] == {"p.d.src2.arr"}


def test_temp_function_body_tables_become_reads_and_the_definition_is_not_skipped():
    pl = build(
        "CREATE TEMP FUNCTION g() AS ((SELECT COUNT(*) FROM `p.d.lkp`)); "
        "CREATE TABLE `p.d.tgt` AS SELECT g() AS n, k FROM `p.d.src`"
    )
    assert "skipped_statements" not in codes(pl)
    assert pl.upstream["p.d.tgt"] == {"p.d.src", "p.d.lkp"}
    got = lineage(pl)
    assert got["k"] == {"p.d.src.k"}
    assert got["n"] <= {"p.d.lkp", "p.d.lkp.x"}


def test_insert_values_is_a_constant_source_with_known_columns():
    pl = build("INSERT INTO `p.d.tgt` (k, v) VALUES (1, 'a'), (2, 'b')")
    assert not {"no_query", "unknown_reads"} & set(codes(pl))
    assert pl.upstream["p.d.tgt"] == set()
    assert "dead_columns_disabled" not in " ".join(codes(pl))
    assert set(lineage(pl)) <= {"k", "v"}


def test_table_function_with_unknown_definition_parses_and_reads_its_table():
    pl = build("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, n => 3)")
    assert "p.d.src" in pl.upstream["p.d.tgt"]
    assert "parse_error" not in codes(pl)


def test_cte_passed_to_a_table_function_is_not_reported_as_a_table():
    pl = build("WITH c AS (SELECT k FROM `p.d.src`) SELECT * FROM `p.d.fn`(TABLE c)")
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}


def test_statements_that_write_no_column_never_report_a_skip():
    for sql in (
        "MERGE `p.d.tgt` T USING `p.d.src` S ON T.k = S.k WHEN MATCHED THEN DELETE",
        "DELETE FROM `p.d.tgt` WHERE k IN (SELECT k FROM `p.d.src`)",
    ):
        assert "skipped_statements" not in codes(build(sql)), sql


def test_a_lone_update_writes_columns_that_are_not_traced_and_says_so():
    pl = build("UPDATE `p.d.tgt` SET v = s.v FROM `p.d.src` s WHERE `p.d.tgt`.k = s.k")
    skipped = [d for d in pl.all_diagnostics() if d.code == "skipped_statements"]
    assert skipped and "not traced (kinds: update x1)" in skipped[0].message
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}


def test_a_statement_that_does_not_parse_keeps_its_tables_and_says_where_it_stopped():
    pl = build("SELECT a FROM `p.d.src` s JOIN `p.d.lkp` l ON s.k = l.x WHERE k ??? 1 ((")
    assert pl.upstream["p.d.tgt"] == {"p.d.src", "p.d.lkp"}
    errors = [d for d in pl.all_diagnostics() if d.code == "parse_error"]
    assert len(errors) == 1 and "Line 1, Col" in errors[0].message and "columns are unknown" in errors[0].message
    assert not lineage(pl)
    assert "p.d.src" not in pl.dead_columns()  # an unknown reader might use any column


def test_one_unparseable_statement_in_a_script_does_not_hide_the_rest():
    pl = build("SELECT 1; INSERT INTO `p.d.other` SELECT a FROM `p.d.src` WHERE k ??? 1 ((; CREATE TABLE `p.d.tgt` AS SELECT k FROM `p.d.lkp2`")
    assert "p.d.src" in pl.upstream["p.d.tgt"]
    assert any(d.code == "parse_error" for d in pl.all_diagnostics())
    skipped = [d for d in pl.all_diagnostics() if d.code == "skipped_statements"]
    assert skipped and "traced" in skipped[0].message and "kinds:" in skipped[0].message


@pytest.mark.parametrize(
    "text, reads, writes",
    [
        ("INSERT INTO a.b.t SELECT * FROM a.b.x JOIN a.b.y ON ??", {"a.b.x", "a.b.y"}, {"a.b.t"}),
        ("WITH c AS (SELECT 1 FROM a.b.x) INSERT INTO a.b.t SELECT * FROM c, a.b.z WHERE ??", {"a.b.x", "a.b.z"}, {"a.b.t"}),
        ("DELETE FROM a.b.t WHERE k IN (SELECT k FROM a.b.s) ???", {"a.b.s"}, {"a.b.t"}),
        ("MERGE INTO `a.b.t` T USING (SELECT * FROM `a.b.s`) S ON ??? WHEN MATCHED THEN UPDATE SET x = 1", {"a.b.s"}, {"a.b.t"}),
        ("SELECT * FROM UNNEST([1, 2]) AS u, a.b.q ???", {"a.b.q"}, set()),
        ("SELECT * FROM a.b.fn(TABLE a.b.src, n => 1) ???", {"a.b.src"}, set()),
        ("CREATE OR REPLACE TABLE a.b.t LIKE a.b.model_table ???", {"a.b.model_table"}, {"a.b.t"}),
    ],
)
def test_reads_and_writes_are_found_in_the_tokens(text, reads, writes):
    from kumosql.scripts import token_reads

    found_reads, found_writes = token_reads(text)
    name = lambda table: ".".join(part for part in (table.catalog, table.db, table.name) if part)  # noqa: E731
    assert {name(t) for t in found_reads} == reads
    assert {name(t) for t in found_writes} == writes


def test_a_constant_insert_leaves_dead_column_detection_on():
    models = {
        "p.d.base": Model(Target("p", "d", "base"), "table", "SELECT k, v FROM `p.d.src`"),
        "p.d.reader": Model(Target("p", "d", "reader"), "table", "SELECT k FROM `p.d.base`"),
        "p.d.seed": Model(Target("p", "d", "seed"), "table", "INSERT INTO `p.d.tgt` (k, v) VALUES (1, 'a')"),
        "p.d.last": Model(Target("p", "d", "last"), "table", "SELECT k FROM `p.d.reader`"),
    }
    pl = Pipeline(models, SOURCES, SCHEMA)
    assert not {"unknown_reads", "no_query"} & set(codes(pl))
    assert pl.dead_columns().get("p.d.base") == ("v",)


def test_a_function_definition_is_not_counted_among_the_statements():
    pl = build(
        "CREATE TEMP FUNCTION g() AS ((SELECT COUNT(*) FROM `p.d.lkp`)); "
        "CREATE TEMP FUNCTION h(x INT64) AS (x + 1); "
        "CREATE TABLE `p.d.tgt` AS SELECT h(k) AS n FROM `p.d.src`"
    )
    assert "skipped_statements" not in codes(pl)
    assert "p.d.lkp" in pl.upstream["p.d.tgt"]  # the body reads it when called


TVF = "CREATE TABLE FUNCTION `p.d.fn`(t TABLE<k INT64, v STRING>, n INT64) AS (SELECT k, v FROM t LIMIT n)"


def test_a_table_function_defined_in_the_project_is_read_as_its_query():
    pl = build("SELECT * FROM `p.d.fn`(TABLE `p.d.src`, n => 3)", extra={"fns": TVF})
    assert "p.d.src" in pl.upstream["p.d.tgt"]
    assert lineage(pl) == {"k": {"p.d.src.k"}, "v": {"p.d.src.v"}}
    assert "unexpanded_star" not in codes(pl) and "parse_error" not in codes(pl)


def test_the_same_call_with_positional_arguments_and_an_alias():
    pl = build("SELECT x.k FROM `p.d.fn`(TABLE `p.d.src`, 3) AS x", extra={"fns": TVF})
    assert lineage(pl) == {"k": {"p.d.src.k"}}


def test_a_table_function_over_a_cte_reads_the_cte_sources():
    pl = build("WITH c AS (SELECT k, v FROM `p.d.src`) SELECT * FROM `p.d.fn`(TABLE c, n => 1)", extra={"fns": TVF})
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}
    assert lineage(pl) == {"k": {"p.d.src.k"}, "v": {"p.d.src.v"}}


def test_the_body_of_a_table_function_reads_its_own_tables():
    fn = "CREATE TABLE FUNCTION `p.d.fn2`(t TABLE<k INT64>) AS (SELECT t.k, l.x FROM t JOIN `p.d.lkp` l ON l.x = t.k)"
    pl = build("SELECT * FROM `p.d.fn2`(TABLE `p.d.src`)", extra={"fns": fn})
    assert pl.upstream["p.d.tgt"] == {"p.d.src", "p.d.lkp"}
    assert lineage(pl) == {"k": {"p.d.src.k"}, "x": {"p.d.lkp.x"}}


def test_a_call_that_does_not_fit_its_definition_stays_opaque():
    pl = build("SELECT * FROM `p.d.fn`(TABLE `p.d.src`)", extra={"fns": TVF})  # missing n
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}
    assert "parse_error" not in codes(pl)


def test_what_a_called_function_reads_feeds_the_table_that_calls_it():
    from kumosql.scripts import analyse_script

    analysis = analyse_script(
        "CREATE TEMP FUNCTION g() AS ((SELECT COUNT(*) FROM `p.d.lkp`)); CREATE TABLE `p.d.tgt` AS SELECT g() AS n, k FROM `p.d.src`"
    )
    written = {w.table.name: {t.name for t in w.sources} for w in analysis.writes}
    assert written == {"tgt": {"src", "lkp"}}


def test_parse_script_keeps_the_shape_its_callers_unpack():
    """``_parse_script`` returns ``(query, analysis)``; the table profile unpacked a longer tuple once and silently fell back
    to analysing every model, so every caller in the package is checked here."""

    import ast
    from pathlib import Path

    from kumosql.pipeline import _parse_script
    from kumosql.scripts import ScriptAnalysis

    query, analysis = _parse_script("SELECT k FROM `p.d.src`")
    assert query is not None and isinstance(analysis, ScriptAnalysis)
    package = Path(__import__("kumosql").__file__).parent
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "_parse_script":
                target = node.targets[0]
                assert isinstance(target, ast.Tuple) and len(target.elts) == 2, f"{path.name}:{node.lineno} unpacks the wrong shape"


@pytest.mark.parametrize("kind", ["table", "operations"])
def test_a_misspelled_keyword_keeps_its_edge_and_reports_one_located_parse_error(kind):
    """The statement is unrecognised from its first word, but it is degraded, not dropped, and it is described the same way
    in a query model and in an operation."""

    pl = build("SELEC k FROM `p.d.src` WHERE ((", kind)
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}
    parse_errors = [d.message for d in pl.all_diagnostics() if d.code == "parse_error"]
    assert len(parse_errors) == 1 and "Line 1, Col" in parse_errors[0] and "read from its tokens" in parse_errors[0]
    assert "unparsed_operation" not in codes(pl)
    assert "may have no edges" not in " ".join(d.message for d in pl.all_diagnostics())


def test_an_operation_statement_that_is_not_readable_at_all_still_says_edges_may_be_missing():
    pl = build("EXECUTE IMMEDIATE CONCAT('SELECT * FROM ', @name)", "operations")
    messages = {d.code: d.message for d in pl.all_diagnostics()}
    assert "may have no edges" in messages["unparsed_operation"]
