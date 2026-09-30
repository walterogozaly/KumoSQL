import json

import pytest

from kumosql import Pipeline, Target
from kumosql.costs import Cost, ObservedJob, attribute_costs, build_cost, load_jobs
from kumosql.pipeline import Model

RAW = Target("proj", "raw", "events")
BASE = Target("proj", "core", "base")
VIEW = Target("proj", "core", "summary")
OTHER = Target("proj", "core", "other")


def pipeline():
    return Pipeline({
        BASE.key: Model(BASE, "table", f"SELECT id FROM `{RAW.key}`", declared_dependencies=(RAW,)),
        VIEW.key: Model(VIEW, "view", f"SELECT id FROM `{BASE.key}`", declared_dependencies=(BASE,)),
        OTHER.key: Model(OTHER, "table", "SELECT 1 AS id"),
    }, sources={RAW.key: RAW})


def job(job_id, billed=1000, **kw):
    record = {"job_id": job_id, "total_bytes_billed": billed, "total_bytes_processed": billed // 2,
              "total_slot_ms": 10, "creation_time": "2025-01-02T00:00:00Z"}
    record.update(kw)
    return ObservedJob.from_record(record)


def test_destination_attribution_and_exercised_edge():
    result = attribute_costs(pipeline(), [job("a", destination_table=BASE.key, referenced_tables=[RAW.key])])
    assert result.nodes[BASE.key].bytes_billed == 1000
    assert result.node_methods[BASE.key] == {"destination_table": 1}
    assert result.edges[(RAW.key, BASE.key)] == {"jobs": 1, "declared": True}


def test_view_reader_cost_lands_on_view_only():
    result = attribute_costs(pipeline(), [job("a", referenced_tables=[VIEW.key, BASE.key, RAW.key])])
    assert set(result.nodes) == {VIEW.key}
    assert result.node_methods[VIEW.key] == {"reader": 1}


def test_unrelated_readers_are_unattributed_not_split():
    result = attribute_costs(pipeline(), [job("a", referenced_tables=[VIEW.key, OTHER.key])])
    assert not result.nodes and set(result.reasons) == {"multiple_readers"}


def test_unattributed_reasons_and_invariant():
    jobs = [
        job("ok", destination_table=BASE.key),
        job("tmp", 300, destination_table="proj._scratch.anon1"),
        job("ext", 200, destination_table="proj.elsewhere.t"),
        job("ext2", 100, referenced_tables=["proj.elsewhere.t"]),
        job("none", 50),
        job("scr", 25, statement_type="SCRIPT"),
        job("sys", 5, referenced_tables=["proj.region-us.INFORMATION_SCHEMA.JOBS"]),
    ]
    out = build_cost(pipeline(), jobs)
    reasons = {r["reason"]: r["measured"] for r in out["unattributed"]}
    assert reasons == {"temporary_destination": 300, "unmatched_destination": 200,
                       "no_matching_references": 105, "no_tables": 50, "script_without_children": 25}
    t = out["totals"]
    assert t["attributed"] + t["unattributed"] == t["measured"] == 1680
    assert out["currency"] is None and out["unit"] == "bytes_billed"


def test_cache_hits_dry_runs_and_script_parents_are_not_double_counted():
    jobs = [
        job("hit", destination_table=BASE.key, cache_hit=True),
        job("dry", destination_table=BASE.key, dry_run=True),
        job("parent", 900, statement_type="SCRIPT"),
        job("child", 900, destination_table=BASE.key, parent_job_id="parent"),
    ]
    out = build_cost(pipeline(), jobs)
    assert out["totals"]["measured"] == 900
    assert out["counts"]["excluded"] == {"cache_hit": 1, "dry_run": 1, "script_parent": 1}


def test_failed_job_with_bytes_still_counts_and_label_fallback():
    jobs = [job("f", 400, destination_table=BASE.key, error_result={"reason": "x"}),
            job("l", 100, labels={"target": VIEW.key})]
    result = attribute_costs(pipeline(), jobs)
    assert result.nodes[BASE.key].bytes_billed == 400
    assert result.node_methods[VIEW.key] == {"label": 1}


def test_decorator_resolves_and_case_is_not_folded():
    result = attribute_costs(pipeline(), [job("a", destination_table=BASE.key + "$20250101")])
    assert BASE.key in result.nodes
    result = attribute_costs(pipeline(), [job("b", destination_table=BASE.key.upper())])
    assert not result.nodes


def test_money_needs_explicit_rate():
    out = build_cost(pipeline(), [job("a", 2 ** 40, destination_table=BASE.key)], usd_per_tib=6.25)
    assert out["currency"] == "USD" and out["totals"]["measured"] == 6.25
    node = out["nodes"][0]
    assert node["node"] == BASE.key and node["runs"] == 1 and node["source"] == "measured"


def test_estimated_and_measured_never_mix():
    with pytest.raises(ValueError):
        Cost() + Cost.estimated(10)
    assert Cost.estimated(10).bytes_billed == 0


def test_window_from_jobs_or_override():
    jobs = [job("a", creation_time="2025-01-01T00:00:00Z"), job("b", creation_time="2025-01-05T00:00:00Z")]
    assert build_cost(pipeline(), jobs)["window"] == {
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-05T00:00:00Z"}
    out = build_cost(pipeline(), jobs, window={"end": "2025-02-01T00:00:00Z"})
    assert out["window"]["end"] == "2025-02-01T00:00:00Z"


def test_loaders(tmp_path):
    rec = {"job_id": "a", "total_bytes_billed": "7",
           "destination_table": {"projectId": "proj", "datasetId": "core", "tableId": "base"}}
    (tmp_path / "j.json").write_text(json.dumps([rec]))
    (tmp_path / "j.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")
    (tmp_path / "j.csv").write_text(
        'job_id,total_bytes_billed,cache_hit,referenced_tables\na,5,false,"[""proj.raw.events""]"\n')
    assert load_jobs(tmp_path / "j.json")[0].total_bytes_billed == 7
    assert len(load_jobs(tmp_path / "j.jsonl")) == 2
    csv_job = load_jobs(tmp_path / "j.csv")[0]
    assert csv_job.referenced_tables == ("proj.raw.events",) and not csv_job.cache_hit
    out = build_cost(pipeline(), load_jobs(tmp_path / "j.json"))
    assert out["nodes"][0]["node"] == BASE.key
