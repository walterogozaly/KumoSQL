"""The static prover ignores SQL comments, except ones that may hold SQL.

sqlglot keeps parsed comments on ``node.comments``, outside ``args``, so the
prover's comment removal used to remove nothing and a trailing ``-- note``
blocked a proof. A comment holding a SQLX interpolation still counts:
Dataform expands ``${...}`` inside SQL comments too, and an expansion with a
line break ends the comment and becomes SQL.
"""

from __future__ import annotations

import pytest
from sqlglot_support import OLD_SQLGLOT

from kumosql import prove_equivalent
from kumosql.rewrite import verify_rewrite
from kumosql.sqlx import mask_sqlx_by_content


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("SELECT 1 AS a -- note", "SELECT 1 AS a"),
        ("SELECT a /* inline */ + 1 AS b FROM t", "SELECT a + 1 AS b FROM t"),
        ("SELECT a FROM t WHERE /* why */ a > 1 -- trailing\n AND b < 2", "SELECT a FROM t WHERE a > 1 AND b < 2"),
        (
            "WITH /* the base */ x AS (\n  SELECT a FROM t -- inner\n)\nSELECT a FROM x # last",
            "WITH x AS (SELECT a FROM t) SELECT a FROM x",
        ),
        ("-- header\nSELECT a FROM t", "SELECT a FROM t /* footer */"),
    ],
)
def test_comments_do_not_block_a_proof(left, right):
    assert prove_equivalent(left, right).proven


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("SELECT a FROM t WHERE a > 1 -- note", "SELECT a FROM t WHERE a > 2 -- note"),
        ("SELECT a FROM t -- WHERE a > 1", "SELECT a FROM t WHERE a > 1"),
        ("SELECT a /* , b */ FROM t", "SELECT a, b FROM t"),
    ],
)
def test_queries_that_differ_apart_from_comments_are_not_proven(left, right):
    assert not prove_equivalent(left, right).proven


def test_a_comment_holding_a_sqlx_interpolation_is_kept():
    commented = 'SELECT a FROM t -- ${when(incremental(), "\\nWHERE b > 0")}'
    assert not prove_equivalent(mask_sqlx_by_content(commented), "SELECT a FROM t").proven
    assert not prove_equivalent(commented, "SELECT a FROM t").proven
    # The same interpolation in the same place on both sides still proves.
    assert prove_equivalent(
        mask_sqlx_by_content(commented.replace("SELECT a", "SELECT a /* note */")),
        mask_sqlx_by_content(commented),
    ).proven


def test_sqlx_rewrite_dropping_a_commented_interpolation_is_not_verified():
    before = 'config { type: "table" }\nSELECT a FROM ${ref("t")} -- ${when(incremental(), "\\nWHERE b > 0")}\n'
    after = 'config { type: "table" }\nSELECT a FROM ${ref("t")}\n'
    assert verify_rewrite(before, after).status.value != "proven"
    if OLD_SQLGLOT:
        pytest.skip("sqlglot 26's BigQuery parser drops a comment after a table name, so a SQLX expression inside one is not seen")
    # A layout-only change beside such an expression is declined too (SQLX safeguards).
    assert verify_rewrite(before, before.replace("SELECT a", "SELECT a /* the key */")).status.value != "proven"


def test_sqlx_rewrite_adding_a_plain_comment_is_verified():
    before = 'config { type: "table" }\nSELECT a FROM ${ref("t")}\n'
    assert verify_rewrite(before, before.replace("SELECT a", "SELECT a /* the key */")).status.value == "proven"
    assert verify_rewrite(before, before.replace("SELECT a", "-- the key\nSELECT a")).status.value == "proven"
