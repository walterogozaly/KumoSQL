import json

import pytest

from kumosql.cli import pipeline_main, scopes_main
from kumosql.scopes import (
    Scope,
    delete_scope,
    get_scope,
    list_scopes,
    parse_scope,
    save_scope,
)


def team():
    return parse_scope({"name": "My Team", "fields": {"author": ["ana@co.com", " Bo@co.com "]}})


def test_scope_matches_case_insensitively_on_every_field():
    scope = parse_scope({"name": "s", "fields": {"author": ["ana@co.com"], "project": ["growth-*"]}})
    assert scope.matches({"author": "ANA@co.com", "project": "growth-eu"})
    assert not scope.matches({"author": "ana@co.com", "project": "core"})
    assert not scope.matches({"author": "ana@co.com"})


def test_scope_filters_records_across_entities():
    projects = parse_scope({"name": "Mine", "fields": {"project": ["a", "b"]}})
    records = [{"project": "a"}, {"project": "c"}, {"project": "b", "job": 1}]
    assert projects.filter(records) == [records[0], records[2]]


@pytest.mark.parametrize("bad", [
    None, {}, {"name": "", "fields": {"a": ["x"]}}, {"name": "n"}, {"name": "n", "fields": {}},
    {"name": "n", "fields": {"a": []}}, {"name": "n", "fields": {"a": [1]}}, {"name": "n", "fields": {"a": "x"}},
])
def test_parse_scope_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        parse_scope(bad)


def test_scopes_persist_replace_and_delete():
    save_scope(team())
    save_scope(Scope("Projects", {"project": ("a",)}))
    assert [s.name for s in list_scopes()] == ["My Team", "Projects"]
    assert get_scope("my team").fields["author"] == ("ana@co.com", "Bo@co.com")

    save_scope(parse_scope({"name": "MY TEAM", "fields": {"author": ["z"]}}))
    assert [s.name for s in list_scopes()] == ["Projects", "MY TEAM"]
    assert delete_scope("projects") and not delete_scope("projects")
    assert [s.name for s in list_scopes()] == ["MY TEAM"]


def test_cli_manages_scopes(capsys):
    scopes_main(["add", "My Team", "--field", "author", "ana@co.com", "bo@co.com"])
    scopes_main(["list"])
    assert json.loads(capsys.readouterr().out)["fields"] == {"author": ["ana@co.com", "bo@co.com"]}
    scopes_main(["remove", "My Team"])
    with pytest.raises(SystemExit):
        scopes_main(["remove", "My Team"])


def test_pipeline_report_can_be_limited_to_a_scope(tmp_path, capsys):
    (tmp_path / "dataform.json").write_text(json.dumps({"defaultDatabase": "p", "defaultSchema": "d"}))
    (tmp_path / "a.sql").write_text("SELECT id FROM `src.raw.t`")
    (tmp_path / "b.sql").write_text("SELECT id FROM `src.raw.t`")
    scopes_main(["add", "Only A", "--field", "name", "a"])

    pipeline_main([str(tmp_path)])
    everything = json.loads(capsys.readouterr().out)
    pipeline_main([str(tmp_path), "--scope", "Only A"])
    scoped = json.loads(capsys.readouterr().out)

    assert everything["models"] == 2
    assert scoped["models"] == 1 and scoped["scope"] == "Only A"
    assert scoped["order"] == ["a"]
    with pytest.raises(SystemExit):
        pipeline_main([str(tmp_path), "--scope", "nope"])
