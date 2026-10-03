import json

import pytest
from sqlglot import exp

from kumosql import (
    PipelineResult,
    RewriteResult,
    RewriteRule,
    Verification,
    VerificationCheck,
    VerificationStatus,
    attach_planner_check,
    engine,
    verify_rewrite,
)
from kumosql.cli import dry_run_main, main, rewrite_main
from kumosql.dryrun import check_rewrite


class _FakePlanner:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def __call__(self, url, headers, body):
        query = json.loads(body)["configuration"]["query"]["query"]
        self.queries.append(query)
        return self.responses[query]


def _planner_result(fields, bytes_processed):
    return 200, {
        "statistics": {
            "totalBytesProcessed": str(bytes_processed),
            "query": {"schema": {"fields": fields}},
        }
    }


class _DropWhereRule(RewriteRule):
    name = "test_drop_where_for_cli"
    summary = "Test that unproven output stays a distinct CLI result"

    def rewrite_statement(self, statement, index):
        changed = 0
        for select in statement.find_all(exp.Select):
            if select.args.get("where") is not None:
                select.set("where", None)
                changed += 1
        return changed, []


class _DropSqlxWhereRule(RewriteRule):
    name = "test_drop_sqlx_where_for_cli"
    summary = "Test that SQLX restoration failures do not reach CLI output"

    def rewrite_statement(self, statement, index):
        where = statement.args.get("where")
        if where is None:
            return 0, []
        statement.set("where", None)
        return 1, []


@pytest.mark.parametrize(
    ("source", "diagnostic_code"),
    [
        ("SELECT * FROM", "parse_error"),
        (
            "WITH __lifted_subquery_001 AS (SELECT * FROM __lifted_subquery_002) "
            "SELECT * FROM __lifted_subquery_001",
            "cte_dependency_error",
        ),
    ],
)
def test_rewrite_cli_withholds_rule_failure_output_and_shows_diagnostic(
    tmp_path, capsys, source, diagnostic_code
):
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text(source, encoding="utf-8")
    output_path.write_text("keep this file", encoding="utf-8")

    status = rewrite_main(
        [str(input_path), "--rule", "lift_subqueries", "--output", str(output_path)]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert output_path.read_text(encoding="utf-8") == "keep this file"
    assert captured.out == ""
    assert "verification=failed" in captured.err
    assert "check rewrite=failed:" in captured.err
    assert f"diagnostic: {diagnostic_code}:" in captured.err


def test_lift_cli_withholds_parse_failure_output(tmp_path, capsys):
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text("SELECT * FROM", encoding="utf-8")
    output_path.write_text("keep this file", encoding="utf-8")

    status = main([str(input_path), "--output", str(output_path)])

    captured = capsys.readouterr()
    assert status == 2
    assert output_path.read_text(encoding="utf-8") == "keep this file"
    assert captured.out == ""
    assert "diagnostic: parse_error:" in captured.err


def test_rewrite_cli_withholds_sqlx_restoration_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(engine._REGISTRY, _DropSqlxWhereRule.name, _DropSqlxWhereRule())
    input_path = tmp_path / "input.sqlx"
    output_path = tmp_path / "output.sqlx"
    input_path.write_text(
        'config { type: "table" }\n'
        'SELECT id FROM source WHERE ${when(incremental(), "id > 0", "TRUE")}',
        encoding="utf-8",
    )
    output_path.write_text("keep this file", encoding="utf-8")

    status = rewrite_main(
        [str(input_path), "--rule", _DropSqlxWhereRule.name, "--output", str(output_path)]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert output_path.read_text(encoding="utf-8") == "keep this file"
    assert captured.out == ""
    assert "diagnostic: sqlx_restore_error:" in captured.err


def test_rewrite_cli_keeps_unproven_output_as_review_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(engine._REGISTRY, _DropWhereRule.name, _DropWhereRule())
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text("SELECT value FROM source WHERE value > 1", encoding="utf-8")

    status = rewrite_main(
        [str(input_path), "--rule", _DropWhereRule.name, "--output", str(output_path)]
    )

    captured = capsys.readouterr()
    assert status == 3
    assert "WHERE" not in output_path.read_text(encoding="utf-8")
    assert "verification=unproven" in captured.err
    assert "check equivalence_proof=not_proven:" in captured.err
    assert "check planner=not_run:" in captured.err


def test_rewrite_cli_reports_planner_checked_as_one_untrusted_label(
    tmp_path, monkeypatch, capsys
):
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text("SELECT 1 AS value", encoding="utf-8")
    source = input_path.read_text(encoding="utf-8")
    candidate = "SELECT 2 AS value"
    verification = Verification(
        VerificationStatus.PLANNER_CHECKED,
        "the planner check passed, but equivalence could not be proven",
        checks=(
            VerificationCheck("equivalence_proof", "not_proven", "No proof was found."),
            VerificationCheck("planner", "passed", "The planner accepted the candidate."),
        ),
    )
    step = RewriteResult(
        "format_sql", source, candidate, 1, (), verification, rule_success=True
    )
    monkeypatch.setattr(
        "kumosql.cli.apply_rules",
        lambda names, sql: PipelineResult(sql, candidate, (step,), verification),
    )

    status = rewrite_main(
        [str(input_path), "--rule", "format_sql", "--output", str(output_path)]
    )

    captured = capsys.readouterr()
    assert status == 3
    assert output_path.read_text(encoding="utf-8").strip() == candidate
    assert captured.err.count("verification=planner_checked") == 2
    assert captured.err.count(
        "check planner=passed: The planner accepted the candidate."
    ) == 1


def test_rewrite_cli_unproven_override_is_explicit_and_warned(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(engine._REGISTRY, _DropWhereRule.name, _DropWhereRule())
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text("SELECT value FROM source WHERE value > 1", encoding="utf-8")

    status = rewrite_main(
        [
            str(input_path),
            "--rule",
            _DropWhereRule.name,
            "--output",
            str(output_path),
            "--allow-unproven",
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert "WHERE" not in output_path.read_text(encoding="utf-8")
    assert "warning: output is not proven equivalent" in captured.err


def test_rewrite_cli_opt_in_attaches_planner_only_label(tmp_path, monkeypatch, capsys):
    source = "SELECT 1 AS value"
    candidate = "SELECT 2 AS value"
    input_path = tmp_path / "input.sql"
    output_path = tmp_path / "output.sql"
    input_path.write_text(source, encoding="utf-8")
    verification = verify_rewrite(source, candidate)
    step = RewriteResult("format_sql", source, candidate, 1, (), verification, True)
    pipeline = PipelineResult(source, candidate, (step,), verification)
    monkeypatch.setattr("kumosql.cli.apply_rules", lambda names, sql: pipeline)
    fields = [{"name": "value", "type": "INTEGER"}]
    fake = _FakePlanner(
        {source: _planner_result(fields, 1000), candidate: _planner_result(fields, 1200)}
    )

    def attach_with_fake(result, project, **kwargs):
        return attach_planner_check(result, project, token="token", transport=fake, **kwargs)

    monkeypatch.setattr("kumosql.cli.attach_planner_check", attach_with_fake)
    status = rewrite_main(
        [
            str(input_path),
            "--rule",
            "format_sql",
            "--output",
            str(output_path),
            "--planner-project",
            "billing",
        ]
    )

    captured = capsys.readouterr()
    assert status == 3
    assert output_path.read_text(encoding="utf-8").strip() == candidate
    assert "verification=planner_checked" in captured.err
    assert "check planner=passed:" in captured.err
    assert "estimated bytes delta: 200 bytes (estimate)" in captured.err
    assert fake.queries == [source, candidate]


def test_rewrite_cli_does_not_attach_planner_without_opt_in(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "input.sql"
    input_path.write_text("SELECT 1", encoding="utf-8")
    monkeypatch.setattr(
        "kumosql.cli.attach_planner_check",
        lambda *args, **kwargs: pytest.fail("planner attachment requires explicit opt-in"),
    )

    status = rewrite_main([str(input_path), "--rule", "remove_trivial_predicates"])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out.strip() == "SELECT 1"
    assert "check planner=" not in captured.err


def test_dry_run_cli_uses_planner_wording_and_labels_bytes_as_estimates(
    tmp_path, monkeypatch, capsys
):
    original_path = tmp_path / "original.sql"
    rewritten_path = tmp_path / "rewritten.sql"
    original_path.write_text("SELECT 1", encoding="utf-8")
    rewritten_path.write_text("SELECT 2", encoding="utf-8")
    fields = [{"name": "value", "type": "INTEGER"}]
    fake = _FakePlanner(
        {"SELECT 1": _planner_result(fields, 1000), "SELECT 2": _planner_result(fields, 900)}
    )
    monkeypatch.setattr(
        "kumosql.cli.check_rewrite",
        lambda before, after, project, location=None: check_rewrite(
            before, after, project, location=location, token="token", transport=fake
        ),
    )

    status = dry_run_main(
        [str(original_path), "--rewritten", str(rewritten_path), "--project", "billing"]
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "planner_check=passed" in output
    assert "schema_matches=True" in output
    assert "results were not compared" in output
    assert "estimated_bytes_delta=-100 (estimate)" in output
    assert not any(word in output.lower() for word in ("equivalent", "proven", "verified", "safe"))


def test_dry_run_cli_never_calls_an_unobserved_schema_a_match(tmp_path, monkeypatch, capsys):
    original_path = tmp_path / "original.sql"
    rewritten_path = tmp_path / "rewritten.sql"
    original_path.write_text("SELECT 1", encoding="utf-8")
    rewritten_path.write_text("SELECT 2", encoding="utf-8")
    no_schema = (200, {"statistics": {"query": {}}})
    fake = _FakePlanner({"SELECT 1": no_schema, "SELECT 2": no_schema})
    monkeypatch.setattr(
        "kumosql.cli.check_rewrite",
        lambda before, after, project, location=None: check_rewrite(
            before, after, project, location=location, token="token", transport=fake
        ),
    )

    status = dry_run_main([str(original_path), "--rewritten", str(rewritten_path), "--project", "billing"])

    output = capsys.readouterr().out
    assert status == 2
    assert "planner_check=not_run" in output
    assert "schema_matches=unknown" in output
    assert "schema_matches=True" not in output


def test_evidence_summary_cli_prints_only_the_aggregate(tmp_path, capsys):
    from kumosql.cli import evidence_summary_main

    (tmp_path / "a.sql").write_text("SELECT secret_col FROM secret_tbl WHERE TRUE AND secret_col > 1", encoding="utf-8")
    (tmp_path / "b.sql").write_text("SELECT 1", encoding="utf-8")
    code = evidence_summary_main(
        [str(tmp_path), "--rule", "remove_trivial_predicates", "--min-changed", "1"]
    )
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert (data["total"], data["changed"], data["unchanged"]) == (2, 1, 1)
    assert data["useful_evidence"] == 1
    assert data["percent_of_changed"]["useful_evidence"] == 100.0
    assert "secret" not in out.lower() and "a.sql" not in out


def test_evidence_summary_cli_withholds_small_percentages_and_needs_files(tmp_path, capsys):
    from kumosql.cli import evidence_summary_main

    (tmp_path / "a.sql").write_text("SELECT a FROM t WHERE TRUE AND a > 1", encoding="utf-8")
    evidence_summary_main([str(tmp_path), "--rule", "remove_trivial_predicates"])
    assert json.loads(capsys.readouterr().out)["percent_of_changed"]["useful_evidence"] is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert evidence_summary_main([str(empty), "--rule", "remove_trivial_predicates"]) == 2
