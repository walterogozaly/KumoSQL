"""Keyed drill-down compares each key's rows as a bag of whole rows.

Per-column checksums alone miss values swapped between rows that share a
key: every column keeps the same values, only their pairing into rows
changes. The generated BigQuery SQL runs on DuckDB after transpiling, with
FARM_FINGERPRINT replaced by DuckDB's own hash, as in test_fingerprint.py.
"""

import json

import pytest
import sqlglot

from kumosql import diff_rows_sql

duckdb = pytest.importorskip("duckdb")


def to_duckdb(sql):
    duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    return duck.replace("FARM_FINGERPRINT(", "farm_fp(").replace("AS BIGDECIMAL)", "AS DECIMAL(38, 5))")


@pytest.fixture
def con():
    connection = duckdb.connect()
    connection.execute("CREATE MACRO farm_fp(x) AS CAST(hash(x) AS HUGEINT)")
    yield connection
    connection.close()


def drill(con, before, after, columns=("k", "a", "b"), keys=("k",)):
    con.execute(f"CREATE TABLE before_t AS SELECT * FROM (VALUES {before}) v(k, a, b)")
    con.execute(f"CREATE TABLE after_t AS SELECT * FROM (VALUES {after}) v(k, a, b)")
    cursor = con.execute(to_duckdb(diff_rows_sql("before_t", "after_t", list(columns), keys=list(keys))))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def test_values_swapped_between_rows_of_one_key_are_reported(con):
    rows = drill(con, "(1, 10, 100), (1, 20, 200)", "(1, 10, 200), (1, 20, 100)")
    assert [(r["status"], r["key_json"], list(r["changed_columns"])) for r in rows] == [
        ("changed", '{"k":1}', ["*"])
    ]
    assert (rows[0]["before_count"], rows[0]["after_count"]) == (2, 2)


def test_same_rows_in_another_order_match(con):
    rows = drill(con, "(1, 10, 100), (1, 20, 200), (2, 5, 5)", "(2, 5, 5), (1, 20, 200), (1, 10, 100)")
    assert rows == []


def test_single_column_change_names_only_that_column(con):
    rows = drill(con, "(1, 10, 100), (2, 20, 200)", "(1, 10, 100), (2, 20, 201)")
    assert [(r["status"], r["key_json"], list(r["changed_columns"])) for r in rows] == [
        ("changed", '{"k":2}', ["b"])
    ]
    assert json.loads(rows[0]["after_row"]) == {"a": 20, "b": 201, "k": 2}


def test_changed_column_with_repeated_key_names_the_column_not_the_row(con):
    rows = drill(con, "(1, 10, 100), (1, 20, 200)", "(1, 10, 200), (1, 21, 100)")
    assert [(r["status"], list(r["changed_columns"])) for r in rows] == [("changed", ["a"])]


def test_missing_and_recounted_keys_keep_their_statuses(con):
    rows = drill(con, "(1, 10, 100), (2, 5, 5)", "(1, 10, 100), (1, 10, 100), (3, 5, 5)")
    assert [(r["status"], r["key_json"]) for r in rows] == [
        ("row_count_differs", '{"k":1}'),
        ("only_before", '{"k":2}'),
        ("only_after", '{"k":3}'),
    ]
    # A key on one side only has nothing to compare column by column.
    assert [list(r["changed_columns"]) for r in rows[1:]] == [[], []]


def test_key_only_projection_still_compares_counts(con):
    rows = drill(con, "(1, 10, 100), (1, 20, 200)", "(1, 10, 200), (1, 20, 100)", columns=("k",))
    assert rows == []
