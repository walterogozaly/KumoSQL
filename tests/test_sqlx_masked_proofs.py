"""Template text is not compiled SQL: a Dataform expression can expand to quotes, operators or comments.

Each case compiles the template with a fake expansion, runs both sides on DuckDB (optimizer off) and checks that
whatever the verifier proves for the template also holds for the compiled SQL. The expansions are plain text
substitution, not a claim about any real project's variables.
"""

from __future__ import annotations

import pytest

from kumosql import load_sqlx_project, prove_equivalent
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.rewrite import VerificationStatus, verify_rewrite
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt
from kumosql.sqlx_fragments import masked_template_problem

duckdb = pytest.importorskip("duckdb")

ROWS = "(1, false, true), (2, true, false), (3, true, true)"


def run(sql: str) -> list[int]:
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t(n INT, b BOOLEAN, y BOOLEAN)")
    connection.execute(f"INSERT INTO t VALUES {ROWS}")
    connection.execute("PRAGMA disable_optimizer")
    return sorted(row[0] for row in connection.execute(sql).fetchall())


def compiled(template: str, expansion: str) -> str:
    return template.replace("${constants.P}", expansion)


# (before, after, expansion that makes the compiled queries differ)
UNEQUAL_WHEN_COMPILED = [
    # Quoted expression closes the quote and adds OR: removing the parentheses changes precedence.
    ("SELECT n FROM t WHERE ('${constants.P}' = 'ok') AND b", "SELECT n FROM t WHERE '${constants.P}' = 'ok' AND b",
     "x' = 'x' OR 'x"),
    ("SELECT n FROM t WHERE ('${constants.P}' = 'ok') AND NOT b", "SELECT n FROM t WHERE '${constants.P}' = 'ok' AND NOT b",
     "x' = 'x' OR 'x"),
    # Layout only, but the expansion ends in a line comment, so the newline decides what it swallows.
    ("SELECT n FROM t WHERE ${constants.P}\n AND y", "SELECT n FROM t WHERE ${constants.P} AND y", "b --"),
    ("SELECT n FROM t WHERE ${constants.P}\n OR y", "SELECT n FROM t WHERE ${constants.P} OR y", "b --"),
]


@pytest.mark.parametrize("before,after,expansion", UNEQUAL_WHEN_COMPILED)
def test_a_template_change_that_compiles_to_different_rows_is_not_proven(before, after, expansion):
    assert run(compiled(before, expansion)) != run(compiled(after, expansion))
    assert verify_rewrite(before, after).status is not VerificationStatus.PROVEN


@pytest.mark.parametrize("before,after", [
    ("SELECT n FROM t WHERE b\n AND y", "SELECT n FROM t WHERE b AND y"),
    ("SELECT n FROM t WHERE ${ref('s', 't')}\n AND y", "SELECT n FROM t WHERE ${ref('s', 't')} AND y"),
    # The expression is not in this statement's way: the whole text is still layout-only and holds none.
    ("SELECT n FROM t WHERE b\n AND y", "select n from t where b and y"),
])
def test_layout_changes_without_a_dynamic_expression_stay_proven(before, after):
    assert verify_rewrite(before, after).status is VerificationStatus.PROVEN


# Witnesses: the loader masks a Dataform expression by position or by text; the prover sees a fixed string.

def test_a_masked_string_next_to_a_fixed_string_is_not_a_contradiction():
    # With paid = 'paid' the second query returns more rows: the masked value is not the literal 'paid'.
    with_filter = 'SELECT n FROM t WHERE s = "__sqlx_token_000__" AND s = \'paid\' AND n > 0'
    without = 'SELECT n FROM t WHERE s = "__sqlx_token_000__" AND s = \'paid\''
    assert masked_template_problem(with_filter, without)
    assert not prove_equivalent(with_filter, without).proven
    assert prove_equivalent_smt(with_filter, without).status is not SmtStatus.PROVEN_EQUIVALENT


def test_positional_tokens_of_two_models_are_not_the_same_expression():
    free = 'SELECT n FROM t WHERE s = "__sqlx_token_000__"'
    paid = 'SELECT n FROM t WHERE s = "__sqlx_token_000__"'
    assert masked_template_problem(free, paid)
    assert not prove_equivalent(free, paid).proven


def test_a_project_wide_variable_token_in_a_string_is_not_one_more_constant():
    # Project reduction masks a movable ${...} as __kumo_x_<hash>__. In a string it is a value nobody knows
    # (the false proof the first reduction eval found): it must not look different from 'paid'.
    token = "__kumo_x_0123456789ab__"
    filtered = f"SELECT n FROM t WHERE s = '{token}' AND s = 'paid'"
    nothing = "SELECT n FROM t WHERE FALSE"
    assert masked_template_problem(filtered, nothing)
    assert masked_template_problem(f'SELECT n FROM t WHERE s = "{token}"', "SELECT 1")
    assert masked_template_problem(f"SELECT n FROM t WHERE s = 'a_{token}'", "SELECT 1")
    assert not prove_equivalent(filtered, nothing).proven
    assert prove_equivalent_smt(filtered, nothing).status is not SmtStatus.PROVEN_EQUIVALENT
    assert not prove_equivalent_algebraic(filtered, nothing).proven
    # as a table name the token is sound: it is not a string literal
    assert masked_template_problem(f"SELECT n FROM {token} WHERE s = 'paid'", "SELECT 1") is None


def test_text_without_masked_expressions_is_not_refused():
    assert masked_template_problem("SELECT n FROM t WHERE s = 'paid'", "SELECT 1") is None


def _project(root, models):
    (root / "definitions").mkdir()
    (root / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n")
    (root / "definitions" / "orders.sqlx").write_text('config { type: "declaration", schema: "raw", name: "orders" }\n')
    for name, body in models.items():
        (root / "definitions" / f"{name}.sqlx").write_text('config { type: "view" }\n' + body)
    return load_sqlx_project(root)


def test_loaded_models_with_masked_strings_are_refused_by_every_prover(tmp_path):
    pipeline = _project(tmp_path, {
        "a": "SELECT id FROM ${ref('orders')} WHERE status = \"${vars.paid}\" AND status = 'paid' AND amount > 0\n",
        "b": "SELECT id FROM ${ref('orders')} WHERE status = \"${vars.paid}\" AND status = 'paid'\n",
        "c": 'SELECT id FROM ${ref("orders")} WHERE status = "${vars.free}"\n',
        "d": 'SELECT id FROM ${ref("orders")} WHERE status = "${vars.paid}"\n',
    })
    sql = {name: pipeline.models[f"proj.analytics.{name}"].sql for name in "abcd"}
    for left, right in (("a", "b"), ("c", "d")):
        assert masked_template_problem(sql[left], sql[right])
        assert not prove_equivalent(sql[left], sql[right]).proven
        assert prove_equivalent_smt(sql[left], sql[right]).status is not SmtStatus.PROVEN_EQUIVALENT
        assert not prove_equivalent_algebraic(sql[left], sql[right]).proven
