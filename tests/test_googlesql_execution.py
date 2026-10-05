from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from kumosql import counterexample
from kumosql.counterexample import Column, Counterexample, Spec, Table
from kumosql.result_equivalence import (
    ExecutionError,
    ResultEquivalenceStatus,
    SyntheticDataset,
    SyntheticTable,
    check_result_equivalence,
    execute_on_dataset,
)


def test_execute_on_dataset_uses_typed_googlesql_payloads():
    schema = {"p.d.t": {"id": "INT64", "amount": "NUMERIC", "created_at": "TIMESTAMP"}}
    dataset = SyntheticDataset(
        seed=0,
        tables={
            "p.d.t": SyntheticTable(
                columns=(("id", "INT64"), ("amount", "NUMERIC"), ("created_at", "TIMESTAMP")),
                rows=((1, Decimal("1.25"), datetime(2024, 1, 2, 3, 4, 5)),),
            )
        },
    )

    output, sql = execute_on_dataset(
        "SELECT amount, created_at FROM `p.d.t`", schema, dataset, engine="googlesql"
    )

    assert output.columns == ("amount", "created_at")
    assert output.rows == ((Decimal("1.25"), datetime(2024, 1, 2, 3, 4, 5)),)
    assert output.deterministic and not output.inexact
    assert sql == ["SELECT amount, created_at FROM `p.d.t`"]


def test_check_result_equivalence_googlesql_reports_difference_without_duckdb(monkeypatch):
    import kumosql.result_equivalence as result_equivalence

    monkeypatch.setattr(result_equivalence, "_connect", lambda *args, **kwargs: pytest.fail("DuckDB was used"))
    schema = {"t": {"a": "INT64"}}

    same = check_result_equivalence("SELECT a FROM t", "SELECT a FROM t", schema, seeds=(1, 2), engine="googlesql")
    different = check_result_equivalence(
        "SELECT a FROM t", "SELECT a + 1 AS a FROM t", schema, seeds=(1,), engine="googlesql"
    )

    assert same.status is ResultEquivalenceStatus.EQUIVALENT
    assert same.engine == "googlesql"
    assert different.status is ResultEquivalenceStatus.DIFFERENT
    assert different.engine == "googlesql"


def test_check_result_equivalence_googlesql_declines_unsupported_sql():
    result = check_result_equivalence(
        "SELECT CONTAINS_SUBSTR('abc', 'b')", "SELECT TRUE", {}, seeds=(1,), engine="googlesql"
    )

    assert result.status is ResultEquivalenceStatus.INCONCLUSIVE
    assert "does not support" in result.reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ANY_VALUE(x) FROM UNNEST([1, 2]) AS x",
        "SELECT LOG(2.0)",
    ],
)
def test_check_result_equivalence_googlesql_declines_unreliable_results(sql):
    result = check_result_equivalence(sql, sql, {}, seeds=(1,), engine="googlesql")

    assert result.status is ResultEquivalenceStatus.INCONCLUSIVE


def test_find_counterexample_googlesql_does_not_create_a_duckdb_connection(monkeypatch):
    monkeypatch.setattr(counterexample, "duckdb", None)
    monkeypatch.setattr(counterexample, "small_database", lambda: pytest.fail("DuckDB was used"))
    monkeypatch.setenv("KUMOSQL_TARGETED", "0")
    spec = Spec({"t": Table("t", [Column("a", "INT")])})

    found = counterexample.find_counterexample(
        spec,
        "SELECT a FROM t",
        "SELECT a + 1 AS a FROM t",
        dialect="bigquery",
        engine="googlesql",
        trials=12,
        seed=0,
    )

    assert isinstance(found, Counterexample)
    assert found.tables["t"]
