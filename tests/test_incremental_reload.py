"""R8, delete-then-reload windows: each proven shape agrees with a deep search, each condition has a near miss."""

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import (
    SourceTable,
    check_incremental,
    first_divergence,
    parse_incremental_sqlx,
    prove,
    replay,
    search_divergence,
)
from kumosql.incremental_reload import RULE, prove_reload_window
from kumosql.incremental_rules import prove_more

EVENTS = {
    "events": SourceTable(
        {"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"},
        ("id",),
        "ts",
    )
}
EV = '${ref("events")}'
OLD = "TIMESTAMP '1970-01-01'"
MAX_SELF = "(SELECT MAX(ts) FROM ${self()})"
WM = f"COALESCE({MAX_SELF}, {OLD})"
KEY = ', uniqueKey: ["id"]'

BASIC = {"insert_new", "empty"}
BOUNDARY = {"insert_new", "insert_boundary", "empty"}
MERGE = {"insert_new", "update_touch", "empty"}
MERGE_BOUNDARY = {"insert_new", "insert_boundary", "update_touch", "empty"}


def when(sql: str, otherwise: str = "") -> str:
    return (
        "${when(incremental(), `"
        + sql
        + "`"
        + (", `" + otherwise + "`" if otherwise else "")
        + ")}"
    )


def window(delete: str, reload: str, config: str = "", where: str = "") -> object:
    """A model that runs ``delete`` before it reads the table and reloads with ``reload``."""

    base = f"SELECT id, customer_id, ts, v FROM {EV}" + (
        f" WHERE {where}" if where else ""
    )
    joiner = "AND" if where else "WHERE"
    body = f"pre_operations {{\n  {when('DELETE FROM ${self()} WHERE ' + delete)}\n}}\n{base}\n{when(joiner + ' ' + reload)}"
    return parse_incremental_sqlx(
        f'config {{ type: "incremental"{config} }}\n{body}\n', "m"
    )


def variable_window(declare: str, delete: str, reload: str, config: str = "") -> object:
    """A model whose window bound is a script variable ``w`` declared before the delete."""

    pre = f"DECLARE w DEFAULT (\n    {when('SELECT ' + declare, 'SELECT ' + OLD)}\n  );\n  {when('DELETE FROM ${self()} WHERE ' + delete)}"
    body = f"pre_operations {{\n  {pre}\n}}\nSELECT id, customer_id, ts, v FROM {EV}\nWHERE {reload}\n"
    return parse_incremental_sqlx(
        f'config {{ type: "incremental"{config} }}\n{body}', "m"
    )


TWO_HOURS = f"ts >= TIMESTAMP_SUB({MAX_SELF}, INTERVAL 2 HOUR)"
W_TWO_HOURS = f"COALESCE(TIMESTAMP_SUB({MAX_SELF}, INTERVAL 2 HOUR), {OLD})"

PROVEN = {
    "append, strict reload": (window(TWO_HOURS, "ts > " + WM), BASIC),
    "append, boundary rows (the delete removes the newest rows)": (
        window(TWO_HOURS, "ts > " + WM),
        BOUNDARY,
    ),
    "append, strict delete of a positive window": (
        window(f"ts > TIMESTAMP_SUB({MAX_SELF}, INTERVAL 1 HOUR)", "ts > " + WM),
        BOUNDARY,
    ),
    "append, delete from the maximum": (
        window(f"ts >= {MAX_SELF}", "ts > " + WM),
        BOUNDARY,
    ),
    "append, constant delete bound": (
        window("ts >= TIMESTAMP '2024-01-01 02:00:00'", "ts > " + WM),
        BASIC,
    ),
    "append with a filter": (window(TWO_HOURS, "ts > " + WM, where="v > 0"), BASIC),
    "merge, strict reload": (window(TWO_HOURS, "ts > " + WM, KEY), MERGE),
    "merge, strict reload, boundary rows": (
        window(TWO_HOURS, "ts > " + WM, KEY),
        MERGE_BOUNDARY,
    ),
    "merge, >= reload, boundary rows": (
        window(TWO_HOURS, "ts >= " + WM, KEY),
        MERGE_BOUNDARY,
    ),
    "merge, >= reload and a constant delete bound": (
        window("ts >= TIMESTAMP '2024-01-01 02:00:00'", "ts >= " + WM, KEY),
        MERGE_BOUNDARY,
    ),
    "merge, lookback reload": (
        window(TWO_HOURS, f"ts >= TIMESTAMP_SUB({WM}, INTERVAL 1 HOUR)", KEY),
        MERGE_BOUNDARY,
    ),
    "merge with a filter and inserts": (
        window(TWO_HOURS, "ts > " + WM, KEY, where="v > 0"),
        {"insert_new", "insert_boundary", "empty"},
    ),
    "variable window, append": (
        variable_window(W_TWO_HOURS, "ts >= w", "ts >= w"),
        BOUNDARY,
    ),
    "variable window, merge": (
        variable_window(W_TWO_HOURS, "ts >= w", "ts >= w", KEY),
        MERGE_BOUNDARY,
    ),
    "variable window, strict delete and reload, positive window": (
        variable_window(
            f"COALESCE(TIMESTAMP_SUB({MAX_SELF}, INTERVAL 1 HOUR), {OLD})",
            "ts > w",
            "ts > w",
        ),
        BOUNDARY,
    ),
    "variable window, strict delete, >= reload, merge": (
        variable_window(W_TWO_HOURS, "ts > w", "ts >= w", KEY),
        MERGE_BOUNDARY,
    ),
    "variable window from the maximum": (
        variable_window(f"COALESCE({MAX_SELF}, {OLD})", "ts >= w", "ts >= w"),
        BOUNDARY,
    ),
}

# (model, kinds, whether a divergence exists that the simulator's generator finds)
UNPROVEN = {
    "late rows (older than the window)": (
        window(TWO_HOURS, "ts > " + WM),
        {"insert_new", "insert_late", "empty"},
        True,
    ),
    "re-delivered old row": (
        window(TWO_HOURS, "ts > " + WM),
        {"insert_new", "duplicate", "empty"},
        True,
    ),
    "update of a row's values": (
        window(TWO_HOURS, "ts > " + WM, KEY),
        {"insert_new", "update", "empty"},
        True,
    ),
    "deleted source row": (
        window(TWO_HOURS, "ts > " + WM, KEY),
        {"insert_new", "delete", "empty"},
        True,
    ),
    "update_touch without a key": (
        window(TWO_HOURS, "ts > " + WM),
        {"insert_new", "update_touch", "empty"},
        True,
    ),
    "update_touch with a WHERE": (
        window(TWO_HOURS, "ts > " + WM, KEY, where="v > 0"),
        MERGE,
        False,
    ),
    "merge on a column that is not the source key": (
        window(TWO_HOURS, "ts > " + WM, ', uniqueKey: ["customer_id"]'),
        BASIC,
        False,
    ),
    "append reloading with >= repeats kept rows": (
        window(TWO_HOURS, "ts >= " + WM),
        BASIC,
        True,
    ),
    "append reloading with a lookback repeats kept rows": (
        window(TWO_HOURS, f"ts >= TIMESTAMP_SUB({WM}, INTERVAL 1 HOUR)"),
        BASIC,
        True,
    ),
    "boundary rows, constant delete bound": (
        window("ts >= TIMESTAMP '2024-01-01 02:00:00'", "ts > " + WM),
        BOUNDARY,
        True,
    ),
    "boundary rows, strict delete from the maximum": (
        window(f"ts > {MAX_SELF}", "ts > " + WM),
        BOUNDARY,
        True,
    ),
    "boundary rows, strict delete, zero interval": (
        window(f"ts > TIMESTAMP_SUB({MAX_SELF}, INTERVAL 0 HOUR)", "ts > " + WM),
        BOUNDARY,
        True,
    ),
    "boundary rows, negative window": (
        window(f"ts >= TIMESTAMP_SUB({MAX_SELF}, INTERVAL -1 HOUR)", "ts > " + WM),
        BOUNDARY,
        True,
    ),
    "delete bound read from the source": (
        window(
            "ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM " + EV + "), INTERVAL 2 HOUR)",
            "ts > " + WM,
        ),
        BASIC,
        False,
    ),
    "delete with another condition": (
        window(TWO_HOURS + " AND v > 0", "ts > " + WM),
        BASIC,
        True,
    ),
    "delete on another column": (
        window("id >= (SELECT MAX(id) FROM ${self()})", "ts > " + WM),
        BASIC,
        False,
    ),
    "reload without COALESCE": (window(TWO_HOURS, "ts > " + MAX_SELF), BASIC, True),
    "reload from the source": (
        window(
            TWO_HOURS, "ts > COALESCE((SELECT MAX(ts) FROM " + EV + "), " + OLD + ")"
        ),
        BASIC,
        False,
    ),
    "variable window without COALESCE": (
        variable_window(
            f"TIMESTAMP_SUB({MAX_SELF}, INTERVAL 2 HOUR)", "ts >= w", "ts >= w"
        ),
        BASIC,
        True,
    ),
    "variable window, delete >= but reload >": (
        variable_window(W_TWO_HOURS, "ts >= w", "ts > w"),
        BASIC,
        True,
    ),
    "variable window, strict delete, >= reload, append": (
        variable_window(W_TWO_HOURS, "ts > w", "ts >= w"),
        BASIC,
        True,
    ),
    "variable window, constant bound": (
        variable_window("TIMESTAMP '2024-01-01 02:00:00'", "ts >= w", "ts >= w"),
        BASIC,
        True,
    ),
    "variable window, strict from the maximum, boundary rows": (
        variable_window(f"COALESCE({MAX_SELF}, {OLD})", "ts > w", "ts > w"),
        BOUNDARY,
        True,
    ),
    "variable window, late rows": (
        variable_window(W_TWO_HOURS, "ts >= w", "ts >= w"),
        {"insert_new", "insert_late", "empty"},
        True,
    ),
    "variable window, delete reads a window above the maximum": (
        variable_window(
            f"COALESCE(TIMESTAMP_ADD({MAX_SELF}, INTERVAL 2 HOUR), {OLD})",
            "ts >= w",
            "ts >= w",
        ),
        BASIC,
        True,
    ),
}


@pytest.mark.parametrize("name", sorted(PROVEN))
def test_rule_proves_its_shape_and_a_deeper_search_agrees(name):
    m, kinds = PROVEN[name]
    verdict = prove_reload_window(m, EVENTS, frozenset(kinds))
    assert verdict is not None and verdict.outcome == "safe" and verdict.rule == RULE
    assert prove(m, EVENTS, kinds).rule == RULE
    assert check_incremental(m, EVENTS, kinds).rule == RULE
    for seed in (3, 17, 101):
        assert (
            search_divergence(m, EVENTS, kinds, seeds=40, batches=6, seed=seed) is None
        ), name


@pytest.mark.parametrize("name", sorted(UNPROVEN))
def test_near_misses_are_not_proven(name):
    m, kinds, diverges = UNPROVEN[name]
    assert prove_reload_window(m, EVENTS, frozenset(kinds)) is None
    assert prove(m, EVENTS, kinds) is None
    assert prove_more(m, EVENTS, frozenset(kinds)) is None
    if diverges:
        found = search_divergence(m, EVENTS, kinds, seeds=60, batches=5)
        assert found is not None, name
        assert check_incremental(m, EVENTS, kinds).outcome == "diverges"


def test_a_pre_operation_that_is_not_a_single_delete_is_not_proven():
    two_deletes = parse_incremental_sqlx(
        'config { type: "incremental" }\npre_operations {\n  '
        + when("DELETE FROM ${self()} WHERE " + TWO_HOURS)
        + ";\n  "
        + when("DELETE FROM ${self()} WHERE ts >= TIMESTAMP '2030-01-01 00:00:00'")
        + f"\n}}\nSELECT id, customer_id, ts, v FROM {EV}\n{when('WHERE ts > ' + WM)}\n",
        "m",
    )
    assert prove_reload_window(two_deletes, EVENTS, frozenset(BASIC)) is None


def test_an_update_partition_filter_is_not_proven():
    m = window(
        TWO_HOURS,
        "ts > " + WM,
        KEY + ", bigquery: { updatePartitionFilter: \"ts > TIMESTAMP '2020-01-01'\" }",
    )
    assert m.update_partition_filter
    assert prove_reload_window(m, EVENTS, frozenset(MERGE)) is None


def test_a_model_without_pre_operations_is_left_to_r1_and_r2():
    m = parse_incremental_sqlx(
        f'config {{ type: "incremental" }}\nSELECT id, ts FROM {EV}\n{when("WHERE ts > " + WM)}\n',
        "m",
    )
    assert prove_reload_window(m, EVENTS, frozenset(BASIC)) is None


def test_the_dev_case_the_rule_was_written_for():
    m, kinds = PROVEN["append, boundary rows (the delete removes the newest rows)"]
    assert check_incremental(m, EVENTS, kinds).outcome == "safe"


def test_update_touch_with_a_where_diverges_when_the_old_version_is_outside_the_window():
    m = window(TWO_HOURS, "ts > " + WM, KEY, where="v > 0")
    insert = "INSERT INTO events (id, customer_id, ts, v) VALUES ({}, 0, TIMESTAMP '2024-01-01 {:02d}:00:00', 1)"
    initial = [insert.format(1, 0), insert.format(2, 6)]
    touch = "UPDATE events SET ts = TIMESTAMP '2024-01-01 07:00:00', v = 0 WHERE id = 1"
    found = first_divergence(replay(m, EVENTS, initial, [[touch]]))
    assert found is not None and found.status == "diverge"
