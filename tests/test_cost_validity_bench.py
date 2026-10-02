import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cost_validity_bench as cvb  # noqa: E402


def test_rule_recommendation_compares_against_the_round_tripped_original():
    rec = cvb.rule_recommendation("WITH unused AS (SELECT 1 AS x) SELECT a FROM t WHERE 1 = 1")
    assert rec["steps"] and "remove_unused_ctes" in rec["steps"]
    assert "unused" in rec["baseline"].lower() and "unused" not in rec["sql"].lower()
    assert rec["label"] == "proven"


def test_rule_recommendation_is_empty_when_nothing_changes():
    assert cvb.rule_recommendation("SELECT a FROM t") == {}


def test_load_workload_names_each_statement(tmp_path):
    (tmp_path / "q1.sql").write_text("SELECT 1;")
    (tmp_path / "q2.sql").write_text("SELECT 1; SELECT 2;")
    names = [q["name"] for q in cvb.load_workload(f"db={tmp_path}")]
    assert names == ["db/q1", "db/q2a", "db/q2b"]


def test_summary_keeps_estimates_and_observations_apart():
    def rec(outcome, estimate, speedup):
        return {"sql": "x", "label": "proven", "outcome": outcome, "estimate": estimate, "speedup": speedup}

    records = [
        {"rules": rec("same_rows", [100.0, 50.0], 2.0)},  # predicted and realized
        {"rules": rec("same_rows", [100.0, 50.0], 1.0)},  # predicted, not realized
        {"rules": rec("same_rows", [100.0, 100.0], 1.5)},  # faster without a predicted saving
        {"rules": rec("different_rows", [100.0, 10.0], 9.0)},  # wrong rows never count as a saving
        {"rules": {}},
    ]
    s = cvb.summarize(records, "rules")
    assert s["recommendations"] == 4 and s["same_rows"] == 3 and s["different_rows"] == 1
    assert s["estimated_cheaper"] == 2 and s["estimated_unchanged"] == 1
    assert s["observed_faster"] == 2 and s["estimated_cheaper_and_observed_faster"] == 1


def test_rows_tied_on_the_sort_keys_may_come_back_in_any_order():
    sql = "SELECT k, v FROM t ORDER BY k LIMIT 10"
    assert cvb.order_key_positions(sql) == [0]
    assert cvb.same_rows([(1, "a"), (1, "b"), (2, "c")], [(1, "b"), (1, "a"), (2, "c")], sql)
    assert not cvb.same_rows([(1, "a"), (2, "c")], [(2, "c"), (1, "a")], sql)
    assert not cvb.same_rows([(1, "a")], [(1, "b")], sql)
