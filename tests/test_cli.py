import pytest
from sqlglot import exp

from kumosql import RewriteRule, engine
from kumosql.cli import main, rewrite_main


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
