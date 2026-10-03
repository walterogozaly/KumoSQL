"""A WITH table hides a model of the same name only where that WITH is in scope (audit 1002, F11).

Pipeline dependencies, graph edges, impact, schema changes, filter pushdown, refactor reads and the
script reader used to collect CTE names from the whole statement and skip every table reference
with one of those names. A nested ``WITH t AS (...)`` then hid a read of the real model ``t``
elsewhere in the query: the reader lost its edge to ``t`` and ``t``'s columns were called dead.
"""

import pytest
import sqlglot
from sqlglot import exp

from kumosql import load_compiled_graph
from kumosql.ast_utils import binding_cte
from kumosql.consolidate import ConsolidationError, consolidate_tables
from kumosql.filter_pushdown import find_upstream_filter_proposals
from kumosql.graph import build_query_graph
from kumosql.impact import assess_change
from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target
from kumosql.refactor import _Reads
from kumosql.schema_change import assess_schema_change
from kumosql.scripts import analyse_script, token_reads

# Each reads the real model t (its column ``secret``) next to a WITH table that is also called t.
HIDDEN_READS = {
    "nested_in_from": "SELECT x.secret FROM t AS x CROSS JOIN (WITH t AS (SELECT 1 AS id) SELECT id FROM t) AS s",
    "self_reference": "WITH t AS (SELECT secret FROM t) SELECT secret FROM t",
    "nested_in_where": "SELECT x.secret FROM t AS x WHERE x.id IN (WITH t AS (SELECT 1 AS id) SELECT id FROM t)",
}


def pipeline_with(reader_sql: str):
    return load_compiled_graph({"tables": [
        {"target": {"name": "t"}, "query": "SELECT 1 AS id, 2 AS secret"},
        {"target": {"name": "read_id"}, "query": "SELECT id FROM t"},
        {"target": {"name": "read_secret"}, "query": reader_sql},
    ]})


def tables(sql: str) -> list[exp.Table]:
    return list(sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table))


def cte_flags(sql: str) -> list[tuple[str, str, bool]]:
    """``(name, where, is a WITH table)`` per table reference; ``where`` is the nearest WITH body or subquery around it."""

    def where(table: exp.Table) -> str:
        holder = table.find_ancestor(exp.CTE, exp.Subquery)
        return f"with {holder.alias_or_name}" if isinstance(holder, exp.CTE) else "subquery" if holder else "query"

    return sorted((table.name, where(table), binding_cte(table) is not None) for table in tables(sql))


# ------------------------------------------------------------------ the scope rule


def test_a_nested_with_binds_only_inside_its_own_query():
    assert cte_flags(HIDDEN_READS["nested_in_from"]) == [("t", "query", False), ("t", "subquery", True)]
    assert cte_flags(HIDDEN_READS["nested_in_where"]) == [("t", "query", False), ("t", "subquery", True)]


def test_a_with_table_body_reads_the_real_table_of_its_own_name():
    # The body of a non-recursive WITH table sees only the ones listed before it.
    assert cte_flags(HIDDEN_READS["self_reference"]) == [("t", "query", True), ("t", "with t", False)]


def test_forward_references_bind_only_under_recursive():
    plain = "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS k) SELECT * FROM a JOIN b ON TRUE"
    assert cte_flags(plain) == [("a", "query", True), ("b", "query", True), ("b", "with a", False)]
    recursive = "WITH RECURSIVE a AS (SELECT * FROM b), b AS (SELECT 1 AS k) SELECT * FROM a"
    assert cte_flags(recursive) == [("a", "query", True), ("b", "with a", True)]
    counting = "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t"
    assert all(flag for _, _, flag in cte_flags(counting))


def test_an_inner_with_body_sees_the_outer_with_table_of_the_same_name():
    sql = "WITH t AS (SELECT 1 AS id) SELECT * FROM (WITH t AS (SELECT * FROM t) SELECT * FROM t)"
    inner_body = next(t for t in tables(sql) if t.find_ancestor(exp.CTE) is not None and t.find_ancestor(exp.Subquery))
    assert binding_cte(inner_body).this.sql() == "SELECT 1 AS id"


def test_scope_follows_set_operations_and_parentheses():
    assert all(flag for _, _, flag in cte_flags("WITH a AS (SELECT 1 AS x) SELECT * FROM a UNION ALL SELECT * FROM a"))
    parenthesized = "(WITH a AS (SELECT 1 AS x) SELECT * FROM a) UNION ALL SELECT * FROM a"
    assert cte_flags(parenthesized) == [("a", "query", False), ("a", "subquery", True)]
    assert cte_flags("WITH t AS (SELECT 1 AS x) SELECT * FROM ds.t") == [("t", "query", False)]


# ------------------------------------------------------------------ pipeline analysis


@pytest.mark.parametrize("shape", sorted(HIDDEN_READS))
def test_a_hidden_read_keeps_its_dependency_and_its_columns(shape):
    pipeline = pipeline_with(HIDDEN_READS[shape])
    assert pipeline.upstream["read_secret"] == {"t"}
    assert "read_secret" in pipeline.downstream["t"]
    assert ColumnRef("t", "secret") in pipeline._analyse().consumed["read_secret"]
    assert "secret" not in pipeline.dead_columns().get("t", ())
    edges = {(e.upstream.stable_key, e.downstream.stable_key) for e in build_query_graph(pipeline).edges}
    assert ("table:t", "table:read_secret") in edges


@pytest.mark.parametrize("shape", sorted(HIDDEN_READS))
def test_dropping_the_hidden_column_breaks_the_reader(shape):
    pipeline = pipeline_with(HIDDEN_READS[shape])
    assert "read_secret" in {m.model for m in assess_change(pipeline, "drop_column", "t", "secret").affected}
    change = assess_schema_change(pipeline, "drop_column", "t", "secret")
    assert "read_secret" in {e.model for e in change.breaks}


@pytest.mark.parametrize(
    "sql",
    [
        "WITH t AS (SELECT 1 AS id, 3 AS secret) SELECT secret FROM t",
        "WITH RECURSIVE t AS (SELECT 1 AS secret UNION ALL SELECT secret + 1 FROM t WHERE secret < 3) SELECT secret FROM t",
    ],
)
def test_a_with_table_in_scope_still_hides_the_model(sql):
    pipeline = pipeline_with(sql)
    assert pipeline.upstream["read_secret"] == set()
    assert pipeline.dead_columns() == {"t": ("secret",)}
    edges = {(e.upstream.stable_key, e.downstream.stable_key) for e in build_query_graph(pipeline).edges}
    assert ("table:t", "table:read_secret") not in edges


def test_no_filter_is_pushed_past_a_hidden_unfiltered_reader():
    pipeline = load_compiled_graph({"tables": [
        {"target": {"name": "t"}, "query": "SELECT id, secret FROM src"},
        {"target": {"name": "r1"}, "query": "SELECT id FROM t WHERE id > 5"},
        {"target": {"name": "r2"}, "query": "SELECT y.id FROM t AS y CROSS JOIN (WITH t AS (SELECT 1 AS k) SELECT k FROM t) AS s"},
    ]})
    result = find_upstream_filter_proposals(pipeline)
    assert not result.proposals
    assert [r.reason for r in result.refusals if r.model == "t"] == ["no_common_filter"]


def test_a_table_with_a_hidden_reader_is_not_folded_away():
    pipeline = load_compiled_graph({"tables": [
        {"target": {"name": "t"}, "query": "SELECT id, secret FROM src"},
        {"target": {"name": "r1"}, "query": "SELECT id FROM t WHERE id > 5"},
        {"target": {"name": "r2"}, "query": "SELECT y.id FROM t AS y CROSS JOIN (WITH t AS (SELECT 1 AS k) SELECT k FROM t) AS s"},
    ]})
    assert _Reads(pipeline)(pipeline.models["r2"].sql) == {"t"}
    with pytest.raises(ConsolidationError) as error:
        consolidate_tables(pipeline, ["t"], "r1")
    assert error.value.readers == {"t": ["r2"]}


# ------------------------------------------------------------------ scripts


def test_a_default_dataset_qualifies_a_hidden_read():
    analysis = analyse_script(
        "SET @@dataset_id = 'ds';\n"
        "SELECT x.secret FROM t AS x CROSS JOIN (WITH t AS (SELECT 1 AS id) SELECT id FROM t) AS s"
    )
    assert sorted(table.sql() for table in analysis.all_reads()) == ["ds.t"]


def test_a_temporary_table_read_next_to_a_with_table_of_its_name_is_not_a_source_table():
    script = (
        "CREATE TEMP TABLE tmp AS SELECT id FROM a;\n"
        "CREATE OR REPLACE TEMP TABLE tmp AS SELECT secret FROM b;\n"
        "SELECT tmp.secret FROM tmp CROSS JOIN (WITH tmp AS (SELECT 1 AS k) SELECT k FROM tmp) AS s"
    )
    models = {
        "a": Model(Target(name="a"), "table", "SELECT 1 AS id"),
        "b": Model(Target(name="b"), "table", "SELECT 1 AS id, 2 AS secret"),
        "m": Model(Target(name="m"), "table", script),
    }
    pipeline = Pipeline(models, {}, {})
    assert pipeline.upstream["m"] == {"a", "b"}
    record = pipeline._analyse().records[ColumnRef("m", "secret")]
    # It reads the second version of the temporary table (from b), never a table called tmp.
    assert all(source.table != "tmp" for source in record.sources)
    assert record.sources <= {ColumnRef("b", "secret")}


@pytest.mark.parametrize(
    "sql, reads",
    [
        (HIDDEN_READS["nested_in_from"], ["t"]),
        (HIDDEN_READS["self_reference"], ["t"]),
        (HIDDEN_READS["nested_in_where"], ["t"]),
        ("WITH t AS (SELECT 1 AS id) SELECT * FROM t", []),
        ("WITH a AS (SELECT * FROM b), b AS (SELECT * FROM c) SELECT * FROM a JOIN b USING (k)", ["b", "c"]),
        ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT * FROM r", []),
        ("WITH RECURSIVE a AS (SELECT * FROM b), b (k) AS (SELECT 1) SELECT * FROM a", []),
        ("WITH m AS MATERIALIZED (SELECT * FROM src) SELECT * FROM m", ["src"]),
        ("(WITH a AS (SELECT 1) SELECT * FROM a) UNION ALL SELECT * FROM a", ["a"]),
    ],
)
def test_token_reads_scope_cte_names(sql, reads):
    # The reader for statements that do not parse applies the same rule from the token stream.
    assert [table.sql() for table in token_reads(sql)[0]] == reads
