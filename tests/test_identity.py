from kumosql import NodeIdentity, Pipeline, Target, load_compiled_graph, normalize_table_reference
from kumosql.pipeline import Model


def test_normalizes_quoted_and_legacy_qualified_table_references_without_case_folding():
    quoted = normalize_table_reference("`Project-X.DataSet.MixedCase`")
    legacy = normalize_table_reference("Project-X:DataSet.MixedCase")

    assert quoted is not None and legacy is not None
    assert quoted.key == "Project-X.DataSet.MixedCase"
    assert quoted == legacy
    assert quoted.stable_key == "table:Project-X.DataSet.MixedCase"


def test_node_identity_keeps_legacy_constructors_and_value_access():
    target = Target("Project-X", "DataSet", "Model")
    table = NodeIdentity.table(target)
    asset = NodeIdentity.asset(r"definitions\model.sqlx")
    legacy_table = NodeIdentity("table", "Project-X.DataSet.Model")
    legacy_asset = NodeIdentity("asset", "definitions/model.sqlx")

    assert table == legacy_table
    assert table.value == "Project-X.DataSet.Model"
    assert asset == legacy_asset
    assert asset.value == "definitions/model.sqlx"


def test_normalizes_defaults_and_keeps_incomplete_names_partial():
    complete = normalize_table_reference(
        "model", default_project="Project-X", default_dataset="DataSet"
    )
    dataset_qualified = normalize_table_reference("DataSet.model", default_project="Project-X")
    partial = normalize_table_reference("model")

    assert complete is not None and dataset_qualified is not None and partial is not None
    assert complete.key == dataset_qualified.key == "Project-X.DataSet.model"
    assert complete.defaulted and dataset_qualified.defaulted
    assert not complete.partial and not dataset_qualified.partial
    assert partial.key == "model" and partial.partial


def test_wildcards_system_views_and_decorators_keep_their_identity_metadata():
    wildcard = normalize_table_reference("Project-X.DataSet.events_*")
    system = normalize_table_reference("Project-X.DataSet.INFORMATION_SCHEMA.TABLES")
    partition = normalize_table_reference("Project-X.DataSet.events$20240101")
    snapshot = normalize_table_reference("Project-X.DataSet.events@1700000000000")

    assert wildcard is not None and wildcard.kind == "wildcard"
    assert wildcard.key == "Project-X.DataSet.events_*"
    assert system is not None and system.kind == "system"
    assert system.key == "Project-X.DataSet.INFORMATION_SCHEMA.TABLES"
    assert partition is not None and partition.key == "Project-X.DataSet.events$20240101"
    assert partition.decorator == "$20240101"
    assert snapshot is not None and snapshot.decorator == "@1700000000000"


def test_compiled_model_table_and_asset_path_share_one_identity_with_observed_refs():
    pipeline = load_compiled_graph(
        {
            "tables": [
                {
                    "target": {"database": "Project-X", "schema": "DataSet", "name": "Model"},
                    "type": "view",
                    "query": "SELECT 1 AS value",
                    "fileName": "definitions/model.sqlx",
                }
            ]
        }
    )

    table_ref, api_ref, asset_ref = pipeline.resolve_observed_references(
        [
            "Project-X.DataSet.Model",
            {"tableReference": {"projectId": "Project-X", "datasetId": "DataSet", "tableId": "Model"}},
            r"definitions\model.sqlx",
        ]
    )

    assert table_ref.status == api_ref.status == asset_ref.status == "exact"
    assert table_ref.identity == api_ref.identity == asset_ref.identity
    assert table_ref.identity == NodeIdentity.for_target("Project-X", "DataSet", "Model")
    assert table_ref.node_kind == "view"
    assert asset_ref.identity.key == "Project-X.DataSet.Model"


def test_decorated_table_resolves_to_base_node_while_retaining_decorator():
    pipeline = Pipeline(
        {"Project-X.DataSet.Model": Model(Target("Project-X", "DataSet", "Model"), "table", "SELECT 1")}
    )

    base, decorated = pipeline.resolve_observed_references(
        ["Project-X.DataSet.Model", "Project-X.DataSet.Model$20240101"]
    )

    assert base.status == decorated.status == "exact"
    assert base.identity == decorated.identity
    assert decorated.decorator == "$20240101"
    assert decorated.to_json()["decorator"] == "$20240101"


def test_unmatched_and_wildcard_observations_are_retained_and_flagged():
    pipeline = Pipeline(
        {"Project-X.DataSet.Model": Model(Target("Project-X", "DataSet", "Model"), "table", "SELECT 1")}
    )

    unmatched, wildcard = pipeline.resolve_observed_references(
        ["Other-Project.OtherData.External", "Project-X.DataSet.Model_*"]
    )

    assert unmatched.reference == "Other-Project.OtherData.External"
    assert unmatched.identity.key == unmatched.reference
    assert unmatched.status == "unmatched"
    assert unmatched.diagnostic_code == "unmatched_reference"
    assert unmatched.diagnostic.code == "unmatched_reference"
    assert unmatched.node_kind == "external"
    assert wildcard.status == "pattern"
    assert wildcard.identity.kind == "wildcard"
    assert wildcard.diagnostic_code == "wildcard_reference"
    assert wildcard.identity.stable_key != NodeIdentity.for_target(
        "Project-X", "DataSet", "Model"
    ).stable_key


def test_partial_observation_reports_ambiguous_candidates_without_merging():
    pipeline = Pipeline(
        {
            "Project-One.DataSet.Model": Model(
                Target("Project-One", "DataSet", "Model"), "table", "SELECT 1"
            ),
            "Project-Two.DataSet.Model": Model(
                Target("Project-Two", "DataSet", "Model"), "table", "SELECT 2"
            ),
        }
    )

    (resolution,) = pipeline.resolve_observed_references(["DataSet.Model"])

    assert resolution.status == "ambiguous"
    assert resolution.identity.key == "DataSet.Model"
    assert len(resolution.candidates) == 2
    assert resolution.diagnostic_code == "ambiguous_reference"


def test_fully_qualified_observations_do_not_use_case_or_suffix_fallbacks():
    pipeline = Pipeline(
        {"Project-X.DataSet.Model": Model(Target("Project-X", "DataSet", "Model"), "table", "SELECT 1")}
    )

    wrong_project, wrong_case = pipeline.resolve_observed_references(
        ["Other-Project.DataSet.Model", "Project-X.DataSet.model"]
    )

    assert wrong_project.status == wrong_case.status == "unmatched"
    assert wrong_project.candidates == wrong_case.candidates == ()

    partial_target_pipeline = Pipeline(
        {"DataSet.Model": Model(Target("", "DataSet", "Model"), "table", "SELECT 1")}
    )
    full_reference = partial_target_pipeline.resolve_observed_references(
        ["Project-X.DataSet.Model"]
    )[0]
    assert full_reference.status == "unmatched"
    assert full_reference.candidates == ()


def test_defaulted_reference_reports_how_it_reached_the_model_and_report_keeps_dotted_keys():
    pipeline = Pipeline(
        {"Project-X.DataSet.Model": Model(Target("Project-X", "DataSet", "Model"), "view", "SELECT 1")},
        default_project="Project-X",
        default_dataset="DataSet",
    )

    resolution = pipeline.resolve_reference("Model", use_defaults=True)
    report = pipeline.report()

    assert resolution.status == "via_default"
    assert resolution.identity.key == "Project-X.DataSet.Model"
    assert pipeline.resolve("Model") == "Project-X.DataSet.Model"
    assert "Project-X.DataSet.Model" in report["node_identities"]
    assert report["node_identities"]["Project-X.DataSet.Model"]["identity"]["id"] == (
        "table:Project-X.DataSet.Model"
    )
