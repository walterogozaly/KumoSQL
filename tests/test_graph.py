from kumosql import (
    Pipeline,
    ObservedRead,
    Scope,
    Target,
    build_query_graph,
    edge_confidence,
)
from kumosql.pipeline import Model


PROJECT = "DemoProject"
DATASET = "Stage"
INPUT = Target(PROJECT, DATASET, "Input")
OUTPUT = Target(PROJECT, DATASET, "Output")


def pipeline_with_declared_and_parsed_edge():
    return Pipeline(
        {
            INPUT.key: Model(INPUT, "table", "SELECT 1 AS id"),
            OUTPUT.key: Model(
                OUTPUT,
                "view",
                f"SELECT id FROM `{INPUT.key}`",
                path="definitions/output.sqlx",
                declared_dependencies=(INPUT,),
            ),
        }
    )


def test_graph_combines_declared_parsed_and_observed_edges_without_changing_upstream():
    pipeline = pipeline_with_declared_and_parsed_edge()
    result = build_query_graph(
        pipeline,
        [
            ObservedRead(
                job_id="synthetic-a",
                creation_time="2025-01-01T00:00:00Z",
                destination=OUTPUT.key,
                referenced_tables=(INPUT.key,),
                attributes={"user": "private@example.invalid"},
            ),
            {
                "job_id": "synthetic-b",
                "creation_time": "2025-01-03T00:00:00+00:00",
                "destination": OUTPUT.key,
                "referenced_tables": [INPUT.key],
            },
        ],
    )

    (edge,) = [
        edge for edge in result.edges
        if edge.upstream.key == INPUT.key and edge.downstream.key == OUTPUT.key
    ]
    assert edge.source == "both"
    assert edge.provenance == ("declared", "parsed", "observed")
    assert edge.confidence == "high"
    assert edge.first_seen == "2025-01-01T00:00:00Z"
    assert edge.last_seen == "2025-01-03T00:00:00Z"
    assert edge.observed_count == 2

    report = pipeline.report()
    assert report["upstream"] == {INPUT.key: [], OUTPUT.key: [INPUT.key]}
    assert report["graph"]["edges"]


def test_unmatched_references_and_destination_less_reads_are_kept_and_flagged():
    pipeline = pipeline_with_declared_and_parsed_edge()
    result = build_query_graph(
        pipeline,
        [
            {
                "job_id": "synthetic-c",
                "creation_time": "2025-02-01T00:00:00Z",
                "destination": OUTPUT.key,
                "referenced_tables": ["OtherProject.Raw.External"],
                "user": "not-serialized@example.invalid",
            },
            {
                "job_id": "synthetic-d",
                "creation_time": "2025-02-02T00:00:00Z",
                "destination": None,
                "referenced_tables": [INPUT.key],
            },
            {
                "job_id": "synthetic-e",
                "creation_time": "2025-02-03T00:00:00Z",
                "destination": "OtherProject.Raw.Output",
                "referenced_tables": [INPUT.key],
            },
        ],
    )

    assert result.unresolved_observation_count == 2
    assert {sample["role"] for sample in result.unresolved_observation_samples} == {
        "reference",
        "destination",
    }
    assert result.unattributed_observation_count == 1
    assert result.unattributed_observation_samples[0]["reason"] == "missing_destination"
    assert any(
        edge.upstream.key == "OtherProject.Raw.External" and edge.observed
        for edge in result.edges
    )
    assert any(
        edge.upstream.key == INPUT.key
        and edge.downstream.key == "OtherProject.Raw.Output"
        and edge.observed
        for edge in result.edges
    )

    serialized = result.to_json()
    encoded = str(serialized)
    assert "synthetic-c" not in encoded and "not-serialized@example.invalid" not in encoded
    assert "synthetic-d" not in encoded and "synthetic-e" not in encoded
    assert any(node["resolved"] is False for node in serialized["nodes"])


def test_observed_scope_filters_rows_before_edge_aggregation():
    pipeline = pipeline_with_declared_and_parsed_edge()
    rows = [
        {
            "job_id": "included",
            "creation_time": "2025-03-01T00:00:00Z",
            "destination": OUTPUT.key,
            "referenced_tables": [INPUT.key],
            "project": "Allowed",
        },
        {
            "job_id": "excluded",
            "creation_time": "2025-03-02T00:00:00Z",
            "destination": OUTPUT.key,
            "referenced_tables": [INPUT.key],
            "project": "Other",
        },
    ]

    result = build_query_graph(
        pipeline,
        rows,
        scope=Scope("generic", {"project": ("Allowed",)}),
    )
    edge = next(
        edge for edge in result.edges
        if edge.upstream.key == INPUT.key and edge.downstream.key == OUTPUT.key
    )

    assert edge.observed_count == 1
    assert result.filtered_observation_count == 1


def test_decorated_observed_table_keeps_base_edge_identity_and_decorator_metadata():
    pipeline = pipeline_with_declared_and_parsed_edge()
    result = build_query_graph(
        pipeline,
        [
            ObservedRead(
                job_id="synthetic-g",
                creation_time="2025-03-03T00:00:00Z",
                destination=OUTPUT.key,
                referenced_tables=(f"{INPUT.key}$20250303",),
            )
        ],
    )
    edge = next(
        edge for edge in result.edges
        if edge.upstream.key == INPUT.key and edge.downstream.key == OUTPUT.key
    )

    assert edge.upstream.key == INPUT.key
    assert edge.decorator_count == 1
    assert edge.decorator_samples == ("$20250303",)


def test_confidence_is_categorical_and_shared_by_edge_builders():
    assert edge_confidence(declared=True, parsed=False, observed=True) == "high"
    assert edge_confidence(declared=True, parsed=False, observed=False) == "medium"
    assert edge_confidence(declared=False, parsed=True, observed=False) == "medium"
    assert edge_confidence(
        declared=False, parsed=True, observed=False, parse_incomplete=True
    ) == "low"


def test_parsed_sql_relationship_is_published_as_declared_source_with_fine_provenance():
    parsed_only = Target(PROJECT, DATASET, "ParsedOnly")
    pipeline = Pipeline(
        {
            INPUT.key: Model(INPUT, "table", "SELECT 1 AS id"),
            parsed_only.key: Model(
                parsed_only,
                "view",
                f"SELECT id FROM `{INPUT.key}`",
            ),
        }
    )

    edge = next(
        edge for edge in build_query_graph(pipeline).edges
        if edge.upstream.key == INPUT.key and edge.downstream.key == parsed_only.key
    )
    assert edge.source == "declared"
    assert edge.provenance == ("parsed",)
    assert edge.confidence == "medium"


def test_model_scoped_report_marks_observation_counts_as_unscoped():
    pipeline = pipeline_with_declared_and_parsed_edge()
    report = pipeline.report(
        scope=Scope("one model", {"name": ("Output",)}),
        observed_reads=[
            {
                "job_id": "synthetic-e",
                "creation_time": "2025-04-01T00:00:00Z",
                "destination": OUTPUT.key,
                "referenced_tables": ["ExternalProject.Raw.Unknown"],
            },
            {
                "job_id": "synthetic-f",
                "creation_time": "2025-04-02T00:00:00Z",
                "destination": None,
                "referenced_tables": [INPUT.key],
            },
        ],
    )

    graph = report["graph"]
    assert graph["unresolved_observations"]["count"] is None
    assert graph["unresolved_observations"]["unscoped_count"] == 1
    assert graph["unresolved_observations"]["scope_applied"] is False
    assert graph["unattributed_observations"]["count"] is None
    assert graph["unattributed_observations"]["unscoped_count"] == 1
    assert graph["scope_applied"] == {"observations": False, "models": True}
