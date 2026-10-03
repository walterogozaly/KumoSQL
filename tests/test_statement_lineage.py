"""Explicit table inputs/outputs do not imply complete output-column lineage."""

import json
import pickle
from pathlib import Path

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import ColumnRef, Model, Target


CASES = json.loads((Path(__file__).parent / "fixtures/statement_lineage/cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["sql"][:60] for case in CASES])
@pytest.mark.parametrize("kind", ["table", "operations"])
def test_statement_table_edges(case, kind):
    target = Target("p", "d", "target")
    tables = {name: Target(*name.split(".")) for name in set(case["reads"] + case["writes"]) - {target.key}}
    pl = Pipeline({target.key: Model(target, kind, case["sql"])}, sources=tables)
    assert pl.table_reads().get(target.key, ()) == frozenset(case["reads"])
    assert pl.table_writes()[target.key] == frozenset(case["writes"])
    assert target.key not in pl.upstream[target.key]  # explicit self-reads are not graph cycles


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("decorator", ["$__UNPARTITIONED__", "$20261003", "@123456"])
def test_decorated_table_uses_base_schema(declared, decorator):
    src, dst = Target("p", "d", "source"), Target("p", "d", "result")
    pl = Pipeline(
        {dst.key: Model(dst, "table", f"SELECT * FROM `p.d.source{decorator}`")},
        sources={src.key: src} if declared else {},
        source_schema={src.key: {"id": "INT64", "value": "STRING"}},
    )
    assert pl.output_columns(dst.key) == ("id", "value")
    assert not any(d.code in {"unexpanded_star", "qualify_error", "lineage_error"} for d in pl.all_diagnostics())


def test_table_edges_keep_update_columns_unknown():
    src, dst = Target("p", "d", "source"), Target("p", "d", "target")
    pl = Pipeline({dst.key: Model(dst, "operations", "UPDATE p.d.target SET value = (SELECT MAX(value) FROM p.d.source)")}, {src.key: src})
    assert pl.table_reads()[dst.key] == {src.key}
    assert pl.table_writes()[dst.key] == {dst.key}
    assert pl.output_columns(dst.key) == ()
    assert pl.trace_column(ColumnRef(dst.key, "value")).unknown


def test_decorated_schema_lookup_asks_for_base_table(monkeypatch):
    from kumosql import schema_fetch

    asked = []

    def resolve(names, project):
        asked.append(set(names))
        return {"p.d.source": {"id": "INT64"}}, {"asked": 1, "found": 1, "from_catalog": 0, "unknown": 0}

    monkeypatch.setattr(schema_fetch, "resolve", resolve)
    target = Target("p", "d", "result")
    pl = Pipeline({target.key: Model(target, "table", "SELECT * FROM `p.d.source$__UNPARTITIONED__`")})
    assert pl.output_columns(target.key) == ("id",)
    assert asked == [{"p.d.source"}]


def test_physical_struct_field_keeps_honest_root_lineage():
    src, dst = Target("p", "d", "source"), Target("p", "d", "result")
    pl = Pipeline({dst.key: Model(dst, "table", "SELECT rec.a AS value FROM p.d.source")}, {src.key: src}, {src.key: {"rec": "STRUCT<a INT64, b INT64>"}})
    assert pl.column_lineage()[ColumnRef(dst.key, "value")] == {ColumnRef(src.key, "rec")}


def test_pre_post_operations_retain_targets():
    target = Target("p", "d", "result")
    pl = Pipeline({target.key: Model(target, "table", "SELECT 1 AS id", operations_sql=("TRUNCATE TABLE p.d.other", "INSERT INTO p.d.other (id) VALUES (1)"))})
    assert pl.table_writes()[target.key] == {"p.d.other"}
    report = pl.report(include_duplicates=False)
    assert report["table_writes"][target.key] == ["p.d.other"]


def test_saved_project_from_before_statement_tables_is_reanalysed():
    target = Target("p", "d", "result")
    pl = Pipeline({target.key: Model(target, "operations", "DROP TABLE p.d.old")})
    old_analysis = pl._analyse()
    del old_analysis.statement_reads
    del old_analysis.statement_writes
    reopened = pickle.loads(pickle.dumps(pl))
    assert reopened.table_writes()[target.key] == {"p.d.old"}
