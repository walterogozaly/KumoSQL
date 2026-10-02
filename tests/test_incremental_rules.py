"""Proof rules R3-R6 for incremental models: each proves its shape, and near misses stay unproven."""

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx, prove_watermark, search_divergence  # noqa: E402
from kumosql.incremental_rules import prove_more  # noqa: E402

EVENTS = {"events": SourceTable({"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, ("id",), "ts")}
WITH_CUSTOMERS = dict(EVENTS, customers=SourceTable({"id": "INT64", "tier": "STRING"}, ("id",)))
SELF_MAX = "COALESCE((SELECT MAX({m}) FROM ${{self()}}), TIMESTAMP '1970-01-01')"
DAY_MAX = "COALESCE((SELECT MAX(d) FROM ${self()}), DATE '1970-01-01')"
DEDUP = "QUALIFY ROW_NUMBER() OVER (PARTITION BY {p} ORDER BY ts DESC) = 1"
EV = '${ref("events")}'
CU = '${ref("customers")}'


def model(config: str, body: str):
    return parse_incremental_sqlx(f'config {{ type: "incremental"{config} }}\n{body}\n', "m")


def when(sql: str) -> str:
    return "${when(incremental(), `" + sql + "`)}"


KEY = ', uniqueKey: ["id"]'
GROUP_KEY = ', uniqueKey: ["customer_id"]'
WM = SELF_MAX.format(m="ts")
GROUP_WM = SELF_MAX.format(m="last_ts")
REAGG = "SELECT customer_id, SUM(v) AS total, COUNT(*) AS n, MAX(ts) AS last_ts FROM " + EV

PROVEN = {
    "dedup merge >=": ("R3", model(KEY, f"SELECT id, customer_id, ts, v FROM {EV}\n{when('WHERE ts >= ' + WM)}\n{DEDUP.format(p='id')}"), EVENTS, {"insert_new", "insert_boundary", "duplicate", "update_touch", "empty"}, None),
    "dedup append >": ("R3", model("", f"SELECT id, ts, v FROM {EV}\n{when('WHERE ts > ' + WM)}\n{DEDUP.format(p='id')}"), EVENTS, {"insert_new", "duplicate", "empty"}, None),
    "date watermark": ("R4", model(KEY, f"SELECT id, ts, DATE(ts) AS d, v FROM {EV}\n{when('WHERE DATE(ts) >= ' + DAY_MAX)}"), EVENTS, {"insert_new", "insert_boundary", "update_touch", "empty"}, None),
    "hour watermark": ("R4", model(KEY, f"SELECT id, v, TIMESTAMP_TRUNC(ts, HOUR) AS h FROM {EV}\n{when('WHERE TIMESTAMP_TRUNC(ts, HOUR) >= ' + SELF_MAX.format(m='h'))}"), EVENTS, {"insert_new", "insert_boundary", "empty"}, None),
    "re-aggregate >=": ("R5", model(GROUP_KEY, f"{REAGG}\n{when(f'WHERE customer_id IN (SELECT customer_id FROM {EV} WHERE ts >= {GROUP_WM})')}\nGROUP BY customer_id"), EVENTS, {"insert_new", "insert_boundary", "empty"}, None),
    "re-aggregate filtered >": ("R5", model(GROUP_KEY, f"SELECT customer_id, MIN(v) AS lo, MAX(ts) AS last_ts FROM {EV} WHERE v > 1\n{when(f'AND customer_id IN (SELECT customer_id FROM {EV} WHERE ts > {GROUP_WM})')}\nGROUP BY customer_id"), EVENTS, {"insert_new", "empty"}, None),
    "inner join": ("R6", model("", f"SELECT e.id, e.ts, e.v, c.tier FROM {EV} e JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events",)),
    "left join": ("R6", model("", f"SELECT e.id, e.ts, c.tier FROM {EV} e LEFT JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events",)),
}

UNPROVEN = {
    "dedup on a non-key": (model(GROUP_KEY, f"SELECT customer_id, ts, v FROM {EV}\n{when('WHERE ts >= ' + WM)}\n{DEDUP.format(p='customer_id')}"), EVENTS, {"insert_new", "insert_boundary", "empty"}, None),
    "dedup with plain updates": (model(KEY, f"SELECT id, customer_id, ts, v FROM {EV}\n{when('WHERE ts >= ' + WM)}\n{DEDUP.format(p='id')}"), EVENTS, {"insert_new", "duplicate", "update", "empty"}, None),
    "duplicates without dedup": (model("", f"SELECT id, ts, v FROM {EV}\n{when('WHERE ts > ' + WM)}"), EVENTS, {"insert_new", "duplicate", "empty"}, None),
    "filtered merge with updates": (model(KEY, f"SELECT id, ts, v FROM {EV} WHERE v > 0\n{when('AND ts > ' + WM)}\n{DEDUP.format(p='id')}"), EVENTS, {"insert_new", "update_touch", "empty"}, None),
    "date watermark strict": (model(KEY, f"SELECT id, ts, DATE(ts) AS d, v FROM {EV}\n{when('WHERE DATE(ts) > ' + DAY_MAX)}"), EVENTS, {"insert_new", "empty"}, None),
    "date watermark append": (model("", f"SELECT id, ts, DATE(ts) AS d, v FROM {EV}\n{when('WHERE DATE(ts) >= ' + DAY_MAX)}"), EVENTS, {"insert_new", "empty"}, None),
    "re-aggregate with updates": (model(GROUP_KEY, f"{REAGG}\n{when(f'WHERE customer_id IN (SELECT customer_id FROM {EV} WHERE ts >= {GROUP_WM})')}\nGROUP BY customer_id"), EVENTS, {"insert_new", "update_touch", "empty"}, None),
    "re-aggregate strict with ties": (model(GROUP_KEY, f"{REAGG}\n{when(f'WHERE customer_id IN (SELECT customer_id FROM {EV} WHERE ts > {GROUP_WM})')}\nGROUP BY customer_id"), EVENTS, {"insert_new", "insert_boundary", "empty"}, None),
    "re-aggregate with HAVING": (model(GROUP_KEY, f"{REAGG}\n{when(f'WHERE customer_id IN (SELECT customer_id FROM {EV} WHERE ts >= {GROUP_WM})')}\nGROUP BY customer_id HAVING SUM(v) < 3"), EVENTS, {"insert_new", "empty"}, None),
    "re-aggregate narrowed groups": (model(GROUP_KEY, f"{REAGG}\n{when(f'WHERE customer_id IN (SELECT customer_id FROM {EV} WHERE ts >= {GROUP_WM} AND v > 1)')}\nGROUP BY customer_id"), EVENTS, {"insert_new", "empty"}, None),
    "aggregate of new rows only": (model(GROUP_KEY, f"{REAGG}\n{when('WHERE ts >= ' + GROUP_WM)}\nGROUP BY customer_id"), EVENTS, {"insert_new", "empty"}, None),
    "join, every table changes": (model("", f"SELECT e.id, e.ts, c.tier FROM {EV} e JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, None),
    "join, dimension changes": (model("", f"SELECT e.id, e.ts, c.tier FROM {EV} e JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events", "customers")),
    "right join": (model("", f"SELECT e.id, e.ts, c.tier FROM {EV} e RIGHT JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events",)),
    "join with >=": (model("", f"SELECT e.id, e.ts, c.tier FROM {EV} e JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts >= ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events",)),
    "join with DISTINCT": (model("", f"SELECT DISTINCT c.tier, e.ts FROM {EV} e JOIN {CU} c ON c.id = e.customer_id\n{when('WHERE e.ts > ' + WM)}"), WITH_CUSTOMERS, {"insert_new", "empty"}, ("events",)),
}


@pytest.mark.parametrize("name", sorted(PROVEN))
def test_rule_proves_its_shape_and_a_deeper_search_agrees(name):
    rule, m, sources, kinds, tables = PROVEN[name]
    verdict = check_incremental(m, sources, kinds, tables=tables)
    assert verdict.outcome == "safe" and verdict.rule.startswith(rule), verdict
    assert search_divergence(m, sources, kinds, seeds=12, batches=5, seed=11, tables=tables) is None


@pytest.mark.parametrize("name", sorted(UNPROVEN))
def test_near_misses_are_not_proven(name):
    m, sources, kinds, tables = UNPROVEN[name]
    assert prove_watermark(m, sources, frozenset(kinds)) is None
    assert prove_more(m, sources, frozenset(kinds), tables) is None


def test_filtered_merge_with_updates_is_refuted():
    """R2 used to prove this: an update that fails the filter leaves the old row behind."""

    m = model(KEY, f"SELECT id, ts, v FROM {EV} WHERE v > 0\n{when('AND ts > ' + WM)}")
    assert prove_watermark(m, EVENTS, frozenset({"insert_new", "update_touch", "empty"})) is None
    assert prove_watermark(m, EVENTS, frozenset({"insert_new", "empty"})) is not None
    assert check_incremental(m, EVENTS, {"insert_new", "update_touch", "empty"}).outcome == "diverges"
