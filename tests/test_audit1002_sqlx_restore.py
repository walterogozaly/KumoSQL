"""Restoring a masked SQLX ``${...}`` expression puts its text back byte for byte.

The text used to go through ``re.sub`` as a replacement template, so a
backslash in it was read as replacement syntax: ``r'\\d'`` lost a backslash,
``\\1`` raised, and ``\\g<0>`` pasted the sentinel back in.
"""

from pathlib import Path

import pytest

from kumosql import apply_rule, load_sqlx_project
from kumosql.sqlx import mask_sqlx_interpolations, restore_sqlx_interpolations


def _round_trip(text):
    masked, restorations = mask_sqlx_interpolations(text)
    return restore_sqlx_interpolations(masked, restorations)


def test_regex_backslash_in_an_optional_clause_survives_restoration():
    text = r'''SELECT * FROM (SELECT a FROM t) AS s ${when(true, "WHERE REGEXP_CONTAINS(a, r'\\d')", "")}'''

    assert _round_trip(text) == text


def test_group_references_in_an_expression_are_not_expanded():
    text = r'''SELECT ${"\1"} AS x, ${'\g<0>'} AS y, ${"\g<name>"} AS z FROM t'''

    assert _round_trip(text) == text


def test_group_reference_in_a_condition_continuation_is_restored():
    text = r'''SELECT a FROM t WHERE b > 0 ${when(incremental(), `AND REGEXP_CONTAINS(c, r"\1")`)}'''

    assert _round_trip(text) == text


_FRAGMENTS = [
    r'''${"\\"}''',
    r'''${"a\\nb"}''',
    r'''${'\1\2'}''',
    r'''${"\g<0>\g<1>"}''',
    r'''${`$\{x\}`}''',
    r'''${"\\\\d+"}''',
    r'''${"café ☃"}''',
    r'''${"caf\u00e9 \x41"}''',
    r'''${when(incremental(), `x > (SELECT MAX(x) FROM ${self()}) AND y = r'\d'`)}''',
]

_POSITIONS = [
    "SELECT {f} AS v FROM t",
    "SELECT a FROM t WHERE b = {f}",
    "SELECT a FROM t WHERE b > 0 ${{when(incremental(), `AND c = {f}`)}}",
    "SELECT a FROM t ${{when(incremental(), `WHERE c = {f}`, ``)}}",
    "SELECT a FROM t ORDER BY {f}",
]


@pytest.mark.parametrize("fragment", _FRAGMENTS)
@pytest.mark.parametrize("position", _POSITIONS)
def test_mask_then_restore_is_the_identity(fragment, position):
    text = position.format(f=fragment)

    assert _round_trip(text) == text


def test_a_rewrite_around_a_backslash_expression_keeps_it_byte_for_byte():
    fragment = r'''${when(true, "WHERE REGEXP_CONTAINS(a, r'\\d')", "")}'''
    source = f'config {{ type: "table" }}\nSELECT * FROM (SELECT a FROM t) AS s {fragment}\n'

    result = apply_rule("lift_subqueries", source)

    assert result.sql != source
    assert result.sql.count(fragment) == 1
    assert "__sqlx_token_" not in result.sql


def test_self_in_a_model_with_a_backslash_in_its_name_is_its_own_table(tmp_path: Path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (tmp_path / "definitions" / "m.sqlx").write_text(
        'config { type: "incremental", name: "t\\d" }\nSELECT 1 AS id FROM ${self()}\n'
    )

    pipeline = load_sqlx_project(tmp_path)

    model = pipeline.models[r"p.d.t\d"]
    assert r"`p.d.t\d`" in model.sql
    assert not [d for d in pipeline.all_diagnostics() if d.code == "asset_unreadable"]
