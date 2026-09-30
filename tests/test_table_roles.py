from kumosql import RoleSignal, TableRole, infer_roles, load_compiled_graph, table_roles_report


def model(name, query, kind="table"):
    return {
        "target": {"database": "p", "schema": "m", "name": name},
        "type": kind,
        "query": query,
        "fileName": f"definitions/{name}.sqlx",
    }


def build(models, sources, schema=None):
    graph = {
        "tables": models,
        "declarations": [{"target": {"database": "p", "schema": "raw", "name": n}} for n in sources],
    }
    return load_compiled_graph(graph, source_schema=schema)


def src(name):
    return f"p.raw.{name}"


def sig(role, kind):
    return [s for s in role.signals if s.kind == kind]


ENTITY = {
    "entity_key": "INT64",
    "label": "STRING",
    "kind": "STRING",
    "active": "BOOL",
    "created": "DATE",
}
EVENTS = {
    "entity_key": "INT64",
    "group_id": "INT64",
    "amount": "FLOAT64",
    "quantity": "INT64",
    "happened": "DATE",
}
LOOKUP_READERS = [
    model(
        f"r{i}",
        "SELECT e.entity_key, d.label FROM `p.raw.events` e JOIN `p.raw.entity` d ON e.entity_key = d.entity_key",
    )
    for i in (1, 2)
]


def pipeline_two_way():
    return build(
        LOOKUP_READERS,
        ["events", "entity"],
        {src("events"): EVENTS, src("entity"): ENTITY},
    )


def test_dimension_from_usage_and_shape_is_high():
    role = infer_roles(pipeline_two_way())[src("entity")]
    assert isinstance(role, TableRole)
    assert role.role == "dimension"
    assert role.confidence == "high"
    assert role.readers_examined == 2 and role.readers_unexamined == 0
    assert all(isinstance(s, RoleSignal) for s in role.signals)


def test_unavailable_signals_are_listed_not_counted():
    role = infer_roles(pipeline_two_way())[src("entity")]
    assert sig(role, "size")[0].available is False
    assert sig(role, "declared")[0].available is False
    assert role.role == "dimension"


def test_fact_from_aggregation_and_shape():
    readers = [
        model(f"agg{i}", "SELECT group_id, SUM(amount) AS total FROM `p.raw.events` GROUP BY group_id")
        for i in (1, 2)
    ]
    role = infer_roles(build(readers, ["events"], {src("events"): EVENTS}))[src("events")]
    assert role.role == "fact" and role.confidence == "high"


def test_single_signal_is_at_most_medium():
    pipeline = build(LOOKUP_READERS, ["events", "entity"], {})
    role = infer_roles(pipeline)[src("entity")]
    assert role.role == "dimension"
    assert role.confidence == "medium"
    assert sig(role, "schema_shape")[0].available is False


def test_declared_wins_and_is_labeled():
    role = infer_roles(pipeline_two_way(), declared={src("entity"): "fact"})[src("entity")]
    assert role.role == "fact" and role.confidence == "high"
    assert role.reason == "declared"
    assert [s.kind for s in role.signals] == ["declared"]


def test_invalid_declared_role_is_unknown_with_reason():
    role = infer_roles(pipeline_two_way(), declared={src("entity"): "banana"})[src("entity")]
    assert role.role == "unknown" and "invalid" in role.reason


def test_table_used_both_ways_is_unknown_with_conflict():
    readers = LOOKUP_READERS + [
        model(f"rep{i}", "SELECT kind, SUM(quantity) AS q FROM `p.raw.entity` GROUP BY kind") for i in (1, 2)
    ]
    schema = {src("events"): EVENTS, src("entity"): {**ENTITY, "quantity": "INT64"}}
    role = infer_roles(build(readers, ["events", "entity"], schema))[src("entity")]
    assert role.role == "unknown"
    assert "conflicting" in role.reason
    directions = {s.points_to for s in role.signals if s.kind == "parsed_usage"}
    assert {"dimension", "fact"} <= directions


def test_two_weak_signals_that_disagree_are_unknown():
    readers = [model("agg", "SELECT kind, SUM(quantity) AS q FROM `p.raw.entity` GROUP BY kind")]
    schema = {src("entity"): {"entity_id": "INT64", "label": "STRING", "kind": "STRING", "quantity": "INT64"}}
    role = infer_roles(build(readers, ["entity"], schema))[src("entity")]
    assert role.role == "unknown"


def test_scd_several_rows_per_key_stays_dimension():
    scd = {"entity_key": "INT64", "label": "STRING", "kind": "STRING", "valid_from": "DATE", "valid_to": "DATE"}
    readers = [
        model(
            f"h{i}",
            "SELECT e.entity_key, d.label FROM `p.raw.events` e JOIN `p.raw.scd` d "
            "ON e.entity_key = d.entity_key AND e.happened >= d.valid_from AND e.happened < d.valid_to",
        )
        for i in (1, 2)
    ]
    schema = {src("events"): EVENTS, src("scd"): scd}
    role = infer_roles(build(readers, ["events", "scd"], schema))[src("scd")]
    assert role.role == "dimension"
    text = " ".join(s.detail for s in role.signals)
    assert "validity" in text and "not be unique" in text


def test_degenerate_dimension_inside_fact_stays_fact():
    events = {**EVENTS, "reference_text": "STRING"}
    readers = [
        model(f"a{i}", "SELECT group_id, SUM(amount) AS t FROM `p.raw.events` GROUP BY group_id") for i in (1, 2)
    ]
    role = infer_roles(build(readers, ["events"], {src("events"): events}))[src("events")]
    assert role.role == "fact"


def test_junction_table_is_bridge():
    link = {"left_id": "INT64", "right_id": "INT64"}
    readers = [
        model(
            f"j{i}",
            "SELECT a.label, b.label AS other FROM `p.raw.entity` a "
            "JOIN `p.raw.link` l ON a.entity_key = l.left_id JOIN `p.raw.entity` b ON l.right_id = b.entity_key",
        )
        for i in (1, 2)
    ]
    schema = {src("entity"): ENTITY, src("link"): link}
    role = infer_roles(build(readers, ["entity", "link"], schema))[src("link")]
    assert role.role == "bridge"
    assert role.confidence == "high"


def test_size_alone_does_not_decide():
    pipeline = build([], ["events", "entity"], {})
    roles = infer_roles(pipeline, row_counts={src("entity"): 10, src("events"): 1_000_000})
    assert roles[src("entity")].role == "unknown"


def test_tiny_fact_is_not_flipped_by_size():
    readers = [
        model(
            f"r{i}",
            "SELECT e.group_id, SUM(e.amount) AS t FROM `p.raw.events` e "
            "JOIN `p.raw.entity` d ON e.entity_key = d.entity_key GROUP BY e.group_id",
        )
        for i in (1, 2)
    ]
    pipeline = build(readers, ["events", "entity"], {src("events"): EVENTS, src("entity"): ENTITY})
    roles = infer_roles(pipeline, row_counts={src("events"): 5, src("entity"): 5_000_000})
    assert roles[src("events")].role == "fact"
    assert sig(roles[src("events")], "size")[0].points_to == "dimension"
    assert roles[src("events")].confidence == "medium"


def test_size_corroborates_when_available():
    pipeline = pipeline_two_way()
    counts = {src("entity"): 10, src("events"): 10_000}
    role = infer_roles(pipeline, row_counts=counts)[src("entity")]
    assert sig(role, "size")[0].available and sig(role, "size")[0].points_to == "dimension"
    assert role.role == "dimension" and role.confidence == "high"


def test_wide_denormalized_table_is_unknown():
    wide = {f"c{i}": "STRING" for i in range(6)} | {f"m{i}": "FLOAT64" for i in range(6)} | {"row_id": "INT64"}
    role = infer_roles(build([], ["wide"], {src("wide"): wide}))[src("wide")]
    assert role.role == "unknown"
    assert "wide" in sig(role, "schema_shape")[0].detail


def test_unparsed_readers_are_counted_not_voted():
    readers = LOOKUP_READERS + [model("broken", "SELECT FROM WHERE (((")]
    pipeline = build(readers, ["events", "entity"], {src("events"): EVENTS, src("entity"): ENTITY})
    pipeline.models["p.m.broken"].declared_dependencies = (pipeline.sources[src("entity")],)
    role = infer_roles(pipeline)[src("entity")]
    assert role.role == "dimension"
    assert role.readers_unexamined == 1
    assert role.readers_examined == 2


def test_model_shape_from_grouping_and_distinct():
    models = [
        model(
            "rollup",
            "SELECT group_id, entity_key, SUM(amount) AS t FROM `p.raw.events` GROUP BY group_id, entity_key",
        ),
        model("uniq", "SELECT DISTINCT entity_key, label FROM `p.raw.entity`"),
        model("plain", "SELECT entity_key FROM `p.raw.entity`"),
    ]
    schema = {src("events"): EVENTS, src("entity"): ENTITY}
    roles = infer_roles(build(models, ["events", "entity"], schema))
    assert roles["p.m.rollup"].role == "fact"
    assert roles["p.m.uniq"].role == "dimension"
    assert roles["p.m.plain"].role == "unknown"


def test_covers_every_model_and_source_and_serializes():
    pipeline = pipeline_two_way()
    roles = infer_roles(pipeline)
    assert set(roles) == {"p.m.r1", "p.m.r2", src("events"), src("entity")}
    report = table_roles_report(pipeline)
    assert report["tables"][src("entity")]["role"] == "dimension"
    assert "SELECT" not in str(report)


def test_bad_input_never_raises():
    pipeline = pipeline_two_way()
    roles = infer_roles(
        pipeline,
        row_counts={src("entity"): -3, "x": "many", None: 1, src("events"): True},
        declared={None: "fact", 5: 1, src("entity"): None},
    )
    assert roles[src("entity")].role == "unknown"
    assert infer_roles(pipeline, row_counts="oops", declared=["x"])  # type: ignore[arg-type]
    assert infer_roles(object()) == {}  # type: ignore[arg-type]
