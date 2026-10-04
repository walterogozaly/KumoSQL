"""A proof of a query that names a type BigQuery does not have is refused (each name was rejected by a dry run)."""

from __future__ import annotations

import pytest

from kumosql import apply_rule, prove_equivalent
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.rewrite import VerificationStatus
from kumosql.smt_equivalence import prove_equivalent_smt
from kumosql.type_names import BIGQUERY_TYPE_NAMES, invalid_type_name

# Each of these returned "Type not found: <name>" from a BigQuery dry run.
REJECTED = ["FLOAT", "INT32", "UUID", "DOUBLE", "VARCHAR", "TEXT", "REAL", "INT8", "BINARY", "TIMESTAMP_NTZ", "CHAR"]
# Each of these was accepted.
ACCEPTED = ["INT64", "INT", "SMALLINT", "INTEGER", "BIGINT", "TINYINT", "BYTEINT", "NUMERIC", "DECIMAL", "BIGNUMERIC",
            "BIGDECIMAL", "FLOAT64", "BOOL", "BOOLEAN", "STRING", "BYTES", "DATE", "DATETIME", "TIME", "TIMESTAMP",
            "INTERVAL", "GEOGRAPHY", "JSON"]


@pytest.mark.parametrize("name", REJECTED)
def test_a_type_name_bigquery_rejects_is_found(name):
    assert name in (invalid_type_name(f"SELECT CAST(x AS {name}) FROM t") or "")
    assert name in (invalid_type_name(f"SELECT SAFE_CAST(x AS {name}) FROM t") or "")


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_type_name_bigquery_accepts_passes(name):
    assert invalid_type_name(f"SELECT CAST(x AS {name}) FROM t") is None
    assert name in BIGQUERY_TYPE_NAMES


@pytest.mark.parametrize("sql,name", [
    ("SELECT CAST(x AS MAP<STRING, INT64>) FROM t", "MAP"),  # BigQuery: "MAP datatype is not supported"
    ("SELECT CAST(x AS ARRAY<FLOAT>) FROM t", "FLOAT"),  # BigQuery: "Type not found: FLOAT"
    ("SELECT CAST(x AS STRUCT<a UUID, b ARRAY<INT64>>) FROM t", "UUID"),
    ("SELECT CAST(CAST(x AS INT64) AS DOUBLE PRECISION) FROM t", "DOUBLE PRECISION"),
    ("SELECT CAST((SELECT a AS b FROM u) AS VARCHAR) FROM t", "VARCHAR"),
    ("SELECT cast(x as text) FROM t", "TEXT"),
])
def test_nested_and_lowercase_names_are_found(sql, name):
    assert name in invalid_type_name(sql)


@pytest.mark.parametrize("sql", [
    "SELECT CAST(x AS ARRAY<FLOAT64>) FROM t",
    "SELECT CAST(x AS STRUCT<a FLOAT64, b ARRAY<INT>>) FROM t",
    "SELECT CAST(x AS STRING FORMAT 'YYYY') FROM t",
    "SELECT CAST(x AS RANGE<DATE>) FROM t",
    "SELECT float, uuid FROM t",  # a column named like a type
    "SELECT x AS float FROM t",
    "SELECT SAFE_CAST(x AS NUMERIC) FROM t",
    "not sql at all '",
])
def test_valid_or_unrelated_text_passes(sql):
    assert invalid_type_name(sql) is None


@pytest.mark.parametrize("bad,good", [
    ("FLOAT", "FLOAT64"), ("INT32", "INT64"), ("UUID", "STRING"), ("VARCHAR", "STRING"), ("DOUBLE", "FLOAT64"),
])
def test_every_prover_refuses_a_pair_with_a_name_bigquery_rejects(bad, good):
    left = f"SELECT CAST(x AS {bad}) AS y FROM t"
    right = f"SELECT CAST(x AS {good}) AS y FROM t"
    assert not prove_equivalent(left, right).proven
    assert not prove_equivalent_smt(left, right).proven
    assert not prove_equivalent_algebraic(left, right).proven
    # the same pair is refused even when both sides name the invalid type
    assert not prove_equivalent(left, left).proven
    assert prove_equivalent(right, right).proven


def test_the_refusal_says_why():
    result = prove_equivalent("SELECT CAST(x AS FLOAT) FROM t", "SELECT CAST(x AS FLOAT64) FROM t")
    assert "FLOAT" in result.reason and "BigQuery" in result.reason


def test_a_rewrite_of_a_statement_with_a_rejected_type_is_unproven():
    result = apply_rule("remove_redundant_parentheses", "SELECT CAST((x) AS INT32) AS y FROM t WHERE ((a))")
    assert result.verification.status is VerificationStatus.UNPROVEN
    # the same rewrite with a valid type is proven
    ok = apply_rule("remove_redundant_parentheses", "SELECT CAST((x) AS INT64) AS y FROM t WHERE ((a))")
    assert ok.verification.status is VerificationStatus.PROVEN
