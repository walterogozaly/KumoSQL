"""Script lineage follows BigQuery's scripting state and never confidently misses a source (docs/scripts.md).

The parametrized cases come from an external audit (``tests/fixtures/scripts_s014/cases.json``): each names the
tables every written table must come from, every table the script reads, and how many statements are unknown.
"""

import json
from pathlib import Path

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target
from kumosql.scripts import analyse_script

CASES = {case["id"]: case for case in json.loads((Path(__file__).parent / "fixtures" / "scripts_s014" / "cases.json").read_text())["cases"]}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_script_reads_and_writes(case_id):
    case = CASES[case_id]
    analysis = analyse_script(case["sql"])
    writes: dict[str, set[str]] = {}
    for write in analysis.writes:
        writes.setdefault(write.table.name, set()).update(t.name for t in write.sources)
    assert writes == {table: set(sources) for table, sources in case["writes"].items()}
    assert {t.name for t in analysis.all_reads()} == set(case["reads"])
    assert len(analysis.unknown) == case["unknown"]
    assert analysis.report()["unknown"] == case["unknown"]


def pipeline(sql: str, *names: str, project: str = "p", dataset: str = "d") -> Pipeline:
    sources = {f"{project}.{dataset}.{name}": Target(project, dataset, name) for name in names}
    schema = {key: {"v": "INT64", "w": "INT64", "k": "INT64"} for key in sources}
    target = Target(project, dataset, "m")
    return Pipeline({target.key: Model(target, "table", sql)}, sources, schema)


def test_a_rolled_back_insert_feeds_no_column():
    pl = pipeline(
        "CREATE TEMP TABLE t AS SELECT v FROM source_a; BEGIN TRANSACTION; INSERT INTO t SELECT v FROM source_b; "
        "ROLLBACK TRANSACTION; SELECT v FROM t;",
        "source_a",
        "source_b",
    )
    assert pl.column_lineage()[ColumnRef("p.d.m", "v")] == {ColumnRef("p.d.source_a", "v")}
    assert "skipped_statements" not in {d.code for d in pl.all_diagnostics()}
    # The INSERT still runs before the rollback, so a change to what it reads is not "no impact".
    impact = pl.assess_change("drop_column", "p.d.source_b", "v")
    assert {u.model: u.reason for u in impact.unknown} == {"p.d.m": "script_columns"}
    assert not impact.complete


def test_a_rollback_that_may_not_run_keeps_the_change():
    analysis = analyse_script(
        "CREATE TEMP TABLE t AS SELECT v FROM source_a; BEGIN TRANSACTION; INSERT INTO t SELECT v FROM source_b; "
        "IF RAND() > 0.5 THEN ROLLBACK TRANSACTION; END IF; CREATE TABLE dest AS SELECT v FROM t;"
    )
    assert {t.name for t in analysis.writes[0].sources} == {"source_a", "source_b"}


def test_a_session_qualified_temporary_table_is_the_temporary_table():
    pl = pipeline("CREATE TEMP TABLE _SESSION.t AS SELECT v FROM source_a; SELECT v FROM _SESSION.t;", "source_a", "t")
    assert pl.column_lineage()[ColumnRef("p.d.m", "v")] == {ColumnRef("p.d.source_a", "v")}
    assert pl.upstream["p.d.m"] == {"p.d.source_a"}


def test_a_drop_that_may_not_run_leaves_the_temporary_table():
    pl = pipeline(
        "CREATE TEMP TABLE t AS SELECT v FROM source_a; IF FALSE THEN DROP TABLE t; END IF; SELECT v FROM t;", "source_a", "t"
    )
    assert pl.column_lineage()[ColumnRef("p.d.m", "v")] == {ColumnRef("p.d.source_a", "v")}


def test_set_dataset_id_moves_unqualified_names():
    sources = {key: Target(*key.split(".")) for key in ("p.old.t", "p.new.t", "q.new.u")}
    schema = {key: {"v": "INT64"} for key in sources}
    pl = Pipeline({"p.old.m": Model(Target("p", "old", "m"), "table", "SET @@dataset_id = 'new'; SELECT v FROM t;")}, sources, schema)
    assert pl.upstream["p.old.m"] == {"p.new.t"}
    pl = Pipeline(
        {"p.old.m": Model(Target("p", "old", "m"), "table", "SET @@dataset_project_id = 'q'; SET @@dataset_id = 'new'; SELECT v FROM u;")},
        sources,
        schema,
    )
    assert pl.upstream["p.old.m"] == {"q.new.u"}
    # A value that is not a constant moves names somewhere unknown: the statement says so.
    analysis = analyse_script("DECLARE d STRING DEFAULT 'new'; SET @@dataset_id = d; SELECT v FROM t;")
    assert [(s.kind, s.disposition) for s in analysis.unknown] == [("set", "unknown")]


def test_a_condition_is_a_dependency_of_what_it_guards():
    pl = pipeline("IF (SELECT MAX(w) FROM guard) > 0 THEN SELECT v FROM body; END IF;", "guard", "body")
    assert pl.upstream["p.d.m"] == {"p.d.guard", "p.d.body"}
    for kind in ("drop_column", "change_expression"):
        impact = pl.assess_change(kind, "p.d.guard", "w")
        assert {u.model: u.reason for u in impact.unknown} == {"p.d.m": "script_columns"}
    # A column no statement names is not read.
    assert not pl.assess_change("drop_column", "p.d.guard", "k").unknown


def test_a_variable_set_from_a_condition_carries_it():
    analysis = analyse_script(
        "DECLARE x INT64 DEFAULT 0; IF (SELECT COUNT(*) FROM guard) > 0 THEN SET x = 1; END IF; "
        "CREATE TABLE dest AS SELECT x AS v;"
    )
    assert {t.name for t in analysis.writes[0].sources} == {"guard"}


def test_a_declared_variable_read_elsewhere_makes_the_script_an_unknown_reader():
    pl = pipeline("DECLARE x INT64 DEFAULT (SELECT MAX(w) FROM source_b); SELECT v FROM source_a WHERE v > x;", "source_a", "source_b")
    impact = pl.assess_change("drop_column", "p.d.source_b", "w")
    assert {u.model: u.reason for u in impact.unknown} == {"p.d.m": "script_columns"}


def test_loop_carried_values_reach_earlier_statements():
    analysis = analyse_script(
        "DECLARE a INT64 DEFAULT (SELECT MAX(v) FROM source_a); DECLARE b INT64 DEFAULT 0; DECLARE c INT64 DEFAULT 0; "
        "LOOP INSERT INTO dest SELECT c AS v; SET c = b; SET b = a; IF c > 9 THEN LEAVE; END IF; END LOOP;"
    )
    assert {t.name for t in analysis.writes[0].sources} == {"source_a"}


def test_procedure_parameters_have_their_own_scope_and_modes():
    analysis = analyse_script(
        "DECLARE x INT64 DEFAULT (SELECT MAX(v) FROM source_a); DECLARE y INT64; "
        "CREATE PROCEDURE f(IN x INT64, OUT r INT64) BEGIN SET r = (SELECT MAX(v) FROM source_b WHERE v > x); END; "
        "CALL f(x, y); CREATE TABLE dest AS SELECT x AS a, y AS b;"
    )
    assert {t.name for t in analysis.writes[0].sources} == {"source_a", "source_b"}


def test_execute_immediate_binds_using_and_into():
    analysis = analyse_script(
        "DECLARE x INT64 DEFAULT (SELECT MAX(v) FROM source_a); DECLARE y INT64; "
        "EXECUTE IMMEDIATE 'SELECT MAX(v) FROM source_b WHERE v > ?' INTO y USING x; CREATE TABLE dest AS SELECT y AS v;"
    )
    assert {t.name for t in analysis.writes[0].sources} == {"source_a", "source_b"}
    # INTO from dynamic text: the variable's sources are unknown, and so is the statement.
    analysis = analyse_script("DECLARE s STRING DEFAULT 'SELECT 1'; DECLARE y INT64; EXECUTE IMMEDIATE s INTO y;")
    assert [s.kind for s in analysis.unknown] == ["execute_immediate"]


def test_unknown_statements_inside_a_call_are_counted():
    analysis = analyse_script("CREATE PROCEDURE f() BEGIN CALL missing(); END; CALL f(); SELECT 1 AS v;")
    report = analysis.report()
    assert (report["unknown"], report["by_kind"]["unknown"]) == (1, {"call": 1})
    assert "1 unknown (call x1; 1 nested)" in analysis.summary()
