"""Small synthetic reductions of defects found by running a real, messy view layer.

Each test isolates one shape: qualified references to bare-keyed models,
re-aggregation, calendar roll-ups, inlined expressions, aggregates that mix
tables, and view-definition jobs in job history. Names and text are invented.
"""

from __future__ import annotations

from pathlib import Path

from kumosql import find_overlaps, find_rollups
from kumosql.observed_usage import observed_usage
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.table_roles import infer_roles

Q = "`p.d."
SCHEMA = {
    "p.d.raw_sales": {"id": "INT64", "shopper": "INT64", "item": "INT64", "status": "STRING", "at": "TIMESTAMP", "price": "FLOAT64"},
    "p.d.raw_items": {"id": "INT64", "cost": "FLOAT64", "kind": "STRING"},
    "p.d.raw_shoppers": {"id": "INT64", "region": "STRING"},
}


def project(tmp_path: Path, views: dict[str, str]):
    for name, sql in views.items():
        (tmp_path / f"{name}.sql").write_text(sql.replace("@", Q).replace("#", "`"))
    return load_sqlx_project(tmp_path, source_schema=SCHEMA)


def verdicts(pipeline, model):
    return {m.table.split(".")[-1]: m.kind for m in find_overlaps(pipeline, model=model).matches}


def derivable(pipeline, model):
    return {(r.table.split(".")[-1], r.attribute): r.derivability for r in find_rollups(pipeline, model=model).rollups}


SALES = "SELECT id AS sale_id, shopper, item, status, at, price FROM @raw_sales#"


def test_select_star_over_a_qualified_reference_to_a_bare_keyed_model_is_expanded(tmp_path):
    pipeline = project(tmp_path, {
        "sales": SALES,
        "wide": "SELECT * FROM @sales# WHERE status != 'x'",
    })
    assert pipeline.output_columns("wide") == ("sale_id", "shopper", "item", "status", "at", "price")
    assert not [d for d in pipeline.all_diagnostics() if d.code == "unexpanded_star"]
    assert pipeline.coverage()["unexpanded_stars"] == 0


def test_a_view_stacked_on_a_star_view_keeps_its_lineage(tmp_path):
    pipeline = project(tmp_path, {
        "sales": SALES,
        "wide": "SELECT * FROM @sales#",
        "top": "SELECT shopper, SUM(price) AS total FROM @wide# GROUP BY shopper",
    })
    lineage = {c["column"]: c for c in pipeline.lineage_report() if c["node"] == "top"}
    assert lineage["total"]["status"] != "unknown"


DAILY = "SELECT DATE(at) AS day, SUM(price) AS revenue FROM @sales# WHERE status != 'x' GROUP BY day"
WEEKLY_FROM_DAILY = "SELECT DATE_TRUNC(day, WEEK) AS week, SUM(revenue) AS revenue FROM @daily# GROUP BY 1"
WEEKLY_FROM_ROWS = "SELECT DATE_TRUNC(DATE(at), WEEK) AS week, SUM(price) AS revenue FROM @sales# WHERE status != 'x' GROUP BY 1"


def test_a_sum_of_sums_means_the_same_as_the_sum(tmp_path):
    pipeline = project(tmp_path, {"sales": SALES, "daily": DAILY, "weekly": WEEKLY_FROM_DAILY, "weekly_rows": WEEKLY_FROM_ROWS})
    assert verdicts(pipeline, "weekly")["weekly_rows"] == "same_meaning"
    assert verdicts(pipeline, "weekly_rows")["weekly"] == "same_meaning"


def test_a_filter_between_the_two_sums_keeps_them_apart(tmp_path):
    kept = "SELECT DATE_TRUNC(day, WEEK) AS week, SUM(revenue) AS revenue FROM @daily# GROUP BY 1"
    having = DAILY + " HAVING SUM(price) > 100"
    pipeline = project(tmp_path, {"sales": SALES, "daily": having, "weekly": kept, "weekly_rows": WEEKLY_FROM_ROWS})
    assert verdicts(pipeline, "weekly").get("weekly_rows") != "same_meaning"


def test_an_average_of_averages_is_not_collapsed(tmp_path):
    daily = "SELECT DATE(at) AS day, AVG(price) AS avg_price FROM @sales# GROUP BY day"
    weekly = "SELECT DATE_TRUNC(day, WEEK) AS week, AVG(avg_price) AS avg_price FROM @daily# GROUP BY 1"
    rows = "SELECT DATE_TRUNC(DATE(at), WEEK) AS week, AVG(price) AS avg_price FROM @sales# GROUP BY 1"
    pipeline = project(tmp_path, {"sales": SALES, "daily": daily, "weekly": weekly, "rows": rows})
    assert verdicts(pipeline, "weekly").get("rows") != "same_meaning"


def test_days_roll_up_to_weeks_and_a_rank_is_not_a_roll_up(tmp_path):
    ranked = (
        "SELECT DATE_TRUNC(day, WEEK) AS week, SUM(revenue) AS revenue, "
        "RANK() OVER (ORDER BY SUM(revenue) DESC) AS place FROM @daily# GROUP BY 1"
    )
    pipeline = project(tmp_path, {"sales": SALES, "daily": DAILY, "weekly_rows": WEEKLY_FROM_ROWS, "ranked": ranked})
    found = derivable(pipeline, "weekly_rows")
    assert found[("daily", "revenue")] == "derivable_exact"
    assert ("daily", "place") not in derivable(pipeline, "ranked")


def test_a_day_table_does_not_roll_up_to_an_unrelated_grain(tmp_path):
    by_shopper = "SELECT shopper, SUM(price) AS revenue FROM @sales# WHERE status != 'x' GROUP BY shopper"
    pipeline = project(tmp_path, {"sales": SALES, "daily": DAILY, "by_shopper": by_shopper})
    assert derivable(pipeline, "by_shopper").get(("daily", "revenue")) != "derivable_exact"


def test_an_expression_read_through_a_view_equals_the_same_expression_written_out(tmp_path):
    items = "SELECT id AS item, cost FROM @raw_items#"
    line = "SELECT s.shopper, s.price - i.cost AS margin FROM @sales# AS s JOIN @items# AS i USING (item)"
    via_view = "SELECT shopper, SUM(margin) AS margin FROM @line# GROUP BY shopper"
    inline = (
        "SELECT s.shopper, SUM(s.price - i.cost) AS margin FROM @sales# AS s "
        "JOIN @items# AS i USING (item) GROUP BY s.shopper"
    )
    pipeline = project(tmp_path, {"sales": SALES, "items": items, "line": line, "via_view": via_view, "inline": inline})
    assert verdicts(pipeline, "via_view")["inline"] == "same_meaning"


def test_a_different_expression_read_through_a_view_is_not_the_same(tmp_path):
    items = "SELECT id AS item, cost FROM @raw_items#"
    line = "SELECT s.shopper, s.price - i.cost AS margin FROM @sales# AS s JOIN @items# AS i USING (item)"
    via_view = "SELECT shopper, SUM(margin * 2) AS margin FROM @line# GROUP BY shopper"
    inline = (
        "SELECT s.shopper, SUM(s.price - i.cost * 2) AS margin FROM @sales# AS s "
        "JOIN @items# AS i USING (item) GROUP BY s.shopper"
    )
    pipeline = project(tmp_path, {"sales": SALES, "items": items, "line": line, "via_view": via_view, "inline": inline})
    assert verdicts(pipeline, "via_view").get("inline") != "same_meaning"


def test_a_lookup_table_used_inside_a_mixed_aggregate_stays_a_dimension(tmp_path):
    items = "SELECT id AS item, cost, kind FROM @raw_items#"
    report = (
        "SELECT i.kind, SUM(s.price - i.cost) AS margin FROM @sales# AS s "
        "JOIN @items# AS i ON i.item = s.item GROUP BY i.kind"
    )
    pipeline = project(tmp_path, {"sales": SALES, "items": items, "report": report})
    roles = infer_roles(pipeline)
    assert roles["items"].role in ("dimension", "unknown")
    assert roles["items"].role != "fact"


def test_a_table_summed_on_its_own_still_counts_as_a_fact(tmp_path):
    report = "SELECT shopper, SUM(price) AS total, COUNT(*) AS n FROM @sales# GROUP BY shopper"
    other = "SELECT shopper, MAX(price) AS top FROM @sales# GROUP BY shopper"
    pipeline = project(tmp_path, {"sales": SALES, "report": report, "other": other})
    assert infer_roles(pipeline)["sales"].role == "fact"


def record(text, user="a@example.com", **extra):
    return {"job_id": "j", "creation_time": "2026-01-01T00:00:00Z", "referenced_tables": [], "query": text, "user_email": user, **extra}


def test_a_job_that_only_defines_a_view_is_not_a_reader(tmp_path):
    pipeline = project(tmp_path, {"sales": SALES, "daily": DAILY})
    definition = record("CREATE OR REPLACE VIEW `p.d.daily` AS\nSELECT shopper FROM `p.d.sales`", user="builder@example.com")
    typed = record("SELECT 1", user="builder@example.com", statement_type="CREATE_VIEW")
    result = observed_usage(pipeline, [definition, typed])
    assert result.readers_examined == 0 and result.readers_unexamined == 0
    assert result.tables == {}
    assert result.records_total == 2 and result.records_unexamined == {"view_definition": 2}


def test_a_query_read_and_a_table_build_still_count(tmp_path):
    pipeline = project(tmp_path, {"sales": SALES, "daily": DAILY})
    read = record("SELECT day FROM `p.d.daily` WHERE revenue > 1", user="analyst@example.com")
    build = record("CREATE OR REPLACE TABLE `p.d.copy` AS SELECT day, revenue FROM `p.d.daily`", user="etl@example.com")
    script = record("CREATE VIEW `p.d.v` AS SELECT 1; SELECT day FROM `p.d.daily`", user="dev@example.com")
    result = observed_usage(pipeline, [read, build, script])
    assert result.readers_examined == 3
    assert not result.records_unexamined
