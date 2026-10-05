"""Core of the GoogleSQL evaluator: set operations, literals, types, UNNEST/FLATTEN paths, PIVOT and recursion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T

I, S = T.INT64, T.STRING


def run(sql, mode="googlesql", **tables):
    db = Database({name: Table(cols, rows) for name, (cols, rows) in tables.items()})
    return evaluate(sql, db, mode=mode)


def rows(sql, mode="googlesql", **tables):
    return run(sql, mode, **tables).rows


# --- set operations by name -----------------------------------------------------------------------------


def test_corresponding_keeps_common_columns_in_first_query_order():
    assert rows("SELECT 1 AS a, 2 AS b UNION ALL CORRESPONDING SELECT 2 AS b, 3 AS c") == [(2,), (2,)]


def test_full_and_left_by_name_pad_with_null():
    assert rows("SELECT 1 AS a, 2 AS b FULL UNION ALL CORRESPONDING SELECT 2 AS b, 3 AS c") == [(1, 2, None), (None, 2, 3)]
    assert rows("SELECT 1 AS a, 2 AS b LEFT UNION ALL CORRESPONDING SELECT 2 AS b, 3 AS c") == [(1, 2), (None, 2)]


def test_strict_corresponding_needs_equal_name_sets():
    assert rows("SELECT 1 AS a, 2 AS b UNION ALL STRICT CORRESPONDING SELECT 2 AS b, 1 AS a") == [(1, 2), (1, 2)]
    with pytest.raises(AnalysisError):
        run("SELECT 1 AS a, 2 AS b UNION ALL STRICT CORRESPONDING SELECT 2 AS b, 3 AS c")


def test_set_operation_without_common_supertype_is_an_error():
    with pytest.raises(AnalysisError):
        run("SELECT [1] UNION ALL SELECT ['a']")


def test_set_operation_widens_float_literal_to_numeric_exactly():
    result = run("SELECT NUMERIC '1.5' UNION ALL SELECT 2.7")
    assert result.columns[0][1] == T.NUMERIC
    assert result.rows == [(Decimal("1.5"),), (Decimal("2.7"),)]


# --- literals -------------------------------------------------------------------------------------------


def test_string_escapes_are_code_points():
    assert rows("SELECT '\\x41\\u00e9\\101'") == [("AéA",)]


@pytest.mark.parametrize("sql", ["SELECT '\\q'", "SELECT '\\x4'", "SELECT '\\ud800'"])
def test_illegal_escapes_are_analysis_errors(sql):
    with pytest.raises(AnalysisError):
        run(sql)


def test_numeric_literal_keeps_all_digits():
    text = "99999999999999999999999999999.999999999"
    assert rows(f"SELECT NUMERIC '{text}'") == [(Decimal(text),)]


def test_arrays_of_arrays_only_in_googlesql_mode():
    assert rows("SELECT [[1]]") == [(((1,),),)]
    with pytest.raises(AnalysisError):
        run("SELECT [[1]]", mode="bigquery")


def test_googlesql_concat_casts_mixed_operands_to_string():
    assert rows("SELECT DATE '2000-01-01' || ' ' || (1 > 0) || 7") == [("2000-01-01 true7",)]


# --- queries --------------------------------------------------------------------------------------------


def test_unnest_of_structs_exposes_fields():
    sql = "SELECT a.x, a.y FROM UNNEST([STRUCT(1 AS x, 'p' AS y), STRUCT(2, 'q')]) a"
    assert rows(sql) == [(1, "p"), (2, "q")]


def test_order_by_prefers_select_alias_over_column():
    t = ([("x", I), ("v", I)], [(1, 3), (2, 2), (3, 1)])
    assert rows("SELECT x AS v FROM t ORDER BY v", t=t) == [(1,), (2,), (3,)]


def test_aggregate_in_untaken_branch_does_not_fail():
    t = ([("x", I)], [(1,)])
    assert rows("SELECT IF(FALSE, SUM(1 / (x - 1)), 1) FROM t", t=t) == [(1,)]
    with pytest.raises(EvalError):
        run("SELECT IF(TRUE, SUM(1 / (x - 1)), 1) FROM t", t=t)


def test_recursive_cte():
    sql = "WITH RECURSIVE a AS (SELECT 1 n UNION ALL SELECT n + 1 FROM a WHERE n < 3) SELECT n FROM a"
    assert rows(sql) == [(1,), (2,), (3,)]


def test_like_any_with_null_pattern():
    t = ([("x", S)], [("abc",), ("zzz",)])
    assert rows("SELECT x FROM t WHERE x LIKE ANY ('a%', NULL)", t=t) == [("abc",)]
    assert rows("SELECT x FROM t WHERE x NOT LIKE ALL ('a%', NULL)", t=t) == []


def test_pivot_and_unpivot():
    pivot = ([("g", I), ("k", S), ("v", I)], [(1, "a", 5), (1, "b", 6), (2, "a", 7)])
    assert rows("SELECT * FROM t PIVOT(SUM(v) FOR k IN ('a', 'b'))", t=pivot) == [(1, 5, 6), (2, 7, None)]
    unpivot = ([("g", I), ("a", I), ("b", I)], [(1, 5, None), (2, 7, 8)])
    assert rows("SELECT * FROM t UNPIVOT(v FOR k IN (a, b))", t=unpivot) == [(1, 5, "a"), (2, 7, "a"), (2, 8, "b")]


def test_foreign_column_types_are_declined():
    with pytest.raises(Unsupported):
        run("SELECT * FROM t", t=([("x", T.Type("UINT64"))], [(1,)]))
