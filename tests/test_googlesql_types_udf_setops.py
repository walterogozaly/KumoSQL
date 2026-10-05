"""GoogleSQL typer: user-defined functions, CAST .. FORMAT, set-operation witnesses, array paths and the other shapes
the compliance tests exercise (``tools/googlesql_types_eval.py``). Every case builds its own catalog."""

from __future__ import annotations

import sqlglot

from kumosql.googlesql_types import Catalog, Column, GType, infer


def types(sql: str, catalog: Catalog | None = None) -> list[str | None]:
    typed = infer(sql, catalog or Catalog())
    assert typed.columns is not None, typed.error
    return [c.type.sql() if c.type is not None else None for c in typed.columns]


def one(sql: str, catalog: Catalog | None = None) -> str | None:
    (only,) = types(sql, catalog)
    return only


def functions(*statements: str, names: tuple[str, ...] = ()) -> Catalog:
    catalog = Catalog.from_types({}, functions=names)
    for statement in statements:
        assert catalog.add_function_sql(statement), statement
    return catalog


# --- user-defined functions ------------------------------------------------------------------------------------------

def test_catalog_functions_can_carry_a_return_type():
    catalog = Catalog.from_types({}, functions={"F": "ARRAY<STRING>", "g": GType("INT64"), "h": None})
    assert one("SELECT f(1)", catalog) == "ARRAY<STRING>"
    assert one("SELECT G()", catalog) == "INT64"
    assert one("SELECT h()", catalog) is None
    assert one("SELECT UPPER('a')", Catalog.from_types({}, functions=["upper"])) is None  # a name only: unknown


def test_a_declared_return_type_is_the_call_type():
    catalog = functions(
        "CREATE TEMP FUNCTION IntegerToDouble(x INT64) RETURNS FLOAT64 AS (CAST(x AS FLOAT64))",
        "create or replace function ds.pair(a STRING, b INT64 DEFAULT 1) returns STRUCT<k STRING, v INT64> as ((a, b))",
        "CREATE TEMP FUNCTION js(s STRING) RETURNS ARRAY<NUMERIC> LANGUAGE js AS r'''return [1];'''",
        "CREATE TEMP FUNCTION opts(x INT64) RETURNS INT64 DETERMINISTIC LANGUAGE sql OPTIONS (description = 'a b') AS (x)",
    )
    assert types("SELECT IntegerToDouble(1), ds.pair('a'), js('x'), opts(1), SAFE.IntegerToDouble(2)", catalog) == [
        "FLOAT64", "STRUCT<k STRING, v INT64>", "ARRAY<NUMERIC>", "INT64", "FLOAT64"]


def test_a_function_without_returns_has_its_body_type_when_every_parameter_is_declared():
    catalog = functions(
        "CREATE TEMP FUNCTION Thousand() AS (1000)",
        "CREATE TEMP FUNCTION StringIdentity(s STRING) AS (s)",
        "CREATE TEMP FUNCTION TimesThousand(x INT64) AS (x * Thousand())",
        "CREATE TEMP FUNCTION Half(x NUMERIC) AS (x / 2)",
        "CREATE TEMP AGGREGATE FUNCTION SumSquares(a INT64) AS (SUM(a * a))",
        "CREATE TEMP AGGREGATE FUNCTION CountPlusOne() AS (COUNT(*) + 1)",
        "CREATE TEMP FUNCTION Wrapped(a INT64, b STRING) AS (STRUCT(a AS x, b AS y))",
    )
    assert types("SELECT Thousand(), StringIdentity('a'), TimesThousand(2), Half(NUMERIC '1'), "
                 "SumSquares(1), CountPlusOne(), Wrapped(1, 'a')", catalog) == [
        "INT64", "STRING", "INT64", "NUMERIC", "INT64", "INT64", "STRUCT<x INT64, y STRING>"]


def test_templated_or_uncertain_functions_stay_unknown():
    catalog = functions(
        "CREATE TEMP FUNCTION IdentityTemplate(x ANY TYPE) AS (x)",
        "CREATE TEMP FUNCTION Declared(x ANY TYPE) RETURNS STRING AS ('a')",  # a declared type is certain
        "CREATE TEMP FUNCTION Js(x INT64) LANGUAGE js AS 'return 1;'",
        "CREATE TEMP FUNCTION Single(x FLOAT) RETURNS FLOAT AS (x)",  # FLOAT: FLOAT32 in GoogleSQL, FLOAT64 in a schema
        "CREATE TEMP FUNCTION Broken(x INT64) AS (x + 'a')",
        "CREATE TEMP FUNCTION Missing(x INT64) AS (UNKNOWN_FUNCTION(x))",
        "CREATE TEMP FUNCTION WrongName(x INT64) AS (y)",
        "CREATE TEMP FUNCTION Proto(x INT64) RETURNS `pkg.Message` AS (NULL)",
    )
    assert types("SELECT IdentityTemplate(1), Declared(1), Js(1), Single(1), Broken(1), Missing(1), WrongName(1), "
                 "Proto(1)", catalog) == [None, "STRING", None, None, None, None, None, None]


def test_a_user_function_hides_the_built_in_of_the_same_name():
    assert one("SELECT COUNT(1)") == "INT64"
    assert one("SELECT COUNT(1)", functions("CREATE TEMP FUNCTION count(x INT64) RETURNS STRING AS ('a')")) == "STRING"
    # A function the catalog only lists by name never falls back to the built-in.
    assert one("SELECT COUNT(1)", Catalog.from_types({}, functions=["count"])) is None


def test_names_registered_first_are_not_guessed_in_a_body():
    # `later` is declared before `outer_fn` is typed: `outer_fn` calls it, so it has to be unknown, not a built-in's type.
    catalog = Catalog.from_types({}, functions=["later", "outer_fn"])
    assert catalog.add_function_sql("CREATE TEMP FUNCTION outer_fn() AS (later())")
    assert catalog.add_function_sql("CREATE TEMP FUNCTION later() AS (1)")
    assert types("SELECT outer_fn(), later()", catalog) == [None, "INT64"]


def test_table_functions_with_a_declared_schema():
    catalog = functions(
        "CREATE TEMP TABLE FUNCTION tvf(x INT64) RETURNS TABLE<a INT64, b STRING> AS (SELECT x AS a, 'q' AS b)",
        "CREATE TEMP TABLE FUNCTION open_tvf(x INT64) AS (SELECT x AS a)",
    )
    assert types("SELECT * FROM tvf(1)", catalog) == ["INT64", "STRING"]
    assert types("SELECT t.b FROM tvf(1) AS t", catalog) == ["STRING"]
    assert infer("SELECT * FROM open_tvf(1)", catalog).columns is None


def test_statements_that_are_not_functions_register_nothing():
    catalog = Catalog()
    assert not catalog.add_function_sql("CREATE TABLE t AS SELECT 1")
    assert not catalog.add_function_sql("SELECT 1")
    assert catalog.functions == set()


# --- CAST(x AS TIMESTAMP | DATETIME | TIME FORMAT ..) ------------------------------------------------------------------

def test_cast_format_types_come_from_the_query_text():
    assert one("SELECT CAST('2020' AS TIMESTAMP FORMAT 'YYYY')") == "TIMESTAMP"
    assert one("SELECT CAST('2020' AS TIMESTAMP FORMAT 'YYYY' AT TIME ZONE 'UTC')") == "TIMESTAMP"
    assert one("SELECT SAFE_CAST('1234' AS DATETIME FORMAT 'tzh')") == "DATETIME"
    assert one("SELECT cast('07:08:09' as time format 'hh24:mi:ss')") == "TIME"
    assert one("SELECT CAST('2020' AS DATE FORMAT 'YYYY')") == "DATE"


def test_cast_formats_of_different_types_in_the_select_list_keep_their_order():
    assert types("SELECT CAST('a' AS TIMESTAMP FORMAT 'YYYY') AS x, CAST('b' AS TIME FORMAT 'hh') AS y, "
                 "CAST('c' AS DATETIME FORMAT 'YYYY')") == ["TIMESTAMP", "TIME", "DATETIME"]


def test_mixed_cast_formats_elsewhere_are_unknown():
    # Text order and tree order are not known to agree outside the select list, so nothing is guessed.
    sql = ("SELECT CAST(CAST('a' AS TIMESTAMP FORMAT 'YYYY') AS STRING) AS s, CAST('b' AS TIME FORMAT 'hh') AS t")
    assert types(sql) == ["STRING", "TIME"] or types(sql) == ["STRING", None]
    mixed = "SELECT IF(TRUE, CAST('a' AS TIMESTAMP FORMAT 'YYYY'), CAST('b' AS DATETIME FORMAT 'YYYY'))"
    assert one(mixed) is None


def test_a_tree_has_no_cast_format_text_to_read():
    tree = sqlglot.parse_one("SELECT CAST('2020' AS DATETIME FORMAT 'YYYY')", read="bigquery")
    assert one_tree(tree) is None


def one_tree(tree) -> str | None:
    typed = infer(tree, Catalog())
    assert typed.columns is not None
    column = typed.columns[0].type
    return column.sql() if column is not None else None


# --- statements ---------------------------------------------------------------------------------------------------------

def test_a_trailing_semicolon_and_comment_is_not_a_second_statement():
    sql = "SELECT 1 AS a,  -- one\n       2.5 AS b;  -- two"
    assert types(sql) == ["INT64", "FLOAT64"]
    assert types("SELECT 1;") == ["INT64"]


def test_several_statements_are_not_typed():
    typed = infer("SELECT 1; SELECT 2", Catalog())
    assert typed.columns is None and typed.error


# --- INT32 and FLOAT in GoogleSQL text ------------------------------------------------------------------------------------

def test_int_is_int32_only_when_int32_is_the_one_spelling():
    assert one("SELECT CAST(1 AS INT32)") == "INT32"
    assert one("SELECT CAST(1 AS INT64)") == "INT64"
    assert one("SELECT CAST(1 AS INT)") == "INT64"
    assert one("SELECT CAST(1 AS INT32) + CAST(1 AS INT64)") == "INT64"
    assert types("SELECT CAST(1 AS INT32), CAST(1 AS INTEGER)") == [None, None]  # INTEGER may be INT64: ambiguous


def test_float_is_float32_in_a_query_that_uses_int32():
    assert one("SELECT CAST(1 AS INT32) + CAST(1 AS FLOAT)") == "FLOAT64"
    assert one("SELECT CAST(1 AS FLOAT)") is None  # BigQuery has no FLOAT in queries; nothing says which is meant
    assert types("SELECT CAST(1 AS INT32), CAST(1 AS FLOAT)") == ["INT32", "FLOAT32"]


def test_float32_with_a_numeric_literal_is_float64():
    catalog = Catalog.from_types({"t": {"f": "FLOAT32", "i": "INT64"}})
    assert types("SELECT f + 0.125, f * 2, f / 2, f + i, f + f FROM t", catalog) == ["FLOAT64"] * 5
    assert one("SELECT CAST(1 AS INT32) + CAST(2.5 AS FLOAT)") == "FLOAT64"


# --- set operations -------------------------------------------------------------------------------------------------------

def test_n_ary_full_corresponding_types_padded_nulls_as_literals():
    # The NULL pad of `a` and INT32 combine as INT32 only in one n-ary operation, not pairwise.
    sql = ("SELECT NULL AS a FULL UNION ALL CORRESPONDING SELECT 1 AS b "
           "FULL UNION ALL CORRESPONDING SELECT 1 AS b, CAST(1 AS INT32) AS a")
    typed = infer(sql, Catalog())
    assert [(c.name, c.type.sql()) for c in typed.columns] == [("a", "INT32"), ("b", "INT64")]
    pairwise = ("(SELECT NULL AS a FULL UNION ALL CORRESPONDING SELECT 1 AS b) "
                "FULL UNION ALL CORRESPONDING SELECT 1 AS b, CAST(1 AS INT32) AS a")
    assert types(pairwise) == ["INT64", "INT64"]


def test_a_nested_set_operation_is_not_a_literal():
    sql = ("(SELECT 1 AS a, 1 AS b FULL UNION ALL CORRESPONDING SELECT 1 AS b, NULL AS a) "
           "FULL UNION ALL CORRESPONDING SELECT 1 AS b, CAST(1 AS INT32) AS a")
    typed = infer(sql, Catalog())
    assert [(c.name, c.type.sql()) for c in typed.columns] == [("a", "INT64"), ("b", "INT64")]
    # The same operation as one n-ary operation (no parentheses) keeps the literals and gives INT32.
    flat = ("SELECT 1 AS a, 1 AS b FULL UNION ALL CORRESPONDING SELECT 1 AS b, NULL AS a "
            "FULL UNION ALL CORRESPONDING SELECT 1 AS b, CAST(1 AS INT32) AS a")
    assert types(flat) == ["INT32", "INT64"]


def test_by_name_on_names_the_output_as_the_on_list_is_written():
    typed = infer("SELECT 1 AS a, 2 AS b UNION ALL BY NAME ON (B, a) SELECT 3 AS b, 4 AS a", Catalog())
    assert [(c.name, c.type.sql()) for c in typed.columns] == [("B", "INT64"), ("a", "INT64")]


def test_integer_literals_above_int64_are_not_int64():
    assert one("SELECT 9223372036854775807") == "INT64"
    assert one("SELECT 9223372036854775808") is None  # UINT64: this typer does not compute with it
    assert one("SELECT 18446744073709551615") is None


def test_struct_unions_coerce_field_by_field():
    assert one("SELECT STRUCT<INT64, DOUBLE>(3, 5.5) UNION ALL SELECT STRUCT<DOUBLE, INT32>(1.4, 2)") == \
        "STRUCT<FLOAT64, FLOAT64>"
    assert one("SELECT STRUCT<INT64, DOUBLE>(3, 5.5) UNION DISTINCT SELECT STRUCT<DOUBLE, INT32>(1.4, 2)") == \
        "STRUCT<FLOAT64, FLOAT64>"


def test_set_operations_of_as_struct_and_as_value_results():
    assert one("SELECT ARRAY(SELECT AS STRUCT 1, 2 UNION ALL SELECT AS STRUCT 3, 4)") == "ARRAY<STRUCT<INT64, INT64>>"
    assert one("SELECT (SELECT AS VALUE 1 UNION ALL SELECT AS VALUE 2.5)") == "FLOAT64"
    # At the top level a SELECT AS STRUCT result is its fields.
    assert types("SELECT AS STRUCT 1, 2 UNION DISTINCT SELECT AS STRUCT 1, 3") == ["INT64", "INT64"]
    assert infer("SELECT 1 UNION ALL SELECT AS VALUE 2", Catalog()).columns is None


def test_a_with_clause_on_a_recursive_union_is_seen_by_both_terms():
    sql = ("WITH RECURSIVE a AS (WITH RECURSIVE b AS (SELECT 0 AS n UNION ALL (SELECT n + 1 FROM b WHERE n < 5)) "
           "SELECT * FROM b UNION ALL SELECT 10 + n FROM a WHERE n < 30) SELECT * FROM a")
    assert types(sql) == ["INT64"]


def test_null_branches_through_set_operations():
    assert types("SELECT * FROM (SELECT 1 AS k, NULL AS v UNION ALL SELECT 2, 10 UNION ALL SELECT 3, NULL)") == \
        ["INT64", "INT64"]
    assert one("SELECT v FROM (SELECT NULL AS v UNION ALL SELECT CAST(100 AS INT32))") == "INT32"
    assert one("SELECT v FROM (SELECT NULL AS v UNION ALL SELECT 's')") == "STRING"


# --- ERROR, ARRAY_AGG, subscripts ---------------------------------------------------------------------------------------------

def test_error_coerces_like_an_untyped_null():
    assert one("SELECT ERROR('m')") == "INT64"
    assert one("SELECT SAFE.ERROR('m')") == "INT64"
    assert one("SELECT IF(FALSE, ERROR('m'), 'foo')") == "STRING"
    assert one("SELECT IF(TRUE, JSON '1', ERROR('error'))") == "JSON"
    assert one("SELECT COALESCE(ERROR('x'), 2.5)") == "FLOAT64"


def test_array_agg_of_arrays_is_an_array_of_arrays():
    assert one("SELECT ARRAY_AGG(arr) FROM (SELECT [1, 2] AS arr UNION ALL SELECT [] UNION ALL SELECT [3, 4])") == \
        "ARRAY<ARRAY<INT64>>"
    assert one("SELECT ARRAY_AGG(a) FROM (SELECT [[1, 2], [3]] AS a)") == "ARRAY<ARRAY<ARRAY<INT64>>>"
    assert one("SELECT ARRAY_AGG(x) FROM UNNEST([1, 2]) x") == "ARRAY<INT64>"


def test_struct_subscripts_use_the_position_and_the_offset_kind():
    assert types("SELECT STRUCT(1 AS a, 'x' AS b)[OFFSET(1)], STRUCT(1 AS a, 'x' AS b)[ORDINAL(1)]") == ["STRING", "INT64"]
    assert one("WITH t AS (SELECT STRUCT('s' AS d, [1, 2] AS d) AS d) SELECT t[OFFSET(0)][OFFSET(1)] FROM t") == "ARRAY<INT64>"
    assert one("WITH t AS (SELECT STRUCT('s' AS d, [1, 2] AS d) AS d) SELECT t[ORDINAL(1)][OFFSET(0)] FROM t") == "STRING"
    # A bare subscript, a SAFE_ one and an out-of-range one say nothing certain.
    assert one("SELECT STRUCT(3.14 AS a, 2.78 AS b)[1]") is None
    assert one("SELECT STRUCT(1 AS a, 'x' AS b)[SAFE_OFFSET(1)]") is None
    assert one("SELECT STRUCT(1 AS a, 'x' AS b)[OFFSET(2)]") is None


# --- array paths ------------------------------------------------------------------------------------------------------------

def test_flatten_reads_a_field_of_every_element():
    assert one("SELECT FLATTEN([STRUCT([1, 2] AS a), STRUCT([4] AS a)].a)") == "ARRAY<INT64>"
    assert one("SELECT FLATTEN([STRUCT(STRUCT(5 AS y) AS x), STRUCT(STRUCT(6) AS x)].x.y)") == "ARRAY<INT64>"
    assert one("SELECT FLATTEN([STRUCT([STRUCT(1 AS y)] AS x)].x.y)") == "ARRAY<INT64>"
    assert one("SELECT FLATTEN(ARRAY<STRUCT<int64_val INT64>>[].int64_val)") == "ARRAY<INT64>"
    assert one("SELECT FLATTEN([STRUCT(JSON '1' AS x, 2 AS y), (JSON '11', 22)].x)") == "ARRAY<JSON>"
    assert one("SELECT FLATTEN([STRUCT(JSON '{\"f\": 1}' AS x)].x.f)") == "ARRAY<JSON>"


def test_flatten_with_subscripts():
    arrays = "[STRUCT([1, 2, 3] AS a), STRUCT([4, 5] AS a)]"
    assert one(f"SELECT FLATTEN({arrays}.a[OFFSET(1)])") == "ARRAY<INT64>"  # per element
    assert one(f"SELECT FLATTEN({arrays}.a[SAFE_OFFSET(2)])") == "ARRAY<INT64>"
    assert one(f"SELECT FLATTEN({arrays}.a)[OFFSET(0)]") == "INT64"  # of the flattened array
    assert one(f"SELECT FLATTEN({arrays}[OFFSET(1)].a)") == "ARRAY<INT64>"
    assert one(f"SELECT FLATTEN({arrays}.a[OFFSET(1)])[OFFSET(0)]") == "INT64"
    assert one("SELECT FLATTEN(1)") is None


def test_unnest_flattens_an_array_path():
    assert one("SELECT v FROM UNNEST([STRUCT(1 AS x), STRUCT(2)].x) AS v") == "INT64"
    assert one("SELECT v FROM UNNEST([STRUCT([STRUCT(2 AS y)] AS x), STRUCT([STRUCT(4)])].x.y) AS v") == "INT64"
    assert types("SELECT v, o FROM UNNEST([STRUCT('a' AS s)].s) AS v WITH OFFSET o") == ["STRING", "INT64"]


def test_arrays_that_differ_only_in_struct_field_names_have_a_common_type():
    sql = "SELECT v FROM UNNEST([STRUCT([STRUCT(5 AS y)] AS x), STRUCT([STRUCT(NULL AS y)]), STRUCT([STRUCT(6)])].x.y) AS v"
    assert one(sql) == "INT64"
    assert one("SELECT [[1], [2.5]]") is None  # element types that differ are not given a common array type here


def test_a_path_into_an_earlier_range_is_an_implicit_unnest():
    catalog = Catalog.from_types({"t": {"id": "INT64", "vals": "ARRAY<STRING>", "info": "STRUCT<tags ARRAY<STRING>>",
                                         "rows": "ARRAY<STRUCT<k INT64, items ARRAY<STRING>>>"}})
    assert one("SELECT v FROM t, t.vals v", catalog) == "STRING"
    assert one("SELECT ARRAY(SELECT v FROM t.vals v ORDER BY v LIMIT 1) FROM t", catalog) == "ARRAY<STRING>"
    assert one("SELECT (SELECT ARRAY_AGG(s) FROM t.info.tags s) FROM t", catalog) == "ARRAY<STRING>"
    assert one("SELECT i FROM t, t.rows r, r.items i", catalog) == "STRING"
    assert one("SELECT i FROM t, t.rows.items i", catalog) is None  # a multi-part name over an array: not modelled
    assert one("SELECT x FROM t, t.id x", catalog) is None  # not an array


# --- joins and ranges --------------------------------------------------------------------------------------------------------

def test_parenthesized_and_nested_joins():
    catalog = Catalog.from_types({"r": {"id": "INT64", "s": "STRING"}, "s": {"id": "INT64", "n": "NUMERIC"},
                                  "d": {"row_id": "INT64", "day": "DATE"}})
    expected = ["INT64", "STRING", "INT64", "NUMERIC", "INT64", "DATE"]
    assert types("SELECT * FROM r JOIN (s JOIN d ON s.id = d.row_id) ON r.id = s.id", catalog) == expected
    assert types("SELECT * FROM r JOIN s JOIN d ON s.id = d.row_id ON r.id = s.id", catalog) == expected
    assert one("SELECT d.day FROM r JOIN (s JOIN d ON s.id = d.row_id) ON r.id = s.id", catalog) == "DATE"


def test_lateral_subqueries_read_the_ranges_to_their_left():
    catalog = Catalog.from_types({"r": {"id": "INT64", "s": "STRING"}, "s": {"id": "INT64", "s": "STRING"}})
    sql = "SELECT r.id, l.s, l.id FROM r CROSS JOIN LATERAL (SELECT r.s || s.s AS s, s.id FROM s WHERE s.s > r.s) AS l"
    assert types(sql, catalog) == ["INT64", "STRING", "INT64"]
    assert types("SELECT * FROM r, LATERAL (SELECT id AS rid FROM s WHERE s.id = r.id) AS l", catalog) == [
        "INT64", "STRING", "INT64"]


def test_multiway_unnest_has_a_column_per_array():
    assert types("SELECT * FROM UNNEST([1, 2], ['a']) WITH OFFSET") == ["INT64", "STRING", "INT64"]
    assert types("SELECT * FROM UNNEST([1, 2], []) WITH OFFSET") == ["INT64", "INT64", "INT64"]
    assert types("SELECT * FROM UNNEST(CAST(NULL AS ARRAY<INT64>), CAST(NULL AS ARRAY<STRING>))") == ["INT64", "STRING"]


def test_using_an_unnest_element_column():
    sql = ("SELECT * FROM UNNEST(CAST(NULL AS ARRAY<INT64>)) a RIGHT JOIN UNNEST([1, 1, 2, NULL]) a USING (a)")
    assert types(sql) == ["INT64"]


# --- WITH expressions and the rest --------------------------------------------------------------------------------------------

def test_with_expression_variables_have_their_expressions_types():
    assert one("SELECT WITH(a AS 1, b AS a + 0.5, b)") == "FLOAT64"
    assert types("SELECT WITH(x AS ARRAY_AGG(x), STRUCT(ARRAY_LENGTH(x) AS n, x[OFFSET(0)] AS first)).* "
                 "FROM UNNEST(['p', NULL]) AS x") == ["INT64", "STRING"]
    assert one("SELECT WITH(a AS 'x', ARRAY_TRANSFORM([1, 2], e -> a || CAST(e AS STRING)))") == "ARRAY<STRING>"


def test_a_table_column_is_not_a_catalog_function_column():
    catalog = Catalog()
    catalog.add("t", [Column("a", GType("INT64"))])
    assert one("SELECT a FROM t", catalog) == "INT64"
