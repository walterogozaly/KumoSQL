"""Regrouping outputs that combine aggregates, such as an average rebuilt from a sum and a count."""

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402

FINE = "(SELECT region, status, SUM(amount) AS total, COUNT(*) AS n, COUNT(amount) AS n_amount FROM orders GROUP BY region, status) AS daily"
AVG = "SELECT region, AVG(amount) AS mean FROM orders GROUP BY region"


def test_ratio_of_rolled_up_sums_folds_to_the_raw_aggregates():
    sql = f"SELECT region, SUM(total) / SUM(n_amount) AS mean FROM {FINE} GROUP BY region"
    assert normalize(sql) == "SELECT region AS region, SUM(amount) / COUNT(amount) AS mean FROM orders GROUP BY region"
    assert prove_equivalent_algebraic(AVG, sql).proven


def test_global_arithmetic_over_partials_folds():
    sql = "SELECT SUM(t) * 2 + MAX(m) AS x FROM (SELECT k, SUM(a) AS t, MAX(b) AS m FROM o GROUP BY k) AS s"
    assert normalize(sql) == "SELECT SUM(a) * 2 + MAX(b) AS x FROM o"


@pytest.mark.parametrize(
    "outer",
    [
        "SUM(total) / SUM(n)",  # COUNT(*) counts NULL amounts too
        "AVG(total / n)",  # average of per-group averages
    ],
)
def test_wrong_averages_stay_unproven(outer):
    assert not prove_equivalent_algebraic(AVG, f"SELECT region, {outer} AS mean FROM {FINE} GROUP BY region").proven


def test_arithmetic_reading_a_column_outside_the_aggregates_is_left_alone():
    sql = f"SELECT region, SUM(total) + region AS x FROM {FINE} GROUP BY region"
    assert "daily" in normalize(sql)
