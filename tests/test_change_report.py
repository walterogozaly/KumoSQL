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
