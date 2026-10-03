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


TIB_BYTES = 2 ** 40


def rest_resource(**overrides):
    """A standard nested BigQuery Job resource: 1 TiB processed and billed, 1,500 slot-ms."""

    resource = {
        "kind": "bigquery#job",
        "jobReference": {"projectId": "proj", "jobId": "rest-1", "location": "US"},
        "configuration": {
            "dryRun": False,
            "labels": {"team": "data"},
            "query": {
                "query": "SELECT id FROM raw.events",
                "destinationTable": {"projectId": "proj", "datasetId": "core", "tableId": "base"},
            },
        },
        "statistics": {
            "creationTime": "1735776000000",
            "totalSlotMs": "1500",
            "query": {
                "totalBytesProcessed": str(TIB_BYTES),
                "totalBytesBilled": str(TIB_BYTES),
                "cacheHit": False,
                "statementType": "SELECT",
                "referencedTables": [{"projectId": "proj", "datasetId": "raw", "tableId": "events"}],
            },
        },
        "status": {"state": "DONE"},
    }
    resource.update(overrides)
    return resource


def test_rest_job_resource_is_normalized_not_zeroed():
    got = ObservedJob.from_record(rest_resource())
    assert got.job_id == "rest-1" and got.project == "proj" and got.location == "US"
    assert got.destination_table == {"projectId": "proj", "datasetId": "core", "tableId": "base"}
    assert got.referenced_tables == ({"projectId": "proj", "datasetId": "raw", "tableId": "events"},)
    assert (got.total_bytes_billed, got.total_bytes_processed, got.total_slot_ms) == (TIB_BYTES, TIB_BYTES, 1500)
    assert got.statement_type == "SELECT" and got.labels == {"team": "data"} and got.measured
    assert got.creation_time == "2025-01-02T00:00:00Z"
    flat = ObservedJob.from_record({
        "job_id": "rest-1", "project_id": "proj", "location": "US", "creation_time": "1735776000000",
        "destination_table": got.destination_table, "referenced_tables": list(got.referenced_tables),
        "total_bytes_billed": TIB_BYTES, "total_bytes_processed": TIB_BYTES, "total_slot_ms": 1500,
        "statement_type": "SELECT", "labels": {"team": "data"}, "query": "SELECT id FROM raw.events",
    })
    assert got == flat
    out = build_cost(pipeline(), [got], usd_per_tib=6.25)
    assert out["totals"]["measured"] == 6.25 and out["nodes"][0]["node"] == BASE.key


def test_rest_resource_flags_and_errors_are_read():
    resource = rest_resource(status={"errorResult": {"reason": "x"}})
    resource["configuration"]["dryRun"] = True
    resource["statistics"]["parentJobId"] = "parent"
    got = ObservedJob.from_record(resource)
    assert got.dry_run and got.error_result == {"reason": "x"} and got.parent_job_id == "parent"
    resource = rest_resource()
    resource["statistics"]["query"]["cacheHit"] = True
    assert ObservedJob.from_record(resource).cache_hit


def test_job_resources_load_from_json_and_jsonl(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps([rest_resource()]))
    (tmp_path / "r.jsonl").write_text(json.dumps(rest_resource()) + "\n")
    for name in ("r.json", "r.jsonl"):
        (loaded,) = load_jobs(tmp_path / name)
        assert loaded.job_id == "rest-1" and loaded.total_bytes_billed == TIB_BYTES


def test_resource_without_measurements_is_unmeasured_not_zero():
    resource = rest_resource()
    del resource["statistics"]
    got = ObservedJob.from_record(resource)
    assert got.job_id == "rest-1" and not got.measured
    assert got.total_bytes_billed is None and got.total_bytes_processed is None and got.total_slot_ms is None
    with pytest.raises(ValueError):
        got.cost()
    out = build_cost(pipeline(), [got, job("ok", 100, destination_table=BASE.key)])
    assert out["totals"]["measured"] == 100
    assert out["counts"]["jobs"] == 1 and out["counts"]["unmeasured"] == 1
    assert out["counts"]["excluded"] == {"unmeasured": 1}
    result = attribute_costs(pipeline(), [got])
    assert result.total == Cost() and not result.nodes
    assert [(a.job_id, a.method) for a in result.attributions] == [("rest-1", "unmeasured")]


def test_flat_row_missing_or_unreadable_bytes_is_unmeasured():
    for bad in (None, "", "n/a", "-5", "1.5", True):
        record = {"job_id": "a", "destination_table": BASE.key, "total_bytes_billed": bad}
        got = ObservedJob.from_record(record)
        assert got.total_bytes_billed is None and not got.measured, bad
    assert ObservedJob.from_record({"job_id": "a"}).total_bytes_billed is None
    # A genuine zero is a measurement.
    zero = ObservedJob.from_record({"job_id": "a", "total_bytes_billed": "0"})
    assert zero.total_bytes_billed == 0 and zero.measured


def test_missing_processed_or_slot_time_is_reported_not_hidden():
    only_billed = ObservedJob.from_record({"job_id": "a", "total_bytes_billed": 7, "destination_table": BASE.key})
    out = build_cost(pipeline(), [only_billed])
    assert out["totals"]["measured"] == 7
    assert out["counts"]["missing_fields"] == {"total_bytes_processed": 1, "total_slot_ms": 1}
    assert build_cost(pipeline(), [job("a", destination_table=BASE.key)])["counts"]["missing_fields"] == {}


def test_integer_strings_keep_every_digit():
    from kumosql.costs import _int

    assert _int("9007199254740993") == 9007199254740993
    assert _int(9007199254740993) == 9007199254740993
    assert _int(" 42 ") == 42 and _int("1e3") == 1000 and _int("2.0") == 2
    assert _int(None) is None and _int("") is None and _int("abc") is None and _int("nan") is None
    assert ObservedJob.from_record(
        {"job_id": "a", "total_bytes_billed": "9007199254740993"}).total_bytes_billed == 9007199254740993


def test_byte_totals_stay_integers():
    big = 9007199254740993
    out = build_cost(pipeline(), [job("a", big, destination_table=BASE.key),
                                  job("b", 1, destination_table=BASE.key)])
    assert out["totals"]["measured"] == big + 1
    assert all(isinstance(out["totals"][key], int) for key in ("measured", "attributed", "unattributed"))
    assert isinstance(out["nodes"][0]["measured"], int) and out["nodes"][0]["bytes_billed"] == big + 1
    assert isinstance(build_cost(pipeline(), [])["totals"]["measured"], int)


def test_money_payload_echoes_rate_model_region_and_invoice_exclusions():
    jobs = [job("a", TIB_BYTES, destination_table=BASE.key, location="US")]
    out = build_cost(pipeline(), jobs, usd_per_tib=6.25, billing_model="on_demand", region="us")
    basis = out["basis"]
    assert basis["measure"] == "total_bytes_billed" and basis["rate_per_tib"] == 6.25
    assert basis["currency"] == "USD" and basis["billing_model"] == "on_demand" and basis["region"] == "us"
    assert basis["job_locations"] == ["US"]
    for named in ("BI Engine", "reservation", "storage", "credits", "taxes"):
        assert named in basis["invoice_exclusions"]
    unset = build_cost(pipeline(), jobs, usd_per_tib=1.0)["basis"]
    assert unset["billing_model"] is None and unset["region"] is None
    bytes_only = build_cost(pipeline(), jobs)["basis"]
    assert bytes_only["rate_per_tib"] is None and bytes_only["currency"] is None
    assert bytes_only["invoice_exclusions"] is None
