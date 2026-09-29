import json

import sqlglot

from kumosql import find_near_duplicates, load_compiled_graph
from kumosql.cli import pipeline_main


BASE = (
    "SELECT o.id, o.customer_id, o.amount, o.created_at "
    "FROM `proj.raw.orders` AS o JOIN `proj.raw.customers` AS c ON o.customer_id = c.id "
    "WHERE o.amount > 0"
)


def parse(sql):
    return sqlglot.parse_one(sql, read="bigquery")


def table(name, query):
    return {
        "target": {"database": "proj", "schema": "mart", "name": name},
        "type": "table",
        "query": query,
        "fileName": f"definitions/{name}.sqlx",
    }


def drifted_copies_graph():
    return {
        "tables": [
            table("a", f"WITH base AS ({BASE}) SELECT id, amount FROM base"),
            table("b", f"WITH us AS ({BASE} AND c.country = 'US') SELECT id FROM us"),
            table("c", f"SELECT id, status FROM ({BASE.replace('o.created_at', 'o.created_at, o.status')}) AS s"),
        ],
        "declarations": [
            {"target": {"database": "proj", "schema": "raw", "name": "orders"}},
            {"target": {"database": "proj", "schema": "raw", "name": "customers"}},
        ],
    }


def test_drifted_copies_cluster_with_their_differences():
    pipeline = load_compiled_graph(drifted_copies_graph())

    clusters = pipeline.near_duplicate_selects()

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.kind == "extra_columns_and_filters"
    locations = {occurrence for v in cluster.variants for occurrence in v.occurrences}
    assert locations == {("proj.mart.a", "cte:base"), ("proj.mart.b", "cte:us"), ("proj.mart.c", "subquery:s")}
    assert cluster.representative.occurrences == (("proj.mart.a", "cte:base"),)

    by_model = {v.occurrences[0][0]: v for v in cluster.variants}
    assert [(d.clause, d.change, d.variant) for d in by_model["proj.mart.b"].differences] == [
        ("where", "added", "c.country = 'US'")
    ]
    assert by_model["proj.mart.b"].residual_filters == ("country = 'US'",)
    assert [(d.clause, d.change, d.variant) for d in by_model["proj.mart.c"].differences] == [
        ("columns", "added", "o.status")
    ]

    shared = parse(cluster.shared_sql)
    assert [e.alias_or_name for e in shared.expressions] == [
        "id", "customer_id", "amount", "created_at", "status", "country"
    ]
    assert shared.args["where"].sql(dialect="bigquery") == "WHERE o.amount > 0"


def test_constants_that_differ_become_query_parameters():
    template = (
        "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n FROM `proj.raw.orders` "
        "WHERE status = '{status}' AND region = 'EU' GROUP BY customer_id LIMIT {limit}"
    )
    parsed = {
        "paid": parse(template.format(status="paid", limit=100)),
        "refunded": parse(template.format(status="refunded", limit=50)),
        "void": parse(template.format(status="void", limit=100)),
    }

    [cluster] = find_near_duplicates(parsed)

    assert cluster.kind == "literal_parameters"
    assert [(p.name, p.type) for p in cluster.parameters] == [("status", "STRING"), ("limit", "INT64")]
    assert sorted(cluster.parameters[0].values.values()) == ["'paid'", "'refunded'", "'void'"]
    assert "status = @status" in cluster.shared_sql
    assert "LIMIT @limit" in cluster.shared_sql
    assert "region = 'EU'" in cluster.shared_sql


def test_extra_filter_after_aggregation_gets_no_shared_sql():
    aggregate = (
        "SELECT customer_id, region, SUM(amount) AS total, COUNT(*) AS n, MAX(created_at) AS last_order "
        "FROM `proj.raw.orders` WHERE amount > 0 GROUP BY customer_id, region"
    )
    parsed = {
        "a": parse(aggregate),
        "b": parse(aggregate.replace("WHERE amount > 0", "WHERE amount > 0 AND status = 'paid'")),
    }

    [cluster] = find_near_duplicates(parsed)

    assert cluster.kind == "mixed"
    assert cluster.shared_sql is None
    assert any(d.clause == "where" and d.change == "added" for v in cluster.variants for d in v.differences)


def test_filter_on_a_column_hidden_by_an_output_name_gets_no_shared_sql():
    parsed = {
        "a": parse(BASE),
        # c.id is filtered, but the shared model's "id" column is o.id.
        "b": parse(BASE + " AND c.id > 10"),
    }

    [cluster] = find_near_duplicates(parsed)

    assert cluster.kind == "mixed"
    assert cluster.shared_sql is None


def test_exact_copies_and_unrelated_queries_are_not_near_duplicates():
    parsed = {
        "a": parse(BASE),
        "b": parse(BASE),
        "c": parse("SELECT user_id, MAX(ts) AS last_seen FROM `proj.raw.events` GROUP BY user_id HAVING COUNT(*) > 3"),
    }

    assert find_near_duplicates(parsed) == []


def test_wrapper_is_not_a_near_duplicate_of_the_query_it_wraps():
    parsed = {
        "a": parse(f"WITH x AS ({BASE}) SELECT id, customer_id, amount, created_at FROM x"),
        "b": parse(BASE.replace("o.amount > 0", "o.amount >= 0")),
    }

    clusters = find_near_duplicates(parsed)

    assert len(clusters) == 1
    locations = {o for v in clusters[0].variants for o in v.occurrences}
    assert locations == {("a", "cte:x"), ("b", "query")}


def test_outer_queries_that_differ_only_inside_a_cte_are_reported_once():
    outer = "WITH x AS ({inner}) SELECT id, customer_id, SUM(amount) AS total FROM x GROUP BY id, customer_id"
    parsed = {
        "a": parse(outer.format(inner=BASE)),
        "b": parse(outer.format(inner=BASE + " AND c.country = 'US'")),
    }

    clusters = find_near_duplicates(parsed, min_nodes=5)

    assert len(clusters) == 1
    assert {o[1] for v in clusters[0].variants for o in v.occurrences} == {"cte:x"}


def test_minhash_banding_finds_the_same_clusters_as_exhaustive_search():
    parsed = {
        f"m{i}": parse(BASE.replace("o.amount > 0", f"o.amount > {i}")) for i in range(6)
    }
    parsed["other"] = parse("SELECT user_id, MAX(ts) AS last_seen FROM `proj.raw.events` GROUP BY user_id")

    exhaustive = find_near_duplicates(parsed)
    banded = find_near_duplicates(parsed, exhaustive_limit=0)

    assert [c.to_json() for c in banded] == [c.to_json() for c in exhaustive]
    assert len(exhaustive[0].variants) == 6


def test_pipeline_report_includes_near_duplicates(tmp_path, capsys):
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(drifted_copies_graph()), encoding="utf-8")

    assert pipeline_main([str(graph), "--similarity", "0.75"]) == 0

    report = json.loads(capsys.readouterr().out)
    [cluster] = report["near_duplicates"]
    assert cluster["kind"] == "extra_columns_and_filters"
    assert cluster["shared_sql"].startswith("SELECT o.id")
    assert report["duplicates"] == []
