"""Pairs an outside audit (S010) found reported as different although BigQuery returns the same rows.

Each pair below is equal in BigQuery (or, for the LIMIT pair, may be: BigQuery picks the row), and
each was reported ``not_equivalent`` by at least one of: the algebraic prover's executed search, the
pasted-query endpoint (``pipeline_equivalence.prove_queries``), the standalone search
(``counterexample.find_counterexample``) or the bounded check. None may report a difference now.
"""

from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql import counterexample as cx  # noqa: E402
from kumosql import equivalences, pipeline_equivalence, prover_context  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.prover_schema import ProverSchema  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

COLUMNS = {"t": ["x"]}
TYPES = {"t": {"x": "INT64"}}

EQUAL_IN_BIGQUERY = {
    # NaN compares unequal to everything and is not above 0 in BigQuery
    "nan_eq": ("SELECT x FROM t WHERE CAST('NaN' AS FLOAT64) = CAST('NaN' AS FLOAT64)", "SELECT x FROM t WHERE FALSE"),
    "nan_gt": ("SELECT x FROM t WHERE CAST('NaN' AS FLOAT64) > 0", "SELECT x FROM t WHERE FALSE"),
    # SAFE_DIVIDE catches the overflow
    "safe_overflow": ("SELECT SAFE_DIVIDE(1e308, 1e-308) AS v FROM t", "SELECT CAST(NULL AS FLOAT64) AS v FROM t"),
    "regex": ("SELECT REGEXP_EXTRACT('abc', 'z') AS v FROM t", "SELECT CAST(NULL AS STRING) AS v FROM t"),
    "week": ("SELECT DATE_TRUNC(DATE '2024-01-03', WEEK) AS v FROM t", "SELECT DATE '2023-12-31' AS v FROM t"),
    "date_day": ("SELECT DATE_TRUNC(DATE '2024-01-03', DAY) AS v FROM t", "SELECT DATE '2024-01-03' AS v FROM t"),
    "struct_names": ("SELECT STRUCT(1 AS a) = STRUCT(1 AS b) AS v FROM t", "SELECT TRUE AS v FROM t"),
    "numeric_literal": ("SELECT NUMERIC '0.123456789' AS v FROM t", "SELECT CAST(123456789 AS NUMERIC) / 1000000000 AS v FROM t"),
    "numeric_div": ("SELECT NUMERIC '1' / NUMERIC '3' AS v FROM t", "SELECT NUMERIC '0.333333333' AS v FROM t"),
    # BigQuery may return any row: the descending sort's first row is one of its choices
    "limit_unsorted": ("SELECT x FROM t LIMIT 1", "SELECT x FROM (SELECT x FROM t ORDER BY x DESC) q LIMIT 1"),
    # BigQuery fails on 1.0 / 0.0, so neither query returns rows to compare
    "division_error": ("SELECT 1.0 / 0.0 AS v FROM t", "SELECT CAST(NULL AS FLOAT64) AS v FROM t"),
}

# every c.x has a parent p.x, so the grouped EXISTS always holds
FK_PAIR = (
    "SELECT x FROM c",
    "SELECT c.x FROM c WHERE EXISTS (SELECT 1 FROM p WHERE p.x = c.x GROUP BY p.x HAVING COUNT(*) >= 1)",
)
FK_COLUMNS = {"c": ["x"], "p": ["x"]}
FK_TYPES = {"c": {"x": "INT64"}, "p": {"x": "INT64"}}


def endpoint(left, right, columns, constraints, types):
    """``POST /api/prove-queries`` with these facts instead of the saved catalog and settings."""

    facts = ProverSchema(columns=columns, constraints=constraints, types=types)
    bounded = be.schema_from_prover(columns, constraints, types)
    with mock.patch.object(prover_context, "settings", lambda: {"enabled": True, "timeout_ms": 2000, "bounded_rows": 2}), \
         mock.patch.object(prover_context, "current_schema", lambda: facts), \
         mock.patch.object(prover_context, "bounded_schema", lambda: bounded), \
         mock.patch.object(equivalences, "load", lambda: []):
        return pipeline_equivalence.prove_queries(left, right)


@pytest.mark.parametrize("label", sorted(EQUAL_IN_BIGQUERY))
def test_algebraic_executed_search_does_not_refute(label):
    left, right = EQUAL_IN_BIGQUERY[label]
    result = prove_equivalent_algebraic(left, right, schema=COLUMNS, types=TYPES, timeout_ms=2000, search_counterexample=True)
    assert result.status.value != "not_equivalent", result.reason


@pytest.mark.parametrize("label", sorted(EQUAL_IN_BIGQUERY))
def test_pasted_query_endpoint_does_not_refute(label):
    left, right = EQUAL_IN_BIGQUERY[label]
    data = endpoint(left, right, COLUMNS, {}, TYPES)
    assert data["status"] != "not_equivalent", data
    assert (data.get("bounded") or {}).get("status") != "different", data


@pytest.mark.parametrize("label", sorted(EQUAL_IN_BIGQUERY))
def test_standalone_search_does_not_refute(label):
    left, right = EQUAL_IN_BIGQUERY[label]
    spec = cx.Spec({"t": cx.Table("t", [cx.Column("x", "INT")])})
    assert not isinstance(cx.find_counterexample(spec, left, right, dialect="bigquery", trials=10, seed=0), cx.Counterexample)


def test_pasted_query_endpoint_keeps_foreign_keys():
    constraints = {
        "c": TableConstraints(not_null=frozenset({"x"}), foreign_keys=((("x",), "p", ("x",)),)),
        "p": TableConstraints(not_null=frozenset({"x"}), keys=(("x",),)),
    }
    data = endpoint(*FK_PAIR, FK_COLUMNS, constraints, FK_TYPES)
    assert data["status"] != "not_equivalent", data
    # without the foreign key a child with no parent tells them apart
    loose = {"c": TableConstraints(not_null=frozenset({"x"})), "p": constraints["p"]}
    assert endpoint(*FK_PAIR, FK_COLUMNS, loose, FK_TYPES)["status"] == "not_equivalent"


def test_pasted_query_endpoint_still_refutes_a_real_difference():
    data = endpoint("SELECT x FROM t", "SELECT x FROM t WHERE x > 0", COLUMNS, {}, TYPES)
    assert data["status"] == "not_equivalent"
    rows = [r["x"] for r in data["counterexample"]["tables"]["t"]]
    assert any(v is None or v <= 0 for v in rows)


def test_bounded_key_columns_are_not_null():
    schema = be.schema_from_prover(COLUMNS, {"t": TableConstraints(keys=(("x",),))}, TYPES)
    result = be.check_bounded("SELECT x FROM t", "SELECT x FROM t WHERE x IS NOT NULL", schema, rows=2, dialect="bigquery", timeout_ms=2000)
    assert result.status is not be.BoundedStatus.DIFFERENT, result.counterexample


def test_bounded_foreign_key_needs_a_non_null_parent():
    constraints = {"c": TableConstraints(not_null=frozenset({"x"}), foreign_keys=((("x",), "p", ("x",)),))}
    schema = be.schema_from_prover(FK_COLUMNS, constraints, FK_TYPES)
    right = "SELECT c.x FROM c WHERE EXISTS (SELECT 1 FROM p WHERE p.x = c.x)"
    result = be.check_bounded("SELECT x FROM c", right, schema, rows=2, dialect="bigquery", timeout_ms=2000)
    assert result.status is not be.BoundedStatus.DIFFERENT, result.counterexample


def test_bounded_numeric_stays_in_range_and_scale():
    schema = be.BoundedSchema({"t": be.BTable("t", [be.BColumn("x", "NUMERIC")])})
    # no NUMERIC reaches 1e29
    result = be.check_bounded("SELECT x FROM t WHERE x >= 1e30", "SELECT x FROM t WHERE FALSE", schema, rows=2, dialect="bigquery", timeout_ms=2000)
    assert result.status is not be.BoundedStatus.DIFFERENT, result.counterexample
    # nor holds a tenth decimal digit
    result = be.check_bounded("SELECT x FROM t WHERE x * 1000000000 <> FLOOR(x * 1000000000)", "SELECT x FROM t WHERE FALSE", schema, rows=2, dialect="bigquery", timeout_ms=2000)
    assert result.status is not be.BoundedStatus.DIFFERENT, result.counterexample
    # a NUMERIC difference is still found
    result = be.check_bounded("SELECT x FROM t WHERE x > 0.5", "SELECT x FROM t WHERE x > 1", schema, rows=2, dialect="bigquery", timeout_ms=2000)
    assert result.status is be.BoundedStatus.DIFFERENT


@pytest.mark.parametrize("label", ["nan_eq", "nan_gt", "safe_overflow"])
def test_direct_executed_search_does_not_refute(label):
    from kumosql.executed_refutation import search_counterexample

    left, right = EQUAL_IN_BIGQUERY[label]
    assert search_counterexample(left, right, schema=COLUMNS, types=TYPES) is None
