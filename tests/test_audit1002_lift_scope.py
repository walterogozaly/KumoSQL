"""Audit 1002, findings 2 and 10: the prover's lifting moved a query body out of the scope that binds its names.

The rewrite rule already kept a correlated or captured derived table in place. The structural prover's own
normalization (``lift_subqueries(..., rewrite_pipe_syntax=True)``) still lifted every one, so a lifted form that
no longer ran was proven equal to its input. It now applies the same checks, and inlining a WITH table whose body
reads a name a WITH around its use redefines is declined.
"""

from __future__ import annotations

import pytest

from kumosql import apply_rule, lift_subqueries, prove_equivalent
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.rewrite import verify_rewrite

F2 = "SELECT (SELECT MAX(s.v) FROM (SELECT t.a AS v) AS s) AS x FROM t"
F2_BARE = "SELECT (SELECT MAX(s.v) FROM (SELECT a AS v) AS s) AS x FROM t"
F10 = "SELECT * FROM (WITH local AS (SELECT 1 AS a) SELECT * FROM (SELECT a FROM local) AS s) AS z"

CAPTURE_PAIR = (
    "SELECT * FROM (WITH c AS (SELECT 1 AS x) SELECT * FROM (SELECT x FROM c) AS d) AS e",
    "WITH l1 AS (SELECT x FROM c), l2 AS (WITH c AS (SELECT 1 AS x) SELECT * FROM l1 AS d) SELECT * FROM l2 AS e",
)
CORRELATED_PAIR = (
    "SELECT * FROM a, (SELECT * FROM b WHERE b.x = a.x) AS s",
    "WITH l AS (SELECT * FROM b WHERE b.x = a.x) SELECT * FROM a CROSS JOIN l AS s",
)
SCHEMA = {"a": ["x"], "b": ["x"], "c": ["x"]}


@pytest.mark.parametrize("sql", [F2, F2_BARE])
def test_lifter_keeps_a_body_that_reads_a_column_of_the_query_around_it(sql):
    result = lift_subqueries(sql)
    assert result.lifted_subqueries == 0
    assert "__lifted_subquery" not in result.sql
    assert any(d.code == "correlated_subquery_kept" for d in result.diagnostics)
    assert "__lifted_subquery" not in apply_rule("lift_subqueries", sql).sql


def test_lifter_keeps_a_body_that_reads_a_with_table_nested_around_it():
    result = lift_subqueries(F10)
    # ``z`` is closed (it carries its own WITH) and lifts; ``s`` reads ``local`` from that WITH and stays inside it.
    assert result.lifted_subqueries == 1
    assert "(SELECT a FROM local) AS s" in result.sql.replace("\n", " ").replace("  ", " ")
    assert any(d.code == "correlated_subquery_kept" for d in result.diagnostics)


@pytest.mark.parametrize("before,after", [CAPTURE_PAIR, CORRELATED_PAIR])
def test_prover_does_not_prove_a_lift_out_of_scope(before, after):
    assert not prove_equivalent(before, after).proven
    assert verify_rewrite(before, after).status.value not in ("proven", "unchanged")


def test_prover_normalization_keeps_correlated_derived_table_in_place():
    left, right = CORRELATED_PAIR
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA).proven


def test_a_bare_column_in_a_select_without_from_is_correlated_but_an_output_alias_is_not():
    # ``a`` can only come from the enclosing query; ``v`` is the subquery's own output used by ORDER BY.
    assert lift_subqueries(F2_BARE).lifted_subqueries == 0
    own = "SELECT * FROM t WHERE a IN (SELECT v FROM (SELECT 1 AS v) AS s ORDER BY v)"
    assert lift_subqueries(own).lifted_subqueries == 1


def test_a_closed_derived_table_still_lifts_and_proves():
    original = "SELECT s.p FROM (SELECT 1 AS p) AS s"
    lifted = lift_subqueries(original).sql
    assert "__lifted_subquery" in lifted
    assert prove_equivalent(original, lifted).proven


def test_inlining_does_not_let_a_nested_with_capture_a_name():
    # A forward reference reads the real table ``l2`` at the point of use; inlining must not resolve it to the later CTE.
    assert not prove_equivalent_algebraic(
        "WITH l1 AS (SELECT a FROM l2), l2 AS (SELECT 1 AS a) SELECT a FROM l1",
        "SELECT 1 AS a",
        schema={"l2": ["a"]},
    ).proven
