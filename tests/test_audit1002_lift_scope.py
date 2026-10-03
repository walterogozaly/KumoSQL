"""Audit 1002, findings 2 and 10: the subquery lifter moved a query body out of the scope that binds its names.

A FROM/JOIN subquery inside a correlated subquery (``SELECT t.a`` under ``... FROM t``) or one reading a
CTE of a nested WITH was hoisted into a top-level CTE, where ``t`` or the CTE is not visible; the raw
lifter reported success and the rewrite was "proven", because the structural prover normalizes both
sides through the same lifter. Such a subquery now stays in place (and counts as remaining), the others
are still lifted, and rewrite verification rejects an output that reads a name outside the scope that
binds it in the input.
"""

from __future__ import annotations

from collections import Counter

import pytest
import sqlglot

duckdb = pytest.importorskip("duckdb")

from kumosql import apply_rule, count_inline_subqueries, lift_subqueries, prove_equivalent  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.rewrite import verify_rewrite  # noqa: E402

F2 = "SELECT (SELECT MAX(s.v) FROM (SELECT t.a AS v) AS s) AS x FROM t"
F2_ESCAPED = (
    "WITH __lifted_subquery_001 AS (SELECT t.a AS v) "
    "SELECT (SELECT MAX(s.v) FROM __lifted_subquery_001 AS s) AS x FROM t"
)
F10 = "SELECT * FROM (WITH local AS (SELECT 1 AS a) SELECT * FROM (SELECT a FROM local) AS s) AS z"
F10_ESCAPED = (
    "WITH __lifted_subquery_001 AS (SELECT a FROM local), "
    "__lifted_subquery_002 AS (WITH local AS (SELECT 1 AS a) SELECT * FROM __lifted_subquery_001 AS s) "
    "SELECT * FROM __lifted_subquery_002 AS z"
)


def _database():
    db = duckdb.connect()
    db.execute("CREATE TABLE t(a BIGINT, b BIGINT); INSERT INTO t VALUES (1, 2), (2, 3), (5, 5)")
    db.execute("CREATE TABLE u(b BIGINT); INSERT INTO u VALUES (2), (3), (7)")
    # Real tables named like the nested CTEs: a body moved away from its WITH would read these instead.
    db.execute("CREATE TABLE local(a BIGINT); INSERT INTO local VALUES (9)")
    return db


def _hashable(value):
    return tuple(sorted(map(_hashable, value), key=repr)) if isinstance(value, (list, tuple)) else value


def _rows(db, sql):
    rows = db.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]).fetchall()
    return Counter(tuple(_hashable(value) for value in row) for row in rows)


def _assert_same_rows(original, rewritten):
    db = _database()
    assert _rows(db, rewritten) == _rows(db, original)


def _assert_kept(sql, *, lifted=0, remaining=1):
    result = lift_subqueries(sql)
    assert result.lifted_subqueries == lifted
    assert result.remaining_inline_subqueries == remaining
    assert not result.success
    assert any(d.code == "inline_subqueries_remaining" for d in result.diagnostics)
    if not lifted:
        assert result.sql == sql
    _assert_same_rows(sql, result.sql)
    return result


def test_correlated_relation_subquery_stays_in_place():
    _assert_kept(F2)
    rewrite = apply_rule("lift_subqueries", F2)
    assert not rewrite.success
    assert rewrite.verification.status.value != "proven"


def test_subquery_reading_a_nested_cte_stays_and_its_enclosing_subquery_lifts():
    result = _assert_kept(F10, lifted=1)
    # The nested WITH travels with ``z``; ``s`` stays next to the WITH that defines ``local``.
    assert "(SELECT a FROM local) AS s" in result.sql
    assert count_inline_subqueries(result.sql) == 1
    assert _rows(_database(), result.sql) == Counter({(1,): 1})
    assert apply_rule("lift_subqueries", F10).verification.status.value != "proven"


@pytest.mark.parametrize(
    "sql",
    [
        # A nested CTE hiding a top-level CTE of the same name.
        "WITH local AS (SELECT 2 AS a) "
        "SELECT * FROM (WITH local AS (SELECT 1 AS a) SELECT * FROM (SELECT a FROM local) AS s) AS z",
        # A nested CTE hiding a real table of the same name.
        "SELECT * FROM (WITH t AS (SELECT 1 AS a) SELECT * FROM (SELECT a FROM t) AS s) AS z",
        # A later CTE of a nested WITH reading an earlier one.
        "SELECT * FROM (WITH local AS (SELECT 1 AS a), k AS (SELECT * FROM (SELECT a FROM local) AS s) SELECT * FROM k) AS z",
    ],
)
def test_nested_with_shadowing_keeps_the_reader_in_place(sql):
    result = _assert_kept(sql, lifted=1)
    assert _rows(_database(), result.sql) == Counter({(1,): 1})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM (SELECT u.b FROM u WHERE u.b = t.b) AS s)",
        "SELECT a FROM t WHERE a IN (SELECT s.v FROM (SELECT u.b - t.b AS v FROM u) AS s)",
        # Unqualified: ``a`` binds nothing inside the body, so it reads ``t``.
        "SELECT (SELECT MAX(s.v) FROM (SELECT a AS v) AS s) AS x FROM t",
        # Unqualified next to a relation: without a schema ``b`` may be ``t.b`` (here it is ``u.b``).
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM (SELECT b FROM u) AS s WHERE s.b = t.b)",
        "SELECT ARRAY(SELECT s.v FROM (SELECT t.a + u.b AS v FROM u) AS s) AS x FROM t",
    ],
)
def test_correlated_exists_in_and_array_subqueries_stay(sql):
    _assert_kept(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (SELECT COUNT(*) FROM (SELECT * FROM o.items) AS s) AS n FROM orders AS o",
        # A joined UNNEST reads the relations before it, so ``t`` is in scope inside the ARRAY subquery.
        "SELECT x FROM t, UNNEST(ARRAY(SELECT s.v FROM (SELECT t.a AS v) AS s)) AS x",
    ],
)
def test_correlated_array_reads_stay(sql):
    result = lift_subqueries(sql)
    assert result.sql == sql
    assert result.remaining_inline_subqueries == 1


def test_unnest_of_a_sibling_relation_inside_the_body_still_lifts():
    sql = "SELECT * FROM (SELECT id, e FROM t, UNNEST(arr) AS e) AS c ORDER BY id, e"
    assert lift_subqueries(sql).success
    assert apply_rule("lift_subqueries", sql).verification.status.value == "proven"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t JOIN (SELECT b FROM u) AS s ON t.b = s.b",
        "SELECT a FROM t WHERE t.b IN (SELECT s.b FROM (SELECT u.b FROM u) AS s)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM (SELECT u.b FROM u) AS s WHERE s.b = t.b)",
        "WITH local AS (SELECT 1 AS a) SELECT * FROM (SELECT a FROM local) AS s",
    ],
)
def test_closed_subqueries_still_lift(sql):
    result = lift_subqueries(sql)
    assert result.lifted_subqueries == 1
    _assert_same_rows(sql, result.sql)
    rewrite = apply_rule("lift_subqueries", sql)
    assert rewrite.verification.status.value == "proven"


def test_other_subqueries_still_lift_next_to_a_kept_one():
    sql = F2 + " JOIN (SELECT b FROM u) AS w ON t.b = w.b"
    result = _assert_kept(sql, lifted=1)
    assert "(SELECT t.a AS v) AS s" in result.sql
    assert "JOIN __lifted_subquery_001 AS w" in result.sql
    # The correlation is bound inside the outer body: ``q`` lifts and carries ``s`` along.
    result = _assert_kept("SELECT * FROM (" + F2 + ") AS q", lifted=1)
    assert result.sql.startswith("WITH __lifted_subquery_001 AS (" + F2 + ")")


def test_kept_subquery_in_a_script_statement_is_not_lifted_again_from_a_stale_copy():
    # Lifting ``z`` copies its body into a CTE; the original query left behind is no longer in the statement.
    result = lift_subqueries("SET x = (SELECT COUNT(*) FROM " + F10[len("SELECT * FROM "):] + ")")
    assert (result.lifted_subqueries, result.remaining_inline_subqueries) == (1, 1)
    assert "(SELECT a FROM local) AS s" in result.sql


def test_struct_field_reads_still_lift_and_verify():
    rewrite = apply_rule("lift_subqueries", "SELECT e.c FROM (SELECT device.category AS c FROM events) AS e")
    assert rewrite.success
    assert rewrite.verification.status.value == "proven"


def test_escaped_outputs_are_not_proven():
    for original, escaped in ((F2, F2_ESCAPED), (F10, F10_ESCAPED)):
        assert not prove_equivalent(original, escaped).proven
        assert not prove_equivalent_algebraic(original, escaped).proven
        verification = verify_rewrite(original, escaped)
        assert verification.status.value == "unproven"
    assert "column qualifier t" in " ".join(verify_rewrite(F2, F2_ESCAPED).details)
    assert "table local" in " ".join(verify_rewrite(F10, F10_ESCAPED).details)


def test_structural_prover_compares_a_kept_subquery_where_it_stands():
    assert prove_equivalent(F2, F2 + " WHERE TRUE").proven
    assert not prove_equivalent(F2, F2.replace("MAX", "MIN")).proven


def test_algebraic_cte_inlining_does_not_capture_a_nested_with():
    # ``l1`` reads the real table ``local``; inlining it under ``WITH local`` read the CTE instead.
    left = "WITH l1 AS (SELECT a FROM local) SELECT * FROM (WITH local AS (SELECT 1 AS a) SELECT * FROM l1 AS s) AS z"
    db = _database()
    assert _rows(db, left) != _rows(db, "SELECT 1 AS a")
    assert not prove_equivalent_algebraic(left, "SELECT 1 AS a").proven
    assert prove_equivalent_algebraic(
        "WITH l1 AS (SELECT a FROM t), l2 AS (SELECT a FROM l1 WHERE a > 1) SELECT * FROM l2",
        "SELECT a FROM t WHERE a > 1",
    ).proven
