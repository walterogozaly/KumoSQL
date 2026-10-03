"""Unread window columns of a derived table are dropped before the prover compares queries."""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus
from kumosql.unread_windows import drop_unread_windows

SCHEMA = {"t": ["id", "k", "v"]}


def dropped(sql):
    return drop_unread_windows(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT


def test_an_unread_window_column_is_dropped_and_the_pair_proves():
    left = "SELECT k, SUM(v) AS s FROM (SELECT k, v, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS rn FROM t) AS d GROUP BY k"
    assert "ROW_NUMBER" not in dropped(left)
    assert proved(left, "SELECT k, SUM(v) AS s FROM t GROUP BY k")
    assert proved("SELECT k, v FROM (SELECT k, v, SUM(v) OVER (PARTITION BY k) AS total FROM t) AS d", "SELECT k, v FROM t")


@pytest.mark.parametrize("sql", [
    "SELECT k FROM (SELECT k, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS rn FROM t) AS d WHERE rn = 1",
    "SELECT k FROM (SELECT k, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS rn FROM t QUALIFY rn = 1) AS d",
    "SELECT * FROM (SELECT k, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t) AS d",
    "SELECT k FROM (SELECT DISTINCT k, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t) AS d",
    "SELECT TO_JSON_STRING(d) AS j FROM (SELECT k, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t) AS d",
    "SELECT d.k FROM (SELECT k, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t) AS d JOIN t AS u USING (rn)",
])
def test_a_window_column_that_may_be_read_stays(sql):
    assert dropped(sql) == sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")


def test_the_read_window_is_still_not_proved_away():
    assert not proved("SELECT k FROM (SELECT k, ROW_NUMBER() OVER (PARTITION BY k ORDER BY id) AS rn FROM t) AS d WHERE rn = 1",
                      "SELECT k FROM t")
