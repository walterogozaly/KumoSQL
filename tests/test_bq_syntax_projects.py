"""Whole-project Dataform layouts from tests/fixtures/bq_syntax/dataform/projects.

What loads is asserted; what does not yet is an xfail with the reason, so the gap shows in the
coverage docs and closing it flips the test.
"""

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
    # Static publish(), operate() and assert() bodies are read; a publish in a loop is not expanded into a model.
    pipeline = load_sqlx_project(PROJECTS / "js_api")
    assert set(pipeline.models) == {
        "kumosql.kumosql_messy.users_copy",
        "kumosql.kumosql_messy.users_view_js",
        "kumosql.kumosql_messy.js_op",
        "kumosql.kumosql_messy.js_assert",
    }
    assert pipeline.models["kumosql.kumosql_messy.users_view_js"].kind == "view"
    assert [t.key for t in pipeline.models["kumosql.kumosql_messy.users_view_js"].declared_dependencies] == [
        "kumosql.kumosql_messy.users_copy"]
    operation = pipeline.models["kumosql.kumosql_messy.js_op"]
    assert operation.kind == "operations" and "DELETE FROM" in operation.sql
    assert [t.key for t in operation.declared_dependencies] == ["kumosql.kumosql_messy.users_copy"]
    assertion = pipeline.models["kumosql.kumosql_messy.js_assert"]
    assert assertion.kind == "assertion" and "WHERE id IS NULL" in assertion.sql
    assert [t.key for t in assertion.declared_dependencies] == ["kumosql.kumosql_messy.users_copy"]


@pytest.mark.xfail(reason="actions.yaml (types, dependencyTargets, file mapping) is not read; its SQL files load as untyped models", strict=True)
def test_actions_yaml_types_and_dependencies_are_read():
    pipeline = load_sqlx_project(PROJECTS / "actions_yaml")
    assert pipeline.models["users_view"].kind == "view"
    assert any(dep.name == "raw_orders" for dep in pipeline.models["users_table"].declared_dependencies)
