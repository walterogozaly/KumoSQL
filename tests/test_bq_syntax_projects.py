"""Whole-project Dataform layouts from tests/fixtures/bq_syntax/dataform/projects."""

from pathlib import Path

import pytest

from kumosql import load_sqlx_project

PROJECTS = Path(__file__).parent / "fixtures" / "bq_syntax" / "dataform" / "projects"


@pytest.mark.parametrize("project", sorted(p.name for p in PROJECTS.iterdir()))
def test_every_project_layout_loads_without_raising(project):
    pipeline = load_sqlx_project(PROJECTS / project)
    assert not [d for d in pipeline.all_diagnostics() if d.code in {"settings_unreadable", "asset_unreadable", "read_error"}]


def test_workflow_settings_and_legacy_dataform_json_give_the_same_defaults():
    modern = load_sqlx_project(PROJECTS / "workflow_settings_full")
    legacy = load_sqlx_project(PROJECTS / "dataform_json_legacy")
    assert (modern.default_project, modern.default_dataset) == ("kumosql", "kumosql_messy")
    assert (legacy.default_project, legacy.default_dataset) == ("kumosql", "kumosql_messy")


def test_sql_files_with_a_config_block_are_actions_not_plain_sql():
    pipeline = load_sqlx_project(PROJECTS / "sql_files_mixed")
    assert pipeline.models["with_config"].kind == "view"
    assert "config" not in pipeline.models["with_config"].sql
    assert pipeline.models["plain"].kind == "sql"


def test_deeply_nested_folders_are_found():
    assert "nested" in load_sqlx_project(PROJECTS / "subfolders").models


def test_workflow_settings_suffixes_and_prefix_are_applied():
    pipeline = load_sqlx_project(PROJECTS / "workflow_settings_full")
    # @dataform/cli 3.0.71 joins with an underscore even when the setting starts or ends with one (`_dev` -> `kumosql__dev`)
    assert list(pipeline.models) == ["kumosql__dev.kumosql_messy__staging.dev__users"]
    assert pipeline.models["kumosql__dev.kumosql_messy__staging.dev__users"].logical == ("kumosql", "kumosql_messy", "users")


def test_javascript_api_actions_are_loaded():
    # publish() with a literal name, config and query is read; operate(), assert() and a publish in a loop are not yet
    pipeline = load_sqlx_project(PROJECTS / "js_api")
    assert set(pipeline.models) == {"kumosql.kumosql_messy.users_copy", "kumosql.kumosql_messy.users_view_js"}
    assert pipeline.models["kumosql.kumosql_messy.users_view_js"].kind == "view"
    assert [t.key for t in pipeline.models["kumosql.kumosql_messy.users_view_js"].declared_dependencies] == [
        "kumosql.kumosql_messy.users_copy"]


def test_actions_yaml_types_and_dependencies_are_read():
    pipeline = load_sqlx_project(PROJECTS / "actions_yaml")
    view = pipeline.models["kumosql.kumosql_messy.users_view"]
    table = pipeline.models["kumosql.kumosql_messy.users_table"]
    incremental = pipeline.models["kumosql.kumosql_messy.users_incr"]
    assertion = pipeline.models["kumosql.kumosql_messy.users_assert"]
    operation = pipeline.models["kumosql.kumosql_messy.users_op"]

    assert view.kind == "view"
    assert table.kind == "table"
    assert table.tags == ("daily",)
    assert any(dep.name == "raw_orders" for dep in table.declared_dependencies)
    assert "created_at" in table.config_reads
    assert incremental.kind == "incremental"
    assert incremental.incremental_sql == ("SELECT id FROM `kumosql.kumosql_messy.raw_users`",)
    assert assertion.kind == "assertion"
    assert operation.kind == "operations" and not operation.has_output
    assert "kumosql.kumosql_messy.raw_extra" in pipeline.sources


def test_unreadable_actions_yaml_keeps_plain_sql_unknown(tmp_path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "actions.yaml").write_text(
        "actions:\n  - incrementalTable:\n      filename: &model users.sql\n", encoding="utf-8"
    )
    (definitions / "users.sql").write_text("SELECT 1 AS id", encoding="utf-8")

    pipeline = load_sqlx_project(tmp_path)

    assert pipeline.models["users"].kind == "unknown"
    assert any(d.code == "actions_yaml_unreadable" for d in pipeline.diagnostics)
