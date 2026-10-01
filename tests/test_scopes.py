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
    assert json.loads(capsys.readouterr().out)["rule"] == {
        "field": "author", "op": "in", "value": ["ana@co.com", "bo@co.com"]
    }
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


def test_pipeline_scope_on_unsupported_field_is_rejected(tmp_path, capsys):
    (tmp_path / "a.sql").write_text("SELECT id FROM `src.raw.t`")
    scopes_main(["add", "Authors", "--field", "author", "ana@co.com"])
    with pytest.raises(SystemExit):
        pipeline_main([str(tmp_path), "--scope", "Authors"])
    assert "author" in capsys.readouterr().err


def test_pipeline_scope_matching_nothing_warns(tmp_path, capsys):
    (tmp_path / "a.sql").write_text("SELECT id FROM `src.raw.t`")
    scopes_main(["add", "Nothing", "--field", "name", "zzz"])
    pipeline_main([str(tmp_path), "--scope", "Nothing"])
    assert "matches no models" in capsys.readouterr().err


# ------------------------------------------------------------- rules engine

from kumosql import state
from kumosql.graph import build_query_graph
from kumosql.pipeline import load_sqlx_project
from kumosql.scopes import UnknownFieldError, discover_fields, parse_rule


def rule_scope(rule, name="r"):
    return parse_scope({"name": name, "rule": rule})


def cond(field, op, value=None, **extra):
    return {"field": field, "op": op, **({} if value is None else {"value": value}), **extra}


def test_user_example_submitter_in_list():
    scope = rule_scope(cond("submitter", "in", ["ana@co.com", "bo@co.com"]))
    assert scope.matches({"submitter": "ANA@co.com"})
    assert not scope.matches({"submitter": "cy@co.com"})
    assert not scope.matches({})


@pytest.mark.parametrize("rule,record,expected", [
    (cond("a", "eq", "X"), {"a": "x"}, True),
    (cond("a", "eq", "X", case_sensitive=True), {"a": "x"}, False),
    (cond("a", "ne", "x"), {"a": "y"}, True),
    (cond("a", "ne", "x"), {}, True),
    (cond("a", "not_in", ["x", "y"]), {"a": "y"}, False),
    (cond("a", "prefix", "growth-"), {"a": "Growth-eu"}, True),
    (cond("a", "suffix", "_v2"), {"a": "t_v2"}, True),
    (cond("a", "contains", "ro"), {"a": "growth"}, True),
    (cond("a", "glob", "g?owth-*"), {"a": "growth-eu"}, True),
    (cond("a", "regex", r"^t\d+$"), {"a": "T42"}, True),
    (cond("a", "regex", r"^t\d+$"), {"a": "t4x"}, False),
    (cond("bytes", "gt", "1000"), {"bytes": 2500}, True),
    (cond("bytes", "lt", 1000), {"bytes": "20"}, True),
    (cond("bytes", "gte", 10), {"bytes": "9"}, False),
    (cond("created", "gte", "2026-01-01"), {"created": "2026-03-05T10:00:00Z"}, True),
    (cond("created", "lt", "2026-01-01"), {"created": "2026-03-05T10:00:00Z"}, False),
    (cond("a", "is_null"), {"a": None}, True),
    (cond("a", "is_null"), {"a": []}, True),
    (cond("a", "is_null"), {"a": "x"}, False),
    (cond("a", "not_null"), {"a": "x"}, True),
    (cond("columns", "eq", "user_id"), {"columns": ["id", "User_ID"]}, True),
    (cond("labels.team", "eq", "growth"), {"labels": {"team": "Growth"}}, True),
    (cond("Submitter", "eq", "ana"), {"submitter": "ana"}, True),
])
def test_operators(rule, record, expected):
    assert rule_scope(rule).matches(record) is expected


def test_groups_nest_with_and_or_not():
    rule = {"all": [
        cond("submitter", "in", ["ana", "bo"]),
        {"any": [cond("dataset", "prefix", "raw"), {"not": cond("project", "eq", "sandbox")}]},
    ]}
    scope = rule_scope(rule)
    assert scope.matches({"submitter": "ana", "dataset": "raw_x", "project": "sandbox"})
    assert scope.matches({"submitter": "bo", "dataset": "core", "project": "prod"})
    assert not scope.matches({"submitter": "bo", "dataset": "core", "project": "sandbox"})
    assert not scope.matches({"submitter": "cy", "dataset": "raw_x", "project": "prod"})
    assert scope.describe() == (
        "submitter is one of [ana, bo] AND (dataset starts with raw OR NOT project equals sandbox)"
    )


@pytest.mark.parametrize("bad", [
    {}, {"all": []}, {"all": [], "any": []}, {"field": ""}, cond("a", "bogus", "x"), cond("a", "in", []),
    cond("a", "eq"), cond("a", "is_null", "x"), cond("a", "regex", "("), {"not": "x"},
    {"all": [cond("a", "eq", "x")], "field": "b"},
])
def test_malformed_rules_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_rule(bad)


def test_rule_depth_is_limited():
    rule = cond("a", "eq", "x")
    for _ in range(12):
        rule = {"not": rule}
    with pytest.raises(ValueError, match="nest"):
        parse_rule(rule)


def test_legacy_scopes_migrate_to_single_condition_rules():
    single = parse_scope({"name": "s", "fields": {"author": ["ana@co.com", "bo@co.com"]}})
    assert single.rule == cond("author", "in", ["ana@co.com", "bo@co.com"])
    both = parse_scope({"name": "s", "fields": {"author": ["ana"], "project": ["growth-*"]}})
    assert both.rule == {"all": [cond("author", "in", ["ana"]), cond("project", "prefix", "growth-")]}
    assert both.matches({"author": "ANA", "project": "Growth-eu"})
    assert not both.matches({"author": "ana", "project": "core"})
    assert Scope("s", {"project": ("a", "b")}).matches({"project": "B"})


def test_stored_legacy_scopes_are_rewritten_as_rules():
    state.set_section("scopes", [{"name": "Old", "fields": {"author": ["ana"]}}])
    assert list_scopes()[0].rule == cond("author", "in", ["ana"])
    assert state.get_section("scopes", []) == [{"name": "Old", "rule": cond("author", "in", ["ana"]), "applies_to": ["jobs"]}]


def test_unknown_fields_are_reported_with_suggestions():
    scope = rule_scope(cond("submiter", "eq", "x"))
    with pytest.raises(UnknownFieldError, match="did you mean 'submitter'"):
        scope.require_fields(["submitter", "project"], "job-history records")
    scope.require_fields(["Submiter"], "records")  # case-insensitive
    assert rule_scope(cond("labels.team", "eq", "x")).unknown_fields(["labels"]) == []


def write_project(tmp_path):
    (tmp_path / "dataform.json").write_text(json.dumps({"defaultDatabase": "p", "defaultSchema": "d"}))
    (tmp_path / "a.sql").write_text("SELECT id, name FROM `src.raw.t`")
    (tmp_path / "b.sql").write_text("SELECT id FROM `src.raw.t`")
    return tmp_path


def test_pipeline_scope_supports_rules_and_profile_fields(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path))
    both = rule_scope({"all": [cond("kind", "eq", "sql"), {"not": cond("name", "eq", "b")}]})
    assert pipeline.scope_keys(both) == {"a"}
    wide = rule_scope(cond("columns", "eq", "name"))
    assert pipeline.scope_keys(wide) == {"a"}
    assert pipeline.scope_keys(rule_scope(cond("column_count", "gte", 2))) == {"a"}


def test_pipeline_scope_on_job_field_names_the_problem(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path))
    with pytest.raises(UnknownFieldError) as excinfo:
        pipeline.report(scope=rule_scope(cond("submitter", "eq", "ana"), "Mine"))
    assert "'submitter'" in str(excinfo.value) and "dataset" in str(excinfo.value)


def job(job_id, submitter, table="src.raw.t"):
    return {"job_id": job_id, "creation_time": "2026-01-01T00:00:00Z", "destination": "p.d.a",
            "referenced_tables": [table], "submitter": submitter}


def test_graph_scopes_job_history_by_submitter_rule(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path))
    reads = [job("1", "ana"), job("2", "bo"), job("3", "cy")]
    scope = rule_scope({"any": [cond("submitter", "in", ["ana", "bo"]), cond("job_id", "eq", "3")]})
    scoped = build_query_graph(pipeline, reads, scope=scope).to_json()
    unscoped = build_query_graph(pipeline, reads).to_json()
    assert scoped["scope_applied"]["observations"] is True
    assert len(scoped["edges"]) <= len(unscoped["edges"])
    only_ana = build_query_graph(pipeline, reads, scope=rule_scope(cond("submitter", "eq", "ana"))).to_json()
    counts = [e.get("observed_count", 0) for e in only_ana["edges"]]
    assert sum(counts) == 1
    with pytest.raises(UnknownFieldError, match="submitter"):
        build_query_graph(pipeline, reads, scope=rule_scope(cond("sumbitter", "eq", "ana")))


def test_discover_fields_comes_from_the_data(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path))
    reads = [{**job("1", "ana"), "labels": {"team": "growth"}, "bytes_billed": 10}]
    found = {f.name: f for f in discover_fields(pipeline, reads)}
    assert found["kind"].source == "model" and found["kind"].examples == ("sql",)
    assert found["submitter"].source == "job" and found["submitter"].examples == ("ana",)
    assert found["bytes_billed"].kind == "number" and "labels.team" in found
    assert "columns" in found  # profile fields are listed even before profiles are computed


def test_cli_adds_rules_and_lists_fields(tmp_path, capsys):
    scopes_main(["add", "Mine", "--rule", json.dumps({"all": [cond("submitter", "in", ["ana"]), cond("project", "prefix", "g")]})])
    assert get_scope("mine").describe() == "submitter is one of [ana] AND project starts with g"
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(json.dumps(cond("dataset", "regex", "^raw")))
    scopes_main(["add", "Raw", "--rule-file", str(rule_file)])
    assert get_scope("raw").rule["op"] == "regex"
    with pytest.raises(SystemExit):
        scopes_main(["add", "Bad", "--rule", "{not json"])
    with pytest.raises(SystemExit):
        scopes_main(["add", "Bad", "--rule", "{}"])
    with pytest.raises(SystemExit):
        scopes_main(["add", "Bad"])
    capsys.readouterr()
    reads = tmp_path / "reads.json"
    reads.write_text(json.dumps([job("1", "ana")]))
    scopes_main(["fields", "--root", str(write_project(tmp_path)), "--observed-reads", str(reads)])
    out = capsys.readouterr().out
    assert "kind\tmodel" in out and "submitter\tjob\ttext  e.g. ana" in out


def test_cli_report_with_rule_scope_and_unknown_field(tmp_path, capsys):
    root = write_project(tmp_path)
    scopes_main(["add", "NotB", "--rule", json.dumps({"not": cond("name", "eq", "b")})])
    pipeline_main([str(root), "--scope", "NotB"])
    assert json.loads(capsys.readouterr().out)["order"] == ["a"]
    scopes_main(["add", "Typo", "--rule", json.dumps(cond("dataet", "eq", "d"))])
    with pytest.raises(SystemExit):
        pipeline_main([str(root), "--scope", "Typo"])
    assert "did you mean 'dataset'" in capsys.readouterr().err


def test_team_rule_on_information_schema_jobs_user_email():
    team = "ana@co.com\r\nBo@co.com; cy@co.com , dee@co.com\n\n"
    scope = rule_scope(cond("user_email", "in", team))
    assert scope.rule["value"] == ["ana@co.com", "Bo@co.com", "cy@co.com", "dee@co.com"]
    row = {"job_id": "bquxjob_1", "user_email": "BO@co.com", "project_id": "p", "job_type": "QUERY",
           "destination_table": {"project_id": "p", "dataset_id": "d", "table_id": "t"},
           "referenced_tables": [{"project_id": "p", "dataset_id": "raw", "table_id": "t"}]}
    assert scope.matches(row)
    assert not scope.matches({**row, "user_email": "zed@co.com"})
    # the same rule composes with other conditions, e.g. only query jobs
    both = rule_scope({"all": [cond("user_email", "in", ["bo@co.com"]), cond("job_type", "eq", "query")]})
    assert both.matches(row)


def test_jobs_view_columns_are_suggested_before_any_job_history_is_loaded():
    found = {f.name: f for f in discover_fields()}
    assert found["user_email"].source == "job"
    assert found["total_bytes_billed"].kind == "number"


# --------------------------------------------------- scopes built from scopes


def ref(name):
    return {"scope": name}


def save_team_scopes():
    save_scope(rule_scope(cond("user_email", "in", ["ana@co.com", "bo@co.com"]), "my_team"))
    save_scope(rule_scope(cond("user_email", "in", ["cy@co.com"]), "partner_team"))


def test_a_scope_can_be_the_union_or_the_intersection_of_other_scopes():
    save_team_scopes()
    either = rule_scope({"any": [ref("my_team"), ref("partner_team")]}, "our_department")
    both = rule_scope({"all": [ref("my_team"), ref("partner_team")]}, "overlap")
    not_mine = rule_scope({"all": [ref("partner_team"), {"not": ref("my_team")}]}, "partners_only")
    assert [either.matches({"user_email": e}) for e in ("ana@co.com", "CY@co.com", "zed@co.com")] == [True, True, False]
    assert not both.matches({"user_email": "ana@co.com"})
    assert not_mine.matches({"user_email": "cy@co.com"}) and not not_mine.matches({"user_email": "bo@co.com"})
    assert either.describe() == "in scope “my_team” OR in scope “partner_team”"
    assert either.fields_used() == ["user_email"] and either.references() == ["my_team", "partner_team"]


def test_nested_scope_references_and_mixed_conditions():
    save_team_scopes()
    save_scope(rule_scope({"any": [ref("my_team"), ref("partner_team")]}, "our_department"))
    org = rule_scope({"all": [ref("our_department"), cond("job_type", "eq", "query")]}, "org_queries")
    assert org.matches({"user_email": "cy@co.com", "job_type": "QUERY"})
    assert not org.matches({"user_email": "cy@co.com", "job_type": "LOAD"})


def test_scope_references_are_checked_on_save():
    save_team_scopes()
    with pytest.raises(ValueError, match="not saved"):
        save_scope(rule_scope(ref("nope"), "broken"))
    with pytest.raises(ValueError, match="cannot refer to themselves"):
        save_scope(rule_scope({"any": [ref("self_ref"), cond("a", "eq", "x")]}, "self_ref"))
    save_scope(rule_scope(ref("my_team"), "a_scope"))
    with pytest.raises(ValueError, match="cannot refer to themselves: a_scope → my_team → a_scope"):
        save_scope(rule_scope(ref("a_scope"), "my_team"))
    assert [s.name for s in list_scopes()] == ["my_team", "partner_team", "a_scope"]


def test_a_referenced_scope_cannot_be_deleted_or_left_dangling():
    save_team_scopes()
    save_scope(rule_scope({"any": [ref("my_team"), ref("partner_team")]}, "our_department"))
    with pytest.raises(ValueError, match="used by 'our_department'"):
        delete_scope("my_team")
    assert delete_scope("our_department") and delete_scope("my_team")


def test_unsaved_reference_fails_loudly_when_used():
    scope = rule_scope(ref("ghost"), "orphan")
    with pytest.raises(ValueError, match="not saved"):
        scope.matches({"a": 1})
    with pytest.raises(ValueError):
        parse_rule({"scope": ""})


def test_cli_scope_from_scopes_and_unknown_field_inside_reference(tmp_path, capsys):
    save_team_scopes()
    scopes_main(["add", "Dept", "--rule", json.dumps({"any": [ref("my_team"), ref("partner_team")]})])
    root = write_project(tmp_path)
    with pytest.raises(SystemExit):
        pipeline_main([str(root), "--scope", "Dept"])
    assert "user_email" in capsys.readouterr().err  # models have no such field, named clearly


def test_scopes_carry_an_applies_to_list_and_migrate_to_what_they_effectively_applied_to(monkeypatch, tmp_path):
    from kumosql import state

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    state.set_section("scopes", [
        {"name": "Models", "rule": {"field": "dataset", "op": "eq", "value": "raw"}},
        {"name": "Team", "rule": {"field": "user_email", "op": "in", "value": ["a@x.com"]}},
        {"name": "Explicit", "rule": {"field": "dataset", "op": "eq", "value": "raw"}, "applies_to": ["jobs"]},
    ])
    by_name = {s.name: s for s in list_scopes()}
    assert by_name["Models"].applies_to == ("models", "bigquery")
    assert by_name["Team"].applies_to == ("jobs",)
    assert by_name["Explicit"].applies_to == ("jobs",)
    assert all("applies_to" in item for item in state.get_section("scopes", []))


def test_applies_to_is_validated_and_ordered():
    scope = parse_scope({"name": "s", "rule": {"field": "a", "op": "eq", "value": "x"}, "applies_to": ["bigquery", "models"]})
    assert scope.to_json()["applies_to"] == ["models", "bigquery"]
    for bad in ([], ["nope"], "models"):
        with pytest.raises(ValueError):
            parse_scope({"name": "s", "rule": {"field": "a", "op": "eq", "value": "x"}, "applies_to": bad})


def test_plan_applies_a_scope_only_to_the_domains_it_lists():
    from kumosql.scopes import plan_scope

    reads = [{"job_id": "1", "user_email": "a@x.com", "referenced_tables": []}]
    rule = {"field": "project", "op": "eq", "value": "p"}
    both = plan_scope(Scope("s", rule=rule, applies_to=("models", "jobs")), reads)
    assert both.models is not None
    only_models = plan_scope(Scope("s", rule=rule, applies_to=("models",)), reads)
    assert only_models.models is not None and only_models.jobs is None and only_models.note is None
    bq_only = plan_scope(Scope("s", rule=rule, applies_to=("bigquery",)), reads)
    assert bq_only.models is None and bq_only.jobs is None and "BigQuery tables" in bq_only.note


def test_tag_rules_refuse_a_scope_that_does_not_apply_to_bigquery(monkeypatch, tmp_path):
    from kumosql import tags

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path))
    save_scope(Scope("jobs_only", rule={"field": "name", "op": "eq", "value": "x"}, applies_to=("jobs",)))
    with pytest.raises(ValueError, match="does not apply"):
        tags.save_rules([{"tag": "T", "rule": {"scope": "jobs_only"}}])
