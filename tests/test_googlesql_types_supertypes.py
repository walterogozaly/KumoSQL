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


STRUCTS = Catalog.from_types({"t": {"s": "STRUCT<a INT32, b INT64>", "n": "NUMERIC", "u": "STRUCT<x NUMERIC, y INT32>"}})


def struct_type(sql: str) -> str | None:
    columns = infer(sql, STRUCTS).columns
    assert columns is not None and len(columns) == 1
    return columns[0].type.sql() if columns[0].type is not None else None


def test_struct_literal_fields_coerce_like_literals():
    # (NULL, NULL) and (1, 2) are struct literals: their fields take the typed struct's field types.
    assert struct_type("SELECT [(NULL, NULL), s] FROM t") == "ARRAY<STRUCT<INT32, INT64>>"
    assert struct_type("SELECT [(1, 2), s] FROM t") == "ARRAY<STRUCT<INT32, INT64>>"
    assert struct_type("SELECT [(1, 2.5), s] FROM t") == "ARRAY<STRUCT<INT32, FLOAT64>>"


def test_struct_field_names_come_from_the_first_argument():
    assert struct_type("SELECT [STRUCT(1 AS a, 'x' AS b), (2, 'y')]") == "ARRAY<STRUCT<a INT64, b STRING>>"
    assert struct_type("SELECT [(1, 'x'), STRUCT(2 AS a, 'y' AS b)]") == "ARRAY<STRUCT<INT64, STRING>>"
    assert struct_type("SELECT [STRUCT<a INT64>(1), STRUCT<b INT64>(2)]") == "ARRAY<STRUCT<a INT64>>"


def test_struct_supertype_needs_the_same_field_count():
    assert struct_type("SELECT [STRUCT<a INT64>(1), STRUCT<b INT64, c INT64>(2, 3)]") is None


def test_null_field_of_the_first_argument_gives_a_nested_struct_no_names():
    inner = "STRUCT<a INT64, b STRUCT<c INT64, d INT64>>(1, (1, 2))"
    assert struct_type(f"SELECT [(NULL, NULL), {inner}]") == "ARRAY<STRUCT<INT64, STRUCT<INT64, INT64>>>"
    assert struct_type(f"SELECT [{inner}, (NULL, NULL)]") == "ARRAY<STRUCT<a INT64, b STRUCT<c INT64, d INT64>>>"


def test_struct_constructor_that_may_not_be_a_literal_must_agree_both_ways():
    # STRUCT(n, 1) has a column field, so GoogleSQL may or may not coerce its literal field to INT32; unknown then.
    assert struct_type("SELECT [(NULL, 1.5), STRUCT(n, 1)] FROM t") == "ARRAY<STRUCT<NUMERIC, FLOAT64>>"
    assert struct_type("SELECT [STRUCT(n, 1), u] FROM t") is None
