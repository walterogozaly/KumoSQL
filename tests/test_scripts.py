"""Scripts: splitting, what is kept or ignored, lineage through temporary tables and variables, jobs, rewriting."""

from __future__ import annotations

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target
from kumosql.scripts import (
    IGNORED,
    KEPT,
    UNKNOWN,
    analyse_script,
    collect_procedures,
    expand_script_jobs,
    lex,
    split_script,
    split_statements,
    string_value,
)

# ------------------------------------------------------------------------- splitting


def test_semicolons_in_strings_comments_and_quoted_names_do_not_split():
    sql = """SELECT 'a;b'; SELECT "x;y" AS s; -- not; a split
    /* nor; this */ SELECT '''multi;
    line;''' AS m; SELECT r'raw;' AS r; SELECT `p.d.t;x` FROM t"""
    assert split_statements(sql) == [
        "SELECT 'a;b'",
        'SELECT "x;y" AS s',
        "SELECT '''multi;\n    line;''' AS m",
        "SELECT r'raw;' AS r",
        "SELECT `p.d.t;x` FROM t",
    ]


def test_blocks_are_opened_not_returned_and_branches_are_conditional():
    parts = split_script(
        """DECLARE x INT64 DEFAULT 1;
        BEGIN
          IF x > 0 THEN SELECT 1; ELSEIF x < 0 THEN SELECT 2; ELSE SELECT IF(x = 0, 3, 4); END IF;
          WHILE x < 3 DO SET x = x + 1; END WHILE;
          FOR r IN (SELECT a FROM t) DO INSERT INTO u SELECT r.a; END FOR;
          LOOP SELECT 5; BREAK; END LOOP;
          REPEAT SET x = x - 1; UNTIL x < 0 END REPEAT;
          CASE x WHEN 1 THEN SELECT 6; ELSE SELECT 7; END CASE;
        EXCEPTION WHEN ERROR THEN SELECT 'err; ok';
        END;
        SELECT 'after'"""
    )
    assert [p.text for p in parts] == [
        "DECLARE x INT64 DEFAULT 1",
        "SELECT 1",
        "SELECT 2",
        "SELECT IF(x = 0, 3, 4)",
        "SET x = x + 1",
        "INSERT INTO u SELECT r.a",
        "SELECT 5",
        "BREAK",
        "SET x = x - 1",
        "SELECT 6",
        "SELECT 7",
        "SELECT 'err; ok'",
        "SELECT 'after'",
    ]
    assert [p.conditional for p in parts] == [False] + [True] * 11 + [False]


def test_case_expressions_and_if_functions_do_not_open_blocks():
    sql = "SELECT CASE WHEN a THEN 1 END AS c, IF(b, 1, 2) AS d FROM t; SELECT 2"
    assert split_statements(sql) == ["SELECT CASE WHEN a THEN 1 END AS c, IF(b, 1, 2) AS d FROM t", "SELECT 2"]


def test_transactions_are_statements_not_blocks():
    assert split_statements("BEGIN TRANSACTION; INSERT INTO t SELECT 1; COMMIT TRANSACTION;") == [
        "BEGIN TRANSACTION",
        "INSERT INTO t SELECT 1",
        "COMMIT TRANSACTION",
    ]


def test_a_procedure_body_is_marked_and_a_function_body_is_one_statement():
    parts = split_script(
        """CREATE OR REPLACE PROCEDURE `p.d.proc`(IN a STRING)
        OPTIONS(description='x; y')
        BEGIN
          SELECT 1;
          INSERT INTO t SELECT CASE WHEN a = 'x' THEN 1 END
        END;
        CREATE TEMP FUNCTION f(x INT64) RETURNS INT64 LANGUAGE js AS r'''return x; ''';
        SELECT f(1)"""
    )
    assert [(p.text[:20], p.in_procedure) for p in parts] == [
        ("SELECT 1", "p.d.proc"),
        ("INSERT INTO t SELECT", "p.d.proc"),
        ("CREATE TEMP FUNCTION", ""),
        ("SELECT f(1)", ""),
    ]


def test_labels_missing_final_semicolons_and_empty_text():
    assert split_statements("lbl: BEGIN SELECT 1; END lbl; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_statements("BEGIN SELECT 1; SELECT 2 END") == ["SELECT 1", "SELECT 2"]
    assert split_statements("") == [] and split_statements(" ;; ") == []


def test_statement_lines_are_reported():
    parts = split_script("SELECT 1;\n\n-- c\nSELECT 2;")
    assert [p.line for p in parts] == [1, 4]


def test_lexer_and_string_values():
    assert [t.text for t in lex("SELECT 'a' -- c\n, `b`")] == ["SELECT", "'a'", ",", "`b`"]
    assert string_value("'a\\nb'") == "a\nb" and string_value("r'a\\nb'") == "a\\nb" and string_value("b'x'") is None
    assert string_value('"""x\'y"""') == "x'y"


# --------------------------------------------------------------------- what is kept


def kinds(analysis, disposition):
    return sorted(s.kind for s in analysis.statements if s.disposition == disposition and not s.nested)


def reads(analysis):
    return sorted(t.name for t in analysis.all_reads())


def test_ignored_statements_read_nothing_and_decoys_in_text_are_not_reads():
    a = analyse_script(
        """-- FROM decoy_comment
        DECLARE n INT64 DEFAULT 3;
        SET n = n + 1;
        ASSERT n > 0 AS 'FROM decoy_assert';
        BEGIN TRANSACTION;
        LOAD DATA INTO `p.d.loaded` FROM FILES (format = 'CSV', uris = ['gs://b/f']);
        DROP TABLE IF EXISTS `p.d.old`;
        SELECT 'FROM decoy_string' AS s, a FROM `p.d.real`;
        COMMIT TRANSACTION"""
    )
    assert reads(a) == ["real"]
    assert kinds(a, UNKNOWN) == []
    assert kinds(a, IGNORED) == ["assert", "declare", "load_data", "set", "transaction", "transaction"]
    assert [(w.table.name, w.kind) for w in a.writes] == [("old", "drop")]


def test_kept_statement_kinds_and_writes():
    a = analyse_script(
        """CREATE OR REPLACE TABLE `p.d.t1` AS SELECT a FROM `p.d.s1`;
        INSERT INTO `p.d.t2` SELECT b FROM `p.d.s2`;
        MERGE `p.d.t3` T USING `p.d.s3` S ON T.id = S.id WHEN MATCHED THEN UPDATE SET v = S.v;
        UPDATE `p.d.t4` SET v = 1 WHERE id IN (SELECT id FROM `p.d.s4`);
        DELETE FROM `p.d.t5` WHERE EXISTS (SELECT 1 FROM `p.d.s5` WHERE s5.id = t5.id);
        CREATE TABLE `p.d.t6` CLONE `p.d.s6`;
        SELECT 1"""
    )
    assert kinds(a, KEPT) == ["clone", "create_table", "delete", "insert", "merge", "select", "update"]
    got = {w.table.name: sorted(s.name for s in w.sources) for w in a.writes}
    assert got == {"t1": ["s1"], "t2": ["s2"], "t3": ["s3"], "t4": ["s4"], "t5": ["s5"], "t6": ["s6"]}


def test_export_reads_but_writes_no_table():
    a = analyse_script("EXPORT DATA OPTIONS (uri = 'gs://b/x*.csv', format = 'CSV') AS SELECT a FROM `p.d.s`")
    assert reads(a) == ["s"] and a.writes == []
    assert kinds(a, KEPT) == ["export_data"]


def test_unrecognised_and_unparseable_statements_are_unknown_not_guessed():
    a = analyse_script("FROBNICATE `p.d.t`; SELECT FROM WHERE; INSERT INTO `p.d.t` SELECT 1 FROM `p.d.ok`")
    assert kinds(a, UNKNOWN) == ["other", "select"]
    assert reads(a) == ["ok"]


def test_branches_are_possible_edges():
    a = analyse_script(
        """DECLARE n INT64 DEFAULT 1;
        IF n > 0 THEN
          INSERT INTO `p.d.out` SELECT a FROM `p.d.then_side`;
        ELSE
          INSERT INTO `p.d.out` SELECT a FROM `p.d.else_side`;
        END IF"""
    )
    assert sorted(w.table.name for w in a.writes) == ["out", "out"]
    assert all(w.conditional for w in a.writes)
    assert reads(a) == ["else_side", "then_side"]
    assert a.report()["conditional"] == 2


def test_execute_immediate_literal_is_read_and_dynamic_is_unknown():
    a = analyse_script(
        """EXECUTE IMMEDIATE "INSERT INTO `p.d.lit` SELECT * FROM `p.d.lit_src`";
        EXECUTE IMMEDIATE 'SELECT * FROM ' || 'p.d.joined';
        EXECUTE IMMEDIATE CONCAT('SELECT * FROM ', 'p.d.t');
        DECLARE q STRING DEFAULT 'SELECT 1';
        EXECUTE IMMEDIATE q"""
    )
    assert reads(a) == ["joined", "lit_src", "t"]
    dynamic = [s for s in a.statements if s.disposition == UNKNOWN]
    assert len(dynamic) == 1 and dynamic[0].reason == "dynamic SQL text"
    assert [w.table.name for w in a.writes] == ["lit"]


def test_for_loop_variable_carries_the_tables_of_its_query():
    a = analyse_script(
        """FOR r IN (SELECT id FROM `p.d.driver`) DO
          INSERT INTO `p.d.out` SELECT x FROM `p.d.fact` WHERE id = r.id;
        END FOR"""
    )
    (write,) = a.writes
    assert sorted(s.name for s in write.sources) == ["driver", "fact"]
    assert a.variable_reads and "driver" in {t.name for t in a.variable_reads.values()}


def test_variables_carry_the_tables_their_value_came_from():
    a = analyse_script(
        """DECLARE cutoff DATE DEFAULT (SELECT MAX(d) FROM `p.d.watermark`);
        DECLARE other INT64 DEFAULT 5;
        INSERT INTO `p.d.out` SELECT a FROM `p.d.fact` WHERE d > cutoff AND n > other;
        SET cutoff = (SELECT MIN(d) FROM `p.d.floor`);
        INSERT INTO `p.d.out2` SELECT a FROM `p.d.fact` WHERE d > cutoff"""
    )
    got = {w.table.name: sorted(s.name for s in w.sources) for w in a.writes}
    assert got == {"out": ["fact", "watermark"], "out2": ["fact", "floor"]}


def test_a_temporary_table_is_followed_to_the_real_sources():
    a = analyse_script(
        """CREATE TEMP TABLE a1 AS SELECT id, x FROM `p.d.raw`;
        CREATE TEMP TABLE a2 AS SELECT id, SUM(x) AS x FROM a1 GROUP BY id;
        CREATE OR REPLACE TABLE `p.d.final` AS SELECT a2.id, a2.x, r.name FROM a2 JOIN `p.d.ref` r USING (id)"""
    )
    (write,) = a.writes
    assert write.table.name == "final"
    assert sorted(s.name for s in write.sources) == ["raw", "ref"]
    assert a.temp_names == {"a1", "a2"}
    assert all(t.name in {"raw", "ref"} for t in a.all_reads())


def test_a_redefined_temporary_table_reads_its_previous_version():
    a = analyse_script(
        """CREATE TEMP TABLE t AS SELECT id FROM `p.d.one`;
        CREATE OR REPLACE TEMP TABLE t AS SELECT id FROM t JOIN `p.d.two` USING (id);
        INSERT INTO `p.d.out` SELECT id FROM t"""
    )
    (write,) = a.writes
    assert sorted(s.name for s in write.sources) == ["one", "two"]
    inlined = a.with_temp_ctes(a.final_query.copy()).sql(dialect="bigquery")
    assert inlined.count("WITH") == 1 and "t__v2" in inlined


def test_drop_ends_a_temporary_table():
    a = analyse_script(
        """CREATE TEMP TABLE t AS SELECT id FROM `p.d.one`;
        DROP TABLE t;
        SELECT id FROM t"""
    )
    assert sorted(x.name for x in a.all_reads()) == ["one", "t"]


def test_dml_that_changes_a_temporary_table_is_followed_at_table_level():
    a = analyse_script(
        """CREATE TEMP TABLE t AS SELECT id, v FROM `p.d.one`;
        UPDATE t SET v = (SELECT MAX(v) FROM `p.d.two`) WHERE TRUE;
        INSERT INTO `p.d.out` SELECT id, v FROM t"""
    )
    (write,) = a.writes
    assert sorted(s.name for s in write.sources) == ["one", "two"]
    assert a.opaque_temps  # its columns are not traced, its tables are


def test_insert_into_a_temporary_table_with_a_schema_unions_the_arms():
    a = analyse_script(
        """CREATE TEMP TABLE t (id INT64, v FLOAT64);
        INSERT INTO t SELECT id, amount FROM `p.d.one`;
        INSERT INTO t (v, id) SELECT price, key FROM `p.d.two`;
        SELECT id, v FROM t"""
    )
    sql = a.with_temp_ctes(a.final_query.copy()).sql(dialect="bigquery")
    assert "UNION ALL" in sql and not a.opaque_temps
    assert sorted(t.name for t in a.all_reads()) == ["one", "two"]


# ------------------------------------------------------------------------ procedures

PROCEDURE = """CREATE OR REPLACE PROCEDURE `p.d.load`(IN src STRING, IN day DATE)
BEGIN
  INSERT INTO `p.d.daily` SELECT a FROM `p.d.facts` WHERE d = day;
END"""


def test_a_call_to_a_defined_procedure_is_expanded_in_place():
    a = analyse_script(PROCEDURE + ";\nCALL `p.d.load`('x', DATE '2024-01-01');")
    assert [w.table.name for w in a.writes] == ["daily"]
    assert kinds(a, UNKNOWN) == []
    call = next(s for s in a.statements if s.kind == "call")
    assert call.disposition == KEPT


def test_a_call_to_a_procedure_defined_elsewhere_uses_the_registry():
    registry = collect_procedures([PROCEDURE])
    a = analyse_script("CALL `p.d.load`('x', CURRENT_DATE())", procedures=registry)
    assert [w.table.name for w in a.writes] == ["daily"]
    assert kinds(analyse_script("CALL `p.d.nowhere`()"), UNKNOWN) == ["call"]


def test_a_procedure_defined_by_name_is_found_by_its_bare_name_and_recursion_stops():
    registry = collect_procedures(["CREATE PROCEDURE loop_me() BEGIN CALL loop_me(); SELECT 1; END"])
    a = analyse_script("CALL `p.d.loop_me`()", procedures=registry)
    assert any(s.reason == "recursive or too deeply nested" for s in a.statements)


def test_procedure_definitions_alone_have_no_edges():
    a = analyse_script(PROCEDURE)
    assert a.writes == [] and a.all_reads() == []
    assert a.report()["procedures"][0]["writes"] == 1


# --------------------------------------------------------------------------- report


def test_the_report_carries_counts_and_kinds_but_no_sql_text():
    a = analyse_script(
        """DECLARE secret_name STRING DEFAULT 'top secret';
        CREATE TEMP TABLE hidden_table AS SELECT confidential FROM `p.d.private_source`;
        EXECUTE IMMEDIATE CONCAT('x', secret_name);
        SELECT confidential FROM hidden_table"""
    )
    text = str(a.report()) + a.summary()
    for secret in ("top secret", "confidential", "private_source", "hidden_table", "secret_name"):
        assert secret not in text
    report = a.report()
    assert report["kept"] == 2 and report["ignored"] == 1 and report["unknown"] == 1
    assert report["by_kind"]["unknown"] == {"execute_immediate": 1}
    assert "1 unknown (execute_immediate x1)" in a.summary()


# --------------------------------------------------------------------- the pipeline

RAW = Target("p", "d", "raw")
REF = Target("p", "d", "ref")
SCHEMA = {"p.d.raw": {"id": "INT64", "amount": "FLOAT64", "d": "DATE"}, "p.d.ref": {"id": "INT64", "name": "STRING"}}


def pipeline(kind="table", **sql):
    models = {f"p.d.{name}": Model(Target("p", "d", name), kind, text) for name, text in sql.items()}
    return Pipeline(models, {RAW.key: RAW, REF.key: REF}, SCHEMA)


def lineage(pl, key):
    return {c.column: sorted(str(s) for s in v) for c, v in pl.column_lineage().items() if c.table == key}


def codes(pl, key):
    return {d.code for d in pl.all_diagnostics() if d.model == key}


def test_column_lineage_runs_through_temporary_tables_to_the_real_sources():
    pl = pipeline(
        final="""DECLARE cutoff DATE DEFAULT (SELECT MAX(d) FROM `p.d.raw`);
        CREATE TEMP TABLE tmp AS SELECT id, amount FROM `p.d.raw` WHERE d > cutoff;
        CREATE TEMP TABLE agg (id INT64, total FLOAT64);
        INSERT INTO agg SELECT id, SUM(amount) FROM tmp GROUP BY id;
        SELECT a.id, a.total, r.name FROM agg a JOIN `p.d.ref` r USING (id)"""
    )
    assert pl.upstream["p.d.final"] == {"p.d.raw", "p.d.ref"}
    assert lineage(pl, "p.d.final") == {"id": ["p.d.raw.id"], "total": ["p.d.raw.amount"], "name": ["p.d.ref.name"]}
    assert "skipped_statements" not in codes(pl, "p.d.final")
    assert "script_summary" in codes(pl, "p.d.final")
    assert not any(d.code == "external_tables" for d in pl.all_diagnostics())


def test_a_table_written_from_a_temporary_table_traces_to_the_real_sources():
    pl = pipeline(
        final="""CREATE TEMP TABLE t AS SELECT id, amount * 2 AS twice FROM `p.d.raw`;
        CREATE OR REPLACE TABLE `p.d.final` AS SELECT id, twice FROM t"""
    )
    assert lineage(pl, "p.d.final") == {"id": ["p.d.raw.id"], "twice": ["p.d.raw.amount"]}


def test_a_temporary_table_that_dml_changed_is_unknown_at_column_level_but_still_a_dependency():
    pl = pipeline(
        final="""CREATE TEMP TABLE t AS SELECT id, amount FROM `p.d.raw`;
        UPDATE t SET amount = (SELECT MAX(id) FROM `p.d.ref`) WHERE TRUE;
        SELECT id, amount FROM t"""
    )
    assert pl.upstream["p.d.final"] == {"p.d.raw", "p.d.ref"}
    rows = {r["column"]: r for r in pl.lineage_report() if r["node"] == "p.d.final"}
    # sqlglot 26 cannot retry an unresolved column, so it reports a lineage error for the same columns
    assert rows["amount"]["status"] == "unknown" and rows["amount"]["reason"] in {"temporary_table", "lineage_error"}
    assert not any(d.code == "external_tables" for d in pl.all_diagnostics())


def test_extra_statements_that_are_not_traced_still_report_skipped_statements():
    pl = pipeline(
        m="""UPDATE `p.d.raw` SET amount = 1 WHERE id = 2;
        CREATE TEMP TABLE unused AS SELECT id FROM `p.d.ref`;
        SELECT id FROM `p.d.raw`"""
    )
    assert "skipped_statements" in codes(pl, "p.d.m")
    assert pl.upstream["p.d.m"] == {"p.d.raw", "p.d.ref"}


def test_an_unparseable_statement_costs_only_itself():
    pl = pipeline(m="SELECT FROM WHERE; SELECT id FROM `p.d.raw`")
    assert lineage(pl, "p.d.m") == {"id": ["p.d.raw.id"]}
    assert "skipped_statements" in codes(pl, "p.d.m")


def test_a_script_that_only_parses_in_blocks_is_analysed():
    pl = pipeline(
        m="""BEGIN
          DECLARE n INT64 DEFAULT 0;
          IF n = 0 THEN
            SELECT id FROM `p.d.raw`;
          END IF;
        EXCEPTION WHEN ERROR THEN SELECT 1;
        END"""
    )
    assert pl.upstream["p.d.m"] == {"p.d.raw"}


def test_operations_feed_the_tables_they_write():
    pl = pipeline(
        tgt="SELECT id FROM `p.d.ref`",
    )
    pl.models["p.d.load"] = Model(Target("p", "d", "load"), "operations", "INSERT INTO `p.d.tgt` SELECT id FROM `p.d.raw`")
    pl2 = Pipeline(pl.models, pl.sources, pl.source_schema)
    assert pl2.upstream["p.d.load"] == {"p.d.raw"}
    assert pl2.upstream["p.d.tgt"] == {"p.d.ref", "p.d.raw"}
    assert not any(d.code == "unparsed_operation" for d in pl2.all_diagnostics())


def test_an_operation_with_dynamic_sql_is_reported():
    pl = pipeline()
    pl.models["p.d.load"] = Model(Target("p", "d", "load"), "operations", "EXECUTE IMMEDIATE FORMAT('SELECT 1 FROM %s', 'x' || CAST(1 AS STRING))")
    pl2 = Pipeline(pl.models, pl.sources, pl.source_schema)
    assert any(d.code == "unparsed_operation" for d in pl2.all_diagnostics())


def test_pre_and_post_operations_of_a_table_are_read(tmp_path):
    from kumosql.pipeline import load_sqlx_project

    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (tmp_path / "definitions" / "lookup.sqlx").write_text('config { type: "table" }\nselect 1 as id')
    (tmp_path / "definitions" / "m.sqlx").write_text(
        'config { type: "table" }\n'
        "pre_operations {\n  DECLARE cutoff INT64 DEFAULT (SELECT MAX(id) FROM ${ref(\"lookup\")});\n}\n"
        "post_operations {\n  INSERT INTO `p.d.audit` SELECT COUNT(*) FROM ${self()};\n  ${when(incremental(), `DELETE 1`)}\n}\n"
        "select 2 as id\n"
    )
    pl = load_sqlx_project(tmp_path)
    assert len(pl.models["p.d.m"].operations_sql) == 2
    assert "p.d.lookup" in pl.upstream["p.d.m"]
    # the audit table is not a model here; the unreadable template is reported without blocking the model
    assert any(d.code == "script_summary" and d.model == "p.d.m" for d in pl.all_diagnostics())
    assert pl.completeness()["by_code"].get("unparsed_operation", 0) == 0


def test_procedures_defined_in_one_model_are_expanded_where_they_are_called():
    pl = pipeline(
        out="CALL `p.d.build`(); SELECT id FROM `p.d.raw`",
    )
    pl.models["p.d.defs"] = Model(
        Target("p", "d", "defs"),
        "operations",
        "CREATE OR REPLACE PROCEDURE `p.d.build`() BEGIN INSERT INTO `p.d.out` SELECT id FROM `p.d.ref`; END",
    )
    pl2 = Pipeline(pl.models, pl.sources, pl.source_schema)
    assert "p.d.ref" in pl2.upstream["p.d.out"] and "p.d.raw" in pl2.upstream["p.d.out"]


# --------------------------------------------------------------------------- jobs


def job(job_id, destination, refs, parent="", statement="", time="2024-01-01T00:00:0{}Z", n=0, **extra):
    return {
        "job_id": job_id,
        "parent_job_id": parent,
        "statement_type": statement,
        "destination": destination,
        "referenced_tables": refs,
        "creation_time": time.format(n),
        **extra,
    }


def test_script_child_jobs_are_followed_through_temporary_tables():
    records = [
        job("s", "", [], statement="SCRIPT"),
        job("s_1", "p._script1.tmp1", ["p.d.raw"], parent="s", statement="CREATE_TABLE_AS_SELECT", n=1),
        job("s_2", "p._script1.tmp2", ["p._script1.tmp1", "p.d.ref"], parent="s", statement="CREATE_TABLE_AS_SELECT", n=2),
        job("s_3", "p.d.final", ["p._script1.tmp2"], parent="s", statement="CREATE_TABLE_AS_SELECT", n=3),
        job("plain", "p.d.other", ["p.d.raw"], statement="SELECT"),
    ]
    out, summary = expand_script_jobs(records)
    by_id = {r["job_id"]: r for r in out}
    assert set(by_id) == {"s_3", "plain"}
    assert sorted(by_id["s_3"]["referenced_tables"]) == ["p.d.raw", "p.d.ref"]
    assert summary["scripts"] == 1 and summary["parents_dropped"] == 1 and summary["temporary_tables_followed"] == 2


def test_a_script_parent_without_children_is_read_from_its_text():
    text = (
        "CREATE TEMP TABLE t AS SELECT id FROM `p.d.raw`;\n"
        "INSERT INTO `p.d.a` SELECT id FROM t;\n"
        "CREATE OR REPLACE TABLE `p.d.b` AS SELECT id FROM t JOIN `p.d.ref` USING (id);"
    )
    out, summary = expand_script_jobs([job("s", "", [], statement="SCRIPT", query=text, user_email="u@example.com")])
    assert [(r["destination"], sorted(r["referenced_tables"])) for r in out] == [("p.d.a", ["p.d.raw"]), ("p.d.b", ["p.d.raw", "p.d.ref"])]
    assert out[0]["user_email"] == "u@example.com" and summary["scripts_read_from_text"] == 1


def test_jobs_that_are_not_scripts_pass_through_unchanged():
    records = [job("a", "p.d.x", ["p.d.y"], statement="INSERT"), job("b", "", ["p.d.y"], statement="SELECT")]
    out, summary = expand_script_jobs(records)
    assert out == records and summary["scripts"] == 0


def test_script_jobs_reach_the_graph_through_load_job_history(tmp_path, monkeypatch):
    import json

    from kumosql import live_graph, state

    monkeypatch.setattr(state, "data_dir", lambda: tmp_path)
    live_graph.set_project(pipeline(final="SELECT id FROM `p.d.raw`"), "test")
    rows = [
        job("s", "", [], statement="SCRIPT"),
        job("s_1", "p._script1.tmp1", ["p.d.raw"], parent="s", n=1),
        job("s_2", "p.d.final", ["p._script1.tmp1", "p.d.ref"], parent="s", n=2),
    ]
    assert live_graph.load_job_history(json.dumps(rows), "jobs.json") == 3
    reads = live_graph.loaded()["observed_reads"]
    assert [r["job_id"] for r in reads] == ["s_2"]
    live_graph.clear_job_history()


def test_observed_usage_reads_script_text_with_blocks():
    from kumosql.observed_usage import observed_usage

    pl = pipeline(final="SELECT id FROM `p.d.raw`")
    text = "BEGIN\n  SELECT r.id FROM `p.d.raw` AS r JOIN `p.d.ref` AS f ON r.id = f.id;\nEND;"
    result = observed_usage(pl, [{"job_id": "j", "query": text, "referenced_tables": ["p.d.raw", "p.d.ref"], "user_email": "a"}])
    assert result.records_examined == 1 and not result.records_unexamined


# ------------------------------------------------------------------------- rewriting


def test_rewrite_rules_run_inside_blocks_and_leave_the_rest_alone():
    from kumosql.rewrite import apply_rule

    script = """DECLARE d DATE DEFAULT CURRENT_DATE();
BEGIN
  IF d > '2020-01-01' THEN
    CREATE TEMP TABLE t AS SELECT a FROM `p.d.x` WHERE 1 = 1 AND a > 1;
  END IF;
  EXECUTE IMMEDIATE "SELECT 1 WHERE 1 = 1";
  INSERT INTO `p.d.y` SELECT a FROM t WHERE TRUE;
EXCEPTION WHEN ERROR THEN SELECT 'x;y';
END;
"""
    result = apply_rule("remove_trivial_predicates", script)
    assert "1 = 1" not in result.sql.split("EXECUTE")[0] and "WHERE TRUE" not in result.sql
    assert 'EXECUTE IMMEDIATE "SELECT 1 WHERE 1 = 1";' in result.sql
    assert result.sql.startswith("DECLARE d DATE DEFAULT CURRENT_DATE();\nBEGIN\n  IF d > '2020-01-01' THEN")
    assert result.rule_success and result.verification.trusted


# ------------------------------------------------- rewrite verification: the skeleton around the queries

SKELETON_SCRIPT = (
    "DECLARE n INT64 DEFAULT 1;\nIF n = 1 THEN\n  SELECT id FROM `p.d.t` WHERE age > 3;\nELSE\n  BEGIN\n    SELECT id FROM `p.d.t`;\n  END;\nEND IF;\n"
)


@pytest.mark.parametrize(
    "after",
    [
        SKELETON_SCRIPT.replace("IF n = 1", "IF n = 2"),
        SKELETON_SCRIPT.replace("DEFAULT 1", "DEFAULT 5"),
        SKELETON_SCRIPT.replace("ELSE\n", "ELSEIF n = 2 THEN\n"),
        SKELETON_SCRIPT.replace("DECLARE n INT64", "DECLARE m INT64"),
        SKELETON_SCRIPT.replace("END IF;", "END IF;\nSET n = 3;"),
    ],
    ids=["if-condition", "declare-default", "else-to-elseif", "declare-name", "added-statement"],
)
def test_changing_only_the_control_flow_around_the_queries_is_not_proven(after):
    """Verification used to compare only the queries, so these were reported proven."""

    from kumosql.rewrite import _verify_sql

    ok, problems = _verify_sql(SKELETON_SCRIPT, after)
    assert not ok and problems


def test_layout_and_comments_around_the_queries_do_not_matter_but_a_changed_query_is_still_proven_or_refused():
    from kumosql.rewrite import _verify_sql

    reflowed = SKELETON_SCRIPT.replace("\n  ", "\n      ").replace("IF n = 1", "if n  =  1").replace("DECLARE", "-- note\nDECLARE")
    assert _verify_sql(SKELETON_SCRIPT, reflowed) == (True, [])
    assert _verify_sql(SKELETON_SCRIPT, SKELETON_SCRIPT.replace("WHERE age > 3", "WHERE 3 < age")) == (True, [])
    ok, problems = _verify_sql(SKELETON_SCRIPT, SKELETON_SCRIPT.replace("age > 3", "age > 4"))
    assert not ok and problems
