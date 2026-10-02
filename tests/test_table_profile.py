from kumosql import (
    Pipeline,
    Target,
    profile_pipeline,
    profile_query,
)
from kumosql.pipeline import Model

ORDERS = Target("proj", "raw", "orders")
ITEMS = Target("proj", "raw", "order_items")

SOURCES = {ORDERS.key: ORDERS, ITEMS.key: ITEMS}
SCHEMA = {
    ORDERS.key: {"id": "INT64", "customer_id": "INT64", "amount": "FLOAT64", "status": "STRING", "region_id": "INT64"},
    ITEMS.key: {"order_id": "INT64", "sku": "STRING", "qty": "INT64"},
}


def build(models: dict[str, str], kinds: dict[str, str] | None = None, schema=SCHEMA) -> Pipeline:
    kinds = kinds or {}
    built = {}
    for name, sql in models.items():
        target = Target("proj", "core", name)
        built[target.key] = Model(target, kinds.get(name, "table"), sql)
    return Pipeline(built, sources=dict(SOURCES), source_schema=dict(schema))


def profiles(models, **kw):
    pipeline = build(models, **kw)
    return {key.split(".")[-1]: value for key, value in profile_pipeline(pipeline).items()}


def test_group_by_grain_and_aggregate_meaning():
    p = profiles({"totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id"})["totals"]
    assert p.grain.status == "derived" and p.grain.keys == ("customer_id",)
    assert p.attribute("total").meaning == "agg:SUM(col:proj.raw.orders.amount)"
    assert p.attribute("customer_id").meaning == "col:proj.raw.orders.customer_id"
    assert p.complete


def test_aggregate_meaning_ignores_names_case_and_alias():
    p = profiles({
        "a": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id",
        "b": "select CUSTOMER_ID as cid, sum(Amount) as revenue from `proj.raw.orders` group by 1",
    })
    assert p["a"].attribute("total").meaning == p["b"].attribute("revenue").meaning
    assert p["b"].grain.keys == ("cid",)
    other = profiles({"c": "SELECT customer_id, AVG(amount) AS total FROM proj.raw.orders GROUP BY customer_id"})["c"]
    assert other.attribute("total").meaning != p["a"].attribute("total").meaning


def test_distinct_grain():
    p = profiles({"d": "SELECT DISTINCT customer_id, status FROM proj.raw.orders"})["d"]
    assert p.grain.status == "derived"
    assert p.grain.keys == ("customer_id", "status")


def test_row_number_dedupe_grain_qualify_and_wrapper():
    q = profiles({
        "latest": "SELECT id, customer_id, amount FROM proj.raw.orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY amount DESC) = 1",
        "wrapped": "WITH ranked AS (SELECT id, amount, ROW_NUMBER() OVER (PARTITION BY id ORDER BY amount) AS rn "
        "FROM proj.raw.orders) SELECT id, amount FROM ranked WHERE rn = 1",
    })
    assert q["latest"].grain.keys == ("id",) and q["latest"].grain.status == "derived"
    assert q["wrapped"].grain.keys == ("id",) and q["wrapped"].grain.status == "derived"


def test_rename_case_passthrough_through_cte_and_view_chain_same_meaning():
    p = profiles(
        {
            "stg": "SELECT id, amount AS Order_Amount FROM proj.raw.orders",
            "step": "WITH x AS (SELECT ORDER_AMOUNT AS value FROM proj.core.stg) SELECT value AS v FROM x",
            "final": "SELECT v AS the_amount FROM proj.core.step",
        },
        kinds={"stg": "view", "step": "view"},
    )
    expected = "col:proj.raw.orders.amount"
    assert p["stg"].attribute("Order_Amount").meaning == expected
    assert p["step"].attribute("v").meaning == expected
    assert p["final"].attribute("the_amount").meaning == expected
    assert p["final"].attribute("the_amount").sources == ("proj.raw.orders.amount",)


def test_aggregate_through_view_keeps_meaning():
    p = profiles(
        {
            "totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id",
            "v": "SELECT customer_id AS c, total AS t FROM proj.core.totals",
        },
        kinds={"v": "view"},
    )
    assert p["v"].attribute("t").meaning == "agg:SUM(col:proj.raw.orders.amount)"
    assert p["v"].attribute("t").transform == "renamed"


def test_other_expression_is_equality_only():
    p = profiles({"e": "SELECT id, amount * 2 AS double_amount FROM proj.raw.orders"})["e"]
    attr = p.attribute("double_amount")
    assert attr.status == "known" and attr.meaning.startswith("expr:") and attr.equality_only
    swapped = profiles({"e": "SELECT id, 2 * amount AS d FROM proj.raw.orders"})["e"].attribute("d")
    assert swapped.meaning != attr.meaning  # equality only: no algebra


def test_non_deterministic_expression_is_unknown():
    p = profiles({"n": "SELECT id, RAND() AS r FROM proj.raw.orders"})["n"]
    assert p.attribute("r").status == "unknown"
    assert p.attribute("r").reason == "non_deterministic"
    assert not p.complete


def test_join_that_fans_out_has_unknown_grain():
    p = profiles({
        "j": "SELECT o.id, o.amount, i.sku FROM (SELECT DISTINCT id, amount FROM proj.raw.orders) o "
        "JOIN proj.raw.order_items i ON i.order_id = o.id"
    })["j"]
    assert p.grain.status == "unknown"
    assert "fan_out_join" in p.grain.reason
    assert not p.complete


def test_many_to_one_join_keeps_grain():
    p = profiles({
        "regions": "SELECT region_id, COUNT(*) AS n FROM proj.raw.orders GROUP BY region_id",
        "j": "SELECT o.id AS order_id, o.amount, r.n FROM "
        "(SELECT id, MAX(amount) AS amount, MAX(region_id) AS region_id FROM proj.raw.orders GROUP BY id) o "
        "LEFT JOIN proj.core.regions r ON r.region_id = o.region_id",
    })
    assert p["regions"].grain.keys == ("region_id",)
    assert p["j"].grain.status == "derived"
    assert p["j"].grain.keys == ("order_id",)


def test_grain_carried_through_passthrough_select():
    p = profiles({
        "base": "SELECT id, MAX(amount) AS amount FROM proj.raw.orders GROUP BY id",
        "next": "SELECT id AS order_id, amount * 2 AS d FROM proj.core.base WHERE amount > 0",
    })
    assert p["next"].grain.status == "derived" and p["next"].grain.keys == ("order_id",)


def test_join_on_partial_grain_fans_out():
    p = profiles({
        "pairs": "SELECT customer_id, region_id, COUNT(*) AS n FROM proj.raw.orders GROUP BY customer_id, region_id",
        "base": "SELECT DISTINCT id, customer_id FROM proj.raw.orders",
        "j": "SELECT b.id, p.n FROM proj.core.base b JOIN proj.core.pairs p ON p.customer_id = b.customer_id",
    })
    assert p["j"].grain.status == "unknown" and "fan_out_join" in p["j"].grain.reason


def test_union_all_is_unknown_and_union_distinct_derived():
    p = profiles({
        "u": "SELECT id, amount FROM proj.raw.orders UNION ALL SELECT id, amount FROM proj.raw.orders",
        "d": "SELECT id, amount FROM proj.raw.orders UNION DISTINCT SELECT id, amount FROM proj.raw.orders",
    })
    assert p["u"].grain.status == "unknown" and p["u"].grain.reason == "union_mixed_grain"
    assert p["d"].grain.status == "derived" and p["d"].grain.keys == ("amount", "id")
    assert p["u"].attribute("amount").meaning == "col:proj.raw.orders.amount"


def test_union_of_different_columns_meaning():
    p = profiles({"u": "SELECT amount AS x FROM proj.raw.orders UNION ALL SELECT qty FROM proj.raw.order_items"})["u"]
    assert p.attribute("x").meaning == "union(col:proj.raw.order_items.qty|col:proj.raw.orders.amount)"
    assert p.attribute("x").equality_only


def test_unexpanded_star_is_unknown():
    p = profiles({"s": "SELECT DISTINCT * FROM proj.raw.unknown_table"}, schema={})["s"]
    assert p.grain.status == "unknown" and p.grain.reason == "unexpanded_star"
    assert p.attribute("*").status == "unknown" and p.attribute("*").reason == "unexpanded_star"
    assert not p.complete


def test_star_with_known_schema_is_expanded():
    p = profiles({"s": "SELECT * FROM proj.raw.order_items"})["s"]
    assert {a.column for a in p.attributes} == {"order_id", "sku", "qty"}
    assert all(a.status == "known" for a in p.attributes)


def test_unparsed_model_is_unknown_and_never_raises():
    p = profiles({"bad": "SELECT FROM WHERE ((", "ops": ""}, kinds={"ops": "operations"})
    assert p["bad"].grain.status == "unknown" and p["bad"].grain.reason == "unparsed"
    assert not p["bad"].complete and not p["bad"].row_scope.comparable
    assert p["ops"].grain.reason == "not_a_query"
    p["bad"].to_json()


def test_downstream_of_unparsed_model_is_unknown():
    p = profiles({"bad": "SELECT FROM WHERE ((", "child": "SELECT id FROM proj.core.bad"})
    assert p["child"].attribute("id").status == "unknown"
    assert not p["child"].complete


def test_filters_normalized_order_and_case():
    p = profiles({
        "a": "SELECT id FROM proj.raw.orders WHERE status = 'paid' AND amount > 10",
        "b": "select id from proj.raw.orders where 10 < AMOUNT and 'paid' = STATUS",
        "c": "SELECT id FROM proj.raw.orders WHERE status = 'PAID' AND amount > 10",
    })
    assert p["a"].row_scope.filters == p["b"].row_scope.filters
    assert p["a"].row_scope.comparable
    assert len(p["a"].row_scope.filters) == 2
    assert p["a"].row_scope.filters != p["c"].row_scope.filters  # string literals keep their case


def test_filters_inherited_through_view_and_cte():
    p = profiles(
        {
            "v": "SELECT id, amount FROM proj.raw.orders WHERE status = 'paid'",
            "t": "WITH x AS (SELECT id, amount FROM proj.core.v WHERE amount > 0) SELECT id, amount FROM x",
        },
        kinds={"v": "view"},
    )
    assert len(p["t"].row_scope.filters) == 2
    assert set(p["v"].row_scope.filters) <= set(p["t"].row_scope.filters)


def test_having_is_part_of_scope():
    p = profiles({"h": "SELECT customer_id, SUM(amount) AS t FROM proj.raw.orders GROUP BY customer_id HAVING t > 100"})["h"]
    assert p.row_scope.filters == ("agg:SUM(col:proj.raw.orders.amount) > 100",)


def test_limit_and_sampling_are_not_comparable():
    p = profiles({
        "l": "SELECT id FROM proj.raw.orders ORDER BY amount DESC LIMIT 10",
        "s": "SELECT id FROM proj.raw.orders TABLESAMPLE SYSTEM (10 PERCENT)",
        "down": "SELECT id FROM proj.core.l",
    })
    assert not p["l"].row_scope.comparable and p["l"].row_scope.reason == "limit"
    assert not p["s"].row_scope.comparable and p["s"].row_scope.reason == "sampling"
    assert not p["down"].row_scope.comparable  # inherited
    assert not p["l"].complete


def test_non_deterministic_filter_is_opaque():
    p = profiles({"w": "SELECT id FROM proj.raw.orders WHERE created > CURRENT_DATE()"})["w"]
    assert not p.row_scope.comparable
    assert p.row_scope.filters[0].startswith("opaque:")


def test_declared_grain():
    pipeline = build({"t": "SELECT id, amount FROM proj.raw.orders"})
    key = "proj.core.t"
    p = profile_pipeline(pipeline, declared_grain={key: ["id"]})[key]
    assert p.grain.status == "declared" and p.grain.keys == ("id",)
    q = profile_query(pipeline, "SELECT id AS x FROM proj.core.t", declared_grain={key: ["id"]})
    assert q.grain.status == "derived" and q.grain.keys == ("x",)


def test_profile_query_not_in_pipeline_matches_model_meaning():
    pipeline = build({"totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id"})
    query = profile_query(
        pipeline,
        "SELECT CUSTOMER_ID AS who, SUM(amount) AS spend FROM `proj.raw.orders` GROUP BY who",
    )
    model = profile_pipeline(pipeline)["proj.core.totals"]
    assert query.table == "<query>"
    assert query.grain.keys == ("who",) and query.grain.status == "derived"
    assert query.attribute("spend").meaning == model.attribute("total").meaning
    assert query.attribute("who").meaning == model.attribute("customer_id").meaning
    assert query.row_scope == model.row_scope
    assert query.complete
    assert "proj.core.totals" in profile_pipeline(pipeline)
    assert set(pipeline.models) == {"proj.core.totals"}  # the pipeline is untouched


def test_profile_query_reads_pipeline_models():
    pipeline = build({"v": "SELECT id, amount FROM proj.raw.orders WHERE status = 'paid'"}, kinds={"v": "view"})
    query = profile_query(pipeline, "SELECT id, amount AS a FROM proj.core.v")
    assert query.attribute("a").meaning == "col:proj.raw.orders.amount"
    assert query.row_scope.filters == profile_pipeline(pipeline)["proj.core.v"].row_scope.filters


def test_profile_query_odd_input_never_raises():
    pipeline = build({"t": "SELECT id FROM proj.raw.orders"})
    for sql in ("", "SELEC ((", "DROP TABLE x", None, 5):
        result = profile_query(pipeline, sql)  # type: ignore[arg-type]
        assert result.grain.status == "unknown" and not result.complete
        result.to_json()


def test_to_json_shape():
    p = profiles({"t": "SELECT customer_id, COUNT(*) AS n FROM proj.raw.orders GROUP BY customer_id"})["t"]
    data = p.to_json()
    assert set(data) == {"table", "complete", "grain", "attributes", "row_scope"}
    assert data["grain"] == {"keys": ["customer_id"], "status": "derived", "reason": None}
    assert data["attributes"][1]["meaning"] == "agg:COUNT(*)"


def test_wildcard_table_grain_unknown_and_suffix_filter_in_scope():
    p = profiles({
        "w": "SELECT id FROM `proj.raw.events_*` WHERE _TABLE_SUFFIX BETWEEN '20240101' AND '20240131'"
    })["w"]
    assert p.grain.status == "unknown" and p.grain.reason == "wildcard_table"
    assert "col:proj.raw.events_*._table_suffix" in p.row_scope.filters[0]


def test_masked_incremental_predicate_not_comparable():
    pipeline = build({"inc": "SELECT id FROM proj.raw.orders"}, kinds={"inc": "incremental"})
    pipeline.models["proj.core.inc"].masked_expressions = ("when(incremental(), 'WHERE x')",)
    assert not profile_pipeline(pipeline)["proj.core.inc"].row_scope.comparable


def test_self_join_meaning():
    p = profiles({
        "sj": "SELECT a.id, b.amount AS other_amount FROM (SELECT DISTINCT id, region_id, amount FROM proj.raw.orders) a "
        "JOIN (SELECT DISTINCT id, region_id, amount FROM proj.raw.orders) b ON a.region_id = b.region_id",
    })["sj"]
    assert p.attribute("other_amount").meaning == "col:proj.raw.orders.amount"
    assert p.grain.status == "unknown"


def test_profiles_are_computed_once_per_pipeline_and_grain():
    pipeline = build({"a": "SELECT id FROM proj.raw.orders"})
    first = profile_pipeline(pipeline)
    assert profile_pipeline(pipeline) == first
    assert profile_pipeline(pipeline) is not first  # a copy of the saved result: callers may edit theirs
    assert next(iter(profile_pipeline(pipeline).values())) is next(iter(first.values()))
    other = profile_pipeline(pipeline, declared_grain={"proj.core.a": ["id"]})
    assert other.keys() == first.keys()
