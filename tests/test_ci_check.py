import json

from kumosql import ci_check, preview_data


def change(label, complete=True, model="m.a"):
    return {"model": model, "kind": "modified",
            "verification": {"label": label, "reason": "r", "checks": []},
            "cost": {"basis": "estimate", "before": 2.0, "after": 1.0},
            "consumers": {"models": [], "complete": complete}}


def rep(*changes, diagnostics=()):
    return {"changes": list(changes), "diagnostics": list(diagnostics)}


def test_success_only_with_full_evidence():
    assert ci_check.conclude(rep(change("proven"), change("unchanged"))) == "success"
    assert ci_check.conclude(rep()) == "success"


def test_unproven_or_unknown_never_success():
    for label in ("unproven", "planner_checked", "bogus"):
        assert ci_check.conclude(rep(change(label))) == "neutral"
    assert ci_check.conclude(rep(change("proven", complete=False))) == "neutral"
    assert ci_check.conclude(rep(change("proven"), diagnostics=[{"asset": "x", "message": "m"}])) == "neutral"
    for bad in (None, {}, {"changes": "x"}, []):
        assert ci_check.conclude(bad) == "neutral"
    assert "not a pass" in ci_check.render_comment(None)


def test_failure_rules():
    assert ci_check.conclude(rep(change("failed"), change("proven"))) == "failure"
    assert ci_check.conclude(rep(change("unproven")), fail_on_unproven=True) == "failure"


def test_preview_report_shape_and_comment():
    report = preview_data.changes()["report"]
    check = ci_check.build_check(report)
    assert set(check) == {"check_name", "conclusion", "summary"}
    assert check["conclusion"] == "neutral"
    text = ci_check.render_comment(report)
    assert text.startswith(ci_check.MARKER)
    assert "Could not be analyzed" in text and "Unproven" in text
    assert "1 model unchanged" in text


def test_comment_escapes_and_caps():
    c = change("proven")
    c["model"] = "a|b\nc"
    assert "a\\|b c" in ci_check.render_comment(rep(c))
    big = rep(*[change("unproven", model=f"m{i}" + "x" * 200) for i in range(600)])
    assert len(ci_check.render_comment(big)) <= 65536


def test_cli(tmp_path, capsys):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"report": rep(change("failed"))}))
    out = tmp_path / "c.md"
    assert ci_check.main([str(p), "--comment-out", str(out), "--exit-code"]) == 1
    assert json.loads(capsys.readouterr().out)["conclusion"] == "failure"
    assert out.read_text().startswith(ci_check.MARKER)
    p.write_text("not json")
    assert ci_check.main([str(p), "--exit-code"]) == 0
    assert json.loads(capsys.readouterr().out)["conclusion"] == "neutral"
