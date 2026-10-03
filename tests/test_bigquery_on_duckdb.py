"""BigQuery SQL run on DuckDB must answer as BigQuery does, or fail.

Every expected value below was read from BigQuery itself (constant queries, 2026-10-03); the
divergences come from the R008 catalogue (rows numbered as there) and from checking it. One test per
divergence acted on; the pair tests at the end show that the searches no longer report a refutation
BigQuery would not show.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

duckdb = pytest.importorskip("duckdb")
sqlglot = pytest.importorskip("sqlglot")

from kumosql.bigquery_on_duckdb import (  # noqa: E402
    MARKER,
    Unfaithful,
    UnfaithfulOutput,
    bigquery_rows,
    configure,
    faithful,
    is_bigquery_failure,
)


@pytest.fixture
def db():
    connection = duckdb.connect(":memory:")
    configure(connection)
    yield connection
    connection.close()


def run(db, sql: str):
    text = faithful(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="duckdb")
    return bigquery_rows(db.execute(text).fetchall())


def one(db, sql: str):
    return run(db, sql)[0][0]


def fails(db, sql: str) -> bool:
    try:
        run(db, sql)
    except Unfaithful:
        return True
    except Exception as error:  # noqa: BLE001
        assert is_bigquery_failure(error), error
        return True
    return False


# --- session settings ------------------------------------------------------------------------


def test_row6_nulls_sort_first_ascending_and_last_descending(db):
    # also for SQL that does not spell NULLS FIRST (sqlglot writes it; raw DuckDB SQL may not)
    assert db.execute("SELECT x FROM (VALUES (2), (NULL), (1)) t(x) ORDER BY x").fetchall() == [(None,), (1,), (2,)]
    assert db.execute("SELECT x FROM (VALUES (2), (NULL), (1)) t(x) ORDER BY x DESC").fetchall() == [(2,), (1,), (None,)]
    assert run(db, "SELECT x, SUM(1) OVER (ORDER BY x) FROM UNNEST([3, NULL, 1]) x ORDER BY x") == [(None, 1), (1, 2), (3, 3)]


def test_rows24_25_timestamps_are_read_in_utc(db):
    zone = db.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert zone in ("UTC", "Etc/UTC")


# --- NaN and infinity never reach DuckDB --------------------------------------------------------


@pytest.mark.parametrize("text", ["NaN", "inf", "-inf", "Infinity"])
def test_rows1_2_3_7_non_finite_strings_are_not_read_as_floats(db, text):
    # BigQuery: NaN = NaN is FALSE, 1.0 < NaN is FALSE, NaN sorts first, LEAST(NaN, 1.0) is NaN;
    # DuckDB orders NaN above every number. With no NaN in DuckDB, none of it can matter.
    assert fails(db, f"SELECT CAST('{text}' AS FLOAT64)")


def test_non_finite_results_are_refused():
    with pytest.raises(UnfaithfulOutput):
        bigquery_rows([(float("nan"),)])
    with pytest.raises(UnfaithfulOutput):
        bigquery_rows([(float("inf"),)])


def test_row5_division_by_zero_fails(db):
    assert fails(db, "SELECT x / 0 FROM UNNEST([1]) x")
    assert fails(db, "SELECT x / 0.0 FROM UNNEST([1.0]) x")
    assert one(db, "SELECT SAFE_DIVIDE(x, 0) FROM UNNEST([1]) x") is None


def test_integer_division_and_mod_by_zero_fail(db):
    assert fails(db, "SELECT MOD(x, 0) FROM UNNEST([1]) x")
    assert fails(db, "SELECT DIV(x, 0) FROM UNNEST([1]) x")


def test_row4_float_overflow_fails(db):
    assert fails(db, "SELECT POW(x, 400) FROM UNNEST([10.0]) x")
    assert fails(db, "SELECT EXP(x) FROM UNNEST([1000.0]) x")
    assert fails(db, "SELECT x * 1e300 FROM UNNEST([1e10]) x")
    assert one(db, "SELECT EXP(x) FROM UNNEST([1.0]) x") == pytest.approx(2.718281828459045)


def test_sum_past_int64_fails(db):
    assert fails(db, "SELECT SUM(x) FROM UNNEST([9223372036854775807, 1]) x")
    assert fails(db, "SELECT SUM(x) OVER () FROM UNNEST([9223372036854775807, 1]) x")
    assert one(db, "SELECT SUM(x) FROM UNNEST([4, 1]) x") == 5
    assert one(db, "SELECT SUM(x) FILTER (WHERE x > 1) FROM UNNEST([1, 2, 3]) x") == 5


# --- strings ---------------------------------------------------------------------------------


def test_row8_concat_with_null_is_null(db):
    assert one(db, "SELECT CONCAT('a', NULL, 'b')") is None


@pytest.mark.parametrize(
    "position, length, expected",
    [(0, 2, "ab"), (-5, 2, "ab"), (-7, 9, "abcde"), (-3, 2, "cd"), (-1, 2, "e"), (3, 9, "cde"), (6, 2, ""), (0, None, "abcde"), (-7, None, "abcde")],
)
def test_substr_reads_position_zero_and_past_the_left_end_as_one(db, position, length, expected):
    arguments = f"{position}" if length is None else f"{position}, {length}"
    assert one(db, f"SELECT SUBSTR('abcde', {arguments})") == expected


def test_regexp_extract_without_a_match_is_null(db):
    assert one(db, "SELECT REGEXP_EXTRACT('abc', 'x')") is None
    assert one(db, "SELECT REGEXP_EXTRACT('abc', 'b*')") == ""
    assert one(db, "SELECT REGEXP_EXTRACT('abc', 'b(c)')") == "c"


def test_format_has_no_faithful_reading(db):
    assert fails(db, "SELECT FORMAT('%d', 5)")  # BigQuery: '5'; DuckDB: '%d'


def test_row32_collation_is_refused(db):
    assert fails(db, "SELECT COLLATE('a', 'und:ci') = 'A'")


# --- casts -----------------------------------------------------------------------------------


def test_float_to_string_fails_and_numeric_to_string_drops_trailing_zeros(db):
    assert fails(db, "SELECT CAST(x AS STRING) FROM UNNEST([CAST(2.0 AS FLOAT64)]) x")  # BigQuery '2', DuckDB '2.0'
    assert run(db, "SELECT CAST(NUMERIC '1.5' AS STRING), CAST(CAST(100 AS NUMERIC) AS STRING), CAST(NUMERIC '0.0001' AS STRING)") == [
        ("1.5", "100", "0.0001")
    ]
    assert one(db, "SELECT CAST(CAST(9.9999999999 AS NUMERIC) AS STRING)") == "10"
    assert one(db, "SELECT CAST(1.5 AS STRING)") == "1.5"
    assert one(db, "SELECT CAST(x AS STRING) FROM UNNEST([7]) x") == "7"


@pytest.mark.parametrize("sql", ["CAST('1.0' AS INT64)", "CAST('1e3' AS INT64)", "CAST('t' AS BOOL)", "SAFE_CAST('yes' AS BOOL)", "CAST(' 2024-01-05' AS DATE)"])
def test_strings_the_engines_read_differently_fail(db, sql):
    # BigQuery rejects each of these; DuckDB reads them as 1, 1000, TRUE, TRUE and a date
    assert fails(db, f"SELECT {sql}")


def test_strings_both_engines_read_alike_still_convert(db):
    assert run(db, "SELECT CAST(' 1' AS INT64), CAST('+5' AS INT64), SAFE_CAST('TRUE' AS BOOL), CAST(' 1.5 ' AS FLOAT64), CAST('2024-01-05' AS DATE)") == [
        (1, 5, True, 1.5, date(2024, 1, 5))
    ]


def test_unverified_rows_round_and_float_to_int_cast_match(db):
    # ROUND and CAST(FLOAT64 AS INT64) round half away from zero in both engines
    assert run(db, "SELECT CAST(2.5 AS INT64), CAST(-2.5 AS INT64), ROUND(2.5), ROUND(-2.5)") == [(3, -3, Decimal("3"), Decimal("-3"))]


def test_unverified_rows_mod_sign_least_greatest_match(db):
    assert run(db, "SELECT MOD(-7, 3), DIV(-7, 2), LEAST(NULL, 1), GREATEST(2, NULL)") == [(-1, -3, None, None)]


def test_right_shift_of_a_negative_number_fails(db):
    assert fails(db, "SELECT -7 >> 1")  # BigQuery shifts logically: 9223372036854775804
    assert one(db, "SELECT 7 >> 1") == 3


# --- NUMERIC ---------------------------------------------------------------------------------


def test_numeric_keeps_nine_digits(db):
    # sqlglot writes NUMERIC as DECIMAL, which DuckDB reads as DECIMAL(18, 3)
    assert one(db, "SELECT CAST(0.0001 AS NUMERIC) > 0") is True


def test_row28_numeric_division_and_product_fail(db):
    # BigQuery rounds NUMERIC quotients and products to 9 digits: 1/3*3 is 0.999999999
    assert fails(db, "SELECT CAST(x AS NUMERIC) / 3 FROM UNNEST([1]) x")
    assert fails(db, "SELECT CAST(x AS NUMERIC) * CAST(x AS NUMERIC) FROM UNNEST([0.00001]) x")
    assert one(db, "SELECT CAST(x AS NUMERIC) * 2 FROM UNNEST([3]) x") == Decimal("6")
    assert one(db, "SELECT x / 2 FROM UNNEST([3]) x") == 1.5


def test_float_overflow_fails_and_safe_divide_overflow_is_null(db):
    # BigQuery: 1e300 / 1e-10 is "double overflow"; SAFE_DIVIDE returns NULL for it and for a zero divisor
    assert fails(db, "SELECT x / 1e-140 / 1e-140 FROM UNNEST([1e140]) x")
    assert fails(db, "SELECT x * x * x FROM UNNEST([1e140]) x")
    assert one(db, "SELECT SAFE_DIVIDE(1e100, 1e-100 * 1e-100 * 1e-100)") is None
    assert one(db, "SELECT SAFE_DIVIDE(4, 0)") is None
    assert one(db, "SELECT SAFE_DIVIDE(4, 2)") == 2.0
    # BigQuery gives 0.333333333; DuckDB divides NUMERIC in floating point
    assert fails(db, "SELECT SAFE_DIVIDE(NUMERIC '1', 3)")


def test_array_subscripts(db):
    # BigQuery: OFFSET counts from 0, ORDINAL from 1, a bare index is an OFFSET; outside the array OFFSET fails
    # and SAFE_OFFSET is NULL (DuckDB reads a negative index from the end)
    assert one(db, "SELECT ['a', 'b', 'c'][OFFSET(1)]") == "b"
    assert one(db, "SELECT ['a', 'b', 'c'][ORDINAL(1)]") == "a"
    assert one(db, "SELECT ['a', 'b', 'c'][1]") == "b"
    assert fails(db, "SELECT ['a', 'b', 'c'][OFFSET(5)]")
    assert one(db, "SELECT ['a', 'b', 'c'][SAFE_OFFSET(5)]") is None
    assert one(db, "SELECT ['a', 'b', 'c'][SAFE_OFFSET(-2)]") is None
    assert one(db, "SELECT ['a', 'b', 'c'][SAFE_ORDINAL(0)]") is None
    assert one(db, "SELECT ['a', 'b', 'c'][OFFSET(NULL)]") is None


def test_intervals_are_not_read(db):
    # BigQuery: EXTRACT(HOUR FROM TIMESTAMP '2021-01-02 12:34:56' - TIMESTAMP '2021-01-01') is 36, DuckDB's 12
    assert fails(db, "SELECT EXTRACT(HOUR FROM TIMESTAMP '2021-01-02 12:34:56' - TIMESTAMP '2021-01-01 00:00:00')")
    assert fails(db, "SELECT TIMESTAMP '2021-01-02 12:34:56' - TIMESTAMP '2021-01-01 00:00:00'")
    assert one(db, "SELECT EXTRACT(HOUR FROM TIMESTAMP '2021-01-02 12:34:56')") == 12


def test_week_starting_sunday_or_monday_from_a_timestamp(db):
    # BigQuery returns 45 for both (its documentation page shows 44 for WEEK(MONDAY))
    assert run(db, "SELECT EXTRACT(WEEK(SUNDAY) FROM TIMESTAMP('2017-11-06 00:00:00+00')), "
                   "EXTRACT(WEEK(MONDAY) FROM TIMESTAMP('2017-11-06 00:00:00+00'))") == [(45, 45)]


@pytest.mark.parametrize("sql", [
    "SELECT ARRAY_SLICE(['a', 'b', 'c', 'd'], 1, 2)",  # BigQuery ['b', 'c']; sqlglot keeps the 0-based bounds
    "SELECT JSON_VALUE(JSON '{\"a\": [1, 2]}', '$.a[0]')",
    "SELECT PERCENTILE_CONT(x, 0.5 RESPECT NULLS) OVER () FROM UNNEST([0, 3, NULL]) x",
    "SELECT CAST(DATE '2024-01-01' AS STRING FORMAT 'DAY')",  # sqlglot drops the format
    "SELECT ARRAY(SELECT 1 UNION ALL SELECT 2)",  # the element order is BigQuery's to pick
    "SELECT ARRAY_AGG(x) FROM UNNEST([3, 1, 2]) x",
    "SELECT STRING_AGG(x) FROM UNNEST(['b', 'a']) x",
    "SELECT CURRENT_DATE('-08')",  # sqlglot 26 drops the time zone
])
def test_constructs_without_a_faithful_reading_are_refused(db, sql):
    assert fails(db, sql)


def test_ordered_array_aggregates_still_run(db):
    assert one(db, "SELECT ARRAY_AGG(x ORDER BY x) FROM UNNEST([3, 1, 2]) x") == (1, 2, 3)
    assert one(db, "SELECT STRING_AGG(s, '-' ORDER BY s) FROM UNNEST(['b', 'a']) s") == "a-b"
    assert one(db, "SELECT ARRAY(SELECT x FROM UNNEST([2, 1]) x ORDER BY x)") == (1, 2)


def test_row29_bignumeric_is_refused(db):
    assert fails(db, "SELECT CAST(x AS BIGNUMERIC) FROM UNNEST([1]) x")


# --- dates -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day, week, week_monday, dayofweek, isoweek",
    [
        ("2016-01-01", 0, 0, 6, 53),
        ("2016-01-03", 1, 0, 1, 53),
        ("2018-12-31", 52, 53, 2, 1),
        ("2019-12-29", 52, 51, 1, 52),
        ("2021-01-02", 0, 0, 7, 53),
        ("2024-06-05", 22, 23, 4, 23),
    ],
)
def test_row22_week_numbers_and_day_of_week(db, day, week, week_monday, dayofweek, isoweek):
    sql = f"SELECT EXTRACT(WEEK FROM DATE '{day}'), EXTRACT(WEEK(MONDAY) FROM DATE '{day}'), EXTRACT(DAYOFWEEK FROM DATE '{day}'), EXTRACT(ISOWEEK FROM DATE '{day}')"
    assert run(db, sql) == [(week, week_monday, dayofweek, isoweek)]


def test_week_starting_another_day_is_refused(db):
    assert fails(db, "SELECT EXTRACT(WEEK(TUESDAY) FROM DATE '2024-06-05')")


def test_row20_week_truncation_starts_on_sunday(db):
    assert one(db, "SELECT DATE_TRUNC(DATE '2024-06-05', WEEK)") == date(2024, 6, 2)


def test_rows21_23_date_functions_return_dates(db):
    assert run(db, "SELECT DATE_TRUNC(DATE '1992-03-07', MONTH), DATE_ADD(DATE '2008-12-25', INTERVAL 5 DAY)") == [
        (date(1992, 3, 1), date(2008, 12, 30))
    ]


@pytest.mark.parametrize("sql", ["CAST('infinity' AS DATE)", "CAST('-infinity' AS TIMESTAMP)"])
def test_rows27_33_infinite_dates_are_not_read(db, sql):
    assert fails(db, f"SELECT {sql}")


# --- arrays and structs ----------------------------------------------------------------------


def test_row16_a_null_array_is_returned_as_empty():
    assert bigquery_rows([(None,), ([],)]) == [(None,), (None,)]


def test_row17_an_array_with_a_null_element_is_refused(db):
    assert fails(db, "SELECT ARRAY_AGG(x) FROM UNNEST([1, NULL]) x")


def test_row18_two_unnests_cross_join(db):
    assert one(db, "SELECT COUNT(*) FROM UNNEST([1, 2]) a, UNNEST([3, 4]) b") == 4


def test_row19_in_unnest_with_null_is_null(db):
    assert one(db, "SELECT 1 IN UNNEST([0, NULL])") is None


def test_rows11_12_structs_compared_are_refused_and_returned_by_position(db):
    assert fails(db, "SELECT STRUCT(1 AS a) = STRUCT(1 AS b)")  # BigQuery: TRUE
    assert one(db, "SELECT STRUCT(1 AS a, 2 AS b) AS s") == (1, 2)


def test_with_offset_counts_from_zero(db):
    # BigQuery's offset counts from 0; sqlglot writes WITH ORDINALITY, which counts from 1
    assert sorted(run(db, "SELECT x, o FROM UNNEST([1, 2, 3]) x WITH OFFSET o WHERE o > 0")) == [(2, 1), (3, 2)]
    # an offset whose element has no name cannot be read back
    assert fails(db, "SELECT COUNT(*) FROM UNNEST([1, 2, 3]) WITH OFFSET o")


def test_rows30_31_approximate_aggregates_are_refused(db):
    assert fails(db, "SELECT APPROX_COUNT_DISTINCT(x) FROM UNNEST([1, 2]) x")
    assert fails(db, "SELECT APPROX_QUANTILES(x, 4) FROM UNNEST([1, 2]) x")


def test_guard_errors_are_recognised(db):
    with pytest.raises(duckdb.Error) as caught:
        db.execute(faithful(sqlglot.parse_one("SELECT 1 / 0", read="bigquery")).sql(dialect="duckdb")).fetchall()
    assert MARKER in str(caught.value) and is_bigquery_failure(caught.value)


# --- the searches no longer refute pairs BigQuery finds equal -------------------------------------

SCHEMA = {"t": {"a": "INT64", "b": "INT64", "s": "STRING", "d": "DATE"}}

# Each pair returns the same rows in BigQuery (checked there on sample rows) and differed on DuckDB.
EQUAL_IN_BIGQUERY = [
    ("SELECT EXTRACT(WEEK FROM d) FROM t", "SELECT CAST(FORMAT_DATE('%U', d) AS INT64) FROM t"),
    ("SELECT EXTRACT(DAYOFWEEK FROM d) FROM t", "SELECT CAST(FORMAT_DATE('%w', d) AS INT64) + 1 FROM t"),
    ("SELECT SUBSTR(s, 0, 2) FROM t", "SELECT SUBSTR(s, 1, 2) FROM t"),
    ("SELECT REGEXP_EXTRACT(s, 'a') FROM t", "SELECT IF(REGEXP_CONTAINS(s, 'a'), 'a', NULL) FROM t"),
    ("SELECT DATE_TRUNC(d, MONTH) FROM t", "SELECT DATE(EXTRACT(YEAR FROM d), EXTRACT(MONTH FROM d), 1) FROM t"),
    ("SELECT a FROM t WHERE CAST(0.0001 AS NUMERIC) > 0", "SELECT a FROM t"),
    # BigQuery fails wherever b = 0, and agrees everywhere else
    ("SELECT COUNT(*) FROM t WHERE 1 / b > 0", "SELECT COUNT(*) FROM t WHERE b > 0"),
]


@pytest.mark.parametrize("left, right", EQUAL_IN_BIGQUERY)
def test_targeted_search_finds_no_difference(left, right):
    from kumosql.refute import find_targeted_difference

    assert find_targeted_difference(left, right, SCHEMA, budget=20.0) is None


@pytest.mark.parametrize("left, right", EQUAL_IN_BIGQUERY)
def test_random_databases_find_no_difference(left, right):
    from kumosql.result_equivalence import check_result_equivalence

    result = check_result_equivalence(left, right, SCHEMA, check_column_names=False, targeted=True)
    assert result.status.value != "different", result.reason


@pytest.mark.parametrize("left, right", EQUAL_IN_BIGQUERY[:5])
def test_bounded_replay_confirms_no_difference(left, right):
    from kumosql.bounded_equivalence import BColumn, BoundedSchema, BTable, DuckDBReplay

    schema = BoundedSchema({"t": BTable("t", [BColumn("a", "INT64"), BColumn("b", "INT64"), BColumn("s", "STRING"), BColumn("d", "DATE")])})
    replay = DuckDBReplay(schema, left, right, "bigquery")
    rows = [(1, 0, "abc", date(2016, 1, 1)), (2, 1, "xa", date(2024, 6, 2)), (3, 2, None, None)]
    assert replay.differ({"t": rows}) is False


def test_a_real_difference_is_still_found():
    from kumosql.refute import find_targeted_difference

    assert find_targeted_difference("SELECT a FROM t WHERE b > 1", "SELECT a FROM t WHERE b >= 1", SCHEMA, budget=20.0) is not None


# day | DATE_TRUNC WEEK | WEEK(MONDAY) | ISOWEEK | DATETIME_TRUNC WEEK | DATE_DIFF from 2024-06-05 in WEEK, ISOWEEK, WEEK(MONDAY)
WEEKS = """2023-12-31|2023-12-31|2023-12-25|2023-12-25|2023-12-31|-22|-23|-23 2024-05-26|2024-05-26|2024-05-20|2024-05-20|2024-05-26|-1|-2|-2
2024-06-01|2024-05-26|2024-05-27|2024-05-27|2024-05-26|-1|-1|-1 2024-06-02|2024-06-02|2024-05-27|2024-05-27|2024-06-02|0|-1|-1
2024-06-03|2024-06-02|2024-06-03|2024-06-03|2024-06-02|0|0|0 2024-06-09|2024-06-09|2024-06-03|2024-06-03|2024-06-09|1|0|0
2024-06-10|2024-06-09|2024-06-10|2024-06-10|2024-06-09|1|1|1""".split()


@pytest.mark.parametrize("row", WEEKS)
def test_row20_week_truncation_and_week_differences(db, row):
    day, sunday, monday, iso, datetime_sunday, *diffs = row.split("|")
    sql = (
        f"SELECT DATE_TRUNC(DATE '{day}', WEEK), DATE_TRUNC(DATE '{day}', WEEK(MONDAY)), DATE_TRUNC(DATE '{day}', ISOWEEK), "
        f"DATETIME_TRUNC(DATETIME '{day} 13:00:00', WEEK), DATE_DIFF(DATE '{day}', DATE '2024-06-05', WEEK), "
        f"DATE_DIFF(DATE '{day}', DATE '2024-06-05', ISOWEEK), DATETIME_DIFF(DATETIME '{day} 00:00:00', DATETIME '2024-06-05 00:00:00', WEEK(MONDAY))"
    )
    expected = tuple(date.fromisoformat(v) for v in (sunday, monday, iso, datetime_sunday)) + tuple(int(v) for v in diffs)
    assert run(db, sql) == [expected]
