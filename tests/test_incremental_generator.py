"""The change generator: gap rows, and the guarantee that the earlier sequences are unchanged."""

import datetime as dt
import hashlib
import json
import re

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import (  # noqa: E402
    CHANGE_KINDS,
    GAP_HOURS,
    SourceTable,
    check_incremental,
    parse_incremental_sqlx,
    random_sequence,
)

EVENTS = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, ("id",), "ts")}
ALL_KINDS = frozenset(CHANGE_KINDS)
TIME = re.compile(r"TIMESTAMP '([^']*)'")


def _digest(plan) -> str:
    return hashlib.sha256(json.dumps(plan).encode()).hexdigest()[:16]


def _hours(statements: list[str]) -> list[int]:
    """Event hours (since the generator's epoch) of the inserted rows, in order."""
    times = [m.group(1) for s in statements if s.startswith("INSERT") for m in [TIME.search(s)] if m]
    first = min(times)
    return [int((_to_seconds(t) - _to_seconds(first)) // 3600) for t in times]


def _to_seconds(text: str) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp()


# Digests of random_sequence(EVENTS, ALL_KINDS, seed, 4) from before gaps existed.
EARLIER = {
    0: "f1785a15d2a432f0",
    1: "321604dcacf3a4a6",
    7: "43cb3e22d36b3197",
    100003: "e317f1da0b8a89af",
    424242: "c8aef0a12ce07626",
}


@pytest.mark.parametrize("seed", sorted(EARLIER))
def test_without_gaps_the_earlier_sequence_is_unchanged(seed):
    initial, batches = random_sequence(EVENTS, ALL_KINDS, seed, 4, gap_rate=0)
    assert _digest((initial, batches)) == EARLIER[seed]


def test_gaps_only_move_event_times_for_time_independent_kinds():
    """The separate gap stream leaves every main-stream choice alone: strip the times and the sequences match."""

    kinds = frozenset({"insert_new", "update", "duplicate", "delete", "null_key", "empty"})
    strip = lambda plan: json.loads(TIME.sub("TIMESTAMP 'T'", json.dumps(plan)))  # noqa: E731
    moved = 0
    for seed in range(200):
        old = random_sequence(EVENTS, kinds, seed, 4, gap_rate=0)
        new = random_sequence(EVENTS, kinds, seed, 4)
        assert strip(new) == strip(old), seed
        moved += new != old
    assert moved > 20  # gaps do change the times


def test_about_a_fifth_of_new_rows_jump_ahead():
    kinds = frozenset({"insert_new"})
    gaps = rows = 0
    for seed in range(300):
        _, batches = random_sequence(EVENTS, kinds, seed, 6)
        hours = _hours([s for b in batches for s in b])
        for prev, cur in zip(hours, hours[1:]):
            rows += 1
            assert cur - prev >= 1
            if cur - prev > 1:
                assert GAP_HOURS[0] <= cur - prev <= GAP_HOURS[1]
                gaps += 1
    assert 0.15 < gaps / rows < 0.25


def test_gaps_are_deterministic_per_seed():
    kinds = frozenset({"insert_new", "insert_boundary", "empty"})
    assert random_sequence(EVENTS, kinds, 5, 4) == random_sequence(EVENTS, kinds, 5, 4)


WINDOW = (
    'config { type: "incremental" }\n'
    "pre_operations {\n"
    '  ${when(incremental(), `DELETE FROM ${self()} WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM ${ref("events")}), INTERVAL 2 HOUR)`)}\n'
    "}\n"
    'SELECT id, ts, v FROM ${ref("events")}\n'
    '${when(incremental(), `WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM ${ref("events")}), INTERVAL 2 HOUR)`)}\n'
)


def test_a_gap_wider_than_the_delete_window_is_refuted():
    """Delete-then-reload from the source loses rows once a run's rows are further apart than the window."""

    verdict = check_incremental(
        parse_incremental_sqlx(WINDOW, "m"), EVENTS, ["insert_new", "insert_boundary", "empty"], seeds=60
    )
    assert verdict.outcome == "diverges"
