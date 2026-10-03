"""Crafted projects and writable caches are data, never executable or outside-root I/O."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import gzip
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import pickle
import subprocess

import pytest

from kumosql import live_graph, resilience, storage
from kumosql.joinorder.stats import Statistics, collect_statistics
from kumosql.pipeline import load_sqlx_project


@pytest.mark.parametrize("name", [
    "Z:outside.sql", "Z:/outside.sql", "model.sql:stream.sql", "definitions/Z:outside.sql",
    "/outside.sql", "//server/share/outside.sql", "\\outside.sql", "Z:\\outside.sql",
    "\\\\?\\C:\\outside.sql", "\\\\server\\share\\outside.sql", "../outside.sql", "a/../outside.sql",
])
def test_windows_and_posix_path_witnesses_are_refused(name, tmp_path):
    # Z:outside.sql is relative in POSIX and drive-relative in Windows; colons include ADS syntax.
    posix, windows = PurePosixPath(name), PureWindowsPath(name)
    assert ":" in name or "\\" in name or posix.root or windows.root or ".." in posix.parts
    with pytest.raises(live_graph.ProjectError):
        live_graph._write_files({name: "select 1"}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_accepted_paths_are_relative_in_both_interpretations(tmp_path):
    name = "definitions/team/model.sql"
    path = live_graph._safe_path(name)
    assert not path.is_absolute()
    assert not PureWindowsPath(name).drive and not PureWindowsPath(name).root
    live_graph._write_files({name: "select 1"}, str(tmp_path))
    assert (tmp_path / name).read_text() == "select 1"


def _link(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if directory and os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            if result.returncode == 0:
                return
        pytest.skip(f"host cannot create symlinks: {type(exc).__name__}")


def test_write_refuses_resolved_outside_target(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _link(root / "definitions", outside, directory=True)
    with pytest.raises(live_graph.ProjectError):
        live_graph._write_files({"definitions/outside.sql": "select 1"}, str(root))
    assert list(outside.iterdir()) == []


def test_read_helper_never_opens_file_links(monkeypatch, tmp_path):
    # Runs even on hosts where symlink creation requires administrator privileges.
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("followed a symlink"))
    assert resilience.read_text_or_reason(tmp_path / "model.sql") == (None, "symbolic links are not read")


@pytest.mark.parametrize("asset", ["model.sql", "declaration.js", "workflow_settings.yaml", "dataform.json"])
def test_local_project_does_not_read_external_file_links(tmp_path, asset):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ordinary.sql").write_text("SELECT 1 AS safe")
    secret = tmp_path / "secret"
    secret.write_text('{"defaultDatabase":"private"}' if asset.endswith(".json") else
                      'defaultProject: private\n' if asset.endswith(".yaml") else "SELECT 99 AS private")
    _link(root / asset, secret)
    pipeline = load_sqlx_project(root)
    assert set(pipeline.models) == {"ordinary"}
    assert pipeline.default_project == ""
    assert any(d.code in ("read_error", "unreadable_directory", "settings_unreadable") for d in pipeline.diagnostics)
    assert "private" not in json.dumps(pipeline.report())


@pytest.mark.parametrize("asset", ["model.sql", "declaration.js", "workflow_settings.yaml", "dataform.json"])
def test_local_asset_and_settings_link_guards_without_host_link_privilege(tmp_path, monkeypatch, asset):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ordinary.sql").write_text("SELECT 1 AS safe")
    (root / asset).write_text("PRIVATE_LINK_TARGET")
    original_symlink, original_text, original_bytes = Path.is_symlink, Path.read_text, Path.read_bytes
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == asset or original_symlink(path))

    def guarded_text(path, *args, **kwargs):
        assert path.name != asset, "linked settings file was read"
        return original_text(path, *args, **kwargs)

    def guarded_bytes(path, *args, **kwargs):
        assert path.name != asset, "linked asset was read"
        return original_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_bytes)
    pipeline = load_sqlx_project(root)
    assert set(pipeline.models) == {"ordinary"}
    assert any(d.code in ("read_error", "unreadable_directory", "settings_unreadable") for d in pipeline.diagnostics)


def test_linked_definitions_directory_is_not_read(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.sql").write_text("SELECT 1 AS private")
    _link(root / "definitions", outside, directory=True)
    pipeline = load_sqlx_project(root)
    assert not pipeline.models
    assert not pipeline.completeness()["complete"]


class _Payload:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return eval, (f"__import__('pathlib').Path({str(self.marker)!r}).write_text('executed')",)


def test_snapshot_pickle_payload_is_never_executed(tmp_path, monkeypatch):
    marker, path = tmp_path / "executed", tmp_path / "cache.json"
    path.write_bytes(pickle.dumps(_Payload(marker)))
    monkeypatch.setattr(live_graph, "_snapshot_file", lambda key: path)
    assert live_graph._load_snapshot("key") is None
    assert not marker.exists()


def test_snapshot_round_trip_preserves_reports_and_source_without_sqlx_parsing(monkeypatch):
    files = {"dataform.json": '{"defaultDatabase":"p","defaultSchema":"d"}',
             "definitions/a.sqlx": 'config { type: "table", tags: ["daily"], assertions: { nonNull: ["id"] } }\nSELECT 1 AS id',
             "definitions/b.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("a")}',
             "definitions/broken.sqlx": 'config { type: "table" }\nSELECT FROM WHERE ('}
    first = live_graph.pipeline_from_files(files)
    key = live_graph._content_key(files)
    expected = first.report(include_duplicates=False)
    lineage = first.lineage_report()
    live_graph._PROJECT_CACHE.clear()
    monkeypatch.setattr(live_graph, "load_sqlx_project", lambda *a, **k: pytest.fail("SQLX reparsed"))
    restored = live_graph._load_snapshot(key)
    assert restored is not None and restored.source_files == files
    assert restored.report(include_duplicates=False) == expected
    assert restored.lineage_report() == lineage
    assert restored.models["p.d.a"].tags == ("daily",)
    assert live_graph._snapshot_file(key).suffix == ".json"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=999), lambda d: d.update(version=True),
    lambda d: d.update(content_key="other"), lambda d: d.update(unexpected={}),
    lambda d: d.update(source_files={"Z:outside.sql": "SELECT 1"}),
    lambda d: d["models"]["a"].update(path="Z:outside.sql"),
    lambda d: d.update(models={"a": {"target": "invalid"}}),
])
def test_invalid_snapshot_schema_is_a_cache_miss(mutate):
    first = live_graph.pipeline_from_files({"a.sql": "SELECT 1 AS a"})
    key = first.content_key
    path = live_graph._snapshot_file(key)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    assert live_graph._load_snapshot(key) is None


def test_statistics_pickle_payload_is_never_executed(tmp_path):
    marker, path = tmp_path / "executed", tmp_path / "stats.pkl.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(pickle.dumps(_Payload(marker)))
    with pytest.raises(ValueError, match="regenerate statistics"):
        Statistics.load(str(path))
    assert not marker.exists()


def test_statistics_round_trip_and_schema_validation(tmp_path):
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE a AS SELECT i AS id, i % 2 AS k FROM range(6) t(i)")
    con.execute("CREATE TABLE b AS SELECT i AS id FROM range(2) t(i)")
    original = collect_statistics(con, ["a", "b"], [(('a', 'k'), ('b', 'id'))], sample_rows=2, heavy_bins=1)
    con.close()
    path = tmp_path / "stats.json.gz"
    original.save(str(path))
    assert Statistics.load(str(path)) == original
    with gzip.open(path, "rt") as handle:
        valid = json.load(handle)
    for edit in [lambda d: d.update(version=True), lambda d: d.update(extra={}),
                 lambda d: d["tables"]["a"]["keys"]["k"].update(sample_bins=[999]),
                 lambda d: d["tables"]["a"].update(weight=-1), lambda d: d.update(column_domain=[]),
                 lambda d: d["tables"]["a"]["sample"][0].update(id={"type": "eval", "value": "1+1"}),
                 lambda d: d["tables"]["a"]["sample"][0].update(id={"type": "decimal", "value": "invalid"})]:
        bad = deepcopy(valid)
        edit(bad)
        with gzip.open(path, "wt") as handle:
            json.dump(bad, handle)
        with pytest.raises(ValueError, match="regenerate statistics"):
            Statistics.load(str(path))


def test_statistics_database_scalar_types_round_trip(tmp_path):
    from kumosql.joinorder.stats import TableStats

    values = [None, True, 2, 1.5, "hello", b"\x00\xff", Decimal("1.250"), date(2026, 10, 3),
              datetime(2026, 10, 3, 12, 30), time(12, 30), timedelta(days=2, seconds=1)]
    original = Statistics({"a": TableStats("a", len(values), ["v"], [{"v": v} for v in values])}, {}, {})
    path = tmp_path / "stats.json.gz"
    original.save(str(path))
    assert Statistics.load(str(path)) == original
