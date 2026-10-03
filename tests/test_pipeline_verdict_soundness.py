"""Pipeline-level verdicts that were wrong (Sol's S011 audit): every case here must stay unproven or diverge.

Each case was replayed with real data: the "equivalent", "safe" or "rewritten" answer returned different rows.
"""

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql import load_sqlx_project, refactor  # noqa: E402
from kumosql.containment import check_containment  # noqa: E402
from kumosql.incremental import (  # noqa: E402
    IncrementalModel,
    SourceTable,
    Simulation,
    check_incremental,
    parse_incremental_sqlx,
    replay,
)
from kumosql.model_reuse import rewrite_over_model  # noqa: E402
from kumosql.ast_utils import captured_names  # noqa: E402
from kumosql.pipeline_equivalence import prove_models  # noqa: E402

TABLE = 'config { type: "table" }\n'


def project(root, files):
    (root / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: d\n", encoding="utf-8")
    for name, text in files.items():
        path = root / "definitions" / f"{name}.sqlx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return load_sqlx_project(root)


def accepted(result):
    return [state for state in result.front if state.moves]


# ------------------------------------------------- S011-001: a reader's WITH table captures an inlined read


CAPTURE = {
    "m": TABLE + "SELECT x FROM q\n",  # the physical table q (holds 1)
    "a": TABLE + 'WITH q AS (SELECT 2 AS x) SELECT x FROM ${ref("m")}\n',  # returns 1, not 2
    "b": TABLE + "SELECT 2 AS x\n",
}


def test_inlining_under_a_with_table_of_the_same_name_is_not_a_proof(tmp_path):
    result = prove_models(project(tmp_path, CAPTURE), "a", "b", declared=[])
    assert not result.proven


def test_refactor_does_not_inline_a_model_where_the_reader_captures_its_source(tmp_path):
    pipeline = project(tmp_path, CAPTURE)
    roles = {"proj.d.m": "editable", "proj.d.a": "protected", "proj.d.b": "frozen"}
    result = refactor.search(pipeline, roles=roles, max_seconds=60)
    assert accepted(result) == []


def test_capture_is_judged_by_scope():
    import sqlglot

    reader = sqlglot.parse_one("SELECT * FROM (WITH q AS (SELECT 1) SELECT * FROM q) s JOIN d.m ON TRUE", read="bigquery")
    at = next(t for t in reader.find_all(sqlglot.exp.Table) if t.name == "m")
    body = sqlglot.parse_one("WITH z AS (SELECT 1) SELECT * FROM q JOIN z ON TRUE", read="bigquery")
    assert captured_names(body, at) == set()  # the reader's q is not in scope at d.m
    inner = next(t for t in reader.find_all(sqlglot.exp.Table) if t.name == "q")
    assert captured_names(body, inner) == {"q"}  # z is the body's own


# ------------------------------------------------- S011-002: identical SQL is not identical rows


CLOCKS = {
    "m": TABLE + "SELECT CURRENT_TIMESTAMP() AS ts\n",
    "b": TABLE + "SELECT CURRENT_TIMESTAMP() AS ts\n",
    "a": TABLE + 'SELECT ts FROM ${ref("m")}\n',
    "c": TABLE + 'SELECT ts FROM ${ref("b")}\n',
}


def test_two_runs_of_the_same_clock_query_are_not_equal_tables(tmp_path):
    result = prove_models(project(tmp_path, CLOCKS), "a", "c", declared=[])
    assert not result.proven and result.lemmas == []


def same_body(tmp_path, body):
    files = {
        "src": 'config { type: "declaration", schema: "raw", name: "src" }\n',
        "m": TABLE + body + "\n",
        "b": TABLE + body + "\n",
        "a": TABLE + 'SELECT * FROM ${ref("m")}\n',
        "c": TABLE + 'SELECT * FROM ${ref("b")}\n',
    }
    return prove_models(project(tmp_path, files), "a", "c", declared=[])


@pytest.mark.parametrize("body", [
    "SELECT id FROM ${ref('raw', 'src')} LIMIT 1",
    "SELECT id, RAND() AS r FROM ${ref('raw', 'src')}",
    "SELECT CURRENT_DATETIME() AS d",
    "SELECT ARRAY_AGG(id) AS ids FROM ${ref('raw', 'src')}",
])
def test_two_runs_of_the_same_nondeterministic_query_are_not_equal_tables(tmp_path, body):
    assert not same_body(tmp_path, body).proven


def test_a_tie_sensitive_window_is_matched_only_under_the_solvers_stated_assumption(tmp_path):
    result = same_body(tmp_path, "SELECT id, ROW_NUMBER() OVER (ORDER BY grp) AS rn FROM ${ref('raw', 'src')}")
    assert not result.proven or any("ties in ORDER BY" in note for note in result.assumptions)


def test_readers_of_one_clock_table_are_still_equal(tmp_path):
    files = dict(CLOCKS, c=TABLE + 'SELECT ts FROM ${ref("m")} AS other\n')
    assert prove_models(project(tmp_path, files), "a", "c", declared=[]).proven


def test_refactor_does_not_merge_two_clock_tables(tmp_path):
    pipeline = project(tmp_path, CLOCKS)
    roles = {"proj.d.m": "editable", "proj.d.a": "protected", "proj.d.b": "frozen", "proj.d.c": "frozen"}
    result = refactor.search(pipeline, roles=roles, max_seconds=60)
    assert accepted(result) == []


def test_an_incremental_table_is_not_its_query(tmp_path):
    files = {
        "src": 'config { type: "declaration", schema: "raw", name: "src" }\n',
        "m": 'config { type: "incremental" }\nSELECT x FROM ${ref("raw", "src")}\n',  # appends every run
        "f": TABLE + 'SELECT x FROM ${ref("raw", "src")}\n',
        "a": TABLE + 'SELECT x FROM ${ref("m")}\n',
        "b": TABLE + 'SELECT x FROM ${ref("raw", "src")}\n',
    }
    pipeline = project(tmp_path, files)
    assert not prove_models(pipeline, "a", "b", declared=[]).proven
    assert not prove_models(pipeline, "m", "f", declared=[]).proven


# ------------------------------------------------- S011-003: Dataform settings that change the run


SRC = {"src": SourceTable({"id": "INT64", "ts": "TIMESTAMP"}, ("id",), "ts")}
WATERMARK = "${when(incremental(), `WHERE ts > COALESCE((SELECT MAX(ts) FROM ${self()}), TIMESTAMP('1999-01-01'))`)}"


def sqlx(config):
    return f"config {{ type: 'incremental'{config} }}\nSELECT id, ts FROM ${{ref('src')}}\n{WATERMARK}"


def test_update_partition_filter_is_read_and_simulated():
    config = ", uniqueKey: ['id'], bigquery: { partitionBy: 'DATE(ts)', updatePartitionFilter: \"ts >= TIMESTAMP('2024-01-02')\" }"
    model = parse_incremental_sqlx(sqlx(config), "target")
    assert model.update_partition_filter == "ts >= TIMESTAMP('2024-01-02')"
    verdict = check_incremental(model, SRC, ["insert_new", "update_touch", "empty"], seeds=20)
    assert verdict.outcome != "safe"
    # the old Jan 1 row is outside the filter, so the MERGE inserts the Jan 3 version beside it
    results = replay(model, SRC, ["INSERT INTO src VALUES (1, TIMESTAMP '2024-01-01')"],
                     [["UPDATE src SET ts = TIMESTAMP '2024-01-03' WHERE id = 1"]])
    assert results[-1].status == "diverge" and len(results[-1].only_incremental) == 1


def test_the_same_model_without_the_filter_is_still_safe():
    model = parse_incremental_sqlx(sqlx(", uniqueKey: ['id'], bigquery: { partitionBy: 'DATE(ts)' }"), "target")
    assert check_incremental(model, SRC, ["insert_new", "update_touch", "empty"]).outcome == "safe"


@pytest.mark.parametrize("config", [
    ", incrementalStrategy: 'insert_overwrite', bigquery: { partitionBy: 'DATE(ts)' }",
    ", uniqueKey: KEYS",
    ", uniqueKey: ['id'], bigquery: { updatePartitionFilter: 'ts >= ' + START }",
    ", uniqueKey: ['id'], incrementalStrategy: 'append'",
])
def test_settings_that_are_not_modelled_are_unsupported(config):
    model = parse_incremental_sqlx(sqlx(config), "target")
    assert model.unmodelled
    assert check_incremental(model, SRC, ["insert_new", "empty"]).outcome == "unsupported"


def test_post_operations_are_unsupported():
    text = sqlx("") + "\npost_operations { DELETE FROM ${self()} WHERE id = 0 }\n"
    model = parse_incremental_sqlx(text, "target")
    assert check_incremental(model, SRC, ["insert_new", "empty"]).outcome == "unsupported"


# ------------------------------------------------- S011-004 / 005: the watermark must mean MAX(ts) of the table


def plain(where, key=()):
    return IncrementalModel("target", "SELECT id, ts FROM src", f"SELECT id, ts FROM src WHERE {where}", tuple(key))


@pytest.mark.parametrize("subquery", [
    "SELECT MAX(ts) FROM target HAVING FALSE",
    "SELECT MAX(ts) FROM target WHERE id < 0",
    "SELECT MAX(ts) FROM target LIMIT 0",
    "SELECT MAX(ts) FROM target JOIN src ON FALSE",
    "SELECT MAX(src.ts) FROM target",
    "SELECT MAX(ts) FROM other.target",
])
def test_a_max_that_is_not_the_tables_newest_time_is_not_a_watermark(subquery):
    model = plain(f"ts > COALESCE(({subquery}), TIMESTAMP('1999-01-01'))")
    assert check_incremental(model, SRC, ["insert_new", "empty"], seeds=20).outcome != "safe"


def test_having_false_reappends_old_rows():
    model = plain("ts > COALESCE((SELECT MAX(ts) FROM target HAVING FALSE), TIMESTAMP('1999-01-01'))")
    results = replay(model, SRC, ["INSERT INTO src VALUES (1, TIMESTAMP '2024-01-01')"],
                     [["INSERT INTO src VALUES (2, TIMESTAMP '2024-01-01 01:00:00')"]])
    assert results[-1].status == "diverge"


@pytest.mark.parametrize("default", [
    "TIMESTAMP_ADD(TIMESTAMP('1999-01-01'), INTERVAL 100 YEAR)",
    "TIMESTAMP('2024-06-01')",
])
def test_the_coalesce_default_must_be_an_old_literal(default):
    model = plain(f"ts > COALESCE((SELECT MAX(ts) FROM target), {default})")
    assert check_incremental(model, SRC, ["insert_new", "empty"], seeds=5).outcome != "safe"


def test_a_negative_lookback_is_not_a_lookback():
    model = plain("ts > TIMESTAMP_SUB(COALESCE((SELECT MAX(ts) FROM target), TIMESTAMP('1999-01-01')), INTERVAL -1 DAY)", ("id",))
    assert check_incremental(model, SRC, ["insert_new", "empty"], seeds=20).outcome != "safe"
    results = replay(model, SRC, ["INSERT INTO src VALUES (1, TIMESTAMP '2024-01-01')"],
                     [["INSERT INTO src VALUES (2, TIMESTAMP '2024-01-01 01:00:00')"]])
    assert results[-1].status == "diverge"


def test_a_positive_lookback_and_a_plain_watermark_are_still_safe():
    lookback = plain("ts > TIMESTAMP_SUB(COALESCE((SELECT MAX(ts) FROM target), TIMESTAMP('1999-01-01')), INTERVAL 1 DAY)", ("id",))
    assert check_incremental(lookback, SRC, ["insert_new", "insert_boundary", "empty"]).outcome == "safe"
    strict = plain("ts > COALESCE((SELECT MAX(ts) FROM target), TIMESTAMP '1999-01-01')")
    assert check_incremental(strict, SRC, ["insert_new", "empty"]).outcome == "safe"


# ------------------------------------------------- S011-006: a.t and b.t are different tables


def test_model_reuse_keeps_schema_qualified_tables_apart():
    result = rewrite_over_model("SELECT x FROM a.t", "SELECT x FROM b.t", schema={"t": ["x"]}, dialect="postgres")
    assert result.status != "rewritten"
    same = rewrite_over_model("SELECT x FROM a.t WHERE x > 1", "SELECT x FROM a.t", schema={"t": ["x"]}, dialect="postgres")
    assert same.status == "rewritten"


def test_model_reuse_does_not_put_the_model_under_a_with_table_of_its_sources_name():
    query = "WITH t AS (SELECT 5 AS x) SELECT s.x FROM (SELECT x FROM t WHERE x > 1) AS s JOIN u ON s.x = u.x"
    result = rewrite_over_model(query, "SELECT x FROM t WHERE x > 1", schema={"t": ["x"], "u": ["x"]}, dialect="postgres")
    assert result.status != "rewritten"


def test_containment_keeps_schema_qualified_tables_apart():
    result = check_containment("SELECT x FROM a.t WHERE x > 1", "SELECT x FROM b.t", schema={"t": ["x"]})
    assert not result.contained


# ------------------------------------------------- S011-007: MERGE updates every matched copy


def test_merge_updates_duplicate_target_rows_in_place():
    sources = {"src": SourceTable({"src_id": "INT64", "id": "INT64", "v": "INT64"}, ("src_id",))}
    model = IncrementalModel("target", "SELECT id, v FROM src", "SELECT id, v FROM src", ("id",))
    sim = Simulation(model, sources, ["INSERT INTO src VALUES (1, 1, 0), (2, 1, 0)"])
    assert sim.step((), 0).status == "agree"
    result = sim.step(["DELETE FROM src WHERE src_id = 2"], 1)
    assert result.status == "diverge"  # both target copies are updated and stay; the full query has one row
    assert sorted(sim.target_rows()[1]) == [(1, 0), (1, 0)]


def test_merge_updates_matched_rows_and_inserts_new_ones():
    sources = {"src": SourceTable({"id": "INT64", "v": "INT64"}, ("id",))}
    model = IncrementalModel("target", "SELECT id, v FROM src", "SELECT id, v FROM src", ("id",))
    results = replay(model, sources, ["INSERT INTO src VALUES (1, 0)"],
                     [["UPDATE src SET v = 5 WHERE id = 1", "INSERT INTO src VALUES (2, 7)"]])
    assert [r.status for r in results] == ["agree", "agree"]


# ------------------------------------------------- S014: a script's result is its last statement
# (older sqlglot releases' parse_one returned only the first statement, so these were proved equal)


def scripts(root, files):
    (root / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n", encoding="utf-8")
    for name, text in files.items():
        path = root / "definitions" / f"{name}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return load_sqlx_project(root)


@pytest.mark.parametrize("left, right", [
    ("SELECT 0 AS v; SELECT 2 AS v;", "SELECT 0 AS v; SELECT 3 AS v;"),
    ("SELECT 0 AS v; INSERT INTO t VALUES (1); SELECT v FROM t;", "SELECT 0 AS v; INSERT INTO t VALUES (2); SELECT v FROM t;"),
    ("SELECT 0 AS v; CALL missing(); SELECT 2 AS v;", "SELECT 0 AS v; CALL missing(); SELECT 3 AS v;"),
])
def test_scripts_sharing_a_first_select_are_not_proved_equal(tmp_path, left, right):
    pipeline = scripts(tmp_path, {"a": left, "b": right})
    assert {m.kind for m in pipeline.models.values()} == {"sql"}
    result = prove_models(pipeline, "a", "b", declared=[])
    # declined before the prover sees it, whatever the sqlglot version's parse_one does with a script
    assert not result.proven and "could not be read as a plain query" in result.reason


def test_a_plain_sql_file_is_still_proved(tmp_path):
    pipeline = scripts(tmp_path, {"a": "SELECT 1 AS v", "b": "SELECT 1 AS v;"})
    assert prove_models(pipeline, "a", "b", declared=[]).proven


def test_consolidation_does_not_drop_a_scripts_final_query(tmp_path):
    from kumosql import consolidate

    pipeline = scripts(tmp_path, {"a": "SELECT 1 AS v", "b": "SELECT v FROM p.d.a; SELECT 9 AS v"})
    try:
        result = consolidate.consolidate_tables(pipeline, ["a"], "b")
    except consolidate.ConsolidationError:
        return
    assert not result.proven


def test_a_saved_or_layer_equivalence_is_not_applied_where_a_with_table_captures_it():
    import sqlglot

    from kumosql import equivalences

    item = equivalences.Equivalence("b", "m", (("x", "x"),), True)
    tree = sqlglot.parse_one("WITH b AS (SELECT 2 AS x) SELECT x FROM m", read="bigquery")
    _, used = equivalences.rewrite_tree(tree, [item])
    assert used == []
    plain = sqlglot.parse_one("SELECT x FROM m", read="bigquery")
    _, used = equivalences.rewrite_tree(plain, [item])
    assert used == [item]
