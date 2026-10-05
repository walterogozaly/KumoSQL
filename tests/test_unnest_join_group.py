"""``FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)``: a parenthesized join that starts with UNNEST is read, not refused.

BigQuery accepts it (each valid form below was checked with a dry run, and so was each refused one); sqlglot stops at the ``JOIN``
("Expecting )") because its UNNEST parser returns before joins are looked for, so the model was unreadable and lost every read.
``kumosql.unnest_join_group`` reads the group as sqlglot reads ``(t1 JOIN t2 ...)``: a subquery around a table that carries the
joins, here with the ``Unnest`` as that table's ``this``. Anything BigQuery rejects stays a parse error.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

import kumosql
from kumosql.equivalence import prove_equivalent
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target
from kumosql.smt_equivalence import prove_equivalent_smt

# (sql, printed): every form BigQuery accepts; the printed text is what reads back, which is the SQL as written.
VALID = [
    "SELECT x, t.a FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)",
    "SELECT x, t.a FROM (UNNEST([1, 2]) AS x CROSS JOIN t)",
    "SELECT x, t.a FROM (UNNEST([1, 2]) AS x LEFT JOIN t ON x = t.a)",
    "SELECT x, y FROM (UNNEST([1]) AS x CROSS JOIN UNNEST([2]) AS y)",
    "SELECT x, y FROM (UNNEST([1]) AS x JOIN UNNEST([1]) AS y ON x = y)",
    "SELECT * FROM (UNNEST([1, 2]) JOIN t ON TRUE)",
    "SELECT x, o, t.a FROM (UNNEST([1, 2]) AS x WITH OFFSET AS o JOIN t ON x = t.a)",
    "SELECT x, offset FROM (UNNEST([1, 2]) AS x WITH OFFSET AS offset CROSS JOIN t)",
    "SELECT x, t.a FROM (UNNEST([1, 2]) AS x JOIN t USING (a))",
    "SELECT x, t.a, u.b FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a) LEFT JOIN u ON u.b = x",
    "SELECT x, t.a, u.b FROM ((UNNEST([1, 2]) AS x JOIN t ON x = t.a) JOIN u ON u.b = x)",
    "SELECT x, y, t.a FROM (UNNEST([1]) AS x JOIN (UNNEST([2]) AS y JOIN t ON y = t.a) ON x = y)",
    "SELECT s.a, x FROM s LEFT JOIN (UNNEST([1]) AS x JOIN t ON x = t.a) ON s.a = x",
    "SELECT x FROM s CROSS JOIN (UNNEST([1]) AS x JOIN t ON x = t.a)",
    "WITH g AS (SELECT x, t.a FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)) SELECT * FROM g",
    "SELECT * FROM (SELECT x FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)) AS q",
    "SELECT EXISTS(SELECT 1 FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a)) AS e",
    "SELECT x FROM (UNNEST(ARRAY(SELECT a FROM (UNNEST([1]) AS y JOIN t ON y = t.a))) AS x JOIN u ON x = u.b)",
    "SELECT x FROM (UNNEST([STRUCT(1 AS a)]) AS x JOIN t ON x.a = t.a) WHERE t.a > 0 GROUP BY x HAVING COUNT(*) > 0",
]

# BigQuery rejects each of these (dry run), so each stays the parse error sqlglot raised.
REFUSED = [
    "SELECT * FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a) AS j",  # an alias after the group
    "SELECT * FROM (UNNEST([1, 2]) AS x JOIN t ON x = t.a) j",
    "SELECT * FROM (UNNEST([1, 2]) AS x, t)",  # a comma inside the group
    "SELECT (UNNEST([1, 2]) AS x JOIN t ON x = t.a)",  # not a table position
    "SELECT * FROM t WHERE a IN (UNNEST([1, 2]) AS x JOIN t ON x = t.a)",
]


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


def group_of(tree: exp.Expression) -> exp.Table:
    """The table that holds the first UNNEST and the joins of the first parenthesized group."""

    tables = [t for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Unnest)]
    assert len(tables) >= 1
    return tables[0]


@pytest.mark.parametrize("sql", VALID)
def test_the_group_parses_and_prints_back_as_written(sql):
    assert parse(sql).sql("bigquery") == sql


@pytest.mark.parametrize("sql", VALID)
def test_no_marker_is_left_in_the_tree(sql):
    tree = parse(sql)
    assert not [call for call in tree.find_all(exp.Anonymous) if call.name.startswith("__KUMO")]


def test_the_group_is_a_subquery_around_a_table_that_holds_the_unnest_and_the_joins():
    tree = parse("SELECT x, o, t.a FROM (UNNEST([1, 2]) AS x WITH OFFSET AS o JOIN t ON x = t.a)")
    group = tree.args["from_" if "from_" in tree.args else "from"].this
    assert isinstance(group, exp.Subquery) and not group.alias
    base = group.this
    assert isinstance(base, exp.Table) and isinstance(base.this, exp.Unnest) and not base.alias_or_name
    assert [c.name for c in base.this.args["alias"].args["columns"]] == ["x"]
    assert base.this.args["offset"].name == "o"
    (join,) = base.args["joins"]
    assert isinstance(join.this, exp.Table) and join.this.name == "t"
    assert join.args["on"].sql() == "x = t.a"


def test_the_unnest_arguments_are_kept_whole():
    tree = parse("SELECT x FROM (UNNEST(['it\\'s', \"a,b\", ')']) AS x JOIN t ON x = t.a)")
    assert [e.sql("bigquery") for e in group_of(tree).this.expressions] == ["['it\\'s', 'a,b', ')']"]


def test_joined_tables_are_tables_of_the_tree():
    tree = parse("SELECT x FROM ((UNNEST([1]) AS x JOIN t ON x = t.a) LEFT JOIN u ON u.b = x) JOIN v ON v.c = x")
    names = sorted(t.name for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Identifier))
    assert names == ["t", "u", "v"]


def test_a_second_group_in_the_same_query_is_read_too():
    sql = "SELECT * FROM (UNNEST([1]) AS x JOIN t ON x = t.a) CROSS JOIN (UNNEST([2]) AS y JOIN u ON y = u.b)"
    tree = parse(sql)
    assert len([t for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Unnest)]) == 2
    assert tree.sql("bigquery") == sql


def test_a_group_that_does_not_start_with_unnest_reads_as_before():
    tree = parse("SELECT x FROM (t JOIN UNNEST(t.arr) AS x ON TRUE)")
    base = tree.find(exp.Subquery).this
    assert isinstance(base, exp.Table) and base.name == "t" and isinstance(base.args["joins"][0].this, exp.Unnest)


@pytest.mark.parametrize("sql", REFUSED)
def test_a_group_bigquery_rejects_is_refused_not_guessed_at(sql):
    with pytest.raises(ParseError):
        parse(sql)


def test_the_first_item_must_be_an_unnest_call():
    # The marker is internal: SQL that spells it is not a group and is never turned into an UNNEST.
    # (it only matters when a real group makes the reader take the rewrite path)
    group = "SELECT * FROM (UNNEST([1]) AS x JOIN t ON x = t.a) CROSS JOIN "
    with pytest.raises(ParseError):
        parse(group + "(__KUMO_UNNEST_FIRST__('SELECT 1') JOIN u ON TRUE)")
    with pytest.raises(ParseError):
        parse(group + "(__KUMO_UNNEST_FIRST__('UNNEST([1]) AS y JOIN v ON TRUE') JOIN u ON TRUE)")


def test_a_statement_is_still_one_script_item_when_the_group_is_in_a_multi_statement_text():
    trees = sqlglot.parse("SELECT 1; SELECT x FROM (UNNEST([1]) AS x JOIN t ON x = t.a)", read="bigquery")
    assert [tree.sql("bigquery") for tree in trees] == ["SELECT 1", "SELECT x FROM (UNNEST([1]) AS x JOIN t ON x = t.a)"]


# ---------------------------------------------------------------- what the rest of KumoSQL does with it

SOURCES = {f"p.d.{n}": Target("p", "d", n) for n in ("src", "src2")}
SCHEMAS = {"p.d.src": {"id": "INT64", "w": "STRING"}, "p.d.src2": {"id": "INT64", "v": "STRING"}}


def pipeline(sql: str) -> Pipeline:
    return Pipeline({"p.d.target": Model(Target("p", "d", "target"), "table", sql)}, SOURCES, SCHEMAS)


GROUPS = [
    "SELECT x, s.v FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id)",
    "SELECT x, s.v FROM (UNNEST([1, 2]) AS x CROSS JOIN `p.d.src2` AS s)",
    "SELECT x, o, s.v FROM (UNNEST([1, 2]) AS x WITH OFFSET AS o JOIN `p.d.src2` AS s ON x = s.id)",
    "SELECT x, s.v, r.w FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id) LEFT JOIN `p.d.src` AS r ON r.id = x",
    "SELECT x, s.v, r.w FROM ((UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id) JOIN `p.d.src` AS r ON r.id = x)",
    "WITH g AS (SELECT x, s.v FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id)) SELECT * FROM g",
]


@pytest.mark.parametrize("sql", GROUPS)
def test_every_table_in_the_group_is_a_read_and_nothing_fails(sql):
    pl = pipeline(sql)
    codes = {d.code for d in pl.all_diagnostics()}
    assert not {"parse_error", "no_query", "unknown_reads", "table_function", "qualify_error"} & codes
    reads = pl.upstream["p.d.target"]
    assert "p.d.src2" in reads
    assert ("p.d.src" in reads) == ("`p.d.src`" in sql)


def test_columns_trace_through_the_group_and_the_unnest_values_are_not_attributed_to_a_table():
    pl = pipeline("SELECT x, s.v, r.w FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id) LEFT JOIN `p.d.src` AS r ON r.id = x")
    status = {row["column"]: row["status"] for row in pl.lineage_report()}
    assert status["v"] == "traced" and status["w"] == "traced" and status["x"] in ("unknown", "constant")
    sources = {ref.column: sorted(str(s) for s in refs) for ref, refs in pl.column_lineage().items()}
    assert sources["v"] == ["p.d.src2.v"] and sources["w"] == ["p.d.src.w"]
    assert sources["x"] == []


def test_a_bare_column_is_still_traced_to_the_table_that_has_it():
    pl = pipeline("SELECT v FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id)")
    assert {ref.column: sorted(str(s) for s in refs) for ref, refs in pl.column_lineage().items()} == {"v": ["p.d.src2.v"]}


def test_star_over_a_group_is_unknown_not_a_guess():
    pl = pipeline("SELECT * FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id)")
    assert {row["status"] for row in pl.lineage_report()} == {"unknown"}


@pytest.mark.parametrize("sql", GROUPS)
def test_formatting_keeps_the_group_and_cleanup_never_changes_it_unproven(sql):
    assert "(UNNEST([1, 2]) AS x" in kumosql.format_sql(sql)
    result = kumosql.apply_rules(list(kumosql.available_rules()), sql)
    # Left alone, or rewritten into a query a prover accepts; nothing here proves a group equal to the flat query.
    assert parse(result.sql).sql("bigquery") == parse(sql).sql("bigquery") or prove_equivalent(sql, result.sql).proven


def test_the_group_is_not_flattened_by_a_rule_unless_the_flat_query_is_proven_equal():
    sql = "SELECT x, s.v FROM (UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id)"
    flat = "SELECT x, s.v FROM UNNEST([1, 2]) AS x JOIN `p.d.src2` AS s ON x = s.id"
    result = kumosql.apply_rules(list(kumosql.available_rules()), sql)
    if " ".join(result.sql.split()) == " ".join(flat.split()):
        assert prove_equivalent(sql, flat).proven or prove_equivalent_smt(sql, flat).status.name == "PROVEN_EQUIVALENT"


BASE = "SELECT * FROM (UNNEST([1, 2]) AS x JOIN (SELECT 1 AS id) AS s ON x = s.id)"
DIFFERENT = [
    BASE.replace("[1, 2]", "[1, 3]"),
    BASE.replace("x = s.id", "x <> s.id"),
    BASE.replace(" JOIN ", " LEFT JOIN "),
    BASE.replace("SELECT 1 AS id", "SELECT 2 AS id"),
    BASE.replace("AS x", "AS x WITH OFFSET AS o"),
    BASE.replace("AS x", "AS y").replace("x =", "y ="),
    "SELECT * FROM ((SELECT 1 AS id) AS s JOIN UNNEST([1, 2]) AS x ON x = s.id)",  # the columns come in another order
    "SELECT * FROM UNNEST([1, 2]) AS x JOIN (SELECT 1 AS id) AS s ON x = s.id",
]


@pytest.mark.parametrize("other", DIFFERENT)
def test_no_prover_calls_a_different_query_equal(other):
    assert not prove_equivalent(BASE, other).proven
    assert prove_equivalent_smt(BASE, other).status.name == "NOT_PROVEN"


def test_the_smt_prover_says_unmodeled_for_the_group():
    result = prove_equivalent_smt(BASE, BASE)
    assert result.status.name == "NOT_PROVEN" and "joins" in result.reason
