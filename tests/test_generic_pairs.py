"""Seven generic BigQuery pairs through both prove commands: five equivalences and two controls."""

import json

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.cli import prove_main
from kumosql.smt_equivalence import main as smt_main
from kumosql.statement_proof import prove_statements, prove_statements_smt, split_create

T1, T2 = "`p.d.t1`", "`p.d.t2`"
CTAS = f"CREATE OR REPLACE TABLE `p.d.out` AS (SELECT a FROM {T1})"

EQUIVALENT = {
    "c1_cte_vs_subquery": (
        f"WITH x AS (SELECT a, b FROM {T1} WHERE c > 0) SELECT a, b FROM x",
        f"SELECT a, b FROM (SELECT a, b FROM {T1} WHERE c > 0)",
    ),
    "c2_redundant_like": (
        f"SELECT a FROM {T1} WHERE v LIKE 'BL1%' OR v LIKE 'BL2%' OR v LIKE 'BL10%'",
        f"SELECT a FROM {T1} WHERE v LIKE 'BL1%' OR v LIKE 'BL2%'",
    ),
    "c3_unused_column": (
        f"SELECT a FROM (SELECT a, MAX(IF(n = 'X', v, NULL)) AS m1 FROM {T1} GROUP BY a)",
        f"SELECT a FROM (SELECT a, MAX(IF(n = 'X', v, NULL)) AS m1, MAX(IF(n = 'Y', v, NULL)) AS m2 FROM {T1} GROUP BY a)",
    ),
    "c4_using_vs_on": (
        f"SELECT t1.a, t2.b FROM {T1} AS t1 LEFT JOIN {T2} AS t2 USING (a)",
        f"SELECT t1.a, t2.b FROM {T1} AS t1 LEFT JOIN {T2} AS t2 ON t1.a = t2.a",
    ),
    "c5_create_wrapper": (CTAS, CTAS),
    "c7_control_identical": (f"SELECT a FROM {T1} WHERE c > 0",) * 2,
}
DIFFERENT = (f"SELECT a FROM {T1} WHERE c > 0", f"SELECT a FROM {T1} WHERE c > 1")


@pytest.mark.parametrize("name", EQUIVALENT)
def test_statement_prover_proves_the_pair(name):
    assert prove_statements(*EQUIVALENT[name]).proven


@pytest.mark.parametrize("name", EQUIVALENT)
def test_smt_prover_proves_the_pair(name):
    assert prove_statements_smt(*EQUIVALENT[name]).proven


def test_control_is_never_proven():
    assert not prove_statements(*DIFFERENT).proven
    assert prove_statements_smt(*DIFFERENT).status.value == "not_equivalent"


@pytest.mark.parametrize("name", EQUIVALENT)
def test_commands_print_proven_equivalent(name, tmp_path, capsys):
    left, right = tmp_path / "a.sql", tmp_path / "b.sql"
    left.write_text(EQUIVALENT[name][0], encoding="utf-8")
    right.write_text(EQUIVALENT[name][1], encoding="utf-8")
    assert prove_main([str(left), str(right)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "proven_equivalent"
    assert smt_main([str(left), str(right)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "proven_equivalent"


@pytest.mark.parametrize(
    "right",
    [
        f"CREATE OR REPLACE TABLE `p.d.other` AS (SELECT a FROM {T1})",
        f"CREATE TABLE `p.d.out` AS (SELECT a FROM {T1})",
        f"CREATE OR REPLACE VIEW `p.d.out` AS (SELECT a FROM {T1})",
        f"CREATE OR REPLACE TABLE `p.d.out` PARTITION BY DATE(a) AS (SELECT a FROM {T1})",
        f"CREATE OR REPLACE TABLE `p.d.out` AS (SELECT a FROM {T1} WHERE c > 0)",
        f"SELECT a FROM {T1}",
    ],
    ids=["name", "replace", "kind", "options", "query", "bare-query"],
)
def test_a_different_write_is_not_proven(right):
    assert not prove_statements(CTAS, right).proven
    assert not prove_statements_smt(CTAS, right).proven


def test_create_view_and_unparenthesized_query_unwrap():
    left = f"CREATE OR REPLACE VIEW `p.d.v` AS SELECT a FROM {T1} WHERE c > 0 AND c > -1 AND c > 0"
    right = f"CREATE OR REPLACE VIEW `p.d.v` AS (SELECT a FROM {T1} WHERE c > 0)"
    assert prove_statements(left, right).proven
    assert split_create("CREATE FUNCTION f() AS (1)") is None
    assert split_create("SELECT 1") is None


def test_sql_functions_and_scripts_are_not_unwrapped():
    assert split_create("CREATE TABLE `p.d.t` (a INT64)") is None
    assert split_create("CREATE TEMP TABLE x AS SELECT 1; SELECT 2") is None


def test_respecting_row_order_keeps_the_structural_proof_only():
    left = f"SELECT a FROM {T1} WHERE c > 0 AND c > 0"
    right = f"SELECT a FROM {T1} WHERE c > 0"
    assert prove_statements(left, right).proven
    assert not prove_statements(left.replace("c > 0 AND c > 0", "c > 0 AND c > -1"), right, ignore_row_order=False).proven


# --- LIKE subsumption

def _p(left, right):
    return prove_equivalent_algebraic(left, right).proven


@pytest.mark.parametrize(
    "left,right",
    [
        ("v LIKE 'ab%' OR v LIKE 'a%'", "v LIKE 'a%'"),
        ("v LIKE 'ab' OR v LIKE 'a%'", "v LIKE 'a%'"),
        ("v LIKE 'a%' AND v LIKE 'ab%'", "v LIKE 'ab%'"),
        ("v LIKE 'a%' AND v LIKE 'ab'", "v LIKE 'ab'"),
        ("v LIKE 'a%' OR v LIKE 'a%'", "v LIKE 'a%'"),
        ("v LIKE '%' OR v LIKE 'x%' OR w > 1", "v LIKE '%' OR w > 1"),
    ],
)
def test_subsumed_like_is_dropped(left, right):
    assert _p(f"SELECT a FROM t WHERE {left}", f"SELECT a FROM t WHERE {right}")


@pytest.mark.parametrize(
    "left,right",
    [
        ("v LIKE 'a%' OR v LIKE 'ab%'", "v LIKE 'ab%'"),  # the narrower one alone loses rows
        ("v LIKE 'a%' AND v LIKE 'ab%'", "v LIKE 'a%'"),
        ("v LIKE 'a%' OR w LIKE 'ab%'", "v LIKE 'a%'"),  # different columns
        ("v LIKE 'a_%' OR v LIKE 'ab%'", "v LIKE 'a_%'"),  # _ is a wildcard
        ("v LIKE 'a\\%' OR v LIKE 'a\\%b%'", "v LIKE 'a\\%'"),  # escapes are not read
        ("v LIKE 'a%b' OR v LIKE 'a%bc'", "v LIKE 'a%b'"),  # inner % is not a prefix pattern
        ("v LIKE 'ab' OR v LIKE 'a'", "v LIKE 'a'"),  # 'a' without % is exact, not a prefix
    ],
)
def test_like_that_is_not_subsumed_stays(left, right):
    assert not _p(f"SELECT a FROM t WHERE {left}", f"SELECT a FROM t WHERE {right}")


# --- USING joins without a schema

USING = f"{T1} AS t1 %s JOIN {T2} AS t2 USING (a)"


@pytest.mark.parametrize(
    "side,select,on_select",
    [
        ("INNER", "a, t2.b", "t1.a, t2.b"),
        ("LEFT", "a, t2.b", "t1.a, t2.b"),
        ("RIGHT", "a, t1.b", "t2.a, t1.b"),
        ("FULL", "a, t1.b", "COALESCE(t1.a, t2.a) AS a, t1.b"),
    ],
)
def test_using_reads_the_merged_column(side, select, on_select):
    on = f"{T1} AS t1 {side} JOIN {T2} AS t2 ON t1.a = t2.a"
    assert _p(f"SELECT {select} FROM {USING % side}", f"SELECT {on_select} FROM {on}")


def test_using_does_not_prove_the_wrong_side():
    left = f"SELECT a, t2.b FROM {USING % 'RIGHT'}"
    right = f"SELECT t1.a, t2.b FROM {T1} AS t1 RIGHT JOIN {T2} AS t2 ON t1.a = t2.a"
    assert not _p(left, right)
    assert not _p(f"SELECT t1.a FROM {USING % 'LEFT'}", f"SELECT t1.a FROM {T1} AS t1 JOIN {T2} AS t2 ON t1.a = t2.a")


def test_using_with_a_star_or_unknown_owner_is_left_alone():
    assert not _p(f"SELECT * FROM {USING % 'INNER'}", f"SELECT * FROM {T1} AS t1 JOIN {T2} AS t2 ON t1.a = t2.a")
    chain = f"SELECT t1.a FROM {T1} AS t1 JOIN {T2} AS t2 ON t1.x = t2.x JOIN `p.d.t3` AS t3 USING (a)"
    on = f"SELECT t1.a FROM {T1} AS t1 JOIN {T2} AS t2 ON t1.x = t2.x JOIN `p.d.t3` AS t3 ON t1.a = t3.a"
    assert not _p(chain, on)


# --- outer joins over tables with undeclared columns

def test_outer_join_with_undeclared_columns_still_separates_different_queries():
    left = f"SELECT t1.a, t2.b FROM {T1} AS t1 LEFT JOIN {T2} AS t2 ON t1.a = t2.a"
    assert not _p(left, f"SELECT t1.a, t2.b FROM {T1} AS t1 JOIN {T2} AS t2 ON t1.a = t2.a")
    assert not _p(left, f"SELECT t1.a, t2.b FROM {T1} AS t1 LEFT JOIN {T2} AS t2 ON t1.a = t2.b")
    assert not _p(left, f"SELECT t1.a, t2.c FROM {T1} AS t1 LEFT JOIN {T2} AS t2 ON t1.a = t2.a")
    assert not _p(left, f"SELECT t1.a, t2.b FROM {T1} AS t1 LEFT JOIN {T2} AS t2 ON t1.a = t2.a WHERE t2.b IS NULL")
    assert _p(left, left.replace("t1.a = t2.a", "t2.a = t1.a"))
