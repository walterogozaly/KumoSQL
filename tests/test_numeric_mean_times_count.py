from collections import Counter
from decimal import Decimal

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402


def test_rounded_numeric_average_times_count_is_not_rewritten_to_sum():
    left = "SELECT m.k, m.a * m.n AS total FROM (SELECT k, AVG(x) AS a, COUNT(x) AS n FROM t GROUP BY k) AS m"
    right = "SELECT k, SUM(x) AS total FROM t GROUP BY k"
    schema = {"t": ["k", "x"]}
    types = {"t": {"k": "INT64", "x": "NUMERIC"}}

    normalized_left = normalize(left, schema=schema, types=types, dialect="bigquery")
    normalized_right = normalize(right, schema=schema, types=types, dialect="bigquery")
    assert normalized_left != normalized_right
    assert "AVG(x) * COUNT(x)" in normalized_left
    assert not prove_equivalent_algebraic(left, right, schema=schema, types=types, dialect="bigquery").proven
    assert not prove_equivalent_algebraic(left, right, dialect="bigquery").proven

    # DuckDB returns DOUBLE for AVG(DECIMAL), so cast to BigQuery's documented
    # NUMERIC(38, 9) result shape before comparing the rounded product with SUM.
    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE t (k BIGINT, x DECIMAL(38, 9))")
    db.execute("INSERT INTO t VALUES (1, 1), (1, 1), (1, 2)")
    rounded_average = "SELECT CAST(AVG(x) AS DECIMAL(38, 9)) * COUNT(x) AS total FROM t"
    total = "SELECT SUM(x) AS total FROM t"
    rounded_rows, sum_rows = run_unoptimized(db, rounded_average, total)
    assert Counter(rounded_rows) != Counter(sum_rows)
    assert rounded_rows == [(Decimal("3.999999999"),)]
    assert sum_rows == [(Decimal("4.000000000"),)]


def test_average_against_sum_over_count_remains_proven():
    average = "SELECT k, AVG(x) AS a FROM t GROUP BY k"
    quotient = "SELECT k, SUM(x) / NULLIF(COUNT(x), 0) AS a FROM t GROUP BY k"
    assert prove_equivalent_algebraic(
        average,
        quotient,
        schema={"t": ["k", "x"]},
        types={"t": {"k": "INT64", "x": "NUMERIC"}},
        dialect="bigquery",
    ).proven
