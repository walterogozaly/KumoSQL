"""The GoogleSQL typer's parse-failure fallbacks (``kumosql.googlesql_text_fallback``): a query sqlglot cannot parse is
rewritten to one it can, with the same output types, or stays unknown. Each test pairs what is typed with what must
stay unknown, because a confidently wrong type is the only failure."""

import sqlglot

from kumosql.googlesql_text_fallback import UNKNOWN_FUNCTION, rewrite
from kumosql.googlesql_types import Catalog, infer

EMP = Catalog.from_types({
    "Employees": {"dept_id": "INT64", "emp_id": "INT64", "salary": "FLOAT64", "job": "STRING", "age": "INT64"},
    "Series": {"ts": "TIMESTAMP", "job": "STRING", "requests": "INT64"},
})


def types(sql: str, catalog: Catalog | None = None) -> list[tuple[str | None, str | None]] | None:
    typed = infer(sql, catalog or EMP)
    if typed.columns is None:
        return None
    return [(c.name, c.type.sql() if c.type is not None and c.type.complete else None) for c in typed.columns]


def unparsable(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return True
    return False


# --- syntax that cannot change a type -----------------------------------------------------------------------------

def test_an_aggregate_filter_is_dropped():
    sql = "SELECT dept_id, COUNT(* WHERE job = 'x') AS n, ARRAY_AGG(emp_id WHERE age < 40 ORDER BY salary) AS ids " \
          "FROM Employees GROUP BY dept_id"
    assert unparsable(sql)
    assert types(sql) == [("dept_id", "INT64"), ("n", "INT64"), ("ids", "ARRAY<INT64>")]


def test_a_filter_inside_a_subquery_is_not_an_aggregate_filter():
    sql = "SELECT SUM(salary WHERE age > 1) AS s, (SELECT MAX(age) FROM Employees WHERE job = 'x') AS m"
    rewritten = rewrite(sql)
    assert rewritten is not None and "WHERE job = 'x'" in rewritten.sql and "age > 1" not in rewritten.sql


def test_a_multi_level_aggregate_keeps_the_type_of_the_outer_aggregate():
    sql = "SELECT SUM(AVG(salary WHERE age < 40) GROUP BY dept_id HAVING job = 'x') AS s FROM Employees"
    assert unparsable(sql)
    assert types(sql) == [("s", "FLOAT64")]


def test_privacy_options_and_contribution_bounds_are_dropped():
    sql = "SELECT WITH DIFFERENTIAL_PRIVACY OPTIONS(epsilon=1e20, delta=1.0) " \
          "SUM(age, contribution_bounds_per_group => (2, 3)) AS s, AVG(age) AS a, COUNT(*) AS c FROM Employees"
    assert unparsable(sql)
    assert types(sql) == [("s", "INT64"), ("a", "FLOAT64"), ("c", "INT64")]
    assert types("SELECT WITH AGGREGATION_THRESHOLD OPTIONS(threshold=2, privacy_unit_column=job) "
                 "dept_id, COUNT(DISTINCT job) AS n FROM Employees GROUP BY dept_id") == [
        ("dept_id", "INT64"), ("n", "INT64")]


def test_a_privacy_call_with_another_named_argument_is_unknown_not_its_plain_type():
    # report_format => "JSON" makes the aggregate a JSON report, not an INT64
    sql = "SELECT WITH DIFFERENTIAL_PRIVACY OPTIONS(epsilon=1, delta=1) COUNT(*, report_format => 'JSON') AS c " \
          "FROM Employees"
    assert types(sql) == [("c", None)]


def test_a_privacy_select_with_an_aggregate_dp_types_differently_stays_unknown():
    # DP APPROX_QUANTILES over INT64 gives ARRAY<FLOAT64>; the ordinary aggregate gives ARRAY<INT64>
    sql = "SELECT WITH DIFFERENTIAL_PRIVACY OPTIONS(epsilon=1, delta=1) APPROX_QUANTILES(age, 2) AS q FROM Employees"
    assert rewrite(sql) is None
    assert types(sql) is None


def test_a_bit_aggregate_mode_and_a_cast_format_do_not_change_the_type():
    assert types("SELECT BIT_AND(CAST(job AS BYTES), mode => 'PAD') AS b FROM Employees") == [("b", "BYTES")]
    assert types("SELECT CAST(age AS STRING FORMAT job) AS s FROM Employees") == [("s", "STRING")]


def test_a_quantified_comparison_over_an_array_is_bool():
    assert types("SELECT age > ALL UNNEST([1, 2]) AS a, job LIKE ANY UNNEST(['a%']) AS b FROM Employees") == [
        ("a", "BOOL"), ("b", "BOOL")]


# --- expressions the typer does not model become unknown ----------------------------------------------------------

def test_a_cast_to_a_type_bigquery_lacks_is_unknown_and_nothing_computed_from_it_is_typed():
    typed = types("SELECT CAST(age AS UINT64) AS u, CAST(age AS UINT64) + 1 AS v, age, CAST(age AS INT64) AS i "
                  "FROM Employees")
    assert typed == [("u", None), ("v", None), ("age", "INT64"), ("i", "INT64")]
    # a union with an unknown branch is unknown, not the other branch's type
    assert types("SELECT 1 AS a UNION ALL SELECT CAST(age AS UINT32) FROM Employees") == [("a", None)]


def test_a_typed_array_or_empty_struct_of_an_unreadable_type_is_unknown_but_its_comparisons_are_bool():
    assert types("SELECT ARRAY<UINT32>[1, 2] AS xs") == [("xs", None)]
    assert types("SELECT STRUCT<>() = STRUCT<>() AS e, STRUCT<>() AS s") == [("e", "BOOL"), ("s", None)]


def test_a_protocol_buffer_constructor_is_unknown():
    assert types("SELECT NEW pkg.Message(1 AS f) AS m, age FROM Employees") == [("m", None), ("age", "INT64")]


# --- operators that add or reshape columns -----------------------------------------------------------------------

def test_a_recursion_depth_column_is_an_int64_after_the_others():
    sql = "WITH RECURSIVE t AS (SELECT 0 AS n UNION ALL SELECT MOD(n + 1, 3) FROM t) WITH DEPTH AS d BETWEEN 1 AND 4 " \
          "SELECT * FROM t"
    assert unparsable(sql)
    assert types(sql) == [("n", "INT64"), ("d", "INT64")]
    assert types("WITH RECURSIVE t AS (SELECT 'a' AS s UNION ALL SELECT s FROM t) WITH DEPTH SELECT * FROM t") == [
        ("s", "STRING"), ("depth", "INT64")]


def test_match_recognize_gives_the_partition_columns_then_the_measures():
    sql = "SELECT * FROM Employees MATCH_RECOGNIZE(PARTITION BY dept_id ORDER BY emp_id " \
          "MEASURES MAX(salary) AS top, COUNT(*) AS n PATTERN (a b) DEFINE a AS age > 1, b AS age < 1)"
    assert unparsable(sql)
    assert types(sql) == [("dept_id", "INT64"), ("top", "FLOAT64"), ("n", "INT64")]


def test_match_recognize_pattern_variable_qualifiers_are_read_and_other_uses_are_unknown():
    sql = "SELECT * FROM Employees MATCH_RECOGNIZE(ORDER BY emp_id MEASURES MAX(a.salary) AS top, " \
          "FIRST(a.salary) AS first_salary, CLASSIFIER() AS label PATTERN (a+) DEFINE a AS age > 1)"
    assert types(sql) == [("top", "FLOAT64"), ("first_salary", None), ("label", None)]


def test_match_recognize_with_a_clause_it_does_not_know_stays_unknown():
    sql = "SELECT * FROM Employees MATCH_RECOGNIZE(ORDER BY emp_id MEASURES MAX(salary) AS top " \
          "ONE ROW PER MATCH SUBSET s = (a) PATTERN (a+) DEFINE a AS age > 1)"
    assert types(sql) is None


def test_a_multiway_unnest_has_one_column_per_array_then_the_offset():
    sql = "SELECT * FROM UNNEST([1, 2] AS x, ['a'] AS y, [1.5], mode => 'PAD') WITH OFFSET AS o"
    assert unparsable(sql)
    assert types(sql) == [("x", "INT64"), ("y", "STRING"), (None, "FLOAT64"), ("o", "INT64")]


def test_a_multiway_unnest_does_not_flatten_struct_elements():
    sql = "SELECT * FROM UNNEST([STRUCT(1 AS a, 'x' AS b)] AS s, [2] AS n, mode => 'TRUNCATE')"
    assert types(sql) == [("s", "STRUCT<a INT64, b STRING>"), ("n", "INT64")]


def test_a_multiway_unnest_of_paths_is_named_by_the_last_identifier_and_other_names_are_left_alone():
    catalog = Catalog.from_types({"T": {"arr_a": "ARRAY<STRING>", "arr_b": "ARRAY<INT64>"}})
    sql = "SELECT * FROM T, UNNEST(T.arr_b, T.arr_a, mode => 'PAD') WITH OFFSET"
    assert types(sql, catalog) == [("arr_a", "ARRAY<STRING>"), ("arr_b", "ARRAY<INT64>"), ("arr_b", "INT64"),
                                   ("arr_a", "STRING"), ("offset", "INT64")]
    # an alias after the call names something this does not know
    assert types("SELECT * FROM UNNEST([1] AS x, [2] AS y, mode => 'PAD') AS t") is None


def test_an_ordinary_unnest_with_a_typed_array_is_not_taken_for_a_multiway_one():
    sql = "SELECT SUM(v GROUP BY k) AS s FROM UNNEST(ARRAY<STRUCT<k STRING, v INT64>>[('a', 1)])"
    rewritten = rewrite(sql)
    assert rewritten is not None and "__kumo_e" not in rewritten.sql


def test_align_gives_partition_columns_metrics_then_the_timestamp():
    sql = "SELECT * FROM Series ALIGN (TIMESTAMP ts PERIOD INTERVAL 1 MINUTE ORIGIN EPOCH PARTITION BY job " \
          "METRICS SUM(requests) WITHIN (1 PERIOD PRECEDING) AS total)"
    assert unparsable(sql)
    assert types(sql) == [("job", "STRING"), ("total", "INT64"), ("ts", "TIMESTAMP")]
    assert types("SELECT * FROM Series ALIGN (TIMESTAMP ts AS bucket PERIOD INTERVAL 1 MINUTE ORIGIN EPOCH)") == [
        ("bucket", "TIMESTAMP")]


def test_align_metrics_naming_an_alias_or_the_aligned_timestamp_are_unknown():
    sql = "SELECT * FROM Series ALIGN (TIMESTAMP ts PERIOD INTERVAL 1 MINUTE ORIGIN EPOCH " \
          "PARTITION BY job AS j METRICS CONCAT(j, 'x') AS m, MAX(aligned_timestamp) WITHIN (1 PERIOD) AS t)"
    assert types(sql) == [("j", "STRING"), ("m", None), ("t", None), ("ts", "TIMESTAMP")]


# --- the fallback does not touch what it should not ---------------------------------------------------------------

def test_a_query_sqlglot_parses_never_reaches_the_fallback_and_an_unfixable_one_stays_unknown():
    assert types("SELECT COUNT(*) AS n FROM Employees") == [("n", "INT64")]
    assert rewrite("SELECT COUNT(*) AS n FROM Employees") is None
    typed = infer("SELECT FROM WHERE (", EMP)
    assert typed.columns is None and typed.error.startswith("parse error")
    assert rewrite("SELECT FROM WHERE (") is None


def test_the_unknown_marker_is_never_part_of_a_column_name():
    typed = infer("SELECT * FROM UNNEST([1], ['a'] AS s, mode => 'PAD')", EMP)
    assert [c.name for c in typed.columns] == [None, "s"]
    assert all(UNKNOWN_FUNCTION not in (c.name or "") for c in typed.columns)
