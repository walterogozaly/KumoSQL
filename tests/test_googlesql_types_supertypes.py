"""GoogleSQL supertype rules of the type checker (``GetCommonSuperTypeImpl`` in the GoogleSQL coercer)."""

from __future__ import annotations

from kumosql.googlesql_types import Catalog, infer

CATALOG = Catalog.from_types({"t": {"i": "INT64", "n": "NUMERIC", "b": "BIGNUMERIC", "f": "FLOAT64"}})


def column_type(sql: str) -> str | None:
    columns = infer(sql, CATALOG).columns
    assert columns is not None and len(columns) == 1
    return columns[0].type.sql() if columns[0].type is not None else None


def test_float64_needs_a_floating_point_input():
    # A float literal counts as floating point: NUMERIC is not a candidate because no input is NUMERIC.
    assert column_type("SELECT COALESCE(i, 2.5) FROM t") == "FLOAT64"
    assert column_type("SELECT IF(TRUE, i, 2.5) FROM t") == "FLOAT64"
    assert column_type("SELECT CASE WHEN TRUE THEN 1 ELSE 2.5 END") == "FLOAT64"


def test_numeric_and_bignumeric_need_an_input_of_that_type():
    assert column_type("SELECT COALESCE(n, 2.5) FROM t") == "NUMERIC"
    assert column_type("SELECT COALESCE(i, n) FROM t") == "NUMERIC"
    assert column_type("SELECT COALESCE(i, b) FROM t") == "BIGNUMERIC"
    assert column_type("SELECT COALESCE(n, f) FROM t") == "FLOAT64"
    assert column_type("SELECT COALESCE(i, 2) FROM t") == "INT64"
