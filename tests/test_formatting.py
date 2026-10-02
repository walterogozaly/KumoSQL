import pytest

from kumosql.formatting import (
    DEFAULT_PREFERENCES,
    complexity,
    format_sql,
    load_preferences,
    parse_preferences,
    save_preferences,
)
from kumosql.rewrite import apply_rules, VerificationStatus

MESSY = "select a,b,count(*) as n from `p.d.t` t join x on x.id=t.id where a=1 and (b=2 or c=3) group by 1,2"


def test_format_sql_applies_preferences():
    upper = format_sql(MESSY)
    assert upper.startswith("SELECT") and "JOIN x ON x.id = t.id" in upper
    lower = format_sql(MESSY, parse_preferences({"keyword_case": "lower", "comma_position": "leading"}))
    assert lower.startswith("select") and "\n  , b" in lower


def test_format_rule_is_verified_like_any_other_rule():
    result = apply_rules(["format_sql"], MESSY)
    assert result.success
    assert result.verification.status is VerificationStatus.PROVEN
    assert result.sql != MESSY


def test_format_rule_uses_saved_preferences_and_leaves_sqlx_alone():
    save_preferences(parse_preferences({"keyword_case": "lower"}))
    assert apply_rules(["format_sql"], MESSY).sql.startswith("select")
    sqlx = "config { type: 'table' }\nselect 1"
    step = apply_rules(["format_sql"], sqlx).steps[0]
    assert step.sql == sqlx and step.diagnostics[0].code == "unsupported_sqlx"


def test_unparseable_sql_is_reported_not_raised():
    step = apply_rules(["format_sql"], "select from from where").steps[0]
    assert step.sql == "select from from where"
    assert step.diagnostics[0].code == "parse_error"


@pytest.mark.parametrize("bad", [
    [], {"unknown": 1}, {"max_line_length": 5}, {"max_line_length": True}, {"keyword_case": "shout"},
    {"rules": []}, {"rules": ["bad rule!"]}, {"exclude_rules": "LT05"},
])
def test_preferences_reject_bad_values(bad):
    with pytest.raises(ValueError):
        parse_preferences(bad)


def test_preferences_persist_and_default():
    assert load_preferences() == DEFAULT_PREFERENCES
    prefs = parse_preferences({"max_line_length": 120, "rules": ["LT01", "layout"]})
    save_preferences(prefs)
    assert load_preferences() == prefs


def test_complexity_grows_with_structure():
    flat = complexity("SELECT a FROM t")
    busy = complexity(
        "WITH a AS (SELECT id FROM t WHERE x = 1 AND y = 2) "
        "SELECT CASE WHEN a.id > 1 THEN 1 END c, ROW_NUMBER() OVER () rn "
        "FROM a JOIN b USING (id) WHERE a.id IN (SELECT id FROM (SELECT id FROM z)) "
        "UNION ALL SELECT 1, 2"
    )
    assert flat.score == 0 and flat.band == "low"
    assert busy.score > flat.score
    assert busy.metrics["joins"] == 1 and busy.metrics["ctes"] == 1 and busy.metrics["subqueries"] == 2
    assert busy.metrics["set_operations"] == 1 and busy.metrics["window_functions"] == 1


def test_complexity_rejects_sqlx():
    with pytest.raises(ValueError):
        complexity("config { type: 'table' }\nselect 1")


def test_union_is_not_counted_as_extra_nesting():
    assert complexity("SELECT a FROM t UNION ALL SELECT b FROM u").metrics["max_nesting"] == 0
    assert complexity("SELECT a FROM t UNION ALL SELECT b FROM u").score == 2.0


def test_format_keeps_the_inputs_trailing_newline_state():
    assert not format_sql("select 1").endswith("\n")
    assert format_sql("select 1\n").endswith("\n")


def test_formatting_keeps_backticked_routine_paths_as_written():
    # BigQuery routine and table paths are case sensitive; sqlfluff upper-cases a quoted function name.
    sql = "SELECT `proj.ds.my_udf`(age) AS x FROM `proj.ds.t`"
    out = format_sql(sql)
    assert "`proj.ds.my_udf`(age)" in out and "`proj.ds.t`" in out
    assert format_sql("DROP FUNCTION IF EXISTS `proj.ds.my_udf`") == "DROP FUNCTION IF EXISTS `proj.ds.my_udf`"


def test_formatting_keeps_the_case_of_unquoted_names():
    # BigQuery table names are case sensitive and an alias's case names the output column,
    # so the capitalisation group must not re-case identifiers (SQLStorm found this).
    sql = "select p.Id as PostId, p.Score from Posts as p where p.Score > 1 order by p.Score"
    formatted = format_sql(sql)
    assert "FROM Posts AS p" in formatted and "p.Id AS PostId" in formatted
    result = apply_rules(["format_sql"], sql)
    assert result.verification.status is VerificationStatus.PROVEN


def test_identifier_capitalisation_runs_only_when_named():
    prefs = parse_preferences({"rules": ["layout", "capitalisation", "CP02"]})
    assert "Name" in format_sql("select Id, name from Posts", prefs)
    assert "name" in format_sql("select Id, name from Posts")
