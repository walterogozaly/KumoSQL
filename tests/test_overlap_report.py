"""Overlaps in change reports: the section, its edge cases and its isolation (issue #83)."""

import json

import pytest

from kumosql import Pipeline, Scope, Target
from kumosql import overlap_report
from kumosql.change_report import build_change_report
from kumosql.ci_check import build_check, conclude, render_comment
from kumosql.overlap_report import OverlapChecker
from kumosql.pipeline import Model

ORDERS = Target("proj", "raw", "orders")
SCHEMA = {ORDERS.key: {"customer_id": "INT64", "amount": "FLOAT64", "status": "STRING", "region_id": "INT64"}}
BY_REGION = "SELECT region_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY region_id"
PAID_BY_REGION = "SELECT region_id, SUM(amount) AS total FROM proj.raw.orders WHERE status = 'paid' GROUP BY region_id"
RENAMED = "select REGION_ID as state, sum(Amount) as revenue from `proj.raw.orders` group by 1"
BY_CUSTOMER = "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id"


def build(models, datasets=None):
    datasets = datasets or {}
    built = {}
    for name, sql in models.items():
        target = Target("proj", datasets.get(name, "core"), name)
        built[target.key] = Model(target, "table", sql)
    return Pipeline(built, sources={ORDERS.key: ORDERS}, source_schema=dict(SCHEMA))


def report(base, head, **kw):
    return build_change_report(base, head, generated_at="t", **kw)


def change(r, name):
    return next(c for c in r["changes"] if c["model"] == name)


def test_new_table_lists_the_existing_table_with_checks_role_and_coverage():
    base = build({"state_totals": BY_REGION, "other": BY_CUSTOMER})
    head = build({"state_totals": BY_REGION, "other": BY_CUSTOMER, "revenue_by_state": RENAMED})
    section = change(report(base, head), "core.revenue_by_state")["overlaps"]
    assert section["status"] == "ok"
    (match,) = section["matches"]
    assert (match["table"], match["kind"], match["confidence"], match["rank"]) == (
        "core.state_totals", "same_meaning", "high", 1)
    assert {c["kind"]: c["outcome"] for c in match["checks"]} == {
        "lineage": "matched", "grain": "matched", "row_scope": "matched"}
    assert {"role", "confidence", "evidence"} <= set(match["role"])
    assert "compared 2 of 2 tables; 0 skipped" in section["summary"]
    assert section["compared"] == 2 and section["skipped"] == {}
    assert not match["in_this_change"] and not match["retiring"]


def test_no_match_still_states_coverage():
    head = build({"a": BY_CUSTOMER, "b": BY_REGION})
    section = change(report(build({"a": BY_CUSTOMER}), head), "core.b")["overlaps"]
    assert section["matches"] == []
    assert section["summary"].startswith("compared 1 of 1 tables; 0 skipped")
    assert "No match among the compared tables" in section["summary"]


def test_several_overlaps_are_ranked_by_match_kind():
    base = build({"paid_totals": PAID_BY_REGION, "state_totals": BY_REGION, "wide": BY_REGION})
    head = build({"paid_totals": PAID_BY_REGION, "state_totals": BY_REGION, "wide": BY_REGION, "new_totals": RENAMED})
    matches = change(report(base, head), "core.new_totals")["overlaps"]["matches"]
    assert [m["rank"] for m in matches] == [1, 2, 3]
    order = {"same_meaning": 0, "contains": 1, "partial": 2}
    kinds = [order[m["kind"]] for m in matches]
    assert kinds == sorted(kinds) and kinds[0] == 0 and kinds[-1] > 0


def test_scope_excluded_table_is_skipped_not_no_match():
    base = build({"state_totals": BY_REGION}, datasets={"state_totals": "mirror"})
    head = build({"state_totals": BY_REGION, "new": RENAMED}, datasets={"state_totals": "mirror"})
    section = change(report(base, head, scope=Scope("core", {"dataset": ("core",)})), "core.new")["overlaps"]
    assert section["matches"] == []
    assert section["skipped"] == {"outside_scope": 1} and section["compared"] == 0
    assert "compared 0 of 1 tables; 1 skipped: outside_scope 1" in section["summary"]


def test_undecided_comparison_is_listed_as_unknown():
    base = build({"broken": "SELECT * FROM proj.raw.missing_table"})
    head = build({"broken": "SELECT * FROM proj.raw.missing_table", "new": BY_REGION})
    section = change(report(base, head), "core.new")["overlaps"]
    assert section["matches"] == []
    assert [u["table"] for u in section["unknown"]] == ["core.broken"]
    assert section["unknown"][0]["reason"]
    assert section["skipped"]


def test_match_inside_the_same_change_is_flagged():
    head = build({"first": BY_REGION, "second": RENAMED})
    r = report(build({}), head)
    (match,) = change(r, "core.first")["overlaps"]["matches"]
    assert match["table"] == "core.second" and match["in_this_change"] is True


def test_retiring_table_is_marked_as_retiring():
    base = build({"old_totals": BY_REGION})
    head = build({"new_totals": RENAMED})
    section = change(report(base, head), "core.new_totals")["overlaps"]
    (match,) = section["matches"]
    assert match["table"] == "core.old_totals" and match["retiring"] is True
    assert "retired in this change" in section["summary"]


def test_a_failing_comparison_leaves_the_rest_of_the_report_alone(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SELECT secret FROM private_table")

    base = build({"a": BY_CUSTOMER})
    head = build({"a": BY_CUSTOMER, "b": BY_REGION})
    good = report(base, head, overlaps=False)
    monkeypatch.setattr(overlap_report, "find_overlaps", boom)
    r = report(base, head)
    entry = change(r, "core.b")
    assert entry["overlaps"]["status"] == "unavailable"
    assert entry["overlaps"]["reason"] == "compare_error: RuntimeError"
    assert "secret" not in json.dumps(r) and r["diagnostics"] == good["diagnostics"] == []
    assert {k: v for k, v in entry.items() if k != "overlaps"} == change(good, "core.b")


def test_overlaps_can_be_turned_off_and_modified_models_are_compared():
    base = build({"a": BY_REGION, "b": BY_CUSTOMER})
    head = build({"a": BY_REGION, "b": RENAMED})
    assert "overlaps" not in change(report(base, head, overlaps=False), "core.b")
    assert change(report(base, head), "core.b")["overlaps"]["matches"][0]["table"] == "core.a"


def test_checker_never_raises_on_a_model_it_does_not_know():
    section = OverlapChecker(build({"a": BY_REGION})).section("proj.core.nope")
    assert section["status"] == "unavailable" and section["matches"] == []


def test_ci_comment_shows_the_section_as_advisory_and_never_changes_the_conclusion():
    base = build({"state_totals": BY_REGION})
    head = build({"state_totals": BY_REGION, "new": RENAMED})
    r = report(base, head)
    r_plain = report(base, head, overlaps=False)
    assert conclude(r) == conclude(r_plain) and build_check(r) == build_check(r_plain)
    text = render_comment(r)
    assert "Already done elsewhere" in text and "advisory" in text
    assert "core.state_totals" in text and "Same meaning" in text
    assert "compared 1 of 1 tables" in text
    assert "Already done elsewhere" not in render_comment(r_plain)


@pytest.mark.parametrize("bad", [None, "x", 3, {"matches": "x"}, {"status": "ok", "matches": [None, 1, {"table": None}]}])
def test_ci_comment_survives_malformed_overlaps(bad):
    r = {"changes": [{"model": "m", "kind": "added", "verification": {"label": "proven", "reason": ""},
                      "cost": {"basis": "unavailable"}, "consumers": {"models": [], "complete": True},
                      "overlaps": bad}], "diagnostics": []}
    assert conclude(r) == "success"
    assert render_comment(r)


def test_ci_comment_lists_an_unavailable_comparison():
    r = {"changes": [{"model": "m", "kind": "added", "verification": {"label": "unproven", "reason": ""},
                      "cost": {}, "consumers": {}, "overlaps": {
                          "status": "unavailable", "reason": "compare_error: X",
                          "summary": "The comparison could not be completed (compare_error: X); no tables were compared"}}],
         "diagnostics": []}
    assert "could not be completed" in render_comment(r)


def test_cli_rejects_an_unknown_scope_and_can_skip_overlaps(tmp_path, capsys):
    from kumosql.change_report import change_report_main
    from test_change_report import project

    a, b = project(tmp_path / "a"), project(tmp_path / "b", "amount > 5")
    with pytest.raises(SystemExit):
        change_report_main([str(a), str(b), "--scope", "no such scope"])
    assert change_report_main([str(a), str(b), "--no-overlaps"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert all("overlaps" not in c for c in data["report"]["changes"])
    assert change_report_main([str(a), str(b)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert all("overlaps" in c for c in data["report"]["changes"])


def test_a_retired_table_is_found_without_profiling_the_new_sql_again(monkeypatch):
    from kumosql import overlap

    def boom(*a, **k):
        raise AssertionError("the changed model's profile is reused")

    monkeypatch.setattr(overlap, "profile_query", boom)
    section = change(report(build({"old_totals": BY_REGION}), build({"new_totals": RENAMED})), "core.new_totals")["overlaps"]
    assert section["matches"][0]["retiring"] is True


def test_the_list_of_tables_that_could_not_be_compared_is_capped(monkeypatch):
    monkeypatch.setattr(overlap_report, "MAX_UNKNOWN_LISTED", 2)
    broken = {f"broken_{i}": "SELECT * FROM proj.raw.missing_table" for i in range(5)}
    section = change(report(build(broken), build({**broken, "new": BY_REGION})), "core.new")["overlaps"]
    assert len(section["unknown"]) == 2 and section["unknown_total"] == 5
