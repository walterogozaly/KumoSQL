import json

from kumosql import ColumnRef, load_compiled_graph, load_sqlx_project
from kumosql.cli import pipeline_main


RAW_ORDERS = {
    "proj.raw.orders": {
        "id": "INT64",
        "customer_id": "INT64",
        "amount": "FLOAT64",
        "status": "STRING",
        "created_at": "TIMESTAMP",
    }
}


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dataform_project(root):
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: analytics\n")
    write(root, "definitions/sources.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(
        root,
        "definitions/staging/stg_orders.sqlx",
        'config { type: "view" }\n'
        "SELECT id, customer_id, amount, status, created_at\n"
        'FROM ${ref("raw", "orders")}\n'
        "WHERE status != 'test'\n",
    )
    write(
        root,
        "definitions/customer_totals.sqlx",
        'config { type: "table" }\n'
        "WITH recent AS (SELECT * FROM ${ref(\"stg_orders\")} WHERE created_at > '2024-01-01')\n"
        "SELECT customer_id, SUM(amount) AS total FROM recent GROUP BY customer_id\n",
    )
    write(
        root,
        "definitions/customer_counts.sqlx",
        'config { type: "table" }\n'
        "SELECT o.customer_id, COUNT(*) AS n\n"
        "FROM ${ref({name: \"stg_orders\"})} AS o\n"
        "${when(incremental(), `WHERE o.created_at > (SELECT MAX(created_at) FROM ${self()})`)}\n"
        "GROUP BY o.customer_id\n",
    )
    return root


def test_sqlx_project_builds_model_graph(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    assert set(pipeline.models) == {
        "proj.analytics.stg_orders",
        "proj.analytics.customer_totals",
        "proj.analytics.customer_counts",
    }
    assert set(pipeline.sources) == {"proj.raw.orders"}
    assert pipeline.upstream["proj.analytics.stg_orders"] == {"proj.raw.orders"}
    assert pipeline.upstream["proj.analytics.customer_totals"] == {"proj.analytics.stg_orders"}
    assert pipeline.upstream["proj.analytics.customer_counts"] == {"proj.analytics.stg_orders"}
    order = pipeline.topological_order()
    assert order.index("proj.analytics.stg_orders") < order.index("proj.analytics.customer_totals")
    assert pipeline.downstream["proj.analytics.stg_orders"] == {
        "proj.analytics.customer_totals",
        "proj.analytics.customer_counts",
    }


def test_column_lineage_crosses_models_and_ctes(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    total = ColumnRef("proj.analytics.customer_totals", "total")
    assert pipeline.column_lineage()[total] == {ColumnRef("proj.analytics.stg_orders", "amount")}
    assert pipeline.upstream_columns(total) == {
        ColumnRef("proj.analytics.stg_orders", "amount"),
        ColumnRef("proj.raw.orders", "amount"),
    }
    assert pipeline.downstream_columns(ColumnRef("proj.raw.orders", "amount")) == {
        ColumnRef("proj.analytics.stg_orders", "amount"),
        total,
    }


def test_dead_columns_count_filter_and_join_reads(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    # created_at is only read in a WHERE clause downstream, so it is live.
    # status is only read inside stg_orders itself, and id is never read.
    assert pipeline.dead_columns() == {"proj.analytics.stg_orders": ("id", "status")}
    assert pipeline.output_columns("proj.analytics.stg_orders") == (
        "id",
        "customer_id",
        "amount",
        "status",
        "created_at",
    )


def test_unknown_source_schema_makes_dead_columns_conservative(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/staging/stg_orders.sqlx",
        'config { type: "view" }\nSELECT * FROM ${ref("raw", "orders")}\n',
    )
    write(
        root,
        "definitions/everything.sqlx",
        'config { type: "table" }\nSELECT * FROM ${ref("stg_orders")}\n',
    )

    pipeline = load_sqlx_project(root)

    assert pipeline.dead_columns() == {}
    codes = {d.code for d in pipeline.all_diagnostics()}
    assert "unexpanded_star" in codes


def test_select_star_expands_with_known_source_schema(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/everything.sqlx",
        'config { type: "table" }\nSELECT * FROM ${ref("stg_orders")}\n',
    )

    pipeline = load_sqlx_project(root, source_schema=RAW_ORDERS)

    assert pipeline.dead_columns() == {}
    assert pipeline.output_columns("proj.analytics.everything") == pipeline.output_columns(
        "proj.analytics.stg_orders"
    )


def compiled_graph():
    shared = (
        "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n "
        "FROM `proj.raw.orders` WHERE status = 'paid' GROUP BY customer_id"
    )
    return {
        "tables": [
            {
                "target": {"database": "proj", "schema": "mart", "name": "a"},
                "type": "table",
                "query": f"WITH paid AS ({shared}) SELECT customer_id, total FROM paid",
                "dependencyTargets": [{"database": "proj", "schema": "raw", "name": "orders"}],
                "fileName": "definitions/a.sqlx",
            },
            {
                "target": {"database": "proj", "schema": "mart", "name": "b"},
                "type": "view",
                "query": f"SELECT p.customer_id, p.n FROM (\n  -- same logic, different layout\n  {shared.lower().replace('`proj.raw.orders`', '`proj.raw.orders`')}\n) AS p",
                "dependencyTargets": [{"database": "proj", "schema": "raw", "name": "orders"}],
                "fileName": "definitions/b.sqlx",
            },
        ],
        "declarations": [{"target": {"database": "proj", "schema": "raw", "name": "orders"}}],
    }


def test_compiled_graph_finds_duplicate_logic_across_models():
    pipeline = load_compiled_graph(compiled_graph(), source_schema=RAW_ORDERS)

    groups = pipeline.duplicate_selects()

    assert len(groups) == 1
    locations = {(o.model, o.location) for o in groups[0].occurrences}
    assert locations == {("proj.mart.a", "cte:paid"), ("proj.mart.b", "subquery:p")}
    assert "sum(amount)" in groups[0].sql.lower()


def test_duplicate_report_skips_small_and_nested_matches():
    pipeline = load_compiled_graph(compiled_graph(), source_schema=RAW_ORDERS)

    assert pipeline.duplicate_selects(min_nodes=10_000) == []


def test_cycles_are_reported_not_fatal():
    graph = {
        "tables": [
            {"target": {"schema": "d", "name": "x"}, "query": "SELECT id FROM d.y"},
            {"target": {"schema": "d", "name": "y"}, "query": "SELECT id FROM d.x"},
        ]
    }

    pipeline = load_compiled_graph(graph)

    assert set(pipeline.topological_order()) == {"d.x", "d.y"}
    assert any(d.code == "cycle" for d in pipeline.all_diagnostics())


def test_plain_sql_folder_resolves_bare_table_names(tmp_path):
    write(tmp_path, "base.sql", "SELECT id, name, extra FROM `proj.raw.people`")
    write(tmp_path, "report.sql", "WITH base AS (SELECT 1 AS id) SELECT b.id, p.name FROM base b JOIN proj.ds.base p USING (id)")

    pipeline = load_sqlx_project(
        tmp_path, source_schema={"proj.raw.people": {"id": "INT64", "name": "STRING", "extra": "STRING"}}
    )

    assert pipeline.upstream["report"] == {"base"}
    assert pipeline.dead_columns() == {"base": ("extra",)}


def test_unparseable_model_is_a_diagnostic_and_blocks_dead_column_claims(tmp_path):
    write(tmp_path, "base.sql", "SELECT id, extra FROM `proj.raw.people`")
    write(tmp_path, "broken.sql", "SELECT id FROM base WHERE (")
    write(tmp_path, "ok.sql", "SELECT id FROM base")

    pipeline = load_sqlx_project(
        tmp_path, source_schema={"proj.raw.people": {"id": "INT64", "extra": "STRING"}}
    )

    assert any(d.code == "parse_error" and d.model == "broken" for d in pipeline.all_diagnostics())
    # broken.sql might read base.extra, so nothing may be called dead.
    assert pipeline.dead_columns() == {}
    assert any(d.code == "unknown_reads" for d in pipeline.all_diagnostics())


def test_reads_inside_masked_dataform_expressions_keep_columns_live(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/customer_totals.sqlx",
        'config { type: "table" }\nSELECT customer_id, SUM(amount) AS total FROM ${ref("stg_orders")} GROUP BY customer_id\n',
    )

    pipeline = load_sqlx_project(root, source_schema=RAW_ORDERS)

    # created_at is now read only inside customer_counts' ${when(...)} block.
    assert pipeline.dead_columns() == {"proj.analytics.stg_orders": ("id", "status")}


def test_pipeline_cli_writes_json_report(tmp_path, capsys):
    root = dataform_project(tmp_path / "project")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(RAW_ORDERS), encoding="utf-8")

    assert pipeline_main([str(root), "--source-schema", str(schema)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["models"] == 3
    assert report["dead_columns"] == {"proj.analytics.stg_orders": ["id", "status"]}


# ------------------------------------------------------------ gaps (#28)


def _gap_codes(report):
    return {g["code"] for g in report["completeness"]["gaps"]}


def test_complete_pipeline_reports_complete(tmp_path):
    write(tmp_path, "a.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "b.sql", "SELECT id FROM a")

    report = load_sqlx_project(tmp_path).report()

    assert report["completeness"]["complete"] is True
    assert all(report["completeness"]["views"].values())
    assert report["graph"]["completeness"]["complete"] is True


def test_unparseable_model_marks_every_view_incomplete(tmp_path):
    write(tmp_path, "base.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "broken.sql", "SELECT id FROM base WHERE (")

    report = load_sqlx_project(tmp_path).report()
    completeness = report["completeness"]

    assert completeness["complete"] is False
    assert not any(completeness["views"].values())
    assert {"parse_error", "unknown_reads"} <= _gap_codes(report)
    assert completeness["assets_not_analyzed"] == 1
    assert report["graph"]["completeness"]["complete"] is False
    assert {g["asset"] for g in completeness["gaps"] if g["blocking"]} == {"broken"}


def test_unknown_reads_is_reported_per_model_and_survives_scoping(tmp_path):
    from kumosql.scopes import parse_scope

    write(tmp_path, "one.sql", "SELECT id FROM (")
    write(tmp_path, "two.sql", "SELECT id FROM (")
    write(tmp_path, "fine.sql", "SELECT 1 AS id")
    pipeline = load_sqlx_project(tmp_path)

    unknown = [d for d in pipeline.all_diagnostics() if d.code == "unknown_reads"]
    assert sorted(d.model for d in unknown) == ["one", "two"]

    report = pipeline.report(scope=parse_scope({"name": "one-only", "fields": {"name": ["one"]}}))
    assert any(d["code"] == "unknown_reads" and d["model"] == "one" for d in report["diagnostics"])
    assert report["completeness"]["complete"] is False
    assert {g["asset"] for g in report["completeness"]["gaps"]} == {"one"}

    clean = pipeline.report(scope=parse_scope({"name": "fine", "fields": {"name": ["fine"]}}))
    assert clean["completeness"]["complete"] is True


def test_cycle_marks_graph_and_impact_incomplete():
    graph = {
        "tables": [
            {"target": {"schema": "d", "name": "x"}, "query": "SELECT id FROM d.y"},
            {"target": {"schema": "d", "name": "y"}, "query": "SELECT id FROM d.x"},
        ]
    }
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["views"]["graph"] is False
    assert completeness["views"]["impact"] is False
    assert completeness["views"]["lineage"] is True


def test_extra_queries_in_a_script_are_reported():
    graph = {
        "tables": [
            {
                "target": {"schema": "d", "name": "x"},
                "queries": ["SELECT id FROM d.a", "SELECT id FROM d.b"],
            },
            {"target": {"schema": "d", "name": "a"}, "query": "SELECT 1 AS id"},
            {"target": {"schema": "d", "name": "b"}, "query": "SELECT 1 AS id"},
        ]
    }
    pipeline = load_compiled_graph(graph)

    assert any(d.code == "skipped_statements" and d.model == "d.x" for d in pipeline.all_diagnostics())
    assert pipeline.completeness()["complete"] is False


def test_operations_are_listed_as_not_analysed():
    graph = {
        "tables": [{"target": {"schema": "d", "name": "a"}, "query": "SELECT 1 AS id"}],
        "operations": [
            {
                "target": {"schema": "d", "name": "load"},
                "queries": ["INSERT INTO d.a SELECT 2"],
                "dependencyTargets": [{"schema": "d", "name": "a"}],
            }
        ],
    }
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["complete"] is False
    assert completeness["by_code"]["unparsed_operation"] == 1
    assert completeness["gaps"][0]["asset"] == "d.load"


def test_ambiguous_table_reference_is_distinct_from_external():
    graph = {
        "tables": [
            {"target": {"schema": "a", "name": "events"}, "query": "SELECT 1 AS id"},
            {"target": {"schema": "b", "name": "events"}, "query": "SELECT 1 AS id"},
            {
                "target": {"schema": "c", "name": "report"},
                "query": "SELECT id FROM events JOIN elsewhere.t USING (id)",
            },
        ]
    }
    pipeline = load_compiled_graph(graph)
    codes = {(d.model, d.code) for d in pipeline.all_diagnostics()}

    assert ("c.report", "ambiguous_reference") in codes
    assert ("c.report", "external_tables") in codes
    assert pipeline.completeness()["views"]["graph"] is False


def test_external_tables_are_listed_but_do_not_block():
    graph = {"tables": [{"target": {"schema": "c", "name": "report"}, "query": "SELECT id FROM elsewhere.t"}]}
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["complete"] is True
    assert completeness["gaps"][0]["kind"] == "unmatched_reference"
    assert completeness["gaps"][0]["blocking"] is False


def test_unattributed_observations_make_the_graph_incomplete(tmp_path):
    from kumosql.graph import ObservedRead

    write(tmp_path, "a.sql", "SELECT 1 AS id")
    pipeline = load_sqlx_project(tmp_path)
    read = ObservedRead("job-1", "2024-01-01T00:00:00Z", None, ("a",))

    report = pipeline.report(observed_reads=[read])

    assert report["completeness"]["complete"] is False
    assert "unattributed_reads" in _gap_codes(report)
