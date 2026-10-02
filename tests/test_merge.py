"""MERGE: edges, column lineage from the USING source into the target, ON and condition reads, and unknown (never a guess).

On the release before this, a model whose SQL was a MERGE had no dependencies at all (no edges, "no parseable query",
unknown reads), so the graph, lineage and impact views lost every table it read and wrote.
"""

from __future__ import annotations

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target

COLUMNS = ("id", "v", "w", "x", "k", "a", "del", "ts")
TABLES = ("src", "src2", "src3", "target", "other")
SCHEMA = {f"p.d.{name}": {c: "INT64" for c in COLUMNS} for name in TABLES}
SOURCES = {f"p.d.{name}": Target("p", "d", name) for name in ("src", "src2", "src3")}
TARGET = "`p.d.target`"


def build(sql: str, kind: str = "table", name: str = "target", sources: dict | None = None) -> Pipeline:
    models = {f"p.d.{name}": Model(Target("p", "d", name), kind, sql)}
    return Pipeline(models, sources if sources is not None else SOURCES, SCHEMA)


def lineage(pl: Pipeline, model: str = "p.d.target") -> dict[str, set[str]]:
    return {ref.column: {str(s) for s in sources} for ref, sources in pl.column_lineage().items() if ref.table == model}


def codes(pl: Pipeline) -> set[str]:
    return {d.code for d in pl.all_diagnostics()}


def merge(*whens: str, using: str = "`p.d.src` AS s", on: str = "t.id = s.id") -> str:
    return f"MERGE {TARGET} AS t USING {using} ON {on} " + " ".join(whens)


@pytest.mark.parametrize("kind", ["table", "view", "incremental", "operations", "query"])
def test_a_merge_model_has_its_source_as_a_dependency_and_no_gap(kind):
    pl = build(merge("WHEN MATCHED THEN UPDATE SET t.v = s.v"), kind)
    assert pl.upstream["p.d.target"] == {"p.d.src"}
    assert not {"no_query", "unknown_reads", "parse_error"} & codes(pl)
    assert pl.completeness()["views"]["graph"]


def test_update_set_traces_the_assigned_column_and_reads_the_on_columns():
    pl = build(merge("WHEN MATCHED THEN UPDATE SET t.v = s.v + s.w"))
    assert lineage(pl) == {"v": {"p.d.src.v", "p.d.src.w"}}
    used = {str(c) for c in pl.consumed_columns()["p.d.target"]}
    assert {"p.d.src.id", "p.d.target.id"} <= used  # the ON condition


def test_insert_with_columns_pairs_names_with_values_by_position():
    pl = build(merge("WHEN NOT MATCHED THEN INSERT (v, id) VALUES (s.w, s.k)"))
    assert lineage(pl) == {"v": {"p.d.src.w"}, "id": {"p.d.src.k"}}


def test_insert_row_takes_every_source_column_by_name():
    pl = build(merge("WHEN NOT MATCHED THEN INSERT ROW"))
    got = lineage(pl)
    assert got["v"] == {"p.d.src.v"} and got["id"] == {"p.d.src.id"} and set(got) == set(COLUMNS)


def test_unqualified_insert_values_read_the_source_even_when_the_target_has_the_same_names():
    pl = build(merge("WHEN NOT MATCHED THEN INSERT (id, v) VALUES (id, v)"))
    assert lineage(pl) == {"id": {"p.d.src.id"}, "v": {"p.d.src.v"}}


def test_subquery_using_traces_through_the_subquery():
    pl = build(merge("WHEN MATCHED THEN UPDATE SET t.v = s.total", using="(SELECT id, SUM(v) AS total FROM `p.d.src2` GROUP BY id) AS s"))
    assert lineage(pl) == {"v": {"p.d.src2.v"}}
    assert pl.upstream["p.d.target"] == {"p.d.src2"}


def test_all_clauses_union_their_sources_and_delete_conditions_are_read():
    pl = build(
        merge(
            "WHEN MATCHED AND s.del = 1 THEN DELETE",
            "WHEN MATCHED THEN UPDATE SET v = s.v, w = s.w + 1",
            "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.k)",
            "WHEN NOT MATCHED BY SOURCE AND t.x > 5 THEN DELETE",
        )
    )
    assert lineage(pl) == {"v": {"p.d.src.v", "p.d.src.k"}, "w": {"p.d.src.w"}, "id": {"p.d.src.id"}}
    used = {str(c) for c in pl.consumed_columns()["p.d.target"]}
    assert {"p.d.src.del", "p.d.target.x", "p.d.src.id", "p.d.target.id"} <= used


def test_by_source_update_with_a_constant_is_constant_not_unknown():
    pl = build(merge("WHEN NOT MATCHED BY SOURCE AND t.x > 1 THEN UPDATE SET t.v = 0"))
    record = next(r for r in pl.lineage_report() if r["column"] == "v")
    assert record["status"] == "constant"
    assert "p.d.target.x" in {str(c) for c in pl.consumed_columns()["p.d.target"]}


def test_subqueries_in_on_and_conditions_count_as_reads():
    pl = build(
        merge(
            "WHEN MATCHED AND EXISTS (SELECT 1 FROM `p.d.src3` WHERE a = s.id) THEN UPDATE SET t.v = s.v",
            on="t.id = s.id AND t.k IN (SELECT k FROM `p.d.src2`)",
        )
    )
    used = {str(c) for c in pl.consumed_columns()["p.d.target"]}
    assert {"p.d.src2.k", "p.d.src3.a", "p.d.target.k"} <= used
    assert pl.upstream["p.d.target"] == {"p.d.src", "p.d.src2", "p.d.src3"}


def test_a_struct_field_assignment_writes_its_root_column():
    pl = build(merge("WHEN MATCHED THEN UPDATE SET t.a.b = s.v"))
    assert lineage(pl) == {"a": {"p.d.src.v"}}


def test_into_and_unaliased_forms_are_read():
    for sql in (
        "MERGE INTO `p.d.target` t USING `p.d.src` s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.v = s.v",
        "MERGE `p.d.target` USING `p.d.src` ON `p.d.target`.id = `p.d.src`.id WHEN MATCHED THEN UPDATE SET v = `p.d.src`.v",
    ):
        pl = build(sql)
        assert pl.upstream["p.d.target"] == {"p.d.src"}, sql
        assert not {"no_query", "unknown_reads"} & codes(pl), sql


def test_delete_only_keeps_the_edges_and_says_columns_are_missing():
    pl = build(merge("WHEN MATCHED THEN DELETE"))
    assert pl.upstream["p.d.target"] == {"p.d.src"}
    assert lineage(pl) == {}
    assert "skipped_statements" in codes(pl) and "unknown_reads" not in codes(pl)
    assert not pl.completeness()["views"]["lineage"]


def test_insert_values_without_column_names_is_not_guessed():
    pl = build(merge("WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.v)"))
    assert pl.upstream["p.d.target"] == {"p.d.src"}
    assert lineage(pl) == {}
    assert "skipped_statements" in codes(pl)


def test_an_unparseable_merge_is_reported_not_raised():
    pl = build(f"MERGE {TARGET} t USING WHEN")
    assert codes(pl) & {"parse_error", "no_query"}
    assert not pl.completeness()["complete"]


def test_a_merge_using_a_temporary_table_traces_to_the_real_sources():
    sql = (
        "CREATE TEMP TABLE staged AS SELECT id, x * 2 AS v FROM `p.d.src2`;\n"
        f"MERGE {TARGET} t USING staged s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.v = s.v WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v);"
    )
    pl = build(sql)
    assert pl.upstream["p.d.target"] == {"p.d.src2"}
    assert lineage(pl) == {"v": {"p.d.src2.x"}, "id": {"p.d.src2.id"}}


def test_the_last_merge_is_traced_and_the_earlier_one_is_reported():
    sql = (
        f"MERGE {TARGET} t USING `p.d.src` s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.v = s.v;\n"
        f"MERGE {TARGET} t USING `p.d.src2` s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.w = s.w;"
    )
    pl = build(sql)
    assert pl.upstream["p.d.target"] == {"p.d.src", "p.d.src2"}
    assert lineage(pl) == {"w": {"p.d.src2.w"}}
    assert "skipped_statements" in codes(pl)


def test_merge_in_a_branch_is_a_possible_edge():
    sql = f"DECLARE n INT64 DEFAULT 1; IF n > 0 THEN MERGE {TARGET} t USING `p.d.src` s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.v = s.v; END IF;"
    pl = build(sql)
    assert pl.upstream["p.d.target"] == {"p.d.src"}


def test_a_merge_does_not_narrow_the_target_to_the_merged_columns():
    """Readers of the merged table keep the columns the MERGE does not write, and a star is not cut down to the merged ones."""

    models = {
        "p.d.target": Model(Target("p", "d", "target"), "operations", merge("WHEN MATCHED THEN UPDATE SET t.v = s.v")),
        "p.d.reader": Model(Target("p", "d", "reader"), "table", "SELECT id, w, v FROM `p.d.target`"),
        "p.d.starred": Model(Target("p", "d", "starred"), "table", "SELECT * FROM `p.d.target`"),
    }
    pl = Pipeline(models, SOURCES, SCHEMA)
    assert pl.upstream["p.d.reader"] == {"p.d.target"} and pl.upstream["p.d.starred"] == {"p.d.target"}
    assert lineage(pl, "p.d.reader") == {c: {f"p.d.target.{c}"} for c in ("id", "w", "v")}
    assert lineage(pl, "p.d.starred")["w"] == {"p.d.target.w"}
    assert set(lineage(pl, "p.d.starred")) == set(COLUMNS)


def test_an_operation_merging_into_its_own_table_traces_the_columns():
    pl = build(merge("WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)"), "operations")
    assert lineage(pl) == {"id": {"p.d.src.id"}, "v": {"p.d.src.v"}}


def test_rules_leave_a_merge_untouched():
    import kumosql

    sql = merge("WHEN MATCHED AND 1 = 1 THEN UPDATE SET t.v = (s.v)")
    result = kumosql.apply_rules(list(kumosql.available_rules()), sql)
    assert "MERGE" in result.sql.upper() and "UPDATE SET" in result.sql.upper()


WALTER_MERGE = """MERGE `p.d.tgt` T USING (SELECT k, v FROM `p.d.src`) S ON T.k = S.k
WHEN MATCHED THEN UPDATE SET v = S.v
WHEN NOT MATCHED THEN INSERT (k, v) VALUES (S.k, S.v)"""


@pytest.mark.parametrize("kind", ["table", "incremental", "operations"])
def test_the_trivial_merge_from_a_user_report_is_one_statement_with_its_source_upstream(kind):
    """It used to be reported as a script of two queries, analysed by its last one, found to have no query, and given up on;
    `p.d.src` was not a read, so the table the MERGE builds had no upstream."""

    from kumosql.scripts import analyse_script, split_script

    assert len(split_script(WALTER_MERGE)) == 1
    assert sum(analyse_script(WALTER_MERGE).counts().values()) == 1
    pl = Pipeline({"p.d.tgt": Model(Target("p", "d", "tgt"), kind, WALTER_MERGE)}, {"p.d.src": Target("p", "d", "src")}, {})
    assert pl.upstream["p.d.tgt"] == {"p.d.src"}
    assert not codes(pl) and pl.completeness()["complete"]
    assert lineage(pl, "p.d.tgt") == {"k": {"p.d.src.k"}, "v": {"p.d.src.v"}}
