import json

import pytest

from kumosql.change_report import build_change_report, change_report_main, load_snapshot, normalize_cost

SETTINGS = "defaultProject: proj\ndefaultDataset: analytics\n"
STAGE = 'config { type: "view" }\nSELECT id, amount FROM ${ref("raw", "orders")}\nWHERE %s\n'
TOTALS = 'config { type: "table" }\nSELECT id, SUM(amount) AS total FROM ${ref("stg")} GROUP BY id\n'
REPORT = 'config { type: "table" }\nSELECT id, total FROM ${ref("totals")}\n'


def project(root, stage_where="amount > 0", extra=None, drop=()):
    files = {
        "workflow_settings.yaml": SETTINGS,
        "definitions/raw_orders.sqlx": 'config { type: "declaration", schema: "raw", name: "orders" }\n',
        "definitions/stg.sqlx": STAGE % stage_where,
        "definitions/totals.sqlx": TOTALS,
        "definitions/rep.sqlx": REPORT,
    }
    files.update(extra or {})
    for name in drop:
        files.pop(name)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def report(base_dir, head_dir, **kw):
    base, br = load_snapshot(base_dir)
    head, hr = load_snapshot(head_dir)
    return build_change_report(base, head, base_root=br, head_root=hr, generated_at="t", **kw)


def by_model(r):
    return {c["model"]: c for c in r["changes"]}


def test_identical_projects_have_no_changes(tmp_path):
    r = report(project(tmp_path / "a"), project(tmp_path / "b"))
    assert r["changes"] == []
    assert {"title", "base", "head", "generated_at", "changes"} <= set(r)


def test_proven_change_lists_transitive_consumers_and_unknown_cost(tmp_path):
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 0 AND TRUE")
    c = by_model(report(a, b))["analytics.stg"]
    assert c["kind"] == "modified"
    assert c["verification"]["label"] == "proven"
    assert any(k["kind"] == "equivalence_proof" for k in c["verification"]["checks"])
    assert c["consumers"] == {"models": ["analytics.rep", "analytics.totals"], "complete": True}
    assert c["cost"] == {"basis": "unavailable"}


def test_unproven_change(tmp_path):
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 5")
    assert by_model(report(a, b))["analytics.stg"]["verification"]["label"] == "unproven"


def test_added_and_removed_models(tmp_path):
    a = project(tmp_path / "a")
    b = project(
        tmp_path / "b",
        extra={"definitions/new.sqlx": 'config { type: "table" }\nSELECT 1 AS x\n'},
        drop=("definitions/rep.sqlx",),
    )
    m = by_model(report(a, b))
    assert m["analytics.new"]["kind"] == "added"
    assert m["analytics.rep"]["kind"] == "removed"
    assert m["analytics.rep"]["verification"]["label"] == "unproven"


def test_removed_model_consumers_come_from_base(tmp_path):
    a = project(tmp_path / "a")
    b = project(tmp_path / "b", drop=("definitions/totals.sqlx",))
    c = by_model(report(a, b))["analytics.totals"]
    assert c["kind"] == "removed"
    assert c["consumers"]["models"] == ["analytics.rep"]


def test_incomplete_graph_marks_consumers_incomplete(tmp_path):
    broken = {"definitions/bad.sqlx": 'config { type: "table" }\nSELECT FROM WHERE (\n'}
    a = project(tmp_path / "a", extra=broken)
    b = project(tmp_path / "b", "amount > 5", extra=broken)
    r = report(a, b)
    assert by_model(r)["analytics.stg"]["consumers"]["complete"] is False
    assert r["diagnostics"]


def test_cost_is_passed_through_and_validated(tmp_path):
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 5")
    r = report(a, b, costs={"analytics.stg": {"basis": "estimate", "before": 10, "after": 4}})
    assert by_model(r)["analytics.stg"]["cost"] == {"basis": "estimate", "before": 10, "after": 4}
    with pytest.raises(ValueError):
        normalize_cost({"basis": "guess", "before": 1})
    assert normalize_cost({"basis": "measured"}) == {"basis": "unavailable"}


def test_per_model_failure_becomes_diagnostic(tmp_path, monkeypatch):
    import kumosql.change_report as cr

    def boom(*a, **k):
        raise RuntimeError("prover crashed")

    monkeypatch.setattr(cr, "verify_rewrite", boom)
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 5")
    r = report(a, b)
    c = by_model(r)["analytics.stg"]
    assert c["verification"]["label"] == "failed"
    assert c["consumers"]["complete"] is False
    assert any("prover crashed" in d["message"] for d in r["diagnostics"])


def test_cli_writes_report(tmp_path):
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 5")
    cost = tmp_path / "cost.json"
    cost.write_text(json.dumps({"analytics.stg": {"basis": "estimate", "before": 2, "after": 1}}))
    out = tmp_path / "out.json"
    assert change_report_main([str(a), str(b), "--cost", str(cost), "-o", str(out)]) == 0
    data = json.loads(out.read_text())["report"]
    assert data["changes"][0]["cost"]["basis"] == "estimate"
    with pytest.raises(SystemExit):
        change_report_main([str(a), str(b), "--cost", str(tmp_path / "missing.json")])


def test_changes_are_flagged_when_the_active_catalogs_do_not_own_them(tmp_path):
    a = project(tmp_path / "a", "amount > 0")
    b = project(tmp_path / "b", "amount > 5")
    (b / "definitions/totals.sqlx").write_text(TOTALS + "-- edited\n", encoding="utf-8")
    base, br = load_snapshot(a)
    head, hr = load_snapshot(b)
    owned = {"proj.analytics.stg"}
    r = build_change_report(base, head, base_root=br, head_root=hr, generated_at="t", owned=(owned, owned))
    flags = {c["model"]: c["owned"] for c in r["changes"]}
    assert flags["analytics.stg"] is True and flags["analytics.totals"] is False
    assert all("owned" not in c for c in report(a, b)["changes"])


STAR_FILES = {
    "workflow_settings.yaml": SETTINGS,
    "definitions/src.sqlx": 'config { type: "declaration", schema: "raw", name: "src" }\n',
    "definitions/model.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("raw", "src")}\n',
    "definitions/reader.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("model")}\n',
}
SRC_BASE = {"proj.raw.src": {"x": "INT64"}}
SRC_HEAD = {"proj.raw.src": {"x": "INT64", "y": "STRING"}}


def star_project(root, files=None):
    for rel, text in {**STAR_FILES, **(files or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def star_report(tmp_path, base_schema, head_schema, head_files=None):
    base, br = load_snapshot(star_project(tmp_path / "a"), base_schema)
    head, hr = load_snapshot(star_project(tmp_path / "b", head_files), head_schema)
    return build_change_report(base, head, base_root=br, head_root=hr, generated_at="t", overlaps=False)


def test_source_schema_change_under_select_star_is_reported_unproven(tmp_path):
    # Model text is byte-identical in both snapshots; only the supplied source schema moved.
    from kumosql.ci_check import conclude

    r = star_report(tmp_path, SRC_BASE, SRC_HEAD)
    m = by_model(r)
    assert set(m) == {"analytics.model", "analytics.reader"}
    for c in m.values():
        assert c["kind"] == "modified"
        assert c["verification"]["label"] == "unproven"
        assert "text is unchanged" in c["verification"]["reason"]
        assert c["contract"] == {"columns_added": ["y"], "columns_removed": []}
    assert m["analytics.model"]["consumers"]["models"] == ["analytics.reader"]
    assert conclude(r) == "neutral"


def test_dropped_source_column_is_reported(tmp_path):
    c = by_model(star_report(tmp_path, SRC_HEAD, SRC_BASE))["analytics.reader"]
    assert c["contract"]["columns_removed"] == ["y"]
    assert c["verification"]["label"] == "unproven"


def test_same_source_schema_reports_nothing(tmp_path):
    assert star_report(tmp_path, SRC_HEAD, SRC_HEAD)["changes"] == []
    assert star_report(tmp_path / "n", None, None)["changes"] == []


def test_proven_text_change_is_not_proven_when_the_resolved_output_moved(tmp_path):
    edited = {"definitions/model.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("raw", "src")} WHERE TRUE\n'}
    moved = by_model(star_report(tmp_path / "m", SRC_BASE, SRC_HEAD, edited))["analytics.model"]
    assert moved["verification"]["label"] == "unproven"
    assert "resolved output changed" in moved["verification"]["reason"]
    # With the schema held still, the same text edit is still proven.
    still = by_model(star_report(tmp_path / "s", SRC_BASE, SRC_BASE, edited))["analytics.model"]
    assert still["verification"]["label"] == "proven"
    assert "contract" not in still


def test_cli_takes_a_source_schema_per_snapshot(tmp_path):
    a, b = star_project(tmp_path / "a"), star_project(tmp_path / "b")
    sa, sb, out = tmp_path / "sa.json", tmp_path / "sb.json", tmp_path / "out.json"
    sa.write_text(json.dumps(SRC_BASE))
    sb.write_text(json.dumps(SRC_HEAD))
    args = [str(a), str(b), "--no-overlaps", "-o", str(out)]
    assert change_report_main(args) == 0
    assert json.loads(out.read_text())["report"]["changes"] == []
    assert change_report_main(args + ["--base-source-schema", str(sa), "--head-source-schema", str(sb)]) == 0
    changes = json.loads(out.read_text())["report"]["changes"]
    assert {c["model"] for c in changes} == {"analytics.model", "analytics.reader"}
    sb.write_text("[1]")
    with pytest.raises(SystemExit):
        change_report_main(args + ["--head-source-schema", str(sb)])
