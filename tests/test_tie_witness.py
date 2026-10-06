"""Tie witnesses: two storage orders of a few rows that give a query two different results."""

import copy
import json

import pytest

pytest.importorskip("duckdb")

from kumosql.result_equivalence import DataRules
from kumosql.smt_equivalence import TableConstraints
from kumosql.tie_determinism import UNKNOWN, analyze
from kumosql.tie_witness import find_tie_witness, replay

SCHEMA = {"events": {"id": "INT64", "user_id": "INT64", "ts": "TIMESTAMP", "value": "INT64"}}
RULES = {"events": DataRules(frozenset({"id"}), (("id",),))}
CONSTRAINTS = {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}

LATEST = "SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1"
TIE_BROKEN = "SELECT user_id, ts, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC, id) = 1"
ONLY_TIE_COLUMNS = "SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1"

# the tie analysis calls each of these `unknown`
NONDETERMINISTIC = [
    LATEST,
    "SELECT * FROM events ORDER BY ts LIMIT 1",
    "SELECT user_id, ANY_VALUE(value) AS v FROM events GROUP BY user_id",
    "SELECT id, value, LAG(value) OVER (PARTITION BY user_id ORDER BY ts) AS p FROM events",
    "SELECT * FROM events LIMIT 1",
]
# ... and each of these `deterministic`, for a reason the declared key or the outputs give
DETERMINISTIC = [
    TIE_BROKEN,
    ONLY_TIE_COLUMNS,
    "SELECT * FROM events ORDER BY ts, id LIMIT 1",
    "SELECT user_id, SUM(value) AS s FROM events GROUP BY user_id",
    "SELECT id, value, RANK() OVER (PARTITION BY user_id ORDER BY ts) AS r FROM events",
]


def _orders(witness):
    return witness["orders"]["first"], witness["orders"]["second"]


def test_dedup_witness_is_two_tied_rows_in_two_orders_and_replays():
    witness = find_tie_witness(LATEST, SCHEMA, RULES)
    assert witness is not None
    rows = witness["tables"]["events"]
    assert len(rows) == 2 and rows[0]["user_id"] == rows[1]["user_id"] and rows[0]["ts"] == rows[1]["ts"]
    first, second = _orders(witness)
    assert first["events"] != second["events"] and sorted(second["events"]) == [0, 1]
    assert witness["results"]["first"] != witness["results"]["second"]
    assert replay(json.loads(json.dumps(witness)))


@pytest.mark.parametrize("sql", NONDETERMINISTIC)
def test_every_unknown_site_shape_gets_a_replaying_witness(sql):
    assert any(site.verdict == UNKNOWN for site in analyze(sql, constraints=CONSTRAINTS).sites)
    witness = find_tie_witness(sql, SCHEMA, RULES)
    assert witness is not None and replay(witness)
    assert sum(len(rows) for rows in witness["tables"].values()) <= 3


@pytest.mark.parametrize("sql", DETERMINISTIC)
def test_no_witness_for_a_query_the_analysis_calls_deterministic(sql):
    assert analyze(sql, constraints=CONSTRAINTS).deterministic
    assert find_tie_witness(sql, SCHEMA, RULES) is None


def test_without_the_declared_key_the_tie_breaker_is_not_one():
    # `id` is only a tie-breaker when it is unique; with duplicates allowed, rows can tie on it too
    assert find_tie_witness(TIE_BROKEN, SCHEMA) is not None
    assert find_tie_witness(TIE_BROKEN, SCHEMA, RULES) is None


def test_witness_keeps_not_null_columns_and_keys():
    rules = {"events": DataRules(frozenset({"id", "value", "ts"}), (("id",),))}
    witness = find_tie_witness(LATEST, SCHEMA, rules)
    assert witness is not None and replay(witness)
    rows = witness["tables"]["events"]
    assert all(row["value"] is not None and row["ts"] is not None for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)


def test_foreign_keys_hold_in_the_witness():
    schema = {"orders": {"id": "INT64", "customer": "INT64", "placed": "DATE"}, "customers": {"id": "INT64"}}
    rules = {"orders": DataRules(frozenset({"id", "customer"}), (("id",),)), "customers": DataRules(frozenset({"id"}), (("id",),))}
    foreign_keys = [("orders", ("customer",), "customers", ("id",))]
    sql = "SELECT customer, placed FROM orders ORDER BY placed LIMIT 1"
    witness = find_tie_witness(sql, schema, rules, foreign_keys=foreign_keys)
    assert witness is None or (replay(witness) and {r["customer"] for r in witness["tables"]["orders"]} <= {r["id"] for r in witness["tables"]["customers"]})
    sql = "SELECT o.id, o.placed FROM orders AS o JOIN customers AS c ON o.customer = c.id ORDER BY o.placed LIMIT 1"
    witness = find_tie_witness(sql, schema, rules, foreign_keys=foreign_keys)
    assert witness is not None and replay(witness)
    assert {r["customer"] for r in witness["tables"]["orders"]} <= {r["id"] for r in witness["tables"]["customers"]}


def test_dates_and_decimals_survive_the_json_round_trip():
    schema = {"t": {"id": "INT64", "d": "DATE", "amount": "NUMERIC", "label": "STRING", "ok": "BOOL", "x": "FLOAT64"}}
    rules = {"t": DataRules(frozenset({"id"}), (("id",),))}
    witness = find_tie_witness("SELECT * FROM t ORDER BY d LIMIT 1", schema, rules)
    assert witness is not None
    assert replay(json.loads(json.dumps(witness)))


def test_replay_rejects_a_witness_that_was_changed():
    witness = find_tie_witness(LATEST, SCHEMA, RULES)
    swapped = copy.deepcopy(witness)
    swapped["results"]["first"], swapped["results"]["second"] = witness["results"]["second"], witness["results"]["first"]
    assert not replay(swapped)
    same_order = copy.deepcopy(witness)
    same_order["orders"]["second"] = same_order["orders"]["first"]
    assert not replay(same_order)
    broken_key = copy.deepcopy(witness)
    broken_key["tables"]["events"][1]["id"] = broken_key["tables"]["events"][0]["id"]
    assert not replay(broken_key)
    other_query = copy.deepcopy(witness)
    other_query["sql"] = TIE_BROKEN
    assert not replay(other_query)
    assert not replay({})


def test_a_query_duckdb_cannot_run_or_that_is_random_gets_no_witness():
    assert find_tie_witness("SELECT nope FROM missing_table", SCHEMA, RULES) is None
    assert find_tie_witness("SELECT id, RAND() AS r FROM events", SCHEMA, RULES) is None
    assert find_tie_witness("SELECT", SCHEMA, RULES) is None
