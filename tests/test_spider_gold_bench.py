"""Spider's gold queries as rewrite inputs (tools/spider_gold_bench.py): zero wrong rewrites and floors on a pinned sample.

The data is downloaded on first use from a pinned commit; the tests that need it skip when it cannot be fetched.
``FLOORS`` only ever goes up. The full run goes through ``python tools/spider_gold_bench.py --set both``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("spider_gold_bench", _tools / "spider_gold_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["spider_gold_bench"] = bench
_spec.loader.exec_module(bench)
import spider_data  # noqa: E402

# a development query whose proof-gated optimizer rewrite is applied, trusted and agrees on valid databases
KNOWN = "SELECT T1.CountryName , T1.CountryId FROM COUNTRIES AS T1 JOIN CAR_MAKERS AS T2 ON T1.CountryId = T2.Country GROUP BY T1.CountryId HAVING count(*) >= 1"
SAMPLE = 12
FLOORS = {"translated": 12}


def _dev():
    try:
        return bench.load_gold("dev")
    except OSError as error:
        pytest.skip(f"Spider data not available: {error}")


def _toy():
    return spider_data.parse_schemas(
        [
            {
                "db_id": "toy",
                "table_names_original": ["parent", "child"],
                "column_names_original": [[-1, "*"], [0, "id"], [0, "name"], [1, "id"], [1, "pid"], [1, "note"]],
                "column_types": ["text", "number", "text", "number", "number", "text"],
                "primary_keys": [1, 3],
                "foreign_keys": [[4, 1]],
            }
        ]
    )["toy"]


def test_the_pinned_files_have_the_published_counts():
    dev = _dev()
    train = bench.load_gold("train")
    assert (len(dev), sum(q.questions for q in dev)) == (564, 1034)
    assert (len(train), sum(q.questions for q in train)) == (3979, 7000)
    assert len({q.database for q in dev}) == 20 and len({q.database for q in train}) == 140
    assert not {q.database for q in dev} & {q.database for q in train}
    assert sum(bench.database_held_out(d) for d in {q.database for q in dev + train}) == 30
    assert bench.check_sources() == []


def test_a_query_is_translated_to_bigquery_with_lower_case_names_and_back():
    bigquery = bench.to_bigquery("SELECT T1.Name FROM Singer AS T1 WHERE T1.Age > 3 ORDER BY T1.Name LIMIT 2")
    assert "Name" not in bigquery and "singer" in bigquery and "name" in bigquery
    assert bench.to_sqlite(bigquery) == "SELECT t1.name FROM singer AS t1 WHERE t1.age > 3 ORDER BY t1.name LIMIT 2"


def test_the_optimizer_is_given_no_key_and_no_not_null():
    catalog = bench.catalog(_toy())
    assert catalog.keys == {} and catalog.not_null == {}
    assert catalog.columns == {"parent": ["id", "name"], "child": ["id", "pid", "note"]}


def _fake(monkeypatch, output, trusted):
    monkeypatch.setattr(bench, "transformations", lambda: ["fake"])
    monkeypatch.setattr(bench, "apply", lambda name, sql, schema: (output, trusted, "proven" if trusted else "failed"))
    gold = bench.Gold("dev", 0, "toy", "SELECT name FROM parent WHERE id > 1")
    return bench.rewrite_query((gold, _toy()))["rewrites"]["fake"]


def test_a_trusted_rewrite_that_changes_a_result_is_wrong(monkeypatch):
    record = _fake(monkeypatch, "SELECT name FROM parent WHERE id > 2", True)
    assert record["changed"] and record["check"] == "differs" and record["wrong"] and record["witness"]


def test_an_untrusted_rewrite_that_changes_a_result_is_caught_not_wrong(monkeypatch):
    record = _fake(monkeypatch, "SELECT name FROM parent WHERE id > 2", False)
    assert record["check"] == "differs" and not record["wrong"]
    results = [{"id": "x", "database": "toy", "held_out": False, "translation": "same", "rewrites": {"fake": record}}]
    summary = bench.summarize(results)
    assert summary["caught"] == 1 and summary["wrong"] == 0 and summary["applied_trusted"] == 0
    assert bench.outcome(results[0]) == "unknown"


def test_a_layout_only_output_is_not_a_rewrite(monkeypatch):
    record = _fake(monkeypatch, "SELECT  name FROM   parent WHERE id>1", True)
    assert record["check"] == "same text" and not bench.applied(record)
    equal = _fake(monkeypatch, "SELECT name FROM parent WHERE 1 < id", True)
    assert equal["check"] == "agree" and equal["wrong"] is False and bench.applied(equal)


def test_a_query_that_runs_past_its_limit_is_a_timeout_never_a_rewrite():
    gold = bench.Gold("dev", 0, "toy", "SELECT name FROM parent")
    result = bench.rewrite_guarded((gold, _toy()), timeout=0)
    assert result["translation"] == "timeout" and not result["rewrites"] and bench.outcome(result) == "timeout"


def test_pinned_sample_has_no_wrong_rewrite():
    dev = [q for q in _dev() if not q.held_out]
    sample = dev[: SAMPLE - 1] + [q for q in dev if q.sql == KNOWN]
    assert len(sample) == SAMPLE
    schemas = spider_data.load_schemas()
    results = bench.run(sample, schemas, jobs=2)
    summary = bench.summarize(results)
    assert not summary["wrong"], [r["id"] for r in results]
    assert summary["translated"] >= FLOORS["translated"], summary
    known = next(r for r in results if r["id"] == next(q.id for q in sample if q.sql == KNOWN))
    assert known["rewrites"]["optimize"]["trusted"] and known["rewrites"]["optimize"]["check"] == "agree"
