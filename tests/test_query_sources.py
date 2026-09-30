import pytest

from kumosql.pipeline import Model, Pipeline, Target
from kumosql.query_sources import (
    GitHubRepoSource,
    ObservedReadsSource,
    SourceRegistry,
)

INPUT = Target("DemoProject", "Stage", "Input")
OUTPUT = Target("DemoProject", "Stage", "Output")


def pipeline():
    return Pipeline({
        INPUT.key: Model(INPUT, "table", "SELECT 1 AS id", path="definitions/input.sqlx"),
        OUTPUT.key: Model(OUTPUT, "view", f"SELECT id FROM `{INPUT.key}`", path="definitions/output.sqlx"),
    })


def read(dest, *refs):
    return {"job_id": "j", "creation_time": "2025-01-01T00:00:00Z", "destination": dest, "referenced_tables": list(refs)}


def test_observed_reads_enabled_when_all_assets_match():
    registry = SourceRegistry()
    registry.register(ObservedReadsSource([read(OUTPUT.key, INPUT.key), read(OUTPUT.key, INPUT.key)]))
    (row,) = registry.to_json(pipeline())
    assert row["state"] == "connected" and row["matched"] == 1.0
    assert row["assets_total"] == 2 and row["assets_unmatched"] == 0


def test_unmatched_asset_keeps_source_not_enabled_with_counts():
    registry = SourceRegistry()
    registry.register(ObservedReadsSource([read(OUTPUT.key, INPUT.key, "DemoProject.Stage.Missing")]))
    (row,) = registry.to_json(pipeline())
    assert row["state"] == "not_enabled"
    assert (row["assets_total"], row["assets_matched"], row["assets_unmatched"]) == (3, 2, 1)
    assert row["matched"] == 2 / 3
    assert row["unmatched_samples"] == ["DemoProject.Stage.Missing"]


def test_repo_files_match_by_asset_path():
    registry = SourceRegistry()
    registry.register(GitHubRepoSource(["definitions/input.sqlx", "definitions/output.sqlx"], name="repo-a"))
    registry.register(GitHubRepoSource(["definitions/input.sqlx", "definitions/other.sqlx"], name="repo-b"))
    a, b = registry.to_json(pipeline())
    assert a["state"] == "connected"
    assert b["state"] == "not_enabled"
    assert b["assets_unmatched"] == 1


def test_repo_source_from_connection_result():
    source = GitHubRepoSource.from_connection({"repository": "org/repo", "files": ["definitions/input.sqlx"]})
    assert source.name == "org/repo"


def test_empty_source_is_not_enabled_and_has_no_fraction():
    registry = SourceRegistry()
    registry.register(ObservedReadsSource([]))
    (row,) = registry.to_json(pipeline())
    assert row["state"] == "not_enabled" and row["matched"] is None


def test_failing_source_is_isolated_as_error():
    class Broken:
        name, kind = "broken", "observed reads"

        def assets(self):
            raise RuntimeError("boom")

    registry = SourceRegistry()
    registry.register(Broken())
    registry.register(ObservedReadsSource([read(OUTPUT.key, INPUT.key)]))
    broken, ok = registry.to_json(pipeline())
    assert broken["state"] == "error" and ok["state"] == "connected"


def test_duplicate_name_rejected():
    registry = SourceRegistry()
    registry.register(ObservedReadsSource([]))
    with pytest.raises(ValueError):
        registry.register(ObservedReadsSource([]))
