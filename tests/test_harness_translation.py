"""Benchmark harness translation: Calcite-printed SQL run on DuckDB, and the SQL repairs in tools/bench_sql_repairs.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from bench_sql_repairs import (  # noqa: E402
    expand_row_predicates,
    fold_table_names,
    pipes_as_concat,
    rebind_correlation_variables,
    uniquify_star_columns,
)

from kumosql import counterexample as cx  # noqa: E402

EMP = ["EMPNO", "DEPTNO", "SAL"]


def emp_spec(*, foreign_key: bool = False) -> cx.Spec:
    spec = cx.Spec({
        "EMP": cx.Table("EMP", [cx.Column("EMPNO", "INT", not_null=True), cx.Column("DEPTNO", "INT"), cx.Column("SAL", "INT")], primary_key=("EMPNO",)),
        "DEPT": cx.Table("DEPT", [cx.Column("DEPTNO", "INT", not_null=True), cx.Column("NAME", "VARCHAR")], primary_key=("DEPTNO",)),
    })
    if foreign_key:
        spec.foreign_keys.append(("EMP", "DEPTNO", "DEPT", "DEPTNO"))
    return spec


def runs(sql: str) -> list:
    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE EMP (EMPNO BIGINT, DEPTNO BIGINT, SAL BIGINT)")
    db.execute("INSERT INTO EMP VALUES (1, 10, 5), (2, 10, NULL), (3, 20, 7)")
    return db.execute(cx.to_duckdb(sql)).fetchall()


def test_dollar_names_are_quoted():
    sql = "SELECT t.$f1, t.EXPR$0 FROM (SELECT DEPTNO AS EXPR$0, COUNT(*) AS $f1 FROM EMP GROUP BY DEPTNO) AS t"
    assert sorted(runs(sql)) == [(1, 20), (2, 10)]


def test_multi_argument_count_counts_rows_with_every_argument():
    assert sorted(runs("SELECT DEPTNO, COUNT(EMPNO, SAL) FROM EMP GROUP BY DEPTNO")) == [(10, 1), (20, 1)]


def test_filter_columns_and_grouping_set_keys_stay_unwrapped():
    sql = (
        "SELECT DEPTNO, COUNT(SAL) FILTER (WHERE $g) FROM (SELECT DEPTNO, SAL, GROUPING(DEPTNO, SAL) = 0 AS $g "
        "FROM EMP GROUP BY GROUPING SETS ((DEPTNO, SAL), DEPTNO)) AS t GROUP BY DEPTNO"
    )
    translated = cx.to_duckdb(sql)
    assert "ANY_VALUE" not in translated
    assert sorted(runs(sql)) == [(10, 1), (20, 1)]


def test_calcite_aggregates_and_order_by_null():
    assert runs("SELECT FIRST_VALUE(DEPTNO) FROM EMP WHERE EMPNO = 3") == [(20,)]
    assert runs("SELECT SINGLE_VALUE(SAL) FROM EMP WHERE EMPNO = 3") == [(7,)]
    assert runs("SELECT SINGLE_VALUE(SAL) FROM EMP WHERE EMPNO = 9") == [(None,)]
    with pytest.raises(duckdb.Error):
        runs("SELECT SINGLE_VALUE(SAL) FROM EMP")
    assert sorted(runs("SELECT EMPNO FROM EMP ORDER BY NULL")) == [(1,), (2,), (3,)]


def test_empty_select_list_keeps_the_row_count():
    assert runs("SELECT FROM EMP WHERE DEPTNO = 10") == [(1,), (1,)]
    assert runs("SELECT * FROM (SELECT DEPTNO FROM EMP WHERE EMPNO = 1) AS a, LATERAL (SELECT FROM EMP) AS b") == [(10,)] * 3


def test_child_tables_get_rows_although_the_parent_is_not_read():
    generator = cx._Generator(emp_spec(foreign_key=True), cx.Constants(), __import__("random").Random(0))
    sizes = [len(generator.database({"EMP"}, 3)["EMP"]) for _ in range(30)]
    assert max(sizes) > 0


def test_search_skips_databases_that_raise():
    spec = emp_spec()
    left = "SELECT SINGLE_VALUE(SAL) FROM EMP"
    right = "SELECT MAX(SAL) + 1 FROM EMP"
    for seed in range(3):
        found = cx.find_counterexample(spec, left, right, seed=seed)
        assert found and len(found.tables["EMP"]) == 1, "only one-row tables separate them; larger ones raise and are skipped"


def test_uninterpreted_predicates_run_as_macros():
    tables = {"r": ["X", "Y"]}
    left, arities = expand_row_predicates("SELECT * FROM R AS a WHERE B1(a)", tables)
    right, _ = expand_row_predicates("SELECT * FROM (SELECT * FROM R AS b WHERE B1(b)) AS c", tables)
    assert left == "SELECT * FROM R AS a WHERE B1(a.X, a.Y)" and arities == {"B1": 2}
    spec = cx.Spec({"R": cx.Table("R", [cx.Column("X", "INT"), cx.Column("Y", "INT")])})
    searcher = cx.Searcher(spec, left, right, predicates=arities)
    assert searcher.runs() and searcher.search(60) is None
    other = cx.Searcher(spec, left, "SELECT * FROM R", predicates=arities)
    assert other.search(60) is not None


def test_correlation_variables_rebind_to_the_lateral_source():
    sql = "SELECT $cor0.EMPNO FROM EMP AS $cor0, LATERAL (SELECT MAX(SAL) AS EXPR$0 FROM EMP) AS t6 WHERE $cor0.SAL = $cor0.EXPR$0"
    assert rebind_correlation_variables(sql, {"emp": EMP}).endswith("WHERE $cor0.SAL = t6.EXPR$0")
    ambiguous = "SELECT $cor0.EMPNO FROM EMP AS $cor0, LATERAL (SELECT 1 AS X) AS a, LATERAL (SELECT 2 AS X) AS b WHERE $cor0.X = 1"
    assert rebind_correlation_variables(ambiguous, {"emp": EMP}) == ambiguous


def test_repeated_star_columns_take_duckdb_names():
    sql = "SELECT t.SAL FROM (SELECT * FROM EMP AS a, EMP AS b) AS t"
    repaired = uniquify_star_columns(sql, {"emp": EMP})
    assert "b.SAL AS SAL_1" in repaired and "a.SAL AS SAL" in repaired
    assert uniquify_star_columns("SELECT t.SAL FROM (SELECT * FROM EMP AS a) AS t", {"emp": EMP}).endswith("FROM EMP AS a) AS t")
    full = "SELECT t.SAL FROM (SELECT * FROM EMP AS a FULL JOIN EMP AS b ON a.EMPNO = b.EMPNO) AS t"
    with pytest.raises(ValueError):
        uniquify_star_columns(full, {"emp": EMP})


def test_table_names_fold_only_when_a_pair_spells_them_two_ways():
    left, right = fold_table_names("SELECT * FROM a x", "SELECT * FROM A y")
    assert right == "SELECT * FROM a AS y" and left == "SELECT * FROM a x"
    assert fold_table_names("SELECT * FROM EMP", "SELECT * FROM EMP") == ["SELECT * FROM EMP", "SELECT * FROM EMP"]


def test_calcite_pipes_concatenate():
    sql = pipes_as_concat("SELECT DEPTNO || 'x' || SAL AS c FROM EMP WHERE DEPTNO || '' = '10' AND 'a||b' <> ''")
    assert sql == "SELECT CONCAT(CONCAT(DEPTNO, 'x'), SAL) AS c FROM EMP WHERE CONCAT(DEPTNO, '') = '10' AND 'a||b' <> ''"
    assert sorted(runs(sql), key=repr) == [("10x5",), (None,)]
    assert pipes_as_concat("SELECT a || b | c FROM t") == "SELECT a || b | c FROM t", "a query that also uses | is left alone"


EXISTS_SQL = "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.d <> t.a AND t.b > u.c)"
COUNT_SQL = "SELECT t.a FROM t WHERE (SELECT COUNT(*) FROM u WHERE u.d <> t.a AND t.b > u.c) > 0"


def test_optimizer_bugs_are_not_counterexamples():
    """DuckDB 1.5's optimizer drops rows of this correlated EXISTS when the tables hold NULLs."""

    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE t (a BIGINT, b BIGINT)")
    db.execute("CREATE TABLE u (c BIGINT, d BIGINT)")
    db.execute("INSERT INTO t VALUES (1, 2), (NULL, 3), (2, 1), (3, 0)")
    db.execute("INSERT INTO u VALUES (0, 0), (0, NULL)")
    exists, count = run_unoptimized(db, EXISTS_SQL, COUNT_SQL)
    assert sorted(exists) == sorted(count) == [(1,), (2,)]
    assert sorted(db.execute(COUNT_SQL).fetchall()) == [(1,), (2,)]

    spec = cx.Spec({
        "t": cx.Table("t", [cx.Column("a", "INT"), cx.Column("b", "INT")]),
        "u": cx.Table("u", [cx.Column("c", "INT"), cx.Column("d", "INT")]),
    })
    for seed in range(2):  # without the recheck, both seeds report a false counterexample
        assert cx.find_counterexample(spec, EXISTS_SQL, COUNT_SQL, seed=seed) is None
