"""The ARRAY and STRUCT value model (kumosql.nested_values) and the generators built on it."""

import random

import pytest

from kumosql.nested_values import Array, Struct, build_value, from_python, literal, parse_type, storable, to_json

ARRAY_INT = parse_type("ARRAY<INT64>")
PAIR = parse_type("STRUCT<a INT64, b STRING>")
PARAMS = parse_type("ARRAY<STRUCT<key STRING, value STRUCT<string_value STRING, int_value INT64>>>")


def test_parse_type_reads_nested_types_and_rejects_arrays_of_arrays():
    assert PARAMS.kind == "ARRAY" and PARAMS.element.kind == "STRUCT"
    assert PARAMS.element.field("KEY").kind == "STRING"  # field names are case-insensitive
    assert PARAMS.element.field("value").field("int_value").kind == "INT64"
    assert PARAMS.sql() == "ARRAY<STRUCT<key STRING, value STRUCT<string_value STRING, int_value INT64>>>"
    assert parse_type("ARRAY<ARRAY<INT64>>") is None  # BigQuery has no array of arrays
    assert parse_type("NOT A TYPE <<") is None


def test_a_struct_with_a_repeated_field_name_has_no_single_field_by_that_name():
    t = parse_type("STRUCT<a INT64, a STRING>")
    assert t is None or t.field("a") is None


def test_struct_equality_is_by_position_and_a_tagged_value_is_never_a_plain_tuple():
    assert Struct((("a", 1), ("b", "x"))) == Struct((("c", 1), ("d", "x")))
    assert Struct((("a", 1), ("b", "x"))) != Struct((("a", "x"), ("b", 1)))
    assert hash(Struct((("a", 1),))) == hash(Struct((("z", 1),)))
    assert Array((1, 2)) != (1, 2)
    assert Array((1, 2)) != Struct((("a", 1), ("b", 2)))
    assert Array((1, 2)) == Array((1, 2))
    assert Array((1, 2)) != Array((2, 1))


def test_build_value_never_gives_an_array_a_null_element_and_follows_the_storage_rules():
    rng = random.Random(7)

    def scalar(t, path):
        return None if rng.random() < 0.5 else rng.randint(0, 3)  # half of the draws are NULL

    t = parse_type("ARRAY<INT64>")
    for _ in range(50):
        value = build_value(t, scalar, lambda path: rng.randint(0, 4), lambda path: False)
        assert isinstance(value, Array)
        assert None not in value.elements
        assert storable(value, t)
    # a struct inside an array is never NULL itself, but its fields can be
    t = parse_type("ARRAY<STRUCT<a INT64, b INT64>>")
    seen_null_field = False
    for _ in range(50):
        value = build_value(t, scalar, lambda path: 3, lambda path: rng.random() < 0.5)
        assert all(isinstance(v, Struct) for v in value)
        seen_null_field |= any(f is None for v in value for f in v.values())
        assert storable(value, t)
    assert seen_null_field


def test_storable_refuses_a_null_array_and_a_null_element():
    assert storable(Array(()), ARRAY_INT)
    assert not storable(None, ARRAY_INT)  # BigQuery stores a missing array as []
    assert not storable(Array((1, None)), ARRAY_INT)
    assert storable(None, PAIR)  # a struct may be NULL
    assert storable(Struct((("a", None), ("b", "x"))), PAIR)
    assert not storable(Struct((("a", 1),)), PAIR)  # a field is missing


def test_literals_type_every_null_and_empty_array_for_bigquery():
    # a bare NULL or [] inside a constructor would be typed INT64 and could not be cast to the column type
    assert literal(None, ARRAY_INT, "bigquery") == "CAST(NULL AS ARRAY<INT64>)"
    assert literal(Array(()), ARRAY_INT, "bigquery") == "CAST(ARRAY<INT64>[] AS ARRAY<INT64>)"
    text = literal(Struct((("a", None), ("b", "x"))), PAIR, "bigquery")
    assert text == "CAST(STRUCT(CAST(NULL AS INT64) AS a, 'x' AS b) AS STRUCT<a INT64, b STRING>)"
    text = literal(Struct((("key", "k"), ("value", Struct((("string_value", None), ("int_value", 3)))))), PARAMS.element, "bigquery")
    assert "CAST(NULL AS STRING) AS string_value" in text and "3 AS int_value" in text


def test_literals_run_in_duckdb_with_the_same_values():
    duckdb = pytest.importorskip("duckdb")
    value = Array((Struct((("a", 1), ("b", "x"))), Struct((("a", None), ("b", "it's")))))
    t = parse_type("ARRAY<STRUCT<a INT64, b STRING>>")
    con = duckdb.connect()
    (result,) = con.execute(f"SELECT {literal(value, t, 'duckdb')}").fetchone()
    assert from_python(result, t) == value
    (empty,) = con.execute(f"SELECT {literal(Array(()), t, 'duckdb')}").fetchone()
    assert empty == []
    (missing,) = con.execute(f"SELECT {literal(None, t, 'duckdb')}").fetchone()
    assert missing is None


def test_from_python_reads_a_dict_by_field_name_when_it_names_every_field():
    assert from_python({"b": "x", "a": 1}, PAIR) == Struct((("a", 1), ("b", "x")))
    assert from_python({"B": "x", "A": 1}, PAIR) == Struct((("a", 1), ("b", "x")))  # names match without case
    assert from_python([1, "x"], PAIR) == Struct((("a", 1), ("b", "x")))
    # a dict that does not name every field (DuckDB renames fields it cannot keep) is read by position
    assert from_python({"x": 1, "y": "z"}, PAIR) == Struct((("a", 1), ("b", "z")))
    assert from_python(None, PAIR) is None
    assert from_python([1, 2], ARRAY_INT) == Array((1, 2))


def test_to_json_writes_arrays_as_lists_and_structs_by_name():
    value = Array((Struct((("a", 1), (None, "x"))),))
    assert to_json(value) == [{"a": 1, "_field_2": "x"}]
    assert from_python(to_json(Array((1, 2))), ARRAY_INT) == Array((1, 2))
