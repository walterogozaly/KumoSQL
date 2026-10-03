"""MySQL TIMESTAMP(p) runs on DuckDB as a plain TIMESTAMP, so the counterexample search can read its rows.

sqlglot reads MySQL's TIMESTAMP as TIMESTAMPTZ, and DuckDB hands TIMESTAMPTZ values to Python only
through pytz: without it every non-empty database raised, and a query casting to TIMESTAMP was only
ever compared on empty tables (VeriEQL Calcite 43 and 256 read as 'agrees' though they differ).
"""

import duckdb
import pytest

from kumosql import counterexample as cx


def _emp_dept() -> cx.Spec:
    emp = cx.Table("EMP", [
        cx.Column("EMPNO", "INT", not_null=True), cx.Column("DEPTNO", "INT"), cx.Column("ENAME", "VARCHAR"),
        cx.Column("HIREDATE", "DATE"), cx.Column("SAL", "INT"),
    ], primary_key=("EMPNO",))
    dept = cx.Table("DEPT", [cx.Column("DEPTNO", "INT", not_null=True), cx.Column("NAME", "VARCHAR")], primary_key=("DEPTNO",))
    return cx.Spec({"EMP": emp, "DEPT": dept})


@pytest.mark.parametrize("sql", [
    "SELECT CAST(HIREDATE AS TIMESTAMP(0)) FROM EMP",
    "SELECT CAST(HIREDATE AS TIMESTAMP) FROM EMP",
    "SELECT TIMESTAMP '2020-01-02 00:00:00' FROM EMP",
])
def test_mysql_timestamp_reads_back_from_a_non_empty_table(sql):
    translated = cx.to_duckdb(sql)
    assert "TIMESTAMPTZ" not in translated.upper()
    db = duckdb.connect()
    db.execute("CREATE TABLE EMP (HIREDATE DATE)")
    db.execute("INSERT INTO EMP VALUES (DATE '2020-01-02')")
    (value,), = db.execute(translated).fetchall()
    assert str(value) == "2020-01-02 00:00:00"


def test_a_zoned_type_from_a_zoned_dialect_is_kept():
    assert "TIMESTAMPTZ" in cx.to_duckdb("SELECT CAST(x AS TIMESTAMPTZ) FROM t", dialect="postgres").upper()


def test_datetime_translation_is_unchanged():
    assert cx.to_duckdb("SELECT CAST(x AS DATETIME) FROM t") == "SELECT CAST(x AS TIMESTAMP) FROM t"


def test_a_timestamp_cast_in_a_reordered_projection_is_refuted():
    """VeriEQL Calcite 256's shape: same rows, but the right side reorders the columns and casts the date."""

    left = "SELECT * FROM DEPT LEFT JOIN EMP ON DEPT.DEPTNO = EMP.DEPTNO WHERE EMP.DEPTNO IS NOT NULL AND EMP.SAL > 100"
    right = ("SELECT DEPT0.DEPTNO, DEPT0.NAME, t1.EMPNO, t1.ENAME, CAST(t1.HIREDATE AS TIMESTAMP(0)) AS HIREDATE, "
             "t1.SAL, t1.DEPTNO AS DEPTNO0 FROM DEPT AS DEPT0 "
             "INNER JOIN (SELECT * FROM EMP WHERE SAL > 100) AS t1 ON DEPT0.DEPTNO = t1.DEPTNO")
    found = cx.find_counterexample(_emp_dept(), left, right, trials=60)
    assert found, "a database with a matching employee tells the two column orders apart"
    assert found.left_rows and found.right_rows


def test_equal_timestamp_casts_are_not_refuted():
    """Near miss: both sides cast the same date to a zoneless timestamp, one as TIMESTAMP(0), one as DATETIME."""

    left = "SELECT EMPNO, CAST(HIREDATE AS TIMESTAMP(0)) AS H FROM EMP WHERE SAL > 100"
    right = "SELECT EMPNO, CAST(HIREDATE AS DATETIME) AS H FROM EMP WHERE 100 < SAL"
    assert cx.find_counterexample(_emp_dept(), left, right, trials=60) is None


def test_null_timestamp_casts_are_not_refuted():
    """Near miss: VeriEQL Calcite 95's shape, a NULL cast to TIMESTAMP(0) padding an emptied outer join."""

    left = "SELECT * FROM (SELECT * FROM EMP WHERE FALSE) AS t0 RIGHT JOIN DEPT ON t0.DEPTNO = DEPT.DEPTNO"
    right = ("SELECT CAST(NULL AS INTEGER) AS EMPNO, CAST(NULL AS INTEGER) AS DEPTNO, CAST(NULL AS VARCHAR(20)) AS ENAME, "
             "CAST(NULL AS TIMESTAMP(0)) AS HIREDATE, CAST(NULL AS INTEGER) AS SAL, DEPTNO AS DEPTNO0, NAME FROM DEPT")
    assert cx.find_counterexample(_emp_dept(), left, right, trials=60) is None
