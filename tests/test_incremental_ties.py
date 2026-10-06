"""Ties: what depends on tie-breaking, the row-order witness, and the nondeterministic verdict."""

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx  # noqa: E402
from kumosql.incremental_monotone import contract_constraints, source_schema  # noqa: E402
from kumosql.incremental_ties import (  # noqa: E402
    differs_by_row_order,
    model_tie_reasons,
    tie_reasons,
    tie_witness,
)

EVENTS = {
    "events": SourceTable(
        {"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"},
        ("id",),
        "ts",
    )
}
SCHEMA = source_schema(EVENTS)
KINDS = {"insert_new", "insert_late", "insert_boundary", "empty"}
EV = '${ref("events")}'
LATEST = "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY {order}) = 1"


def reasons(sql, kinds=KINDS, **options):
    constraints = contract_constraints(EVENTS, kinds, exact_copies=True)
    return tie_reasons(sql, constraints, SCHEMA, **options)


def model(body, key=', uniqueKey: ["customer_id"]'):
    return parse_incremental_sqlx(
        f'config {{ type: "incremental"{key} }}\n{body}\n', "m"
    )


def insert(i, customer, hour, v=0):
    return (
        f"INSERT INTO events (id, customer_id, ts, v) VALUES ({i}, {customer}, "
        f"TIMESTAMP '2024-01-01 {hour:02d}:00:00', {v})"
    )


# --- tie_reasons: each construct and what rules it out ---------------------------------------------

CAN_TIE = {
    "ROW_NUMBER over a non-unique order": f"SELECT id FROM events {LATEST.format(order='ts DESC')}",
    "ROW_NUMBER with no key in the order": "SELECT id, ROW_NUMBER() OVER (ORDER BY ts) AS n FROM events",
    "LAG": "SELECT id, LAG(v) OVER (PARTITION BY customer_id ORDER BY ts) AS p FROM events",
    "FIRST_VALUE": "SELECT id, FIRST_VALUE(v) OVER (PARTITION BY customer_id ORDER BY ts) AS f FROM events",
    "NTILE": "SELECT id, NTILE(2) OVER (ORDER BY ts) AS n FROM events",
    "ROWS frame": "SELECT id, SUM(v) OVER (PARTITION BY customer_id ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS s FROM events",
    "ANY_VALUE": "SELECT customer_id, ANY_VALUE(v) AS v FROM events GROUP BY customer_id",
    "unordered ARRAY_AGG": "SELECT customer_id, ARRAY_AGG(v) AS vs FROM events GROUP BY customer_id",
    "unordered STRING_AGG": "SELECT customer_id, STRING_AGG(CAST(v AS STRING)) AS vs FROM events GROUP BY customer_id",
    "LIMIT after a tying ORDER BY": "SELECT id FROM events ORDER BY ts LIMIT 3",
    "LIMIT without ORDER BY": "SELECT id FROM events LIMIT 3",
    "LIMIT on a set operation": "SELECT id FROM events UNION ALL SELECT id FROM events LIMIT 2",
    "RAND": "SELECT id, RAND() AS r FROM events",
    "GENERATE_UUID": "SELECT id, GENERATE_UUID() AS u FROM events",
    "a query that does not parse": "SELECT FROM (",
}

DETERMINISTIC = {
    "a total order (the key breaks the tie)": f"SELECT id FROM events {LATEST.format(order='ts DESC, id')}",
    "a partition by the key": "SELECT id FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1",
    "LAG over a total order": "SELECT id, LAG(v) OVER (PARTITION BY customer_id ORDER BY ts, id) AS p FROM events",
    "ARRAY_AGG with its own total ORDER BY": "SELECT customer_id, ARRAY_AGG(v ORDER BY id) AS vs FROM events GROUP BY customer_id",
    "ANY_VALUE of a group the key determines": "SELECT customer_id, ANY_VALUE(v) AS v FROM events GROUP BY id, customer_id",
    "LIMIT after a total ORDER BY": "SELECT id FROM events ORDER BY ts, id LIMIT 3",
    "a window over the whole partition": "SELECT id, SUM(v) OVER (PARTITION BY customer_id) AS s FROM events",
    "no order-sensitive construct": "SELECT id, v FROM events WHERE v > 0",
}


@pytest.mark.parametrize("sql", CAN_TIE.values(), ids=list(CAN_TIE))
def test_constructs_that_can_depend_on_tie_breaking_or_chance(sql):
    assert reasons(sql)


@pytest.mark.parametrize("sql", DETERMINISTIC.values(), ids=list(DETERMINISTIC))
def test_total_orders_and_other_deterministic_queries_have_no_reason(sql):
    assert reasons(sql) == []


def test_a_reason_names_the_construct():
    assert reasons(CAN_TIE["ROW_NUMBER over a non-unique order"]) == [
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts DESC) can tie"
    ]
    assert reasons(CAN_TIE["LIMIT without ORDER BY"]) == ["LIMIT without ORDER BY"]
    assert "RAND() is random" in reasons(CAN_TIE["RAND"])[0]


def test_key_facts_are_what_make_a_partition_by_the_key_deterministic():
    sql = DETERMINISTIC["a total order (the key breaks the tie)"]
    assert reasons(sql) == []
    # without the key fact, `id` does not determine the row and the order can tie
    assert tie_reasons(sql, {}, SCHEMA)
    # a NULL key can repeat, so the contract that inserts one takes the fact away
    assert reasons(sql, KINDS | {"null_key"})


def test_exact_copies_do_not_tie():
    """A re-delivered row is equal to its original, so picking either gives the same output."""

    sql = DETERMINISTIC["a total order (the key breaks the tie)"]
    duplicated = KINDS | {"duplicate"}
    assert reasons(sql, duplicated) == []
    strict = tie_reasons(sql, contract_constraints(EVENTS, duplicated), SCHEMA)
    assert strict  # duplicate rows are only harmless because they are exact copies


def test_model_tie_reasons_read_the_models_full_query():
    tying = model(f"SELECT id, customer_id FROM {EV} {LATEST.format(order='ts DESC')}")
    total = model(
        f"SELECT id, customer_id FROM {EV} {LATEST.format(order='ts DESC, id')}"
    )
    assert model_tie_reasons(tying, EVENTS, KINDS)
    assert model_tie_reasons(total, EVENTS, KINDS) == []


# --- witnesses -----------------------------------------------------------------------------------------

TYING = model(f"SELECT customer_id, id FROM {EV} {LATEST.format(order='ts DESC')}")
TOTAL = model(f"SELECT customer_id, id FROM {EV} {LATEST.format(order='ts DESC, id')}")
TIED_STATE = [
    insert(0, 0, 1),
    insert(3, 0, 1),
]  # one customer, two rows sharing the newest ts


def test_row_order_changes_the_answer_only_when_there_is_a_tie():
    assert differs_by_row_order(TYING, EVENTS, TIED_STATE)
    assert (
        differs_by_row_order(TYING, EVENTS, [insert(0, 0, 1), insert(3, 0, 2)]) is None
    )
    assert (
        differs_by_row_order(TYING, EVENTS, [insert(0, 0, 1), insert(3, 1, 1)]) is None
    )
    assert differs_by_row_order(TOTAL, EVENTS, TIED_STATE) is None
    assert differs_by_row_order(TYING, EVENTS, []) is None


def test_a_witness_is_a_small_replayable_state_with_a_description():
    found = tie_witness(TYING, EVENTS, KINDS, seeds=30)
    assert found is not None and found.status == "nondeterministic"
    assert found.batches == () and 2 <= len(found.initial) <= 3
    assert "can tie" in found.detail and "one evaluation returns" in found.detail
    assert differs_by_row_order(TYING, EVENTS, list(found.initial))  # it replays


def test_a_given_sequence_is_tried_before_the_random_ones():
    found = tie_witness(TYING, EVENTS, KINDS, seeds=0, sequences=[(TIED_STATE, [])])
    assert found is not None and set(found.initial) == set(TIED_STATE)
    assert (
        tie_witness(
            TYING,
            EVENTS,
            KINDS,
            seeds=0,
            sequences=[([insert(0, 0, 1), insert(3, 0, 2)], [])],
        )
        is None
    )


def test_no_witness_without_a_reason():
    assert tie_witness(TOTAL, EVENTS, KINDS, seeds=10) is None
    assert tie_witness(TYING, EVENTS, KINDS, reasons=[], seeds=10) is None


def test_a_suspicion_without_a_tie_is_not_a_witness():
    """The state a contract can reach may never tie: ``ts`` is unique per customer when every insert is new."""

    only_one_row_per_customer = model(
        f"SELECT customer_id, id FROM {EV} WHERE customer_id = 0 {LATEST.format(order='ts DESC')}"
    )
    assert model_tie_reasons(only_one_row_per_customer, EVENTS, {"empty"})
    assert tie_witness(only_one_row_per_customer, EVENTS, {"empty"}, seeds=10) is None


# --- the verdict -----------------------------------------------------------------------------------------


def test_a_tie_dependent_model_is_nondeterministic_with_a_witness():
    verdict = check_incremental(TYING, EVENTS, KINDS, seeds=30)
    assert verdict.outcome == "nondeterministic"
    assert (
        verdict.counterexample is not None
        and verdict.counterexample.status == "nondeterministic"
    )


def test_a_total_order_is_never_nondeterministic():
    # R7 proves it: customer_id is the partition and the key, and the order is total
    keyed = model(
        f"SELECT customer_id, id FROM {EV} WHERE customer_id IS NOT NULL {LATEST.format(order='ts DESC, id')}"
    )
    assert check_incremental(keyed, EVENTS, KINDS, seeds=30).outcome == "safe"
    assert (
        check_incremental(TOTAL, EVENTS, KINDS, seeds=30).outcome != "nondeterministic"
    )


def test_a_total_order_that_diverges_is_a_divergence_not_nondeterministic():
    appended = model(
        f"SELECT customer_id, id FROM {EV} {LATEST.format(order='ts DESC, id')}", key=""
    )
    assert check_incremental(appended, EVENTS, KINDS, seeds=30).outcome == "diverges"


@pytest.mark.parametrize("call", ["RAND()", "GENERATE_UUID()"])
def test_random_columns_are_nondeterministic_not_proved(call):
    random_column = model(f"SELECT id, {call} AS r FROM {EV}", ', uniqueKey: ["id"]')
    assert (
        check_incremental(random_column, EVENTS, KINDS, seeds=15).outcome
        == "nondeterministic"
    )
