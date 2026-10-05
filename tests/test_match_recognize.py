"""BigQuery's MATCH_RECOGNIZE clause (docs/match-recognize.md): read as BigQuery reads it, traced by its result columns, never proven or rewritten.

Every spelling used here was checked against BigQuery by dry run (``tests/fixtures/bq_syntax`` keeps the recorded results); the
forms BigQuery rejects are refused so that they are never mistaken for a query.
"""

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)
from kumosql import equivalence, parse_check, smt_equivalence
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.bigquery_on_duckdb import refusal
from kumosql.bounded_equivalence import check_bounded, schema_from_prover
from kumosql.containment import check_containment
from kumosql.engine import available_rules
from kumosql.match_recognize import text_has_clause
from kumosql.match_recognize_view import UnknownOutput, lineage_form, output_names
from kumosql.model_reuse import rewrite_over_model
from kumosql.output_properties import infer_properties
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target
from kumosql.rewrite import apply_rule
from kumosql.schema_change import assess_schema_change
from kumosql.sql_simplify import simpler_forms, tidy
from kumosql.statement_proof import prove_statements, prove_statements_smt
from sqlglot_support import OLD_SQLGLOT, skip_if_unparseable

SRC = "(SELECT 1 AS ts, 2 AS v, 'a' AS p)"


def mr(body: str = "PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS v > 1", source: str = SRC) -> str:
    return f"SELECT * FROM {source} MATCH_RECOGNIZE ({body})"


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


def prints(sql: str) -> str:
    return parse(sql).sql("bigquery")


# ---------------------------------------------------------------------------------------------------- reading

SPELLINGS = {
    "basic": mr(),
    "no_partition": mr("ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS v > 1"),
    "two_partitions": mr("PARTITION BY p, ts ORDER BY v MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "order_directions": mr("PARTITION BY p ORDER BY ts DESC, v MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "skip_past_last_row": mr("ORDER BY ts MEASURES COUNT(*) AS n AFTER MATCH SKIP PAST LAST ROW PATTERN (X+) DEFINE X AS v > 1"),
    "skip_to_next_row": mr("ORDER BY ts MEASURES COUNT(*) AS n AFTER MATCH SKIP TO NEXT ROW PATTERN (X+) DEFINE X AS v > 1"),
    "pattern_operators": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (^ X+ Y? | Z{2,3} $) DEFINE X AS v > 1, Y AS v < 5, Z AS v = 1"),
    "pattern_quantifiers": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN ((X | Y)+ Z{2} W{1,} U{,3} T*?) DEFINE X AS v > 1, Y AS v < 0, Z AS v = 3, W AS v = 4, U AS v = 5, T AS v = 6"),
    "navigation": mr("ORDER BY ts MEASURES FIRST(v) AS fv, LAST(v) AS lv, MATCH_NUMBER() AS mn, ARRAY_AGG(CLASSIFIER()) AS c PATTERN (X+) DEFINE X AS v > PREV(v, 1)"),
    "options": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1 OPTIONS (use_longest_match = TRUE)"),
    "table": "SELECT * FROM `p.d.t` MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)",
    "table_alias": "SELECT * FROM `p.d.t` AS t MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m",
    "bare_aliases": "SELECT m.n FROM d.t s MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) m",
    "subquery_alias": f"SELECT m.n FROM {SRC} AS t MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m",
    "lower_case": "select m.n from d.t match_recognize (partition by p order by ts measures count(*) as n pattern (x+) define x as v > 1) as m",
    "where_order_limit": mr() .replace("SELECT *", "SELECT m.p, m.fv") + " AS m WHERE m.fv > 0 ORDER BY m.p LIMIT 5",
    "in_cte": f"WITH r AS ({mr()}) SELECT p, fv FROM r",
    "in_subquery": f"SELECT 1 FROM ({mr()}) AS q",
    "cte_source": f"WITH t AS {SRC} SELECT r.p, u.v FROM t MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) r JOIN t AS u ON u.p = r.p",
    "joined_after": f"SELECT m.n, o.k FROM {SRC} MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m JOIN d.o AS o ON o.k = m.n",
    "joined_before": f"SELECT m.n, o.k FROM d.o AS o JOIN {SRC} MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m ON o.k = m.n",
    "set_operation": f"SELECT n FROM {SRC} MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) UNION ALL SELECT 1",
    "define_subquery": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > (SELECT MAX(w) FROM (SELECT 1 AS w))"),
    "string_with_paren": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS CONCAT(')', 'x') = ')x'"),
    "script": f"SELECT 1;\n{mr()};\nSELECT 2",
}
PIPES = {
    "pipe": f"FROM {SRC} |> WHERE v > 0 |> MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) |> WHERE n > 0",
    "pipe_first": f"FROM {SRC} |> MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)",
}


@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_every_spelling_bigquery_accepts_is_read(name):
    trees = sqlglot.parse(SPELLINGS[name], read="bigquery")
    clauses = [clause for tree in trees if tree is not None for clause in tree.find_all(exp.MatchRecognize)]
    assert len(clauses) == 1
    clause = clauses[0]
    assert clause.args.get("order") is not None and clause.args.get("measures") and clause.args.get("define") and clause.args.get("pattern")


@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_printing_it_gives_the_same_reading(name):
    if name == "script":
        pytest.skip("a script is printed one statement at a time")
    tree = parse(SPELLINGS[name])
    text = tree.sql("bigquery")
    assert text.upper().count("MATCH_RECOGNIZE (") == 1
    assert parse(text) == tree
    assert parse(text).sql("bigquery") == text


@pytest.mark.parametrize("name", ["basic", "table_alias", "options", "where_order_limit", "pattern_operators", "navigation", "skip_to_next_row"])
def test_text_it_was_written_in_prints_back_unchanged(name):
    assert prints(SPELLINGS[name]) == SPELLINGS[name]


@pytest.mark.parametrize("name", sorted(PIPES))
def test_the_pipe_operator_is_the_same_table_operator(name):
    skip_if_unparseable(PIPES[name])
    tree = parse(PIPES[name])
    assert len(list(tree.find_all(exp.MatchRecognize))) == 1
    text = tree.sql("bigquery")
    assert parse(text) == tree


def test_the_clause_reads_the_table_before_it_and_not_the_whole_select():
    tree = parse(f"SELECT m.n, o.k FROM d.o AS o JOIN d.t AS t MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m ON o.k = m.n WHERE o.k > 0")
    inner = next(select for select in tree.find_all(exp.Select) if select.args.get("match"))
    assert [table.name for table in inner.find_all(exp.Table)] == ["t"]
    assert tree.args["where"] is not None and not tree.args.get("match")
    assert sorted(table.name for table in tree.find_all(exp.Table)) == ["o", "t"]


def test_a_string_or_comment_that_says_match_recognize_is_not_a_clause():
    tree = parse("SELECT 'MATCH_RECOGNIZE (ORDER BY x)' AS s -- MATCH_RECOGNIZE (a)\nFROM t")
    assert not list(tree.find_all(exp.MatchRecognize))
    assert not text_has_clause("SELECT 'match_recognize (' FROM t")
    assert text_has_clause(mr())


def test_a_column_or_alias_named_like_the_keyword_is_untouched():
    assert prints("SELECT match_recognize FROM t") == "SELECT match_recognize FROM t"
    assert prints("SELECT a FROM t AS match_recognize") == "SELECT a FROM t AS match_recognize"


# ---------------------------------------------------------------------------------------------------- refusal

REJECTED = {
    "one_row_per_match": mr("ORDER BY ts MEASURES COUNT(*) AS n ONE ROW PER MATCH PATTERN (X+) DEFINE X AS v > 1"),
    "all_rows_per_match": mr("ORDER BY ts MEASURES COUNT(*) AS n ALL ROWS PER MATCH PATTERN (X+) DEFINE X AS v > 1"),
    "skip_to_first": mr("ORDER BY ts MEASURES COUNT(*) AS n AFTER MATCH SKIP TO FIRST X PATTERN (X+) DEFINE X AS v > 1"),
    "skip_to_last": mr("ORDER BY ts MEASURES COUNT(*) AS n AFTER MATCH SKIP TO LAST X PATTERN (X+) DEFINE X AS v > 1"),
    "final": mr("ORDER BY ts MEASURES FINAL COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "running": mr("ORDER BY ts MEASURES RUNNING COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "no_order_by": mr("MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "no_measures": mr("ORDER BY ts PATTERN (X+) DEFINE X AS v > 1"),
    "no_pattern": mr("ORDER BY ts MEASURES COUNT(*) AS n DEFINE X AS v > 1"),
    "no_define": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+)"),
    "out_of_order": mr("ORDER BY ts PARTITION BY p MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1"),
    "repeated": mr("ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) PATTERN (Y+) DEFINE X AS v > 1"),
    "measure_without_a_name": mr("ORDER BY ts MEASURES COUNT(*) PATTERN (X+) DEFINE X AS v > 1"),
    "tablesample_after": mr() + " TABLESAMPLE SYSTEM (10 PERCENT)",
    "pivot_after": mr() + " PIVOT (SUM(fv) FOR p IN ('a'))",
    "after_an_array_scan": "SELECT * FROM UNNEST([1, 2, 3]) AS v MATCH_RECOGNIZE (ORDER BY v MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)",
    "after_a_join": "SELECT * FROM d.t AS t JOIN d.u AS u USING (a) MATCH_RECOGNIZE (ORDER BY a MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)",
    "nested_in_the_table": f"SELECT * FROM ({mr()}) MATCH_RECOGNIZE (ORDER BY p MEASURES COUNT(*) AS n PATTERN (Y+) DEFINE Y AS fv > 0)",
    "nested_in_a_pipe": f"FROM {SRC} |> MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) |> MATCH_RECOGNIZE (ORDER BY n MEASURES COUNT(*) AS m PATTERN (Y+) DEFINE Y AS n > 0)",
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_what_bigquery_rejects_is_refused_and_not_read_as_a_query(name):
    skip_if_unparseable(SPELLINGS["basic"])
    with pytest.raises(ParseError):
        sqlglot.parse(REJECTED[name], read="bigquery")


def test_a_clause_after_something_that_is_not_a_table_is_refused():
    with pytest.raises(ParseError):
        parse("SELECT 1 + MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)")


# ---------------------------------------------------------------------------------------------------- columns

SCHEMA = {"src": {"ts": "INT64", "v": "INT64", "p": "STRING", "q": "INT64", "unused": "INT64"}, "other": {"k": "INT64"}}
SRC_TABLE = "src"


def pipeline(*queries: str) -> Pipeline:
    models = {f"m{i}": Model(Target(name=f"m{i}"), "table", query) for i, query in enumerate(queries, 1)}
    sources = {name: Target(name=name) for name in SCHEMA}
    return Pipeline(models, sources, SCHEMA)


def lineage(pl: Pipeline, model: str = "m1") -> dict[str, tuple[str, frozenset]]:
    return {
        ref.column: (record.status, frozenset((source.table, source.column) for source in record.sources))
        for ref, record in pl.explain_lineage().items()
        if ref.table == model
    }


def test_the_result_is_the_partition_columns_then_the_measures_and_nothing_else():
    pl = pipeline("SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv, COUNT(*) AS n PATTERN (X+) DEFINE X AS q > 1)")
    assert pl.output_columns("m1") == ("p", "fv", "n")


def test_a_measure_traces_to_the_columns_it_reads_and_a_partition_column_to_itself():
    found = lineage(pipeline("SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv, LAST(v) - FIRST(q) AS d PATTERN (X+) DEFINE X AS q > 1)"))
    assert found["p"] == ("traced", frozenset({("src", "p")}))
    assert found["fv"] == ("traced", frozenset({("src", "v")}))
    assert found["d"] == ("traced", frozenset({("src", "v"), ("src", "q")}))
    assert set(found) == {"p", "fv", "d"}


def test_the_columns_that_decide_which_rows_match_are_read_and_count_as_deciding():
    pl = pipeline("SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS q > PREV(q))")
    consumed = pl._analyse().consumed["m1"]
    assert {ref.column for ref in consumed} == {"p", "ts", "v", "q"}
    assert ColumnRef("src", "unused") not in consumed
    deciding = pl._analyse().conditions["m1"]
    assert {ref.column for ref in deciding} >= {"q", "ts"}


def test_a_column_nothing_in_the_clause_reads_is_not_attributed_to_the_result():
    pl = pipeline("SELECT * FROM src MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)")
    assert pl.output_columns("m1") == ("n",)
    assert lineage(pl)["n"][1] == frozenset()
    assert ColumnRef("src", "unused") not in pl._analyse().consumed["m1"]


@pytest.mark.parametrize(
    "partition, measures, expected",
    [
        ("p", "FIRST(v) AS fv", ["p", "fv"]),
        ("p, ts", "COUNT(*) AS n", ["p", "ts", "n"]),
        ("UPPER(p)", "COUNT(*) AS n", ["f0_", "n"]),
        ("p + 1, UPPER(p), ts", "COUNT(*) AS n", ["f0_", "f1_", "ts", "n"]),
        ("p", "COUNT(*) AS p", ["p", "p_1"]),
        ("p + 1, p", "COUNT(*) AS p, LAST(v) AS x", ["f0_", "p", "p_1", "x"]),
        ("p", "COUNT(*) AS p, SUM(v) AS p", ["p", "p_1", "p_2"]),
        ("t.p", "COUNT(*) AS n", ["p", "n"]),
    ],
)
def test_the_columns_are_named_as_bigquery_names_them(partition, measures, expected):
    clause = parse(f"SELECT * FROM src AS t MATCH_RECOGNIZE (PARTITION BY {partition} ORDER BY ts MEASURES {measures} PATTERN (X+) DEFINE X AS v > 1)").find(exp.MatchRecognize)
    assert output_names(clause) == expected


AMBIGUOUS = "SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES COUNT(*) AS p, SUM(v) AS p_1 PATTERN (X+) DEFINE X AS v > 1)"


def test_names_that_bigquery_would_have_to_choose_between_are_not_guessed():
    with pytest.raises(UnknownOutput):
        output_names(parse(AMBIGUOUS).find(exp.MatchRecognize))
    pl = pipeline(AMBIGUOUS)
    assert pl.output_columns("m1") == ()
    assert [d.code for d in pl.all_diagnostics() if d.model == "m1"] == ["qualify_error"]


def test_a_pattern_variable_in_front_of_a_column_is_the_column_and_not_a_table():
    found = lineage(pipeline("SELECT * FROM src MATCH_RECOGNIZE (ORDER BY ts MEASURES ARRAY_AGG(high.v) AS hs, ARRAY_AGG(low.q) AS ls PATTERN (low | high) DEFINE low AS v <= 2, high AS v >= 2)"))
    assert found["hs"][1] == frozenset({("src", "v")})
    assert found["ls"][1] == frozenset({("src", "q")})


def test_the_options_after_the_last_condition_are_not_a_condition_or_a_read():
    pl = pipeline("SELECT * FROM src MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS q > 1 OPTIONS (use_longest_match = TRUE))")
    assert {ref.column for ref in pl._analyse().consumed["m1"]} == {"ts", "q"}


def test_the_clause_inside_a_with_table_a_subquery_or_after_a_filter_gives_the_same_columns():
    clause = "MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS q > 1)"
    for sql in (
        f"WITH r AS (SELECT * FROM src {clause}) SELECT p, fv FROM r",
        f"SELECT m.p, m.fv FROM src {clause} AS m WHERE m.fv > 0",
        f"SELECT x.p, x.fv FROM (SELECT * FROM src {clause}) AS x",
        f"SELECT p, fv FROM src {clause} m ORDER BY fv",
    ):
        pl = pipeline(sql)
        assert pl.output_columns("m1") == ("p", "fv"), sql
        found = lineage(pl)
        assert found["fv"][1] == frozenset({("src", "v")}), sql
        assert found["p"][1] == frozenset({("src", "p")}), sql


def test_a_join_next_to_the_clause_keeps_each_side_s_columns_apart():
    pl = pipeline("SELECT m.fv, o.k FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS q > 1) AS m JOIN other AS o ON o.k = m.fv")
    assert pl.output_columns("m1") == ("fv", "k")
    found = lineage(pl)
    assert found["fv"][1] == frozenset({("src", "v")})
    assert found["k"][1] == frozenset({("other", "k")})
    assert set(pl.upstream["m1"]) == {"src", "other"}


def test_a_model_that_reads_the_clause_s_result_traces_through_it():
    pl = pipeline(
        "SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv, COUNT(*) AS n PATTERN (X+) DEFINE X AS q > 1)",
        "SELECT p, fv FROM m1 WHERE fv > 3",
    )
    trace = pl.trace_column(ColumnRef("m2", "fv"))
    assert trace.sources == frozenset({ColumnRef("src", "v")}) and not trace.unknown
    assert pl.dead_columns() == {"m1": ("n",)}


def test_a_clause_the_columns_of_which_cannot_be_named_leaves_its_readers_untraced_and_not_wrong():
    pl = pipeline(AMBIGUOUS, "SELECT * FROM m1")
    trace = pl.trace_column(ColumnRef("m2", "p"))
    assert not trace.sources


def test_the_plain_form_is_never_a_match_recognize_and_leaves_the_tree_alone():
    tree = parse("SELECT m.fv FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS q > 1) AS m")
    before = tree.sql("bigquery")
    plain = lineage_form(tree)
    assert not list(plain.find_all(exp.MatchRecognize))
    assert tree.sql("bigquery") == before and list(tree.find_all(exp.MatchRecognize))


def test_a_changed_input_column_changes_the_models_that_read_it():
    pl = pipeline("SELECT * FROM src MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES FIRST(v) AS fv PATTERN (X+) DEFINE X AS q > PREV(q))")
    for column in ("p", "ts", "v", "q"):
        change = assess_schema_change(pl, "drop_column", "src", column)
        assert [effect.model for effect in change.breaks] == ["m1"], column
    unused = assess_schema_change(pl, "drop_column", "src", "unused")
    assert not unused.breaks and not unused.output_changes


# ---------------------------------------------------------------------------------------------------- provers

PAIRS = {
    "same": (mr(), mr()),
    "other_define": (mr(), mr().replace("v > 1", "v > 2")),
    "other_pattern": (mr(), mr().replace("PATTERN (X+)", "PATTERN (X{2,})")),
    "other_skip": (mr(), mr().replace("PATTERN", "AFTER MATCH SKIP TO NEXT ROW PATTERN")),
    "other_order": (mr(), mr().replace("ORDER BY ts", "ORDER BY ts DESC")),
    "other_measure": (mr(), mr().replace("FIRST(v)", "LAST(v)")),
    "options": (mr(), mr().replace("v > 1", "v > 1 OPTIONS (use_longest_match = TRUE)")),
    "plain_form": (mr().replace("FIRST(v)", "SUM(v)"), f"SELECT p, SUM(v) AS fv FROM {SRC} WHERE v > 1 GROUP BY p"),
    "the_table_itself": (mr(), f"SELECT * FROM {SRC}"),
    "wrapped": (f"SELECT p, fv FROM ({mr()}) WHERE fv > 1", f"SELECT p, fv FROM ({mr().replace('v > 1', 'v > 3')}) WHERE fv > 1"),
}
PROVERS = {
    "prove_equivalent": equivalence.prove_equivalent,
    "smt": smt_equivalence.prove_equivalent_smt,
    "algebraic": prove_equivalent_algebraic,
    "statements": prove_statements,
    "statements_smt": prove_statements_smt,
}


@pytest.mark.parametrize("prover", sorted(PROVERS))
@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_no_prover_proves_anything_about_a_match_recognize_query(prover, pair):
    result = PROVERS[prover](*PAIRS[pair])
    assert not result.proven and not getattr(result, "conditionally_proven", False), (prover, pair, result.status)


def test_the_parse_check_names_the_clause_as_the_reason():
    reason = parse_check.guarded(mr(), mr())
    assert reason and "MATCH_RECOGNIZE" in reason
    assert parse_check.guarded("SELECT 1", "SELECT 1") is None


def test_containment_and_bounded_equivalence_and_reuse_decline():
    contained = check_containment(mr(), mr(), schema={"src": ["ts", "v", "p"]}, dialect="bigquery")
    assert contained.status == "unsupported" and "MATCH_RECOGNIZE" in contained.reason
    bounded = check_bounded(mr(), mr(), schema_from_prover({"src": ["ts", "v", "p"]}, None, {"src": {"ts": "INT64", "v": "INT64", "p": "STRING"}}))
    assert not bounded.bounded_equivalent and "MATCH_RECOGNIZE" in bounded.reason
    clause = "SELECT * FROM src MATCH_RECOGNIZE (ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1)"
    for query, model in ((clause, "SELECT ts, v FROM src"), ("SELECT ts, v FROM src", clause)):
        reuse = rewrite_over_model(query, model, schema={"src": ["ts", "v"]}, dialect="bigquery")
        assert reuse.status == "unsupported" and "MATCH_RECOGNIZE" in reuse.reason


@pytest.mark.skipif(OLD_SQLGLOT, reason="the DuckDB translation guard reads expression classes sqlglot 26 does not have")
def test_duckdb_has_no_such_clause_so_no_local_check_runs_it():
    assert refusal(parse(mr())) == "MatchRecognize"


def test_output_properties_do_not_report_the_columns_of_the_table_it_reads():
    properties = infer_properties(mr(), schema={"src": ["ts", "v", "p"]})
    assert properties.unsupported and not properties.columns and not properties.keys


# ---------------------------------------------------------------------------------------------------- rewrites


@pytest.mark.parametrize("rule", sorted(r for r in available_rules()))
@pytest.mark.parametrize(
    "sql",
    [
        mr().replace("v > 1", "(v > 1) AND 1 = 1") + " WHERE 1 = 1",
        f"WITH r AS ({mr()}), unused AS (SELECT 1 AS a) SELECT p FROM r",
        f"SELECT p, fv FROM ({mr()}) AS q WHERE fv > 1 AND TRUE",
        f"SELECT DISTINCT p, fv FROM ({mr()}) GROUP BY p, fv",
        f"SELECT * FROM ({mr()}) AS a JOIN d.o AS o ON o.k = a.fv",
    ],
)
def test_no_rule_changes_a_statement_with_the_clause(rule, sql):
    result = apply_rule(rule, sql)
    assert result.sql == sql


def test_the_engine_says_why_it_left_the_statement_alone():
    result = apply_rule("remove_redundant_parentheses", mr().replace("v > 1", "(v > 1)"))
    assert "match_recognize_kept" in [d.code for d in result.diagnostics]


def test_other_statements_of_a_script_are_still_rewritten():
    sql = f"SELECT (1) + 2 AS a;\n{mr()};\nSELECT (3) + 4 AS b"
    result = apply_rule("remove_redundant_parentheses", sql)
    assert mr() in result.sql and "(1)" not in result.sql and "(3)" not in result.sql


def test_the_tidying_helpers_leave_the_query_as_written():
    sql = f"SELECT m.p FROM {SRC} AS t MATCH_RECOGNIZE (PARTITION BY p ORDER BY ts MEASURES COUNT(*) AS n PATTERN (X+) DEFINE X AS v > 1) AS m"
    assert tidy(sql, {"src": ["ts", "v", "p"]}) == sql
    assert simpler_forms(sql, {"src": ["ts", "v", "p"]}) == []
