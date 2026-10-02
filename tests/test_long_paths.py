"""Files whose path is longer than Windows' 260-character limit must load, trace and report."""

from pathlib import Path

from kumosql import resilience
from kumosql.live_graph import pipeline_from_files
from kumosql.pipeline import load_sqlx_project

DEEP = "/".join(["definitions"] + [f"folder_{i:02d}_" + "x" * 30 for i in range(8)])  # over 260 characters
assert len(DEEP) > 260


def test_extended_prefix_forms():
    assert resilience._extended_text("C:\\a\\b") == "\\\\?\\C:\\a\\b"
    assert resilience._extended_text("\\\\?\\C:\\a") == "\\\\?\\C:\\a"
    assert resilience._extended_text("\\\\srv\\share\\a") == "\\\\?\\UNC\\srv\\share\\a"


def test_extended_path_is_unchanged_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(resilience.os, "name", "posix")
    assert resilience.extended_path(tmp_path) == tmp_path


def _files():
    return {
        f"{DEEP}/orders.sqlx": 'config { type: "table", schema: "marts" }\nselect id, amount from ${ref("raw_orders")}\n',
        "definitions/raw_orders.sqlx": 'config { type: "table", schema: "raw" }\nselect 1 as id, 2 as amount\n',
        "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: scratch\n",
    }


def test_project_with_a_very_long_path_loads_and_traces():
    pipeline = pipeline_from_files(_files())
    model = next(m for m in pipeline.models.values() if m.path and m.path.endswith("orders.sqlx") and len(m.path) > 260)
    assert "raw_orders" in " ".join(pipeline.models)
    assert pipeline.lineage_report()


def test_local_folder_with_a_very_long_path_loads(tmp_path):
    for name, text in _files().items():
        target = Path(tmp_path, *name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    pipeline = load_sqlx_project(tmp_path)
    assert any(len(m.path or "") > 260 for m in pipeline.models.values())
    assert not [d for d in pipeline.diagnostics if d.code in resilience.ASSET_FAILURE_CODES]
