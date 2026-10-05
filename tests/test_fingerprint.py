"""Output fingerprints, checked by running the generated SQL on DuckDB.

The generated SQL is BigQuery. DuckDB runs it after sqlglot transpiles it,
with FARM_FINGERPRINT replaced by DuckDB's own 64-bit hash. That checks the
logic (bag sensitivity, NULL handling, column localisation, drill-down
statuses) without BigQuery credentials.
"""

import json

import pytest
import sqlglot
from sqlglot import exp

from kumosql import (
    Location,
    Target,
    compare_snapshots,
    compare_tables_sql,
    diff_rows_sql,
    load_compiled_graph,
    plan_output_comparison,
    summarize_comparison,
    table_fingerprint_sql,
)
from kumosql.cli import compare_outputs_main

duckdb = pytest.importorskip("duckdb")


RAW = {
    "raw.orders": {
        "id": "INT64",
        "customer_id": "INT64",
        "amount": "FLOAT64",
        "status": "STRING",
    }
}
ORDERS = [
    (1, 10, 5.0, "paid"),
    (2, 10, 7.5, "paid"),
    (3, 11, 2.25, "test"),
    (4, 12, None, "paid"),
    (5, 12, 1.0, "refunded"),
]

STG = "SELECT id, customer_id, amount, status FROM raw.orders WHERE status != 'test'"
TOTALS = (
    "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n "
    "FROM analytics.stg_orders GROUP BY customer_id"
)


def graph(stg=STG, totals=TOTALS):
    def table(name, query):
        return {"target": {"schema": "analytics", "name": name}, "type": "table", "query": query}

    return load_compiled_graph(
        {
            "tables": [table("stg_orders", stg), table("customer_totals", totals)],
            "declarations": [{"target": {"schema": "raw", "name": "orders"}}],
        },
        source_schema=RAW,
    )


def to_duckdb(sql):
    duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    # Older sqlglot releases spell BIGNUMERIC as a type DuckDB lacks.
    return duck.replace("FARM_FINGERPRINT(", "farm_fp(").replace("AS BIGDECIMAL)", "AS DECIMAL(38, 5))")


@pytest.fixture
def con():
    connection = duckdb.connect()
    connection.execute("CREATE MACRO farm_fp(x) AS CAST(hash(x) AS HUGEINT)")
    connection.execute("CREATE SCHEMA raw; CREATE SCHEMA b; CREATE SCHEMA a")
    connection.execute(
        "CREATE TABLE raw.orders (id BIGINT, customer_id BIGINT, amount DOUBLE, status VARCHAR)"
    )
    connection.executemany("INSERT INTO raw.orders VALUES (?, ?, ?, ?)", ORDERS)
    return connection


def build(con, pipeline, schema):
    """Materialise every model of ``pipeline`` into ``schema``."""

    for key in pipeline.topological_order():
        model = pipeline.models.get(key)
        if model is None:
            continue
        tree = sqlglot.parse_one(model.sql, read="bigquery")
        for table in tree.find_all(exp.Table):
            if pipeline.resolve(table) in pipeline.models:
                table.set("db", exp.to_identifier(schema))
        con.execute(f"CREATE TABLE {schema}.{model.target.name} AS {tree.sql('duckdb')}")


def rows(con, sql):
    cursor = con.execute(to_duckdb(sql))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def compare(con, before, after, **options):
    plan = plan_output_comparison(
        before, after, before_location=Location(dataset="b"), after_location=Location(dataset="a"), **options
    )
    return plan, {diff.model: diff for diff in summarize_comparison(rows(con, plan.compare_sql()))}


def run_plan(con, before, after, **options):
    build(con, before, "b")
    build(con, after, "a")
    return compare(con, before, after, **options)


STG_KEY = "analytics.stg_orders"
TOTALS_KEY = "analytics.customer_totals"


def test_equivalent_refactor_matches_every_model(con):
    lifted = (
        "WITH per_customer AS (SELECT customer_id, amount FROM analytics.stg_orders) "
        "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n FROM per_customer GROUP BY customer_id"
    )
    plan, diffs = run_plan(con, graph(), graph(totals=lifted))

    assert [table.model for table in plan.tables] == [STG_KEY, TOTALS_KEY]
    assert plan.diagnostics == []
    assert all(diff.matches for diff in diffs.values()), diffs
    assert diffs[TOTALS_KEY].before_rows == 2


def test_reordered_columns_still_match(con):
    reordered = "SELECT status, amount, id, customer_id FROM raw.orders WHERE status != 'test'"
    _, diffs = run_plan(con, graph(), graph(stg=reordered))

    assert diffs[STG_KEY].matches


def test_changed_filter_is_a_row_count_mismatch(con):
    wider = "SELECT id, customer_id, amount, status FROM raw.orders"
    plan, diffs = run_plan(con, graph(), graph(stg=wider))

    stg = diffs[STG_KEY]
    assert stg.status == "mismatch"
    assert (stg.before_rows, stg.after_rows) == (4, 5)
    assert "row count" in stg.note
    # The downstream model changes too: customer 11 appears.
    assert diffs[TOTALS_KEY].status == "mismatch"

    drill = rows(con, plan.drilldown_sql(STG_KEY, keys=["id"]))
    assert [(r["status"], r["key_json"]) for r in drill] == [("only_after", '{"id":3}')]
    assert drill[0]["before_row"] is None


def test_changed_expression_is_localised_to_its_column(con):
    doubled = (
        "SELECT customer_id, SUM(amount) * 2 AS total, COUNT(*) AS n "
        "FROM analytics.stg_orders GROUP BY customer_id"
    )
    plan, diffs = run_plan(con, graph(), graph(totals=doubled))

    totals = diffs[TOTALS_KEY]
    assert totals.status == "mismatch"
    assert totals.mismatched_columns == ("total",)
    assert totals.before_rows == totals.after_rows == 2
    assert diffs[STG_KEY].matches

    drill = rows(con, plan.drilldown_sql(TOTALS_KEY, keys=["customer_id"]))
    # Customer 12's total is 1.0 before and 2.0 after; customer 10's doubles too.
    assert {r["key_json"] for r in drill} == {'{"customer_id":10}', '{"customer_id":12}'}
    assert all(r["status"] == "changed" and list(r["changed_columns"]) == ["total"] for r in drill)

    bag = rows(con, plan.drilldown_sql(TOTALS_KEY, columns=["total"]))
    assert {r["status"] for r in bag} == {"only_before", "only_after"}
    assert all(set(json.loads(r["row_json"])) == {"total"} for r in bag)


def test_values_moved_between_rows_are_caught_by_the_row_checksum(con):
    swapped = (
        "SELECT id, CASE customer_id WHEN 10 THEN 12 WHEN 12 THEN 10 ELSE customer_id END AS customer_id, "
        "amount, status FROM raw.orders WHERE status != 'test'"
    )
    _, diffs = run_plan(con, graph(), graph(stg=swapped))

    stg = diffs[STG_KEY]
    # customer_id's multiset {10,10,12,12} is unchanged, so every column matches.
    assert stg.status == "mismatch"
    assert stg.mismatched_columns == ()
    assert "moved between rows" in stg.note


def test_duplicate_rows_count(con):
    con.execute("CREATE TABLE b.t AS SELECT * FROM (VALUES (1), (1), (2)) v(x)")
    con.execute("CREATE TABLE a.t AS SELECT * FROM (VALUES (1), (2), (2)) v(x)")

    [diff] = summarize_comparison(rows(con, compare_tables_sql("b.t", "a.t", ["x"])))
    assert diff.status == "mismatch" and diff.mismatched_columns == ("x",)

    drill = rows(con, diff_rows_sql("b.t", "a.t", ["x"]))
    assert [(r["status"], r["before_count"], r["after_count"]) for r in drill] == [
        ("count_differs", 2, 1),
        ("count_differs", 1, 2),
    ]


def test_identically_duplicated_keys_match_but_changed_counts_do_not(con):
    con.execute("CREATE TABLE b.t AS SELECT * FROM (VALUES (1, 'a'), (1, 'b'), (2, 'c')) v(k, v)")
    con.execute("CREATE TABLE a.t AS SELECT * FROM (VALUES (1, 'b'), (1, 'a'), (2, 'c'), (2, 'c')) v(k, v)")

    drill = rows(con, diff_rows_sql("b.t", "a.t", ["k", "v"], keys=["k"]))
    assert [(r["status"], r["key_json"]) for r in drill] == [("row_count_differs", '{"k":2}')]


def test_nulls_are_values(con):
    con.execute("CREATE TABLE b.t AS SELECT * FROM (VALUES (1, NULL), (2, 'x')) v(k, v)")
    con.execute("CREATE TABLE a.t AS SELECT * FROM (VALUES (1, 'x'), (2, NULL)) v(k, v)")

    diffs = summarize_comparison(rows(con, compare_tables_sql("b.t", "a.t", ["k", "v"])))
    assert diffs[0].note.endswith("moved between rows")

    drill = rows(con, diff_rows_sql("b.t", "a.t", ["k", "v"], keys=["k"]))
    assert [r["key_json"] for r in drill] == ['{"k":1}', '{"k":2}']
    assert json.loads(drill[0]["before_row"]) == {"k": 1, "v": None}


def test_normalize_and_ignore_columns(con):
    before = graph(totals=TOTALS.replace("COUNT(*) AS n", "COUNT(*) AS n, 'y' AS loaded_by"))
    after = graph(
        totals=TOTALS.replace("SUM(amount)", "SUM(amount) + 1e-9").replace(
            "COUNT(*) AS n", "COUNT(*) AS n, 'x' AS loaded_by"
        )
    )
    _, diffs = run_plan(con, before, after)
    assert diffs[TOTALS_KEY].mismatched_columns == ("loaded_by", "total")

    plan, diffs = compare(
        con, before, after, normalize={"Total": "ROUND({col}, 6)"}, ignore_columns=["loaded_by"]
    )
    assert diffs[TOTALS_KEY].matches
    assert plan.table(TOTALS_KEY).ignored == ("loaded_by",)


def test_added_column_is_reported_and_shared_columns_compared(con):
    extra = STG.replace("status FROM", "status, amount * 100 AS cents FROM")
    plan, diffs = run_plan(con, graph(), graph(stg=extra))

    assert plan.table(STG_KEY).only_after == ("cents",)
    assert [d.code for d in plan.diagnostics] == ["columns_differ"]
    # The shared columns agree, but the output gained a column, so it is not a match.
    assert diffs[STG_KEY].status == "mismatch"
    assert diffs[STG_KEY].note == "columns only after: cents"
    assert diffs[TOTALS_KEY].matches

    _, accepted = compare(con, graph(), graph(stg=extra), ignore_columns=["cents"])
    assert accepted[STG_KEY].matches


def test_dropped_column_is_not_a_match(con):
    dropped = STG.replace(", status FROM", " FROM")
    _, diffs = run_plan(con, graph(), graph(stg=dropped))
    assert diffs[STG_KEY].status == "mismatch"
    assert diffs[STG_KEY].note == "columns only before: status"


def test_models_on_one_side_are_summarized_as_missing(con):
    before = graph()
    after = load_compiled_graph(
        {
            "tables": [
                {"target": {"schema": "analytics", "name": "stg_orders"}, "query": STG},
                {"target": {"schema": "analytics", "name": "totals_v2"}, "query": TOTALS},
            ],
            "declarations": [{"target": {"schema": "raw", "name": "orders"}}],
        },
        source_schema=RAW,
    )
    build(con, before, "b")
    build(con, after, "a")
    plan, diffs = compare(con, before, after)
    assert [t.model for t in plan.unmatched] == [TOTALS_KEY, "analytics.totals_v2"]
    assert diffs[TOTALS_KEY].status == "missing_after"
    assert diffs["analytics.totals_v2"].status == "missing_before"
    assert diffs[STG_KEY].matches


def test_results_without_a_whole_row_checksum_are_incomplete():
    [diff] = summarize_comparison(
        [{"model": "m", "column_name": "x", "before_rows": 1, "after_rows": 1,
          "before_checksum": "4", "after_checksum": "4", "matches": True}]
    )
    assert diff.status == "incomplete" and not diff.matches


def test_snapshots_compare_like_the_joined_query(con):
    doubled = TOTALS.replace("SUM(amount)", "SUM(amount) * 2")
    plan, joined = run_plan(con, graph(), graph(totals=doubled))

    before = rows(con, plan.fingerprint_sql("before"))
    after = rows(con, plan.fingerprint_sql("after"))
    assert {r["model"] for r in before} == {STG_KEY, TOTALS_KEY}
    # Snapshots are usually exported as JSON, where every value is a string.
    exported = [{k: str(v) for k, v in row.items()} for row in after]
    assert {d.model: d for d in compare_snapshots(before, exported)} == joined


def test_missing_tables_in_snapshots():
    before = [{"model": "m", "column_name": "*", "row_count": 1, "checksum": "5"}]
    assert compare_snapshots(before, [])[0].status == "missing_after"
    assert compare_snapshots([], before)[0].status == "missing_before"


def test_unknown_columns_fall_back_to_whole_rows(con):
    pipeline = load_compiled_graph(
        {"tables": [{"target": {"schema": "analytics", "name": "copy"}, "query": "SELECT * FROM raw.other"}]}
    )
    plan = plan_output_comparison(pipeline, after_location=Location(dataset_suffix="_dev"))

    assert plan.table("analytics.copy").columns is None
    assert plan.table("analytics.copy").after_table == "analytics_dev.copy"
    assert [d.code for d in plan.diagnostics] == ["unknown_columns"]

    con.execute("CREATE TABLE b.t AS SELECT * FROM (VALUES (1, 'x')) v(k, v)")
    fingerprint = rows(con, table_fingerprint_sql("b.t"))
    assert [(r["column_name"], r["row_count"]) for r in fingerprint] == [("*", 1)]


def test_locations_and_model_selection():
    pipeline = graph()
    target = Target("proj", "analytics", "orders")
    assert Location().table(target) == "proj.analytics.orders"
    assert Location(project="dev", dataset_suffix="_pr1").table(target) == "dev.analytics_pr1.orders"
    assert Location(table_prefix="old_").table(target) == "proj.analytics.old_orders"

    plan = plan_output_comparison(
        pipeline,
        after_location=lambda t: f"scratch.{t.name}",
        models=[TOTALS_KEY, "nope"],
    )
    assert [(t.before_table, t.after_table) for t in plan.tables] == [
        ("analytics.customer_totals", "scratch.customer_totals")
    ]
    assert [d.code for d in plan.diagnostics] == ["unknown_model"]

    with pytest.raises(ValueError, match="same table"):
        plan_output_comparison(pipeline)


def test_models_added_or_removed_by_the_refactor_are_reported():
    before = graph()
    after = load_compiled_graph(
        {
            "tables": [
                {"target": {"schema": "analytics", "name": "stg_orders"}, "query": STG},
                {"target": {"schema": "analytics", "name": "totals_v2"}, "query": TOTALS},
            ]
        },
        source_schema=RAW,
    )
    plan = plan_output_comparison(before, after, after_location=Location(dataset="a"))

    assert [t.model for t in plan.tables] == [STG_KEY]
    assert {(d.model, d.code) for d in plan.diagnostics} == {
        (TOTALS_KEY, "missing_after"),
        ("analytics.totals_v2", "missing_before"),
    }


def test_generated_sql_is_strict_bigquery():
    plan = plan_output_comparison(
        graph(),
        after_location=Location(dataset="a"),
        normalize={"amount": "ROUND({col}, 2)"},
        where={STG_KEY: "status = 'paid'"},
    )
    queries = [
        plan.fingerprint_sql("before"),
        plan.compare_sql(),
        plan.drilldown_sql(STG_KEY),
        plan.drilldown_sql(STG_KEY, keys=["id"], columns=["amount"]),
        table_fingerprint_sql("p.d.odd-name", ["weird col", "sé", "order"]),
    ]
    for sql in queries:
        sqlglot.parse_one(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE)
    assert "ROUND(t.`amount`, 2)" in plan.compare_sql()
    assert "WHERE status = 'paid'" in plan.compare_sql()
    assert "`weird col`" in queries[-1] and "`sé`" in queries[-1]
    # Each table is scanned once per side, not once per column.
    assert plan.compare_sql().count("FROM `a.stg_orders`") == 1


def test_cli_prints_sql_and_summarises_results(tmp_path, capsys):
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "tables": [
                    {"target": {"schema": "analytics", "name": "stg_orders"}, "query": STG},
                ],
                "declarations": [{"target": {"schema": "raw", "name": "orders"}}],
            }
        ),
        encoding="utf-8",
    )
    schema_path = tmp_path / "sources.json"
    schema_path.write_text(json.dumps(RAW), encoding="utf-8")

    assert compare_outputs_main(
        ["compare", str(graph_path), "--source-schema", str(schema_path), "--after-dataset-suffix", "_dev"]
    ) == 0
    sql = capsys.readouterr().out
    assert "`analytics_dev.stg_orders`" in sql and "`analytics.stg_orders`" in sql

    assert compare_outputs_main(
        ["drilldown", str(graph_path), "--after-dataset", "a", "--model", STG_KEY, "--keys", "id"]
    ) == 0
    assert "key_json" in capsys.readouterr().out

    results = tmp_path / "results.json"
    results.write_text(
        json.dumps(
            [
                {"model": "m", "column_name": "*", "before_rows": "2", "after_rows": "2",
                 "before_checksum": "1", "after_checksum": "2", "matches": "false"},
                {"model": "m", "column_name": "x", "before_rows": "2", "after_rows": "2",
                 "before_checksum": "3", "after_checksum": "4", "matches": "false"},
            ]
        ),
        encoding="utf-8",
    )
    assert compare_outputs_main(["summarize", str(results)]) == 1
    assert "m: mismatch (values differ in x)" in capsys.readouterr().out

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert compare_outputs_main(["summarize", str(empty)]) == 2
    assert "nothing was compared" in capsys.readouterr().err
