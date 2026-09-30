from kumosql import find_repeated_work, load_compiled_graph, repeated_work_report

BODY = (
    "SELECT o.customer_id, SUM(o.amount) AS total, COUNT(*) AS n "
    "FROM `proj.raw.orders` AS o WHERE o.status = 'paid' AND o.amount > 0 GROUP BY o.customer_id"
)


def table(name, query):
    return {
        "target": {"database": "proj", "schema": "mart", "name": name},
        "type": "table",
        "query": query,
        "fileName": f"definitions/{name}.sqlx",
    }


def graph(*tables):
    return {
        "tables": list(tables),
        "declarations": [
            {"target": {"database": "proj", "schema": "raw", "name": "orders"}},
            {"target": {"database": "other", "schema": "raw", "name": "orders"}},
        ],
    }


def by_kind(items, kind):
    return [i for i in items if i.kind == kind]


def test_repeat_within_one_query():
    pipeline = load_compiled_graph(
        graph(table("a", f"WITH p AS ({BODY}) SELECT * FROM p UNION ALL SELECT * FROM ({BODY}) AS q"))
    )

    items = by_kind(find_repeated_work(pipeline), "identical_logic")

    assert len(items) == 1
    assert items[0].scope == "within_query"
    assert items[0].certainty == "identical_text"
    assert {(r.node, r.where) for r in items[0].repeats} == {
        ("proj.mart.a", "cte:p"),
        ("proj.mart.a", "subquery:q"),
    }


def test_repeat_across_assets_and_both_scope():
    pipeline = load_compiled_graph(
        graph(
            table("a", f"WITH p AS ({BODY}) SELECT * FROM p"),
            table("b", f"SELECT * FROM ({BODY}) AS q"),
            table("c", f"WITH p AS ({BODY}), r AS ({BODY}) SELECT * FROM p JOIN r USING (customer_id)"),
        )
    )

    items = by_kind(find_repeated_work(pipeline), "identical_logic")

    assert len(items) == 1
    assert items[0].scope == "both"
    assert items[0].models == ("proj.mart.a", "proj.mart.b", "proj.mart.c")
    assert len(items[0].repeats) == 4


def test_same_text_over_different_tables_is_not_grouped():
    other = BODY.replace("proj.raw.orders", "other.raw.orders")
    pipeline = load_compiled_graph(
        graph(
            table("a", f"WITH p AS ({BODY}) SELECT * FROM p"),
            table("b", f"WITH p AS ({other}) SELECT * FROM p"),
        )
    )

    assert by_kind(find_repeated_work(pipeline), "identical_logic") == []


def test_shadowing_cte_name_is_not_grouped():
    inner = (
        "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n "
        "FROM base WHERE amount > 0 GROUP BY customer_id"
    )
    other_base = "SELECT id AS customer_id, 0 AS amount FROM `other.raw.orders`"
    pipeline = load_compiled_graph(
        graph(
            table("a", f"WITH base AS (SELECT * FROM `proj.raw.orders`), p AS ({inner}) SELECT * FROM p"),
            table("b", f"WITH base AS ({other_base}), p AS ({inner}) SELECT * FROM p"),
        )
    )

    assert by_kind(find_repeated_work(pipeline), "identical_logic") == []


def test_similar_logic_is_labelled_similar():
    variant = BODY.replace("o.amount > 0", "o.amount > 10")
    pipeline = load_compiled_graph(
        graph(
            table("a", f"WITH p AS ({BODY}) SELECT * FROM p"),
            table("b", f"WITH p AS ({variant}) SELECT * FROM p"),
        )
    )

    items = find_repeated_work(pipeline)

    similar = by_kind(items, "similar_logic")
    assert len(similar) == 1 and similar[0].certainty == "similar"
    assert by_kind(items, "identical_logic") == []


def test_repeated_scan_needs_enough_readers():
    tables = [table(n, f"SELECT id FROM `proj.raw.orders` WHERE id > {i}") for i, n in enumerate("abc")]
    pipeline = load_compiled_graph(graph(*tables))

    scans = by_kind(find_repeated_work(pipeline), "repeated_scan")
    assert len(scans) == 1
    assert scans[0].scope == "across_assets"
    assert scans[0].tables == ("proj.raw.orders",)
    assert len(scans[0].repeats) == 3

    assert by_kind(find_repeated_work(pipeline, min_readers=4), "repeated_scan") == []


def test_repeated_scan_within_one_query():
    pipeline = load_compiled_graph(
        graph(table("a", "SELECT x.id FROM `proj.raw.orders` AS x JOIN `proj.raw.orders` AS y ON x.id = y.id"))
    )

    scans = by_kind(find_repeated_work(pipeline), "repeated_scan")
    assert len(scans) == 1 and scans[0].scope == "within_query"


def test_report_shape_has_locations_and_no_cost():
    pipeline = load_compiled_graph(
        graph(
            table("a", f"WITH p AS ({BODY}) SELECT * FROM p"),
            table("b", f"SELECT * FROM ({BODY}) AS q"),
        )
    )

    report = repeated_work_report(pipeline)

    item = next(o for o in report["opportunities"] if o["kind"] == "identical_logic")
    assert item["repeats"] == [
        {"node": "proj.mart.a", "where": "cte:p"},
        {"node": "proj.mart.b", "where": "subquery:q"},
    ]
    assert "measured_cost" not in item and "savings" not in item
