"""DATE, DATETIME, TIME, TIMESTAMP and INTERVAL functions of the GoogleSQL evaluator (``fn_time`` and ``datetimes``).

Expected values come from BigQuery's documented examples and from the GoogleSQL compliance results
(tests/fixtures/googlesql_conformance, dev split). A case the evaluator cannot answer exactly must raise
``Unsupported``: those are asserted too, because declining is the contract (never an approximation).
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
import sqlglot
from sqlglot import exp

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import datetimes as D
from kumosql.gsql_eval import types as T
from kumosql.gsql_eval import values as V
from kumosql.gsql_eval.functions import NODE_MAP, REGISTRY

UTC = V.zone("UTC")


def value(sql: str, tz: str = "UTC", **tables):
    db = Database({name: Table(cols, rows) for name, (cols, rows) in tables.items()})
    return evaluate("SELECT " + sql, db, time_zone=tz).rows[0][0]


def ts(text: str) -> int:
    return V.parse_timestamp(text, UTC)


def iv(months: int = 0, days: int = 0, micros: int = 0) -> V.Interval:
    return V.Interval(months, days, micros)


def same(sql: str, expected, tz: str = "UTC"):
    got = value(sql, tz)
    assert got == expected and type(got) is type(expected), (sql, got, expected)


def text(sql: str, expected: str, tz: str = "UTC"):
    """The interval, timestamp or civil value printed as GoogleSQL prints it."""

    result = evaluate("SELECT " + sql, time_zone=tz)
    kind = result.columns[0][1]
    assert V.to_string(kind, result.rows[0][0], V.zone(tz)) == expected, sql


def error(sql: str, kind, tz: str = "UTC"):
    with pytest.raises(kind):
        value(sql, tz)


# --- the registry: every call parses to the node its mapper reads ----------------------------------------------------

# NAME -> (SQL of one call, number of argument expressions the mapper returns)
SAMPLES = {
    "CURRENT_DATE": ("CURRENT_DATE('UTC')", 1),
    "CURRENT_DATETIME": ("CURRENT_DATETIME()", 0),
    "CURRENT_TIME": ("CURRENT_TIME()", 0),
    "CURRENT_TIMESTAMP": ("CURRENT_TIMESTAMP()", 0),
    "DATE": ("DATE(a, b, c)", 3),
    "DATETIME": ("DATETIME(a, b)", 2),
    "TIME": ("TIME(a, b, c)", 3),
    "TIMESTAMP": ("TIMESTAMP(a, b)", 2),
    "DATE_ADD": ("DATE_ADD(a, INTERVAL 1 DAY)", 2),
    "DATE_SUB": ("DATE_SUB(a, INTERVAL 1 DAY)", 2),
    "DATETIME_ADD": ("DATETIME_ADD(a, INTERVAL 1 HOUR)", 2),
    "DATETIME_SUB": ("DATETIME_SUB(a, INTERVAL 1 HOUR)", 2),
    "TIME_ADD": ("TIME_ADD(a, INTERVAL 1 HOUR)", 2),
    "TIME_SUB": ("TIME_SUB(a, INTERVAL 1 HOUR)", 2),
    "TIMESTAMP_ADD": ("TIMESTAMP_ADD(a, INTERVAL 1 HOUR)", 2),
    "TIMESTAMP_SUB": ("TIMESTAMP_SUB(a, INTERVAL 1 HOUR)", 2),
    "DATE_DIFF": ("DATE_DIFF(a, b, DAY)", 2),
    "DATETIME_DIFF": ("DATETIME_DIFF(a, b, HOUR)", 2),
    "TIME_DIFF": ("TIME_DIFF(a, b, HOUR)", 2),
    "TIMESTAMP_DIFF": ("TIMESTAMP_DIFF(a, b, HOUR)", 2),
    "DATE_TRUNC": ("DATE_TRUNC(a, MONTH)", 1),
    "DATETIME_TRUNC": ("DATETIME_TRUNC(a, HOUR)", 1),
    "TIME_TRUNC": ("TIME_TRUNC(a, HOUR)", 1),
    "TIMESTAMP_TRUNC": ("TIMESTAMP_TRUNC(a, DAY, b)", 2),
    "LAST_DAY": ("LAST_DAY(a, MONTH)", 1),
    "EXTRACT": ("EXTRACT(HOUR FROM a AT TIME ZONE b)", 2),
    "FORMAT_DATE": ("FORMAT_DATE(a, b)", 2),
    "FORMAT_TIME": ("FORMAT_TIME(a, b)", 2),
    "FORMAT_DATETIME": ("FORMAT_DATETIME(a, b)", 2),
    "FORMAT_TIMESTAMP": ("FORMAT_TIMESTAMP(a, b, c)", 3),
    "PARSE_DATE": ("PARSE_DATE(a, b)", 2),
    "PARSE_TIME": ("PARSE_TIME(a, b)", 2),
    "PARSE_DATETIME": ("PARSE_DATETIME(a, b)", 2),
    "PARSE_TIMESTAMP": ("PARSE_TIMESTAMP(a, b, c)", 3),
    "TIMESTAMP_SECONDS": ("TIMESTAMP_SECONDS(a)", 1),
    "TIMESTAMP_MILLIS": ("TIMESTAMP_MILLIS(a)", 1),
    "TIMESTAMP_MICROS": ("TIMESTAMP_MICROS(a)", 1),
    "UNIX_SECONDS": ("UNIX_SECONDS(a)", 1),
    "UNIX_MILLIS": ("UNIX_MILLIS(a)", 1),
    "UNIX_MICROS": ("UNIX_MICROS(a)", 1),
    "UNIX_DATE": ("UNIX_DATE(a)", 1),
    "DATE_FROM_UNIX_DATE": ("DATE_FROM_UNIX_DATE(a)", 1),
    "STRING": ("STRING(a, b)", 2),
    "GENERATE_DATE_ARRAY": ("GENERATE_DATE_ARRAY(a, b, INTERVAL 1 DAY)", 3),
    "GENERATE_TIMESTAMP_ARRAY": ("GENERATE_TIMESTAMP_ARRAY(a, b, INTERVAL 1 DAY)", 3),
    "JUSTIFY_DAYS": ("JUSTIFY_DAYS(a)", 1),
    "JUSTIFY_HOURS": ("JUSTIFY_HOURS(a)", 1),
    "JUSTIFY_INTERVAL": ("JUSTIFY_INTERVAL(a)", 1),
    "MAKE_INTERVAL": ("MAKE_INTERVAL(a, b, day => c)", 3),
}


def _parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one("SELECT " + sql, read="bigquery").expressions[0]


def _mapped(node: exp.Expression) -> tuple[str, list]:
    return NODE_MAP[type(node)](node)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_call_maps_to_name_and_arguments(name):
    sql, count = SAMPLES[name]
    mapped_name, args = _mapped(_parse(sql))
    assert mapped_name == name and name in REGISTRY
    assert len(args) == count
    assert all(isinstance(a, exp.Expression) for a in args)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_flagged_variant_is_unsupported(name):
    """A mapper reads every argument of its node, so an argument it does not know raises Unsupported."""

    node = _parse(SAMPLES[name][0])
    node.set("unknown_flag", exp.true())
    with pytest.raises(Unsupported):
        NODE_MAP[type(node)](node)


# --- INTERVAL -------------------------------------------------------------------------------------------------------


def test_interval_literals():
    text("INTERVAL '1' YEAR", "1-0 0 0:0:0")
    text("INTERVAL -1 DAY", "0-0 -1 0:0:0")
    text("INTERVAL 5 WEEK", "0-0 35 0:0:0")
    text("INTERVAL 1 QUARTER", "0-3 0 0:0:0")
    text("INTERVAL 1 MILLISECOND", "0-0 0 0:0:0.001")
    text("INTERVAL '6.789' SECOND", "0-0 0 0:0:6.789")
    text("INTERVAL '-10000' MINUTE", "0-0 0 -166:40:0")
    text("INTERVAL '-20 30' MONTH TO DAY", "-1-8 30 0:0:0")
    text("INTERVAL '10:20:30.456789' HOUR TO SECOND", "0-0 0 10:20:30.456789")
    text("INTERVAL '1-2 3 4:5:6.789' YEAR TO SECOND", "1-2 3 4:5:6.789")
    text("INTERVAL '1-2' YEAR TO MONTH", "1-2 0 0:0:0")
    text("INTERVAL '3 4' DAY TO HOUR", "0-0 3 4:0:0")
    text("INTERVAL '4:5' HOUR TO MINUTE", "0-0 0 4:5:0")
    text("INTERVAL '5:6.5' MINUTE TO SECOND", "0-0 0 0:5:6.5")
    text("INTERVAL '1-2 35 49:5:6.789' YEAR TO SECOND", "1-2 35 49:5:6.789")


def test_interval_constructor_from_values():
    rows = [(None,), (0,), (1,), (-2,)]
    db = Database({"t": Table([("n", T.INT64)], rows)})
    out = evaluate("SELECT INTERVAL n YEAR, INTERVAL 10 * n + 5 DAY, INTERVAL 10001 * n SECOND FROM t ORDER BY n", db).rows
    assert out[0] == (None, None, None)
    assert out[1] == (iv(-24), iv(0, -15), iv(0, 0, -20002 * 1_000_000))
    assert out[2] == (iv(0), iv(0, 5), iv(0))
    assert out[3] == (iv(12), iv(0, 15), iv(0, 0, 10001 * 1_000_000))


def test_interval_ranges_and_declined_forms():
    error("INTERVAL 10001 YEAR", EvalError)
    error("MAKE_INTERVAL(year => 10000, month => 1)", EvalError)
    error("INTERVAL 1 DAYOFWEEK", AnalysisError)
    error("INTERVAL '0.123456789' SECOND", Unsupported)
    error("INTERVAL '1-12' YEAR TO MONTH", Unsupported)
    error("INTERVAL '1:75' HOUR TO MINUTE", Unsupported)
    error("INTERVAL 1 NANOSECOND", Unsupported)


def test_interval_arithmetic_and_comparison():
    text("INTERVAL 1 DAY + INTERVAL 2 HOUR", "0-0 1 2:0:0")
    text("INTERVAL 10 YEAR / 5", "2-0 0 0:0:0")
    text("INTERVAL '2' HOUR / 4", "0-0 0 0:30:0")
    text("INTERVAL '0.000008' SECOND / 3", "0-0 0 0:0:0.000002")
    text("INTERVAL '0.000008' SECOND / -3", "0-0 0 -0:0:0.000002")
    error("INTERVAL 1 DAY / 0", EvalError)
    error("INTERVAL 1 MONTH / 4", Unsupported)  # a month remainder carries into days: not a documented rule
    same("INTERVAL 1 YEAR > INTERVAL 360 DAY", False)
    same("INTERVAL 1 DAY = INTERVAL 24 HOUR", True)
    same("INTERVAL 1 MONTH = INTERVAL 30 DAY", True)
    same("INTERVAL 1 HOUR < NULL", None)


def test_dates_and_timestamps_subtract_into_intervals():
    text("DATE '2021-05-20' - DATE '2020-04-19'", "0-0 396 0:0:0")
    text("TIMESTAMP '2021-06-01 12:34:56.789+00' - TIMESTAMP '2021-05-31 00:00:00+00'", "0-0 0 36:34:56.789")
    error("DATETIME '2021-05-20 00:00:00' - DATETIME '2020-04-19 00:00:00'", Unsupported)


def test_adding_intervals_to_dates_and_datetimes():
    text("DATE '2010-10-10' + INTERVAL '10 20:20:20' DAY TO SECOND", "2010-10-20 20:20:20")
    text("INTERVAL '24' HOUR + DATE '1999-12-31'", "2000-01-01 00:00:00")
    text("DATETIME '1970-01-02 03:04:05.678' + INTERVAL '1-1 1 1:1:1.1111' YEAR TO SECOND", "1971-02-03 04:05:06.789100")
    text("INTERVAL 1 YEAR + DATETIME '0201-02-02 02:02:02'", "0202-02-02 02:02:02")
    text("DATE '2010-10-10' - INTERVAL '10 1:1:1' DAY TO SECOND", "2010-09-29 22:58:59")
    text("DATETIME '2020-01-31 10:00:00' + INTERVAL 1 MONTH", "2020-02-29 10:00:00")
    error("DATE '9999-12-31' + INTERVAL 1 DAY", EvalError)


def test_interval_helpers_directly():
    assert D.datetime_add_interval(date(2020, 1, 31), iv(1, 1, 3_600_000_000)) == datetime(2020, 3, 1, 1, 0)
    # a month, then a day, then the time: the day of the month is clamped before the days are added
    assert D.datetime_add_interval(datetime(2020, 1, 31), iv(1, 1)) == datetime(2020, 3, 1)
    assert D.timestamp_add_interval(ts("2020-01-01 00:00:00+00"), iv(0, 0, 5_000_000)) == ts("2020-01-01 00:00:05+00")
    with pytest.raises(Unsupported):
        D.timestamp_add_interval(ts("2020-01-01 00:00:00+00"), iv(0, 1))  # a calendar day needs the time zone
    assert D.timestamp_add_interval(ts("2020-01-31 12:00:00+00"), iv(1, 1), UTC) == ts("2020-03-01 12:00:00+00")
    la = V.zone("America/Los_Angeles")
    # a plain day across no transition is exact in a zone with daylight saving time; across the transition it declines
    assert D.timestamp_add_interval(ts("2020-01-10 12:00:00+00"), iv(0, 1), la) == ts("2020-01-11 12:00:00+00")
    with pytest.raises(Unsupported):
        D.timestamp_add_interval(ts("2020-03-07 12:00:00+00"), iv(0, 1), la)
    with pytest.raises(Unsupported):
        D.timestamp_add_interval(ts("2020-01-10 12:00:00+00"), iv(1), la)
    fixed = V.zone("+05:30")
    assert D.timestamp_add_interval(ts("2020-01-31 20:00:00+00"), iv(1), fixed) == ts("2020-02-29 20:00:00+00")


def test_cast_of_text_to_interval_helper():
    assert D.interval_from_string("-1-2 -3 -4:5:6.789") == iv(-14, -3, -(4 * 3600 + 5 * 60) * 1_000_000 - 6_789_000)
    assert D.interval_from_string("P1Y2M3DT4H5M6.789S") == iv(14, 3, (4 * 3600 + 5 * 60) * 1_000_000 + 6_789_000)
    assert D.interval_from_string("PT1H2M") == iv(0, 0, 3_720_000_000)
    assert D.interval_from_string("1-2") == iv(14)
    for bad in ("1", "P1", "P-", "P-aY2M3D", "P1Y2M<3D"):
        with pytest.raises(EvalError):
            D.interval_from_string(bad)
    with pytest.raises(Unsupported):
        D.interval_from_string("P1Y2M3DT4H5M6.123456789S")


def test_justify():
    text("JUSTIFY_DAYS(INTERVAL 29 DAY)", "0-0 29 0:0:0")
    text("JUSTIFY_DAYS(INTERVAL -30 DAY)", "-0-1 0 0:0:0")
    text("JUSTIFY_DAYS(INTERVAL 31 DAY)", "0-1 1 0:0:0")
    text("JUSTIFY_DAYS(INTERVAL -65 DAY)", "-0-2 -5 0:0:0")
    text("JUSTIFY_DAYS(INTERVAL 370 DAY)", "1-0 10 0:0:0")
    text("JUSTIFY_HOURS(INTERVAL 23 HOUR)", "0-0 0 23:0:0")
    text("JUSTIFY_HOURS(INTERVAL -24 HOUR)", "0-0 -1 0:0:0")
    text("JUSTIFY_HOURS(INTERVAL 47 HOUR)", "0-0 1 23:0:0")
    text("JUSTIFY_HOURS(INTERVAL -12345 MINUTE)", "0-0 -8 -13:45:0")
    text("JUSTIFY_INTERVAL(INTERVAL '29 49:00:00' DAY TO SECOND)", "0-1 1 1:0:0")
    # the compliance interval cases: signs made to agree
    text("JUSTIFY_INTERVAL(INTERVAL '1-2 -35 4:5:6.789' YEAR TO SECOND)", "1-0 25 4:5:6.789")
    text("JUSTIFY_INTERVAL(INTERVAL '1-2 -12 -40:5:6.789' YEAR TO SECOND)", "1-1 16 7:54:53.211")
    text("JUSTIFY_INTERVAL(INTERVAL '1-2 -48 -75:7:4.657' YEAR TO SECOND)", "1-0 8 20:52:55.343")
    text("JUSTIFY_DAYS(INTERVAL '1-2 -35 -48:5:6.789' YEAR TO SECOND)", "1-0 25 -48:5:6.789")
    text("JUSTIFY_HOURS(INTERVAL '1-2 35 49:5:6.789' YEAR TO SECOND)", "1-2 37 1:5:6.789")
    same("JUSTIFY_DAYS(CAST(NULL AS INTERVAL))", None)
    error("JUSTIFY_DAYS(1)", AnalysisError)
    # whether a month borrows for a pure time remainder is not documented
    error("JUSTIFY_INTERVAL(INTERVAL 1 MONTH - INTERVAL 1 HOUR)", Unsupported)


def test_make_interval():
    text("MAKE_INTERVAL(1, 6, 15)", "1-6 15 0:0:0")
    text("MAKE_INTERVAL(1, -2, 3, -4, 5, -6)", "0-10 3 -3:55:6")
    text("MAKE_INTERVAL(hour => 10, second => 20)", "0-0 0 10:0:20")
    text("MAKE_INTERVAL(second => -6, minute => 5, hour => -4, day => 3, month => -2, year => 1)", "0-10 3 -3:55:6")
    text("MAKE_INTERVAL()", "0-0 0 0:0:0")
    text("MAKE_INTERVAL(1, day => 3)", "1-0 3 0:0:0")
    same("MAKE_INTERVAL(NULL)", None)
    same("MAKE_INTERVAL(1, 2, 3, 4, 5, NULL)", None)
    error("MAKE_INTERVAL(week => 1)", Unsupported)


def test_extract_from_interval():
    row = evaluate(
        "SELECT EXTRACT(YEAR FROM i), EXTRACT(MONTH FROM i), EXTRACT(DAY FROM i), EXTRACT(HOUR FROM i), "
        "EXTRACT(MINUTE FROM i), EXTRACT(SECOND FROM i), EXTRACT(MILLISECOND FROM i), EXTRACT(MICROSECOND FROM i) "
        "FROM (SELECT INTERVAL '1-0 25 -48:5:6.789' YEAR TO SECOND AS i)"
    ).rows[0]
    assert row == (1, 0, 25, -48, -5, -6, -789, -789000)
    same("EXTRACT(MONTH FROM INTERVAL -20 MONTH)", -8)
    same("EXTRACT(YEAR FROM INTERVAL -20 MONTH)", -1)
    error("EXTRACT(DAYOFWEEK FROM INTERVAL 1 DAY)", AnalysisError)
    error("EXTRACT(NANOSECOND FROM INTERVAL 1 DAY)", Unsupported)


# --- constructors ---------------------------------------------------------------------------------------------------


def test_date_constructors():
    same("DATE(2016, 12, 25)", date(2016, 12, 25))
    same("DATE(NULL, 1, 1)", None)
    error("DATE(2016, 13, 25)", EvalError)
    error("DATE(0, 1, 1)", EvalError)
    same("SAFE.DATE(2016, 13, 25)", None)
    same("DATE(TIMESTAMP '2016-12-25 05:30:00+07', 'America/Los_Angeles')", date(2016, 12, 24))
    same("DATE(TIMESTAMP '2016-12-25 20:30:00+00')", date(2016, 12, 26), tz="Asia/Tokyo")
    same("DATE(DATETIME '2016-12-25 23:59:59')", date(2016, 12, 25))
    # a string literal reads as a TIMESTAMP in the session time zone
    same("DATE('2020-09-01 12:34:56.789-08')", date(2020, 9, 1))
    same("DATE('2019-08-01 03:00:00+00')", date(2019, 7, 31), tz="America/Los_Angeles")
    same("DATE('2019-08-01 03:00:00')", date(2019, 8, 1), tz="America/Los_Angeles")
    same("DATE(CAST(NULL AS TIMESTAMP))", None)
    error("DATE(1)", AnalysisError)
    error("DATE(DATETIME '2016-12-25 23:59:59', 'UTC')", AnalysisError)


def test_date_of_a_string_column_is_declined():
    db = Database({"t": Table([("s", T.STRING)], [("2020-01-01",)])})
    with pytest.raises(Unsupported):
        evaluate("SELECT DATE(s) FROM t", db)


def test_datetime_constructors():
    same("DATETIME(2008, 12, 25, 5, 30, 0)", datetime(2008, 12, 25, 5, 30))
    same("DATETIME(DATE '2008-12-25', TIME '05:30:00')", datetime(2008, 12, 25, 5, 30))
    same("DATETIME(DATE '2008-12-25')", datetime(2008, 12, 25))
    same("DATETIME(TIMESTAMP '2008-12-25 05:30:00+00', 'America/Los_Angeles')", datetime(2008, 12, 24, 21, 30))
    same("DATETIME(TIMESTAMP '2008-12-25 05:30:00+00')", datetime(2008, 12, 25, 5, 30))
    error("DATETIME(2008, 13, 25, 5, 30, 0)", EvalError)
    error("DATETIME('2008-12-25 05:30:00')", Unsupported)
    error("DATETIME(DATETIME '2008-12-25 05:30:00')", Unsupported)


def test_time_constructors():
    same("TIME(15, 30, 0)", time(15, 30))
    same("TIME(TIMESTAMP '2008-12-25 15:30:00+00', 'America/Los_Angeles')", time(7, 30))
    same("TIME(DATETIME '2008-12-25 15:30:00.123')", time(15, 30, 0, 123000))
    error("TIME(24, 0, 0)", EvalError)
    error("TIME('15:30:00')", Unsupported)


def test_timestamp_constructors():
    same("TIMESTAMP('2008-12-25')", ts("2008-12-25 00:00:00+00"))
    same("TIMESTAMP('2008-12-25 05:30:00+00', 'America/Los_Angeles')", ts("2008-12-25 05:30:00+00"))
    same("TIMESTAMP('2008-12-25 05:30:00', 'America/Los_Angeles')", ts("2008-12-25 13:30:00+00"))
    same("TIMESTAMP(DATE '2008-12-25', 'America/Los_Angeles')", ts("2008-12-25 08:00:00+00"))
    same("TIMESTAMP(DATETIME '2008-12-25 05:30:00')", ts("2008-12-25 05:30:00+00"))
    same("TIMESTAMP(DATE '2014-01-31')", ts("2014-01-30 10:15:00+00"), tz="Pacific/Chatham")
    error("TIMESTAMP('garbage')", EvalError)
    error("TIMESTAMP('2008-12-25', 'No/Such_Zone')", EvalError)


def test_current_functions_are_nondeterministic():
    result = evaluate("SELECT CURRENT_DATE() = CURRENT_DATE, CURRENT_TIMESTAMP() = CURRENT_TIMESTAMP(), CURRENT_TIME() IS NOT NULL")
    assert result.rows == [(True, True, True)] and result.deterministic is False
    same("CURRENT_DATE(CAST(NULL AS STRING))", None)
    error("CURRENT_DATE('foo')", EvalError)
    same("ABS(DATE_DIFF(CURRENT_DATE('+05'), CURRENT_DATE('-06'), DAY)) IN (0, 1)", True)
    assert isinstance(value("CURRENT_DATETIME()"), datetime)
    assert isinstance(value("CURRENT_TIME('Asia/Tokyo')"), time)


# --- adding, subtracting and comparing parts --------------------------------------------------------------------------


def test_date_add_and_sub():
    same("DATE_ADD(DATE '2008-12-25', INTERVAL 5 DAY)", date(2008, 12, 30))
    same("DATE_SUB(DATE '2008-12-25', INTERVAL 5 DAY)", date(2008, 12, 20))
    same("DATE_ADD(DATE '2020-01-31', INTERVAL 1 MONTH)", date(2020, 2, 29))
    same("DATE_ADD(DATE '2020-02-29', INTERVAL 1 YEAR)", date(2021, 2, 28))
    same("DATE_ADD(DATE '2020-02-29', INTERVAL 1 QUARTER)", date(2020, 5, 29))
    same("DATE_ADD(DATE '2020-02-29', INTERVAL 2 WEEK)", date(2020, 3, 14))
    same("DATE_ADD(DATE '2020-01-01', INTERVAL -1 DAY)", date(2019, 12, 31))
    same("DATE_ADD('2020-01-01', INTERVAL 1 DAY)", date(2020, 1, 2))
    same("DATE_ADD(NULL, INTERVAL 1 DAY)", None)
    same("DATE_ADD(DATE '2020-01-01', INTERVAL NULL DAY)", None)
    error("DATE_ADD(DATE '9999-12-31', INTERVAL 1 DAY)", EvalError)
    error("DATE_SUB(DATE '0001-01-01', INTERVAL 1 DAY)", EvalError)
    error("DATE_ADD(DATE '2020-01-01', INTERVAL 10000000 YEAR)", EvalError)
    same("SAFE.DATE_ADD(DATE '9999-12-31', INTERVAL 1 DAY)", None)
    error("DATE_ADD(DATE '2020-01-01', INTERVAL 1 HOUR)", AnalysisError)
    error("DATE_ADD(DATE '2020-01-01', INTERVAL 1 ISOWEEK)", AnalysisError)
    error("DATE_ADD(DATE '2020-01-01', INTERVAL 1.5 DAY)", AnalysisError)
    error("DATE_ADD(1, INTERVAL 1 DAY)", AnalysisError)
    error("DATE_ADD(DATETIME '2020-01-01 00:00:00', INTERVAL 1 DAY)", Unsupported)


def test_datetime_add_and_sub():
    same("DATETIME_ADD(DATETIME '2008-12-25 15:30:00', INTERVAL 10 MINUTE)", datetime(2008, 12, 25, 15, 40))
    same("DATETIME_SUB(DATETIME '2008-12-25 15:30:00', INTERVAL 10 MINUTE)", datetime(2008, 12, 25, 15, 20))
    same("DATETIME_ADD(DATETIME '2020-01-31 15:30:00', INTERVAL 1 MONTH)", datetime(2020, 2, 29, 15, 30))
    same("DATETIME_ADD(DATETIME '2020-01-31 15:30:00', INTERVAL 1 DAY)", datetime(2020, 2, 1, 15, 30))
    same("DATETIME_ADD(DATE '2020-01-31', INTERVAL 1 HOUR)", datetime(2020, 1, 31, 1))
    same("DATETIME_ADD(DATETIME '2020-01-01 00:00:00', INTERVAL 1500 MILLISECOND)", datetime(2020, 1, 1, 0, 0, 1, 500000))
    error("DATETIME_ADD(DATETIME '9999-12-31 23:59:59.999999', INTERVAL 1 MICROSECOND)", EvalError)
    error("DATETIME_ADD(TIMESTAMP '2020-01-01 00:00:00+00', INTERVAL 1 HOUR)", Unsupported)


def test_time_add_and_sub_wrap_around():
    same("TIME_ADD(TIME '15:30:00', INTERVAL 10 MINUTE)", time(15, 40))
    same("TIME_ADD(TIME '23:30:00', INTERVAL 60 MINUTE)", time(0, 30))
    same("TIME_SUB(TIME '00:30:00', INTERVAL 60 MINUTE)", time(23, 30))
    same("TIME_ADD(TIME '00:00:00', INTERVAL 25 HOUR)", time(1))
    error("TIME_ADD(TIME '00:00:00', INTERVAL 1 DAY)", AnalysisError)


def test_timestamp_add_and_sub():
    text("TIMESTAMP_ADD(TIMESTAMP '2008-12-25 15:30:00+00', INTERVAL 10 MINUTE)", "2008-12-25 15:40:00+00")
    text("TIMESTAMP_SUB(TIMESTAMP '2008-12-25 15:30:00+00', INTERVAL 10 MINUTE)", "2008-12-25 15:20:00+00")
    # a DAY of a TIMESTAMP is 24 hours, also across a daylight saving transition
    text("TIMESTAMP_ADD(TIMESTAMP '2020-03-07 12:00:00+00', INTERVAL 1 DAY)", "2020-03-08 05:00:00-07", tz="America/Los_Angeles")
    text("TIMESTAMP_ADD(TIMESTAMP '2020-03-08 12:00:00+00', INTERVAL 1 DAY)", "2020-03-09 05:00:00-07", tz="America/Los_Angeles")
    error("TIMESTAMP_ADD(TIMESTAMP '2008-12-25 15:30:00+00', INTERVAL 1 MONTH)", AnalysisError)
    error("TIMESTAMP_ADD(TIMESTAMP '9999-12-31 23:59:59+00', INTERVAL 1 DAY)", EvalError)
    error("TIMESTAMP_ADD(DATE '2020-01-01', INTERVAL 1 DAY)", Unsupported)


def test_date_diff():
    same("DATE_DIFF(DATE '2010-07-07', DATE '2008-12-25', DAY)", 559)
    same("DATE_DIFF(DATE '2010-07-07', DATE '2008-12-25', MONTH)", 19)
    same("DATE_DIFF(DATE '2010-07-07', DATE '2008-12-25', QUARTER)", 7)
    same("DATE_DIFF(DATE '2010-07-07', DATE '2008-12-25', YEAR)", 2)
    same("DATE_DIFF(DATE '2008-12-25', DATE '2010-07-07', DAY)", -559)
    # boundaries, not whole units
    same("DATE_DIFF(DATE '2017-10-15', DATE '2017-10-14', WEEK)", 1)
    same("DATE_DIFF(DATE '2017-10-15', DATE '2017-10-14', WEEK(MONDAY))", 0)
    same("DATE_DIFF(DATE '2017-10-15', DATE '2017-10-14', ISOWEEK)", 0)
    same("DATE_DIFF(DATE '2017-12-30', DATE '2014-12-30', ISOYEAR)", 2)
    same("DATE_DIFF(DATE '2017-05-22', DATE '2017-05-19', WEEK)", 1)
    same("DATE_DIFF(DATE '2000-01-01', DATE '2001-01-01', WEEK)", -53)
    same("DATE_DIFF(DATE '2013-01-01', DATE '2012-12-31', YEAR)", 1)
    same("DATE_DIFF(NULL, DATE '2012-12-31', YEAR)", None)
    error("DATE_DIFF(DATE '2013-01-01', DATE '2012-12-31', HOUR)", AnalysisError)
    error("DATE_DIFF(DATE '2013-01-01', DATE '2012-12-31', DAYOFWEEK)", AnalysisError)


def test_datetime_diff_counts_boundaries():
    same("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00', DATETIME '2008-12-25 15:30:00', DAY)", 559)
    same("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00', DATETIME '2010-07-07 09:59:59', HOUR)", 1)
    same("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00', DATETIME '2010-07-07 09:59:59', MINUTE)", 21)
    same("DATETIME_DIFF(DATETIME '2010-07-07 09:59:59', DATETIME '2010-07-07 10:20:00', HOUR)", -1)
    same("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00.5', DATETIME '2010-07-07 10:20:00.4', MILLISECOND)", 100)
    same("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00', DATETIME '2010-01-31 10:20:00', MONTH)", 6)
    error("DATETIME_DIFF(DATETIME '2010-07-07 10:20:00', DATETIME '2010-01-31 10:20:00', DAYOFYEAR)", AnalysisError)


def test_time_diff():
    same("TIME_DIFF(TIME '15:30:00', TIME '14:35:00', MINUTE)", 55)
    same("TIME_DIFF(TIME '14:35:00', TIME '15:30:00', MINUTE)", -55)
    same("TIME_DIFF(TIME '15:35:00', TIME '14:35:00', HOUR)", 1)
    # boundaries and whole units disagree here, and which one BigQuery uses is not settled
    error("TIME_DIFF(TIME '15:30:00', TIME '14:35:00', HOUR)", Unsupported)
    error("TIME_DIFF(TIME '15:30:00', TIME '14:35:00', DAY)", AnalysisError)


def test_timestamp_diff_counts_whole_units():
    same("TIMESTAMP_DIFF(TIMESTAMP '2010-07-07 10:20:00+00', TIMESTAMP '2008-12-25 15:30:00+00', HOUR)", 13410)
    same("TIMESTAMP_DIFF(TIMESTAMP '2008-12-25 15:30:00+00', TIMESTAMP '2010-07-07 10:20:00+00', HOUR)", -13410)
    same("TIMESTAMP_DIFF(TIMESTAMP '2010-07-07 10:20:00+00', TIMESTAMP '2008-12-25 15:30:00+00', DAY)", 558)
    same("TIMESTAMP_DIFF(TIMESTAMP '2014-12-26', TIMESTAMP '2014-12-25', HOUR)", 24)
    same("TIMESTAMP_DIFF(TIMESTAMP '2001-02-01 01:00:00+00', TIMESTAMP '2001-02-01 00:00:01+00', MINUTE)", 59)
    error("TIMESTAMP_DIFF(TIMESTAMP '2014-12-26', TIMESTAMP '2014-12-25', WEEK)", AnalysisError)
    error("TIMESTAMP_DIFF(TIMESTAMP '2014-12-26', TIMESTAMP '2014-12-25', MONTH)", AnalysisError)


# --- truncation ----------------------------------------------------------------------------------------------------------


def test_date_trunc():
    same("DATE_TRUNC(DATE '2008-12-25', MONTH)", date(2008, 12, 1))
    same("DATE_TRUNC(DATE '2008-12-25', YEAR)", date(2008, 1, 1))
    same("DATE_TRUNC(DATE '2008-12-25', QUARTER)", date(2008, 10, 1))
    same("DATE_TRUNC(DATE '2008-12-25', WEEK)", date(2008, 12, 21))
    same("DATE_TRUNC(DATE '2008-12-25', WEEK(MONDAY))", date(2008, 12, 22))
    same("DATE_TRUNC(DATE '2008-12-25', WEEK(FRIDAY))", date(2008, 12, 19))
    same("DATE_TRUNC(DATE '2008-12-25', ISOWEEK)", date(2008, 12, 22))
    same("DATE_TRUNC(DATE '2015-06-15', ISOYEAR)", date(2014, 12, 29))
    same("DATE_TRUNC(DATE '2008-12-25', DAY)", date(2008, 12, 25))
    error("DATE_TRUNC(DATE '0001-01-01', WEEK)", EvalError)
    error("DATE_TRUNC(DATE '2008-12-25', HOUR)", AnalysisError)
    error("DATE_TRUNC(TIMESTAMP '2008-12-25 00:00:00+00', MONTH)", Unsupported)
    same("DATE_TRUNC(NULL, MONTH)", None)


def test_datetime_and_time_trunc():
    same("DATETIME_TRUNC(DATETIME '2008-12-25 15:30:45.123456', HOUR)", datetime(2008, 12, 25, 15))
    same("DATETIME_TRUNC(DATETIME '2008-12-25 15:30:45.123456', MILLISECOND)", datetime(2008, 12, 25, 15, 30, 45, 123000))
    same("DATETIME_TRUNC(DATETIME '2008-12-25 15:30:45.123456', DAY)", datetime(2008, 12, 25))
    same("DATETIME_TRUNC(DATETIME '2008-12-25 15:30:45.123456', MONTH)", datetime(2008, 12, 1))
    same("DATETIME_TRUNC(DATETIME '2008-12-25 15:30:45', WEEK(MONDAY))", datetime(2008, 12, 22))
    same("DATETIME_TRUNC(DATE '2008-12-25', MONTH)", datetime(2008, 12, 1))
    same("TIME_TRUNC(TIME '15:30:45.123456', MINUTE)", time(15, 30))
    same("TIME_TRUNC(TIME '15:30:45.123456', MICROSECOND)", time(15, 30, 45, 123456))
    error("TIME_TRUNC(TIME '15:30:45', DAY)", AnalysisError)


def test_timestamp_trunc_works_in_civil_time_of_the_zone():
    ts_sql = "TIMESTAMP '2000-02-03 04:05:06.789+00'"
    text(f"TIMESTAMP_TRUNC({ts_sql}, SECOND)", "2000-02-03 04:05:06+00")
    text(f"TIMESTAMP_TRUNC({ts_sql}, DAY)", "2000-02-03 00:00:00+00")
    text(f"TIMESTAMP_TRUNC({ts_sql}, DAY, 'America/Los_Angeles')", "2000-02-02 08:00:00+00")
    text(f"TIMESTAMP_TRUNC({ts_sql}, HOUR, 'Asia/Kolkata')", "2000-02-03 03:30:00+00")
    text(f"TIMESTAMP_TRUNC({ts_sql}, WEEK(MONDAY))", "2000-01-31 00:00:00+00")
    text(f"TIMESTAMP_TRUNC({ts_sql}, YEAR)", "2000-01-01 00:00:00+00")
    # the compliance default-time-zone cases: the offset +13:45 makes the hour and the day local
    chatham = "Pacific/Chatham"
    text("TIMESTAMP_TRUNC('2000-02-03 04:05:06.789', HOUR)", "2000-02-03 04:00:00+13:45", tz=chatham)
    text("TIMESTAMP_TRUNC('2000-02-03 04:05:06.789', DAY)", "2000-02-03 00:00:00+13:45", tz=chatham)
    text("TIMESTAMP_TRUNC('2000-02-03 04:05:06.789', MONTH)", "2000-02-01 00:00:00+13:45", tz=chatham)
    same("TIMESTAMP_TRUNC(NULL, DAY)", None)
    same("TIMESTAMP_TRUNC(TIMESTAMP '2000-02-03 04:05:06+00', DAY, CAST(NULL AS STRING))", None)
    error("TIMESTAMP_TRUNC(TIMESTAMP '2000-02-03 04:05:06+00', DAY, 'No/Such_Zone')", EvalError)
    error("TIMESTAMP_TRUNC(DATETIME '2000-02-03 04:05:06', DAY)", Unsupported)


def test_last_day():
    same("LAST_DAY(DATE '2008-11-25')", date(2008, 11, 30))
    same("LAST_DAY(DATE '2020-02-10', MONTH)", date(2020, 2, 29))
    same("LAST_DAY(DATE '2008-11-25', YEAR)", date(2008, 12, 31))
    same("LAST_DAY(DATE '2008-11-25', QUARTER)", date(2008, 12, 31))
    same("LAST_DAY(DATE '2008-11-25', WEEK)", date(2008, 11, 29))
    same("LAST_DAY(DATE '2008-11-25', WEEK(MONDAY))", date(2008, 11, 30))
    same("LAST_DAY(DATE '2008-11-25', ISOWEEK)", date(2008, 11, 30))
    same("LAST_DAY(DATE '2008-11-25', ISOYEAR)", date(2008, 12, 28))
    same("LAST_DAY(DATETIME '2008-11-25 10:00:00')", date(2008, 11, 30))
    same("LAST_DAY(NULL)", None)
    error("LAST_DAY(DATE '9999-12-31', ISOYEAR)", EvalError)
    error("LAST_DAY(DATE '2008-11-25', DAY)", AnalysisError)
    error("LAST_DAY(DATE '2008-11-25', 'x')", AnalysisError)


# --- EXTRACT -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "part, expected",
    [("YEAR", 2008), ("ISOYEAR", 2008), ("QUARTER", 4), ("MONTH", 12), ("WEEK", 51), ("WEEK(MONDAY)", 51), ("ISOWEEK", 52),
     ("DAY", 25), ("DAYOFWEEK", 5), ("DAYOFYEAR", 360)],
)
def test_extract_date_parts(part, expected):
    same(f"EXTRACT({part} FROM DATE '2008-12-25')", expected)


def test_extract_week_numbering():
    same("EXTRACT(WEEK FROM DATE '2017-01-01')", 1)  # the first Sunday starts week 1
    same("EXTRACT(WEEK FROM DATE '2016-01-01')", 0)  # days before it are in week 0
    same("EXTRACT(WEEK(MONDAY) FROM DATE '2017-01-02')", 1)
    same("EXTRACT(WEEK(MONDAY) FROM DATE '2017-01-01')", 0)
    same("EXTRACT(ISOWEEK FROM DATE '2008-12-29')", 1)
    same("EXTRACT(ISOYEAR FROM DATE '2008-12-29')", 2009)
    same("EXTRACT(DAYOFWEEK FROM DATE '2008-12-28')", 1)  # Sunday is 1
    same("EXTRACT(DAYOFWEEK FROM DATE '2008-12-27')", 7)


def test_extract_from_timestamp_datetime_and_time():
    stamp = "TIMESTAMP '2008-12-25 15:30:45.123456+00'"
    same(f"EXTRACT(HOUR FROM {stamp})", 15)
    same(f"EXTRACT(MINUTE FROM {stamp})", 30)
    same(f"EXTRACT(SECOND FROM {stamp})", 45)
    same(f"EXTRACT(MILLISECOND FROM {stamp})", 123)
    same(f"EXTRACT(MICROSECOND FROM {stamp})", 123456)
    same(f"EXTRACT(HOUR FROM {stamp} AT TIME ZONE 'America/Los_Angeles')", 7)
    same(f"EXTRACT(DATE FROM {stamp} AT TIME ZONE 'Asia/Tokyo')", date(2008, 12, 26))
    same(f"EXTRACT(TIME FROM {stamp} AT TIME ZONE 'Asia/Tokyo')", time(0, 30, 45, 123456))
    same(f"EXTRACT(DATETIME FROM {stamp} AT TIME ZONE 'Asia/Tokyo')", datetime(2008, 12, 26, 0, 30, 45, 123456))
    same(f"EXTRACT(DAY FROM {stamp})", 26, tz="Asia/Tokyo")
    same("EXTRACT(DATE FROM DATETIME '2008-12-25 15:30:00')", date(2008, 12, 25))
    same("EXTRACT(TIME FROM DATETIME '2008-12-25 15:30:00')", time(15, 30))
    same("EXTRACT(MINUTE FROM TIME '15:30:45')", 30)
    same("EXTRACT(YEAR FROM CAST(NULL AS DATE))", None)
    error("EXTRACT(HOUR FROM DATE '2008-12-25')", AnalysisError)
    error("EXTRACT(DATE FROM DATE '2008-12-25')", AnalysisError)
    error("EXTRACT(DAY FROM TIME '15:30:45')", AnalysisError)
    error("EXTRACT(D FROM DATE '2008-12-25')", AnalysisError)
    error("EXTRACT(HOUR FROM DATETIME '2008-12-25 15:30:00' AT TIME ZONE 'UTC')", AnalysisError)
    error("EXTRACT(YEAR FROM NULL)", Unsupported)
    error("EXTRACT(YEAR FROM '2008-12-25')", Unsupported)


def test_extract_with_a_zone_per_row():
    db = Database({"z": Table([("tz", T.STRING)], [("Asia/Dubai",), (None,), ("UTC+1234",), ("Pacific/Honolulu",)])})
    rows = evaluate("SELECT tz, EXTRACT(DATE FROM TIMESTAMP '2000-01-01 00:00:00+00' AT TIME ZONE tz) FROM z", db).rows
    assert rows == [("Asia/Dubai", date(2000, 1, 1)), (None, None), ("UTC+1234", date(2000, 1, 1)),
                    ("Pacific/Honolulu", date(1999, 12, 31))]
    rows = evaluate("SELECT tz, STRING(TIMESTAMP '2014-02-28 10:20:30+00', tz) FROM z", db).rows
    assert rows[0][1] == "2014-02-28 14:20:30+04" and rows[1][1] is None and rows[2][1] == "2014-02-28 22:54:30+12:34"


# --- FORMAT_* ---------------------------------------------------------------------------------------------------------------


def test_format_date():
    same("FORMAT_DATE('%Y-%m-%d', DATE '2008-12-25')", "2008-12-25")
    same("FORMAT_DATE('%x', DATE '2008-12-25')", "12/25/08")
    same("FORMAT_DATE('%b-%d-%Y', DATE '2008-12-25')", "Dec-25-2008")
    same("FORMAT_DATE('%A, %B %e %Y', DATE '2008-12-05')", "Friday, December  5 2008")
    same("FORMAT_DATE('%j %U %W %u %w %V %G %g %C %y %Q', DATE '2008-12-25')", "360 51 51 4 4 52 2008 08 20 08 4")
    same("FORMAT_DATE('%F %D %h', DATE '2008-12-25')", "2008-12-25 12/25/08 Dec")
    same("FORMAT_DATE('100%%', DATE '2008-12-25')", "100%")
    same("FORMAT_DATE('%E4Y', DATE '0005-12-25')", "0005")
    same("FORMAT_DATE('%Q', NULL)", None)
    same("FORMAT_DATE(NULL, DATE '2008-12-25')", None)
    error("FORMAT_DATE('%H', DATE '2008-12-25')", Unsupported)  # a time element on a DATE: BigQuery's result is not settled
    error("FORMAT_DATE('%Y', DATE '0005-12-25')", Unsupported)  # padding of a short year is not documented
    error("FORMAT_DATE('%i', DATE '2008-12-25')", Unsupported)
    error("FORMAT_DATE('%', DATE '2008-12-25')", Unsupported)
    error("FORMAT_DATE('%Y', DATETIME '2008-12-25 00:00:00')", Unsupported)
    error("FORMAT_DATE('%Y', 1)", AnalysisError)


def test_format_time_and_datetime():
    same("FORMAT_TIME('%R', TIME '15:30:00')", "15:30")
    same("FORMAT_TIME('%T|%I %p %P|%l|%k', TIME '15:30:01')", "15:30:01|03 PM pm| 3|15")
    same("FORMAT_TIME('%H:%M:%E3S|%E6S|%E1f', TIME '15:30:01.123456')", "15:30:01.123|01.123456|1")
    same("FORMAT_TIME('%H:%M:%E6S', TIME '15:30:01.5')", "15:30:01.500000")
    same("FORMAT_TIME('%I %p', TIME '00:05:00')", "12 AM")
    same("FORMAT_DATETIME('%c', DATETIME '2008-12-25 15:30:00')", "Thu Dec 25 15:30:00 2008")
    same("FORMAT_DATETIME('%D %T', DATETIME '2008-12-25 15:30:00')", "12/25/08 15:30:00")
    same("FORMAT_DATETIME('%Y%m%d %H%M%S', DATETIME '2008-12-25 15:30:00')", "20081225 153000")
    same("FORMAT_DATETIME('%d', DATE '2008-12-25')", "25")
    error("FORMAT_TIME('%Y', TIME '15:30:00')", Unsupported)
    error("FORMAT_TIME('%E*S', TIME '15:30:00')", Unsupported)
    error("FORMAT_DATETIME('%Z', DATETIME '2008-12-25 15:30:00')", Unsupported)


def test_format_timestamp():
    stamp = "TIMESTAMP '2050-12-25 15:30:55+00'"
    same(f"FORMAT_TIMESTAMP('%c', {stamp})", "Sun Dec 25 15:30:55 2050")
    same(f"FORMAT_TIMESTAMP('%b-%d-%Y', {stamp})", "Dec-25-2050")
    same(f"FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S %Z %z %Ez %s', {stamp})", "2050-12-25 15:30:55 UTC +0000 +00:00 2555595055")
    same(f"FORMAT_TIMESTAMP('%H %Z %z %Ez', {stamp}, 'America/Los_Angeles')", "07 PST -0800 -08:00")
    same(f"FORMAT_TIMESTAMP('%y', {stamp}, 'Pacific/Honolulu')", "50")
    same("FORMAT_TIMESTAMP('%y', TIMESTAMP '2000-01-01 00:00:00+00', 'Pacific/Honolulu')", "99")
    same(f"FORMAT_TIMESTAMP('%H:%M', {stamp})", "07:30", tz="America/Los_Angeles")
    same("FORMAT_TIMESTAMP('%E3S', TIMESTAMP '2000-01-01 00:00:01.0125+00')", "01.012")
    error("FORMAT_TIMESTAMP('%Z', TIMESTAMP '2000-01-01 00:00:00+00', '+05:30')", Unsupported)
    error("FORMAT_TIMESTAMP('%Z', TIMESTAMP '2000-01-01 00:00:00+00', 'No/Such_Zone')", EvalError)


def test_format_strings_from_a_column_are_not_rewritten():
    # sqlglot rewrites %e and %E6S in a literal format; a format that is data arrives as written
    db = Database({"f": Table([("fmt", T.STRING)], [("%e|%E6S",)])})
    rows = evaluate("SELECT FORMAT_DATETIME(fmt, DATETIME '2008-12-05 10:11:12.5') FROM f", db).rows
    assert rows == [(" 5|12.500000",)]


def test_unmapped_formats():
    assert D.unmap_format("%-d %S.%f") == "%e %E6S"
    assert D.unmap_format("100%%") == "100%%"
    with pytest.raises(Unsupported):
        D.unmap_format("%%m/%d/%y")
    with pytest.raises(Unsupported):
        D.unmap_format("%%-d")


# --- PARSE_* ----------------------------------------------------------------------------------------------------------------


def test_parse_date():
    same("PARSE_DATE('%Y-%m-%d', '2008-12-25')", date(2008, 12, 25))
    same("PARSE_DATE('%m/%d/%Y', '1/2/2020')", date(2020, 1, 2))
    same("PARSE_DATE('%Y%m%d', '20081225')", date(2008, 12, 25))
    same("PARSE_DATE('%d %b %Y', '25 dec 2008')", date(2008, 12, 25))
    same("PARSE_DATE('%B %d, %Y', 'December 25, 2008')", date(2008, 12, 25))
    same("PARSE_DATE('%Y', '2008')", date(2008, 1, 1))  # unspecified fields come from 1970-01-01
    same("PARSE_DATE('%m', '12')", date(1970, 12, 1))
    same("PARSE_DATE('%x', '12/25/08')", date(2008, 12, 25))
    same("PARSE_DATE('%F', '2008-12-25')", date(2008, 12, 25))
    same("PARSE_DATE('%e/%m/%y', ' 5/12/08')", date(2008, 12, 5))
    same("PARSE_DATE('%Y/%j', '2008/360')", date(2008, 12, 25))
    same("PARSE_DATE('%y-%m-%d', '69-01-01')", date(1969, 1, 1))
    same("PARSE_DATE('%y-%m-%d', '68-01-01')", date(2068, 1, 1))
    same("PARSE_DATE('%Y-%m-%d', '  2008-12-25  ')", date(2008, 12, 25))
    same("PARSE_DATE('%Y-%m-%d', NULL)", None)
    same("PARSE_DATE(NULL, '2008-12-25')", None)
    same("PARSE_DATE('%U%C', NULL)", None)
    error("PARSE_DATE('%Y-%m-%d', '2008-02-30')", EvalError)
    error("PARSE_DATE('%Y-%m-%d', '2008-12-25 extra')", EvalError)
    error("PARSE_DATE('%Y%j', '2020 50')", EvalError)  # compliance: repro_b403599925
    error("PARSE_DATE('%Y-%m-%d', 'xxx')", EvalError)
    same("SAFE.PARSE_DATE('%Y-%m-%d', 'xxx')", None)
    error("PARSE_DATE('%b', 'December')", Unsupported)
    error("PARSE_DATE('%W%y', '092')", Unsupported)  # week-of-year elements are not implemented
    error("PARSE_DATE('%U%g', '82')", Unsupported)
    error("PARSE_DATE('%H', '10')", Unsupported)
    error("PARSE_DATE('%Y%j%m', '200836012')", Unsupported)


def test_parse_time_and_datetime():
    same("PARSE_TIME('%H:%M:%S', '15:30:45')", time(15, 30, 45))
    same("PARSE_TIME('%I:%M:%S %p', '3:30:45 PM')", time(15, 30, 45))
    same("PARSE_TIME('%I:%M %p', '12:05 AM')", time(0, 5))
    same("PARSE_TIME('%T', '15:30:45')", time(15, 30, 45))
    same("PARSE_TIME('%H:%M:%E*S', '15:30:45.123456')", time(15, 30, 45, 123456))
    same("PARSE_TIME('%H', '7')", time(7))
    error("PARSE_TIME('%H:%M', '25:30')", EvalError)
    error("PARSE_TIME('%H:%M:%E*S', '15:30:45.1234567')", Unsupported)
    error("PARSE_TIME('%Y', '2008')", Unsupported)
    same("PARSE_DATETIME('%Y-%m-%d %H:%M:%S', '1998-10-18 13:45:55')", datetime(1998, 10, 18, 13, 45, 55))
    same("PARSE_DATETIME('%m/%d/%Y %I:%M:%S %p', '8/30/2018 2:23:38 pm')", datetime(2018, 8, 30, 14, 23, 38))
    same("PARSE_DATETIME('%a %b %e %I:%M:%S %p %Y', 'Thu Dec 25 07:30:00 PM 2008')", datetime(2008, 12, 25, 19, 30))
    error("PARSE_DATETIME('%a %b %e %I:%M:%S %p %Y', 'Fri Dec 25 07:30:00 PM 2008')", Unsupported)  # not a Friday
    error("PARSE_DATETIME('%Y-%m-%d%z', '2008-12-25+0100')", Unsupported)


def test_parse_timestamp():
    same("PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', '2008-12-25 07:30:00')", ts("2008-12-25 07:30:00+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', '2008-12-25 07:30:00', 'America/Los_Angeles')", ts("2008-12-25 15:30:00+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S%z', '2008-12-25 07:30:00+0200')", ts("2008-12-25 05:30:00+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S%Ez', '2008-12-25 07:30:00-08:00', 'Asia/Tokyo')", ts("2008-12-25 15:30:00+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%E*S%Ez', '2008-12-25 07:30:00.5-08:00')", ts("2008-12-25 15:30:00.5+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d', '2008-12-25')", ts("2008-12-25 00:00:00+00"))
    same("PARSE_TIMESTAMP('%y', '0', 'Asia/Dubai')", ts("1999-12-31 20:00:00+00"))
    same("PARSE_TIMESTAMP('%Y-%m-%d', NULL)", None)
    same("PARSE_TIMESTAMP('%y', '0', CAST(NULL AS STRING))", None)
    same("PARSE_TIMESTAMP('%c', 'Thu Dec 25 07:30:00 2008')", ts("2008-12-25 07:30:00+00"))
    error("PARSE_TIMESTAMP('%Y-%m-%d', '2008-12-25', 'No/Such_Zone')", EvalError)
    error("PARSE_TIMESTAMP('%Y-%m-%d', 'nope')", EvalError)
    error("PARSE_TIMESTAMP('%Y-%m-%d %Z', '2008-12-25 PST')", Unsupported)
    error("PARSE_TIMESTAMP('%s', '1230219000')", Unsupported)


# --- unix times -------------------------------------------------------------------------------------------------------------------


def test_unix_conversions():
    text("TIMESTAMP_SECONDS(1230219000)", "2008-12-25 15:30:00+00")
    text("TIMESTAMP_MILLIS(1230219000123)", "2008-12-25 15:30:00.123+00")
    text("TIMESTAMP_MICROS(1230219000123456)", "2008-12-25 15:30:00.123456+00")
    text("TIMESTAMP_SECONDS(-62135596800)", "0001-01-01 00:00:00+00")
    same("TIMESTAMP_SECONDS(NULL)", None)
    error("TIMESTAMP_SECONDS(253402300800)", EvalError)
    error("TIMESTAMP_SECONDS(-62135596801)", EvalError)
    error("TIMESTAMP_MILLIS(9223372036854775807)", EvalError)
    error("TIMESTAMP_SECONDS(1.5)", AnalysisError)
    same("UNIX_SECONDS(TIMESTAMP '2008-12-25 15:30:00.9+00')", 1230219000)
    same("UNIX_MILLIS(TIMESTAMP '1969-12-31 23:59:59.9995+00')", -1)  # rounds down
    same("UNIX_SECONDS(TIMESTAMP '1969-12-31 23:59:59.5+00')", -1)
    same("UNIX_MICROS(TIMESTAMP '2008-12-25 15:30:00+00')", 1230219000000000)
    same("UNIX_MICROS('2008-12-25 15:30:00+00')", 1230219000000000)
    same("UNIX_SECONDS(CAST(NULL AS TIMESTAMP))", None)
    same("UNIX_DATE(DATE '2008-12-25')", 14238)
    same("UNIX_DATE('2014-01-01')", 16071)
    same("UNIX_DATE(DATE '0001-01-01')", -719162)
    same("UNIX_DATE(DATE '9999-12-31')", 2932896)
    same("DATE_FROM_UNIX_DATE(14238)", date(2008, 12, 25))
    same("DATE_FROM_UNIX_DATE(-719162)", date(1, 1, 1))
    same("DATE_FROM_UNIX_DATE(2932896)", date(9999, 12, 31))
    error("DATE_FROM_UNIX_DATE(2932897)", EvalError)
    error("DATE_FROM_UNIX_DATE(-719163)", EvalError)
    error("UNIX_SECONDS(DATE '2008-12-25')", Unsupported)


def test_string_of_timestamp():
    same("STRING(TIMESTAMP '2008-12-25 15:30:00+00')", "2008-12-25 15:30:00+00")
    same("STRING(TIMESTAMP '2008-12-25 15:30:00+00', 'America/Los_Angeles')", "2008-12-25 07:30:00-08")
    same("STRING(TIMESTAMP '2008-12-25 15:30:00+00', 'Asia/Kolkata')", "2008-12-25 21:00:00+05:30")
    same("STRING(TIMESTAMP '2008-12-25 15:30:00.5+00', 'UTC+1234')", "2008-12-26 04:04:00.500+12:34")
    same("STRING(CAST(NULL AS TIMESTAMP))", None)
    error("STRING(DATE '2008-12-25')", Unsupported)
    error("STRING(TIMESTAMP '2008-12-25 15:30:00+00', 'No/Such_Zone')", EvalError)


def test_time_zone_names_this_machine_may_lack_are_declined():
    for name in ("US/Eastern", "NZ-CHAT", "Japan"):
        try:
            V.zone(name)
        except EvalError:
            with pytest.raises(Unsupported):
                D.zone(name)
        else:
            assert D.zone(name) is V.zone(name)
    with pytest.raises(EvalError):
        D.zone("foo")


# --- sequences -----------------------------------------------------------------------------------------------------------------------


def test_generate_date_array():
    d = date
    same("GENERATE_DATE_ARRAY('2016-10-05', '2016-10-08')", (d(2016, 10, 5), d(2016, 10, 6), d(2016, 10, 7), d(2016, 10, 8)))
    same("GENERATE_DATE_ARRAY('2016-10-05', '2016-10-09', INTERVAL 2 DAY)", (d(2016, 10, 5), d(2016, 10, 7), d(2016, 10, 9)))
    same("GENERATE_DATE_ARRAY('2016-10-05', '2016-10-01', INTERVAL -3 DAY)", (d(2016, 10, 5), d(2016, 10, 2)))
    same("GENERATE_DATE_ARRAY('2016-10-05', '2016-10-01')", ())
    same("GENERATE_DATE_ARRAY('2016-10-05', '2016-10-05')", (d(2016, 10, 5),))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2016-07-01', INTERVAL 2 MONTH)", (d(2016, 1, 1), d(2016, 3, 1), d(2016, 5, 1), d(2016, 7, 1)))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2017-06-01', INTERVAL 1 QUARTER)", tuple(d(2016 + (m - 1) // 12, (m - 1) % 12 + 1, 1) for m in range(1, 19, 3)))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2016-02-01', INTERVAL 1 WEEK)", tuple(d(2016, 1, 1 + 7 * k) for k in range(5)))
    same("GENERATE_DATE_ARRAY('2016-01-01', '2018-01-01', INTERVAL 1 YEAR)", (d(2016, 1, 1), d(2017, 1, 1), d(2018, 1, 1)))
    same("GENERATE_DATE_ARRAY(NULL, '2016-02-01')", None)
    same("GENERATE_DATE_ARRAY('2016-02-01', NULL, INTERVAL 1 DAY)", None)
    same("GENERATE_DATE_ARRAY('2016-02-01', '2016-02-02', INTERVAL NULL DAY)", None)
    error("GENERATE_DATE_ARRAY('2016-01-01', '2016-02-01', INTERVAL 0 DAY)", EvalError)
    error("GENERATE_DATE_ARRAY('2016-01-01', '2016-02-01', INTERVAL 1 HOUR)", AnalysisError)
    # stepping from a month's end: clamping makes the result depend on how BigQuery steps
    error("GENERATE_DATE_ARRAY('2016-01-31', '2016-05-31', INTERVAL 1 MONTH)", Unsupported)
    # an array this long is not generated here
    error("GENERATE_DATE_ARRAY('2016-01-01', '2056-01-01')", Unsupported)


def test_generate_timestamp_array():
    got = value("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:00+00', TIMESTAMP '2016-10-07 00:00:00+00', INTERVAL 1 DAY)")
    assert got == (ts("2016-10-05 00:00:00+00"), ts("2016-10-06 00:00:00+00"), ts("2016-10-07 00:00:00+00"))
    got = value("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:00+00', TIMESTAMP '2016-10-05 00:00:02+00', INTERVAL 1 SECOND)")
    assert len(got) == 3 and got[2] - got[0] == 2_000_000
    got = value("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:02+00', TIMESTAMP '2016-10-05 00:00:00+00', INTERVAL -1 SECOND)")
    assert len(got) == 3 and got[0] - got[2] == 2_000_000
    same("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:02+00', TIMESTAMP '2016-10-05 00:00:00+00', INTERVAL 1 SECOND)", ())
    same("GENERATE_TIMESTAMP_ARRAY(NULL, TIMESTAMP '2016-10-05 00:00:00+00', INTERVAL 1 SECOND)", None)
    error("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:00+00', TIMESTAMP '2016-10-07 00:00:00+00', INTERVAL 0 DAY)", EvalError)
    error("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:00+00', TIMESTAMP '2016-10-07 00:00:00+00', INTERVAL 1 MONTH)", AnalysisError)
    error("GENERATE_TIMESTAMP_ARRAY(TIMESTAMP '2016-10-05 00:00:00+00', TIMESTAMP '2016-10-07 00:00:00+00', INTERVAL 1 NANOSECOND)", Unsupported)
