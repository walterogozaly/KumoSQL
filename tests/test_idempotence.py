"""Idempotence: re-running a rule or pipeline on its own output changes nothing.

The rule list comes from the registry, so a newly registered rule is covered
automatically. Comparison is on exact text, so formatting oscillation counts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kumosql import VerificationStatus, apply_rule, apply_rules, available_rules, canonical_rule_order
from kumosql.formatting import FormatPreferences, FormatSqlRule

FIXTURE = Path(__file__).parent / "fixtures" / "sql_subquery_samples.json"
FIXTURE_ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))

HAND_WRITTEN = {
    "nested_join": "SELECT a.id FROM (SELECT id FROM (SELECT id FROM `p.d.t`) AS x) AS a JOIN (SELECT id FROM `p.d.u`) AS b ON a.id = b.id",
    "chained_ctes": "WITH a AS (SELECT id FROM `p.d.t`), b AS (SELECT id FROM a), c AS (SELECT id FROM b) SELECT * FROM c",
    "duplicate_ctes": "WITH a AS (SELECT 1 AS x), b AS (SELECT 1 AS x) SELECT * FROM a JOIN b USING (x)",
    "unused_cte": "WITH a AS (SELECT 1 AS x), unused AS (SELECT 2 AS y) SELECT * FROM a",
    "parens_and_trivial": "SELECT ((id)) FROM `p.d.t` WHERE (TRUE AND ((id > 1)) AND (FALSE OR id < 9))",
    "comments": "-- leading\nSELECT a.id /* inline */ FROM (SELECT id FROM `p.d.t`) AS a -- trailing\n",
    "block_comment": "/* header */\nSELECT (1) AS x FROM `p.d.t`\n",
    "multi_statement": "SELECT * FROM (SELECT 1 AS a) AS s;\n\nSELECT 2 AS b FROM `p.d.t` WHERE TRUE;\n",
    "multi_no_trailing": "SELECT * FROM (SELECT 1 AS a) AS s; SELECT 2",
    "nested_scopes": "WITH a AS (WITH inner_cte AS (SELECT 1 AS x) SELECT x FROM inner_cte) SELECT * FROM a",
    "delete": "DELETE FROM `p.d.t` WHERE TRUE AND id IN (SELECT id FROM (SELECT id FROM `p.d.u`) AS s)",
    "merge": "MERGE `p.d.t` AS t USING (SELECT id FROM `p.d.u`) AS s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "bigquery_only": "SELECT * FROM `p.d.t` FOR SYSTEM_TIME AS OF TIMESTAMP '2024-01-01' WHERE TRUE",
    "sqlx": "config { type: \"table\" }\nSELECT a.id FROM (SELECT id FROM ${ref(\"t\")}) AS a WHERE TRUE\n",
    "empty": "",
}

CORPUS = {row["id"]: row["sql_text"] for row in FIXTURE_ROWS} | HAND_WRITTEN

CANONICAL_ORDER = canonical_rule_order()


def _assert_second_run_is_noop(names, sql):
    first = apply_rules(names, sql)
    second = apply_rules(names, first.sql)
    assert second.sql == first.sql, f"{list(names)} changed its own output"
    assert second.verification.status is VerificationStatus.UNCHANGED


@pytest.mark.parametrize("rule", sorted(available_rules()))
@pytest.mark.parametrize("case", sorted(CORPUS))
def test_every_rule_is_idempotent(rule, case):
    once = apply_rule(rule, CORPUS[case])
    twice = apply_rule(rule, once.sql)

    assert twice.sql == once.sql
    assert twice.changes == 0
    assert twice.verification.status is VerificationStatus.UNCHANGED


@pytest.mark.parametrize(
    "prefs",
    [
        FormatPreferences(comma_position="leading"),
        FormatPreferences(indent_unit="tab"),
        FormatPreferences(max_line_length=30),
    ],
    ids=["leading_commas", "tabs", "short_lines"],
)
@pytest.mark.parametrize("case", sorted(CORPUS))
def test_format_sql_is_idempotent_with_non_default_preferences(prefs, case):
    overrides = {"format_sql": FormatSqlRule(prefs)}
    once = apply_rule("format_sql", CORPUS[case], overrides=overrides)
    twice = apply_rule("format_sql", once.sql, overrides=overrides)

    assert twice.sql == once.sql
    assert twice.changes == 0
    assert twice.verification.status is VerificationStatus.UNCHANGED


def test_canonical_order_runs_every_rule_with_formatting_last():
    assert set(CANONICAL_ORDER) == set(available_rules()) - {"lift_subqueries", "qualify_columns"}
    assert CANONICAL_ORDER[-1] == "format_sql"


@pytest.mark.parametrize("case", sorted(CORPUS))
def test_full_pipeline_in_canonical_order_is_idempotent(case):
    _assert_second_run_is_noop(CANONICAL_ORDER, CORPUS[case])


def test_lift_then_inline_is_documented_as_not_a_fixed_point():
    # The two rules are inverses, which is why the canonical order omits the
    # lifter: together they re-render an already-rewritten statement.
    sql = "SELECT * FROM (SELECT 1 AS x) AS a"
    names = ("lift_subqueries", "inline_single_use_ctes")
    first = apply_rules(names, sql)
    second = apply_rules(names, first.sql)
    assert sum(step.changes for step in second.steps) > 0


def test_check_idempotence_passes_for_the_canonical_pipeline():
    from kumosql import canonical_rule_order, check_idempotence

    check = check_idempotence(canonical_rule_order(), "SELECT a FROM (SELECT a FROM t) WHERE TRUE")
    assert check.idempotent
    assert check.to_json()["outcome"] == "passed"


def test_check_idempotence_reports_the_rule_that_keeps_changing():
    from kumosql import check_idempotence

    # The lifter and the CTE inliner undo each other, so together they never settle.
    check = check_idempotence(["lift_subqueries", "inline_single_use_ctes"], "SELECT a FROM (SELECT a FROM t) s")
    assert not check.idempotent
    assert set(check.rules_that_changed) == {"lift_subqueries", "inline_single_use_ctes"}
    assert check.to_json()["outcome"] == "failed"


def test_cli_check_idempotence_flag(tmp_path, capsys):
    from kumosql.cli import rewrite_main

    src = tmp_path / "q.sql"
    src.write_text("SELECT a FROM t WHERE TRUE\n")
    code = rewrite_main([str(src), "-r", "remove_trivial_predicates", "--check-idempotence"])
    assert code == 0
    assert "idempotence=passed" in capsys.readouterr().err
