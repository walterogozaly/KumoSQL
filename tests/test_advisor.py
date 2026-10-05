"""The Dataform materialization advisor: workload facts, column-level bytes, freshness evidence, selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kumosql import Pipeline, Target
from kumosql.advisor import BytesModel, TableSize, advise, columns_read
from kumosql.cost_model import Pricing
from kumosql.costs import ObservedJob
from kumosql.pipeline import Model
from kumosql.pipeline_types import ColumnRef
from kumosql.workload import runs_per_day, workload

RAW = Target("p", "raw", "events")
BASE = Target("p", "core", "base")
DAILY = Target("p", "core", "daily")
RECENT = Target("p", "core", "recent")
DIRECT = Target("p", "core", "direct")
MART = Target("p", "core", "mart")

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
ROWS = 1_000_000


def project() -> Pipeline:
    return Pipeline(
        {
            BASE.key: Model(BASE, "table", f"SELECT id, user_id, amount, kind FROM `{RAW.key}` WHERE kind != 'test'"),
            DAILY.key: Model(DAILY, "view", f"SELECT user_id, SUM(amount) AS total FROM `{BASE.key}` GROUP BY user_id"),
            RECENT.key: Model(RECENT, "view", f"SELECT id, amount, CURRENT_DATE() AS d FROM `{BASE.key}`"),
            DIRECT.key: Model(DIRECT, "view", f"SELECT user_id, amount FROM `{RAW.key}`"),
            MART.key: Model(MART, "table", f"SELECT user_id, total FROM `{DAILY.key}` WHERE total > 0"),
        },
        sources={RAW.key: RAW},
        source_schema={RAW.key: {"id": "INT64", "user_id": "INT64", "amount": "FLOAT64", "kind": "STRING", "ts": "TIMESTAMP"}},
    )


def sizes() -> dict[str, TableSize]:
    int_cols = {"id": ROWS * 8.0, "user_id": ROWS * 8.0, "amount": ROWS * 8.0}
    return {
        RAW.key: TableSize(ROWS, None, {**int_cols, "kind": ROWS * 6.0, "ts": ROWS * 8.0}),
        BASE.key: TableSize(ROWS * 0.9, None, {k: v * 0.9 for k, v in {**int_cols, "kind": ROWS * 6.0}.items()}),
        DAILY.key: TableSize(10_000, None, {"user_id": 80_000.0, "total": 80_000.0}),
        MART.key: TableSize(9_000, None, {"user_id": 72_000.0, "total": 72_000.0}),
    }


def at(day: int, hour: float) -> str:
    return (T0 + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")


def history(days: int = 14) -> list[ObservedJob]:
    jobs: list[dict] = []
    for day in range(days):
        jobs.append({"job_id": f"b{day}", "creation_time": at(day, 6), "destination_table": BASE.key,
                     "referenced_tables": [RAW.key], "total_bytes_processed": 30 * ROWS, "total_bytes_billed": 30 * ROWS,
                     "total_slot_ms": 60_000})
        jobs.append({"job_id": f"m{day}", "creation_time": at(day, 6.2), "destination_table": MART.key,
                     "referenced_tables": [DAILY.key, BASE.key], "total_bytes_processed": 16 * ROWS * 0.9,
                     "total_bytes_billed": 16 * ROWS * 0.9, "total_slot_ms": 20_000})
        for k in range(40):
            jobs.append({"job_id": f"d{day}-{k}", "creation_time": at(day, 8 + k * 0.2), "user_email": f"u{k % 5}@x",
                         "query": f"SELECT user_id, total FROM `{DAILY.key}` WHERE user_id = 7",
                         "referenced_tables": [DAILY.key, BASE.key], "total_bytes_processed": 16 * ROWS * 0.9,
                         "total_bytes_billed": 16 * ROWS * 0.9, "total_slot_ms": 9_000})
        for k in range(5):
            jobs.append({"job_id": f"r{day}-{k}", "creation_time": at(day, 9 + k), "user_email": "a@x",
                         "query": f"SELECT id, d FROM `{RECENT.key}`", "referenced_tables": [RECENT.key, BASE.key],
                         "total_bytes_processed": 8 * ROWS * 0.9, "total_bytes_billed": 8 * ROWS * 0.9, "total_slot_ms": 2_000})
        for k in range(3):
            jobs.append({"job_id": f"x{day}-{k}", "creation_time": at(day, 10 + k), "user_email": "b@x",
                         "query": f"SELECT SUM(amount) FROM `{DIRECT.key}`", "referenced_tables": [DIRECT.key, RAW.key],
                         "total_bytes_processed": 8 * ROWS, "total_bytes_billed": 8 * ROWS, "total_slot_ms": 3_000})
        jobs.append({"job_id": f"c{day}", "creation_time": at(day, 11), "cache_hit": True,
                     "query": "SELECT 1", "referenced_tables": [DAILY.key]})
    return [ObservedJob.from_record(j) for j in jobs]


def test_workload_sorts_builds_reads_and_templates():
    use = workload(project(), history())
    assert use.nodes[BASE.key].builds.jobs == 14
    assert use.nodes[MART.key].builds.jobs == 14
    assert use.nodes[DAILY.key].reads.jobs == 14 * 40
    assert len(use.nodes[DAILY.key].readers) == 5
    assert use.excluded == {"cache_hit": 14}
    daily = [t for t in use.templates.values() if t.nodes == (DAILY.key,)]
    assert len(daily) == 1 and daily[0].runs.jobs == 14 * 40
    assert runs_per_day(use.nodes[BASE.key].builds.times) == pytest.approx(1.0)
    payload = use.to_json()
    assert "SELECT" not in str(payload)  # query text never leaves the module


def test_columns_read_and_lineage_scan():
    p = project()
    model = BytesModel(p, sizes())
    reads = columns_read(p, f"SELECT user_id, total FROM `{DAILY.key}` WHERE user_id = 7", model.outputs)
    assert reads == {DAILY.key: frozenset({"user_id", "total"})}
    star = columns_read(p, f"SELECT * FROM `{DIRECT.key}`", model.outputs)
    assert star == {DIRECT.key: None}
    stored = frozenset({BASE.key, MART.key})
    through_view = model.scan(DAILY.key, frozenset({"total"}), stored)
    # total needs amount, and the grouping key user_id is read whichever outputs are wanted
    assert through_view == {ColumnRef(BASE.key, "amount"), ColumnRef(BASE.key, "user_id")}
    assert model.scan(DAILY.key, frozenset({"total"}), stored | {DAILY.key}) == {ColumnRef(DAILY.key, "total")}


def test_advice_stores_the_hot_aggregate_and_refuses_the_clock_view():
    advice = advise(project(), history(), sizes=sizes(), pricing=Pricing())
    payload = advice.to_json()
    by_id = {c["id"]: c for c in advice.candidates}
    daily = by_id["store:" + DAILY.key]
    assert daily["evidence"]["label"] == "proven"
    assert daily["saving_per_day"] > 0
    assert daily["chosen"]
    assert daily["storage_basis"] == "measured"
    assert by_id["store:" + RECENT.key]["evidence"]["label"] == "changes_results"
    direct = by_id["store:" + DIRECT.key]
    assert direct["evidence"]["label"] == "conditional"
    assert direct["evidence"]["conditions"]
    assert not direct["chosen"]
    assert "store:" + DAILY.key in payload["selection"]["chosen"]
    assert all(c["evidence"]["label"] == "proven" for c in payload["recommendations"])
    assert {c["id"] for c in payload["needs_proof"]} >= {"store:" + DIRECT.key}
    assert payload["calibration"]["bytes_estimate_vs_measured"]["jobs"] >= 2


def test_unstoring_a_table_read_by_nothing_but_its_refresh():
    advice = advise(project(), history(), sizes=sizes(), pricing=Pricing())
    mart = {c["id"]: c for c in advice.candidates}["unstore:" + MART.key]
    # Nothing reads the mart, so making it a view saves its whole refresh; it reads only stored models refreshed together.
    assert mart["saving_per_day"] == pytest.approx(16 * ROWS * 0.9, rel=0.01)
    assert mart["evidence"]["label"] == "proven"


def test_schedules_decide_freshness_when_given():
    advice = advise(project(), history(), sizes=sizes(), schedules={BASE.key: ["nightly"], MART.key: ["hourly"]})
    by_id = {c["id"]: c for c in advice.candidates}
    assert by_id["store:" + DAILY.key]["evidence"]["label"] == "proven"
    assert "nightly" in by_id["store:" + DAILY.key]["evidence"]["reason"]


def test_editions_pricing_uses_slot_time():
    advice = advise(project(), history(), sizes=sizes(), pricing=Pricing("editions"))
    assert advice.unit == "slot_ms"
    daily = {c["id"]: c for c in advice.candidates}["store:" + DAILY.key]
    assert daily["saving_per_day"] > 0
    assert advice.calibration["slot_ms_per_byte"] > 0
