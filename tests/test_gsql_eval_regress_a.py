"""BETWEEN with NaN and NULL bounds, and the civil-time string parsers (leap seconds, lower-case ``t``)."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from kumosql.gsql_eval import EvalError, Unsupported, evaluate

NAN = 'CAST("nan" AS FLOAT64)'
INF = 'CAST("inf" AS FLOAT64)'
NINF = 'CAST("-inf" AS FLOAT64)'


def one(sql):
    return evaluate("SELECT " + sql).rows[0][0]


@pytest.mark.parametrize(
    "expr, expected",
    [
        (f"{NAN} BETWEEN -5.0 AND 1.0", False),
        (f"2.0 BETWEEN {NAN} AND 5.0", False),
        (f"2.0 BETWEEN 1.0 AND {NAN}", False),
        (f"{NAN} BETWEEN {NAN} AND {NAN}", False),
        (f"{NAN} NOT BETWEEN {NINF} AND {INF}", True),
        (f"{INF} BETWEEN {NINF} AND {INF}", True),
        (f"{NINF} BETWEEN {NINF} AND {INF}", True),
        (f"{INF} BETWEEN 0.0 AND 1.0", False),
        (f"3.0 BETWEEN 3.0 AND 3.0", True),
        (f"5.0 BETWEEN 6.0 AND 1.0", False),
    ],
)
def test_between_float_edges(expr, expected):
    assert one(expr) is expected


@pytest.mark.parametrize(
    "expr, expected",
    [
        (f"{NAN} BETWEEN 1.0 AND NULL", False),
        (f"{NAN} BETWEEN NULL AND 1.0", False),
        (f"{NAN} BETWEEN NULL AND NULL", None),
        (f"2.0 BETWEEN {NAN} AND NULL", False),
        (f"2.0 BETWEEN NULL AND {NAN}", False),
        (f"NULL BETWEEN {NAN} AND 1.0", None),
        (f"CAST(NULL AS FLOAT64) BETWEEN 1.0 AND {NAN}", None),
        ("5 BETWEEN 1 AND NULL", None),
        ("0 BETWEEN 1 AND NULL", False),
        ("5 BETWEEN NULL AND 1", False),
        ("0 NOT BETWEEN 1 AND NULL", True),
        (f"{NAN} NOT BETWEEN 1.0 AND NULL", True),
        ("5 NOT BETWEEN 1 AND NULL", None),
    ],
)
def test_between_with_null_and_nan(expr, expected):
    assert one(expr) is expected


def test_between_matches_pairwise_comparisons():
    arr = f"[-10.0, 0.0, 10.0, {NAN}, {INF}, {NINF}, CAST(NULL AS FLOAT64)]"
    sql = (
        "SELECT x BETWEEN a AND b, x >= a AND x <= b, x NOT BETWEEN a AND b, NOT (x >= a AND x <= b) "
        f"FROM UNNEST({arr}) AS x, UNNEST({arr}) AS a, UNNEST({arr}) AS b"
    )
    rows = evaluate(sql).rows
    assert len(rows) == 7**3
    for between, expanded, not_between, not_expanded in rows:
        assert between == expanded
        assert not_between == not_expanded


@pytest.mark.parametrize(
    "text, expected",
    [
        ("23:59:60", time(0, 0)),
        ("12:59:60", time(13, 0)),
        ("00:00:60", time(0, 1)),
        ("10:20:60.25", time(10, 21)),
        ("10:20:60.123456789", time(10, 21)),
        ("1:2:3", time(1, 2, 3)),
    ],
)
def test_time_string_leap_second(text, expected):
    assert one(f'CAST("{text}" AS TIME)') == expected
    assert one(f'TIME "{text}"') == expected


def test_time_string_rejects_second_61():
    with pytest.raises(EvalError):
        one('CAST("10:20:61" AS TIME)')
    with pytest.raises(EvalError):
        one('CAST("24:00:00" AS TIME)')


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2020-02-28 23:59:60", datetime(2020, 2, 29)),
        ("2021-02-28 23:59:60", datetime(2021, 3, 1)),
        ("2020-12-31 23:59:60", datetime(2021, 1, 1)),
        ("2020-06-15 08:09:60.5", datetime(2020, 6, 15, 8, 10)),
        ("2020-06-15T08:09:10", datetime(2020, 6, 15, 8, 9, 10)),
        ("2020-06-15t08:09:10.25", datetime(2020, 6, 15, 8, 9, 10, 250000)),
    ],
)
def test_datetime_string_leap_second_and_separator(text, expected):
    assert one(f'CAST("{text}" AS DATETIME)') == expected


def test_datetime_leap_second_past_the_end_is_unsupported():
    with pytest.raises(Unsupported):
        one('CAST("9999-12-31 23:59:60" AS DATETIME)')


def test_timestamp_leap_second_keeps_fraction():
    assert one('CAST(TIMESTAMP "2020-02-28 12:59:60.125" AS DATETIME)') == datetime(2020, 2, 28, 13, 0, 0, 125000)
    assert one('CAST(TIMESTAMP "2020-02-28 23:59:60" AS TIME)') == time(0, 0)
    assert one('CAST(TIMESTAMP "2020-02-28 23:59:60+02:00" AS DATETIME)') == datetime(2020, 2, 28, 22, 0)


def test_timestamp_with_lowercase_t_is_not_guessed():
    with pytest.raises(Unsupported):
        one('CAST("2020-02-28t12:00:00" AS TIMESTAMP)')


def test_leap_second_roundtrips():
    assert one('CAST(CAST(DATETIME "2015-11-06 12:59:60.5" AS STRING) AS DATETIME)') == datetime(2015, 11, 6, 13, 0)
    assert one('CAST(CAST(TIME "23:59:60" AS STRING) AS TIME)') == time(0, 0)
    assert one('CAST(CAST(DATETIME "2015-11-06 12:59:60" AS TIMESTAMP) AS DATETIME)') == datetime(2015, 11, 6, 13, 0)
