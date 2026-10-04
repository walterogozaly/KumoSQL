"""A top-k over UNION ALL whose last branch's ORDER BY .. LIMIT parses onto the whole union (SPES testSortUnionTranspose)."""

import duckdb
import sqlglot

import pytest
from sqlglot_support import OLD_SQLGLOT

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.limit_rules import limit_rule

SCHEMA = {"dept": ["deptno", "name"]}

LEFT = "SELECT * FROM (SELECT DEPT.NAME FROM DEPT AS DEPT UNION ALL SELECT DEPT0.NAME FROM DEPT AS DEPT0) AS t1 ORDER BY t1.NAME FETCH NEXT 10 ROWS ONLY"


def _right(first="ORDER BY DEPT1.NAME FETCH NEXT 10 ROWS ONLY", last="ORDER BY DEPT2.NAME FETCH NEXT 10 ROWS ONLY", outer="ORDER BY t10.NAME FETCH NEXT 10 ROWS ONLY"):
    return f"SELECT * FROM (SELECT DEPT1.NAME FROM DEPT AS DEPT1 {first} UNION ALL SELECT DEPT2.NAME FROM DEPT AS DEPT2 {last}) AS t10 {outer}"


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def test_branch_cuts_under_the_same_top_k_are_redundant():
    assert _proven(LEFT, _right())
    # A longer union-level cut keeps every row the outer cut keeps.
    assert _proven(LEFT, _right(last="ORDER BY DEPT2.NAME FETCH NEXT 12 ROWS ONLY"))


def test_a_shorter_union_cut_is_not_redundant():
    short = _right(last="ORDER BY DEPT2.NAME FETCH NEXT 5 ROWS ONLY")
    assert not _proven(LEFT, short)
    # Witness: twelve departments, the union-level cut keeps five rows, the outer cut on its own ten.
    db = duckdb.connect()
    db.execute("CREATE TABLE DEPT (DEPTNO INT, NAME VARCHAR)")
    db.execute("INSERT INTO DEPT SELECT i, 'd' || i FROM range(12) AS r(i)")
    queries = [sqlglot.transpile(q, read="mysql", write="duckdb")[0] for q in (LEFT, short)]
    try:
        left_rows, right_rows = run_unoptimized(db, *queries)
    except duckdb.ParserException:
        if not OLD_SQLGLOT:
            raise
        pytest.skip("sqlglot 26 prints the parenthesised union operand without its parentheses, which DuckDB cannot read")
    assert len(left_rows) == 10 and len(right_rows) == 5


def test_other_orders_or_offsets_on_the_union_cut_do_not_drop():
    assert not _proven(LEFT, _right(last="ORDER BY DEPT2.NAME DESC FETCH NEXT 10 ROWS ONLY"))
    assert not _proven(LEFT, _right(last="ORDER BY DEPT2.NAME LIMIT 10 OFFSET 1"))
    # NULLs placed differently: the union cut keeps its NULL names last, the outer cut wants them first.
    assert not _proven(LEFT, _right(last="ORDER BY DEPT2.NAME IS NULL, DEPT2.NAME FETCH NEXT 10 ROWS ONLY"))
    assert not _proven(LEFT, _right(last="ORDER BY DEPT2.NAME NULLS LAST FETCH NEXT 10 ROWS ONLY"))
    # The same NULL placement on both cuts is one order again.
    nulls_last = dict(outer="ORDER BY t10.NAME NULLS LAST LIMIT 5", first="ORDER BY DEPT1.NAME NULLS LAST LIMIT 5")
    assert _proven(_right(last="", **nulls_last), _right(last="ORDER BY DEPT2.NAME NULLS LAST LIMIT 10", **nulls_last))
    assert not _proven(_right(last="", **nulls_last), _right(last="ORDER BY DEPT2.NAME LIMIT 10", **nulls_last))


def test_hidden_columns_keep_the_cut():
    # Ordered by NAME only, a tie on NAME can keep either DEPTNO.
    left = "SELECT * FROM (SELECT d.NAME, d.DEPTNO FROM DEPT AS d UNION ALL SELECT e.NAME, e.DEPTNO FROM DEPT AS e) AS t ORDER BY t.NAME LIMIT 1"
    right = "SELECT * FROM (SELECT d.NAME, d.DEPTNO FROM DEPT AS d UNION ALL SELECT e.NAME, e.DEPTNO FROM DEPT AS e ORDER BY e.NAME LIMIT 1) AS t ORDER BY t.NAME LIMIT 1"
    assert not _proven(left, right)


def test_qualified_union_order_key_must_name_one_position():
    # e.name is output 2 of the last branch but the output named name is 1: engines disagree, keep the cut.
    sql = "SELECT t.name, t.deptno FROM (SELECT d.name, d.deptno FROM dept AS d UNION ALL SELECT e.deptno, e.name FROM dept AS e ORDER BY e.name, e.deptno LIMIT 5) AS t ORDER BY t.name, t.deptno LIMIT 5"
    out = limit_rule(sqlglot.parse_one(sql, read="mysql"))
    assert out is None or "ORDER BY e.name, e.deptno LIMIT 5" in out.sql(dialect="mysql")
    agreed = sql.replace("SELECT e.deptno, e.name", "SELECT e.name, e.deptno")
    assert "LIMIT 5)" not in limit_rule(sqlglot.parse_one(agreed, read="mysql")).sql(dialect="mysql")
