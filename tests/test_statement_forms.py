"""Statements sqlglot rejects or keeps as raw text, read by their shape (kumosql.statement_forms).

EXPORT MODEL, UNDROP SCHEMA, LOAD DATA, CREATE SNAPSHOT TABLE, search and vector indexes, row access policies, DROP EXTERNAL TABLE and
DROP SNAPSHOT TABLE: each one is recognised exactly or refused, parses the same way on every sqlglot release, passes through cleanup
unchanged and reads and writes the right tables in a script.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)
from kumosql import rewrite
from kumosql.pipeline import load_sqlx_project
from kumosql.scripts import IGNORED, KEPT, UNKNOWN, analyse_script
from kumosql.statement_forms import recognise, recognise_command

LOAD = "LOAD DATA INTO `p.d.t` FROM FILES (format = 'CSV', uris = ['gs://b/in/*.csv'])"
CLEANUP = ("remove_trivial_predicates", "remove_redundant_parentheses", "deduplicate_ctes", "remove_unused_ctes", "inline_single_use_ctes", "remove_redundant_distinct")


def _kinds(sql: str) -> list[str]:
    return [statement.__class__.__name__ for statement in sqlglot.parse(sql, read="bigquery")]


# ----------------------------------------------------------------------------------- recognised forms


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("EXPORT MODEL `p.d.m` OPTIONS (uri = 'gs://b/m/')", "export_model"),
        ("export model d.m options (uri = 'gs://b/m/')", "export_model"),
        ("UNDROP SCHEMA `p.ds`", "undrop"),
        ("UNDROP SCHEMA IF NOT EXISTS ds OPTIONS (location = 'us')", "undrop"),
        (LOAD, "load_data"),
        ("LOAD DATA OVERWRITE p.d.t (id INT64, name STRING) PARTITION BY DATE(ts) FROM FILES (format = 'PARQUET', uris = ['gs://b/x'])", "load_data"),
        ("LOAD DATA INTO p.d.t FROM FILES (format = 'PARQUET', uris = ['gs://b/x']) WITH PARTITION COLUMNS (dt DATE)", "load_data"),
        ("LOAD DATA INTO p.d.t FROM FILES (format = 'CSV', uris = ['g']) WITH CONNECTION `p.us.c`", "load_data"),
        ("LOAD DATA INTO TEMP TABLE tmp_load FROM FILES (format = 'JSON', uris = ['gs://b/*.json'])", "load_data"),
        ("LOAD DATA INTO p.d.t (a INT64, b STRUCT<x INT64, y STRING>) CLUSTER BY a, b OPTIONS (description = 'x') FROM FILES (format = 'CSV', uris = ['g'])", "load_data"),
        ("LOAD DATA INTO my-project.d.t FROM FILES (format = 'CSV', uris = ['g'])", "load_data"),
        ("CREATE SNAPSHOT TABLE p.d.s CLONE p.d.t OPTIONS (expiration_timestamp = TIMESTAMP '2030-01-01')", "create_snapshot_table"),
        ("CREATE OR REPLACE SNAPSHOT TABLE IF NOT EXISTS p.d.s CLONE p.d.t FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)", "create_snapshot_table"),
        ("CREATE EXTERNAL TABLE p.d.e (a INT64) WITH CONNECTION `p.us.c` OPTIONS (format = 'CSV', uris = ['g'])", "create_external_table"),
        ("CREATE SEARCH INDEX IF NOT EXISTS i ON p.d.t (ALL COLUMNS) OPTIONS (analyzer = 'LOG_ANALYZER')", "index"),
        ("CREATE VECTOR INDEX i ON p.d.t(emb) STORING (a) PARTITION BY dt OPTIONS (index_type = 'IVF')", "index"),
        ("DROP SEARCH INDEX IF EXISTS i ON p.d.t", "index"),
        ("DROP VECTOR INDEX i ON p.d.t", "index"),
        ("CREATE OR REPLACE ROW ACCESS POLICY r ON p.d.t GRANT TO ('user:a@example.com') FILTER USING (state = 'CA')", "row_access_policy"),
        ("DROP ROW ACCESS POLICY IF EXISTS r ON p.d.t", "row_access_policy"),
        ("DROP ALL ROW ACCESS POLICIES ON p.d.t", "row_access_policy"),
        ("CREATE RESERVATION `region-us.res` OPTIONS (slot_capacity = 0, edition = 'STANDARD')", "reservation"),
    ],
)
def test_a_statement_that_matches_a_form_is_recognised(sql, kind):
    form = recognise(sql)
    assert form is not None and form.kind == kind


def test_the_tables_a_form_names_are_the_ones_it_writes_reads_or_sits_on():
    load = recognise("LOAD DATA OVERWRITE `p.d.t` (id INT64, name STRING) FROM FILES (format = 'CSV', uris = ['g'])")
    assert (load.table.catalog, load.table.db, load.table.name) == ("p", "d", "t")
    assert load.replace and not load.temp and load.columns == ("id", "name") and load.source is None
    temp = recognise("LOAD DATA INTO TEMP TABLE tmp FROM FILES (format = 'CSV', uris = ['g'])")
    assert temp.temp and temp.table.name == "tmp"
    snapshot = recognise("CREATE SNAPSHOT TABLE p.d.s CLONE p.d.t")
    assert snapshot.table.name == "s" and snapshot.source.name == "t"
    assert recognise("CREATE SEARCH INDEX i ON p.d.t (ALL COLUMNS)").on.name == "t"
    assert recognise("CREATE ROW ACCESS POLICY r ON p.d.t GRANT TO ('x') FILTER USING (a = 1)").on.name == "t"
    assert recognise("EXPORT MODEL p.d.m OPTIONS (uri = 'g')").table is None  # a model is not a table
    assert recognise("UNDROP SCHEMA ds").table is None


def test_export_data_keeps_the_text_of_its_query():
    form = recognise("EXPORT DATA OPTIONS (uri = 'gs://b/*.csv', format = 'CSV') AS SELECT a FROM p.d.t")
    assert form.kind == "export_data" and form.query == "SELECT a FROM p.d.t"
    assert not form.opaque


@pytest.mark.parametrize(
    "sql",
    [
        "EXPORT MODEL p.d.m",  # OPTIONS is required
        "EXPORT MODEL p.d.m OPTIONS (uri = 'x') AS SELECT 1",
        "EXPORT MODEL p.d.m OPTIONS (uri = 'x'",  # unbalanced
        "UNDROP TABLE p.d.t",
        "UNDROP SCHEMA",
        "UNDROP SCHEMA ds EXTRA",
        "LOAD DATA INTO p.d.t",  # no FROM FILES
        "LOAD DATA INTO p.d.t FROM FILES (format = 'CSV', uris = ['g']) EXTRA",
        "LOAD DATA INTO p.d.t FROM FILES (format = 'CSV', uris = ['g'",
        "LOAD DATA INTO p.d.t SELECT 1 FROM FILES (format = 'CSV', uris = ['g'])",
        "LOAD DATA INTO FROM FILES (format = 'CSV', uris = ['g'])",
        "LOAD DATA INTO p.d.t FROM FILES (format = 'CSV', uris = ['g']); SELECT 1",
        "LOAD DATA p.d.t FROM FILES (format = 'CSV', uris = ['g'])",
        "CREATE SNAPSHOT TABLE p.d.s AS SELECT 1",
        "CREATE SNAPSHOT TABLE p.d.s CLONE (SELECT 1)",
        "CREATE SNAPSHOT TABLE p.d.s CLONE p.d.t UNEXPECTED",
        "CREATE SEARCH INDEX i ON p.d.t",  # the column list is required
        "DROP SEARCH INDEX i",  # ON is required
        "CREATE ROW ACCESS POLICY r ON p.d.t FILTER USING (a = 1)",  # GRANT TO is required
        "DROP ROW ACCESS POLICY r",
        "DROP ALL ROW ACCESS POLICIES",
        "CREATE RESERVATION r",
    ],
)
def test_a_statement_that_does_not_match_a_form_exactly_is_refused(sql):
    assert recognise(sql) is None


def test_a_refused_statement_is_never_read_as_something_else_in_a_script():
    analysis = analyse_script("LOAD DATA INTO p.d.t FROM FILES (format = 'CSV', uris = ['g']) EXTRA CLAUSE")
    (statement,) = analysis.statements
    assert statement.disposition == UNKNOWN and statement.degraded
    assert [w.kind for w in analysis.writes] == ["opaque"]  # only what its tokens say: a table it may write, never a read
    assert analysis.reads == {}


# ----------------------------------------------------------------------------------- one parse on every release


@pytest.mark.parametrize(
    "sql",
    [
        "EXPORT MODEL `p.d.m` OPTIONS (uri = 'gs://b/m/')",
        "UNDROP SCHEMA IF NOT EXISTS `p.ds`",
        LOAD,
        "LOAD DATA OVERWRITE `p.d.t` (id INT64, name STRING) PARTITION BY DATE(ts) FROM FILES (format = 'PARQUET', uris = ['gs://b/x'])",
        "LOAD DATA INTO `p.d.t` FROM FILES (format = 'PARQUET', uris = ['gs://b/x']) WITH PARTITION COLUMNS (dt DATE)",
        "LOAD DATA INTO TEMP TABLE tmp_load FROM FILES (format = 'JSON', uris = ['gs://b/*.json'])",
    ],
)
def test_the_opaque_forms_parse_as_one_raw_text_command_that_prints_back_as_written(sql):
    (tree,) = sqlglot.parse(sql, read="bigquery")
    assert isinstance(tree, exp.Command)
    assert recognise_command(tree).opaque
    assert tree.sql("bigquery") == sql


def test_a_script_keeps_each_command_apart_from_the_statements_around_it():
    sql = f"SELECT 1 AS a;\n{LOAD};\nEXPORT MODEL p.d.m OPTIONS (uri = 'g');\nSELECT b FROM p.d.t"
    trees = sqlglot.parse(sql, read="bigquery")
    assert [type(tree).__name__ for tree in trees] == ["Select", "Command", "Command", "Select"]
    assert [tree.this for tree in trees[1:3]] == ["LOAD", "EXPORT"]


def test_a_statement_that_does_not_match_a_form_is_left_to_sqlglot():
    with pytest.raises(sqlglot.errors.SqlglotError):
        sqlglot.parse_one("EXPORT MODEL OPTIONS", read="bigquery")


@pytest.mark.parametrize("kind", ["EXTERNAL", "SNAPSHOT"])
def test_drop_external_and_snapshot_tables_are_drops_of_that_kind(kind):
    for sql in (f"DROP {kind} TABLE `p.d.t`", f"DROP {kind} TABLE IF EXISTS `p.d.t`", f"drop {kind.lower()} table d.t"):
        (tree,) = sqlglot.parse(sql, read="bigquery")
        assert isinstance(tree, exp.Drop) and tree.args.get("kind") == f"{kind} TABLE"
        assert tree.sql("bigquery").upper() == " ".join(sql.split()).upper()


def test_dropping_a_plain_table_and_a_column_named_external_are_unchanged():
    table, view = sqlglot.parse("DROP TABLE p.d.t; DROP VIEW p.d.v", read="bigquery")
    assert table.args.get("kind") == "TABLE" and view.args.get("kind") == "VIEW"
    (select,) = sqlglot.parse("SELECT external FROM p.d.t", read="bigquery")
    assert isinstance(select, exp.Select)


# ----------------------------------------------------------------------------------- cleanup leaves them alone


@pytest.mark.parametrize(
    "sql",
    [
        "EXPORT MODEL `p.d.m` OPTIONS (uri = 'gs://b/m/')",
        "UNDROP SCHEMA IF NOT EXISTS `p.ds`",
        LOAD,
        "LOAD DATA OVERWRITE `p.d.t` (id INT64, name STRING) PARTITION BY DATE(ts) FROM FILES (format = 'PARQUET', uris = ['gs://b/x'])",
        "LOAD DATA INTO `p.d.t` FROM FILES (format = 'PARQUET', uris = ['gs://b/x']) WITH PARTITION COLUMNS (dt DATE)",
        "LOAD DATA INTO TEMP TABLE tmp_load FROM FILES (format = 'JSON', uris = ['gs://b/*.json'])",
        "DROP EXTERNAL TABLE IF EXISTS `p.d.e`",
        "DROP SNAPSHOT TABLE IF EXISTS `p.d.s`",
    ],
)
def test_cleanup_leaves_the_statement_unchanged_with_no_diagnostic(sql):
    result = rewrite.apply_rules(CLEANUP, sql)
    assert result.sql == sql
    assert result.verification.status == rewrite.VerificationStatus.UNCHANGED
    codes = {d.code for step in getattr(result, "steps", ()) for d in getattr(step, "diagnostics", ())}
    assert not codes & {"recovered_parse", "parse_error", "output_parse_error"}


def test_cleanup_still_cleans_the_query_next_to_a_command_and_keeps_the_command_as_written():
    sql = f"{LOAD};\nSELECT a FROM p.d.t WHERE 1 = 1"
    result = rewrite.apply_rules(CLEANUP, sql)
    assert LOAD in result.sql and "1 = 1" not in result.sql


# ----------------------------------------------------------------------------------- tables in a script


def test_load_data_writes_its_table_and_reads_none():
    analysis = analyse_script(LOAD)
    (statement,) = analysis.statements
    assert statement.kind == "load_data" and statement.disposition == KEPT
    assert [(w.table.catalog, w.table.db, w.table.name) for w in analysis.writes] == [("", "p", "d.t")] or [
        w.table.name for w in analysis.writes
    ] == ["t"]
    assert analysis.reads == {} and analysis.side_reads == {}


def test_a_table_loaded_from_files_is_not_read_from_anywhere():
    analysis = analyse_script("LOAD DATA OVERWRITE p.d.t (a INT64) FROM FILES (format = 'CSV', uris = ['g']); SELECT a FROM p.d.t")
    assert [w.table.name for w in analysis.writes] == ["t"]


def test_a_temporary_table_loaded_from_files_is_a_temporary_table_with_opaque_rows():
    analysis = analyse_script(
        "LOAD DATA INTO TEMP TABLE tmp (a INT64) FROM FILES (format = 'JSON', uris = ['g']);\nINSERT INTO p.d.o SELECT a FROM tmp"
    )
    assert analysis.opaque_temps == {"tmp": "its rows come from files"}
    assert [w.table.name for w in analysis.writes] == ["o"]
    assert analysis.reads == {}  # no real table feeds p.d.o


def test_export_model_and_undrop_touch_no_table():
    analysis = analyse_script("EXPORT MODEL p.d.m OPTIONS (uri = 'g');\nUNDROP SCHEMA IF NOT EXISTS p.ds")
    assert [s.disposition for s in analysis.statements] == [IGNORED, IGNORED]
    assert analysis.writes == [] and analysis.reads == {}


def test_a_snapshot_table_reads_its_source_and_writes_itself():
    analysis = analyse_script("CREATE SNAPSHOT TABLE p.d.snap CLONE p.d.src OPTIONS (expiration_timestamp = TIMESTAMP '2030-01-01')")
    assert [w.table.name for w in analysis.writes] == ["snap"]
    assert sorted(t.name for t in analysis.side_reads.values()) == ["src"]


def test_an_external_table_reads_only_files():
    analysis = analyse_script("CREATE EXTERNAL TABLE p.d.e (a INT64) OPTIONS (format = 'CSV', uris = ['g'])")
    assert [w.table.name for w in analysis.writes] == ["e"] and analysis.reads == {}


def test_indexes_and_reservations_move_no_data_and_a_row_access_policy_changes_its_table():
    analysis = analyse_script(
        "CREATE SEARCH INDEX i ON p.d.t (ALL COLUMNS);\nDROP VECTOR INDEX v ON p.d.t;\nCREATE RESERVATION `region-us.r` OPTIONS (slot_capacity = 0)"
    )
    assert [s.disposition for s in analysis.statements] == [IGNORED, IGNORED, IGNORED]
    assert analysis.writes == [] and analysis.reads == {}
    policy = analyse_script("CREATE ROW ACCESS POLICY r ON p.d.t GRANT TO ('x') FILTER USING (a = 1);\nDROP ALL ROW ACCESS POLICIES ON p.d.u")
    assert sorted(w.table.name for w in policy.writes) == ["t", "u"] and policy.reads == {}


def test_a_dropped_external_or_snapshot_table_is_a_dropped_table():
    analysis = analyse_script("DROP EXTERNAL TABLE IF EXISTS p.d.e;\nDROP SNAPSHOT TABLE p.d.s")
    assert [w.table.name for w in analysis.writes] == ["e", "s"]


def test_a_default_dataset_qualifies_the_table_a_load_writes():
    analysis = analyse_script("SET @@dataset_id = 'ds';\nLOAD DATA INTO t FROM FILES (format = 'CSV', uris = ['g'])")
    assert [(w.table.db, w.table.name) for w in analysis.writes] == [("ds", "t")]


# ----------------------------------------------------------------------------------- in a pipeline


def test_a_pipeline_file_that_loads_files_reads_no_table(tmp_path):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultLocation: US\ndefaultDataset: d\n")
    (tmp_path / "definitions").mkdir()
    (tmp_path / "definitions" / "load.sql").write_text(LOAD)
    (tmp_path / "definitions" / "snap.sql").write_text("CREATE SNAPSHOT TABLE `p.d.snap` CLONE `p.d.src`")
    pipeline = load_sqlx_project(tmp_path)
    assert not [d for d in pipeline.all_diagnostics() if d.code in {"read_error", "asset_unreadable", "sqlx_parse_error", "no_query"}]
    external = [d.message for d in pipeline.all_diagnostics() if d.code == "external_tables"]
    assert external == ["reads tables outside the pipeline: p.d.src"]
