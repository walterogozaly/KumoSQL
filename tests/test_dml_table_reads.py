"""Tables read by statements that are not queries (DELETE, UPDATE, INSERT ... VALUES, CREATE ... LIKE/CLONE).

They become graph edges and appear in ``table_reads()``; the table a statement writes is not a read, and the columns of
such statements are still not traced (the model keeps its ``unknown_reads`` flag).
"""

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _pipeline(sql: str, **extra: str) -> Pipeline:
    models = {"p.d.raw": Model(Target("p", "d", "raw"), "table", "SELECT 1 AS a"), "p.d.m": Model(Target("p", "d", "m"), "table", sql)}
    for name, query in extra.items():
        models[f"p.d.{name}"] = Model(Target("p", "d", name), "table", query)
    return Pipeline(models, {}, {})


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM `p.d.victim` AS t USING (SELECT a FROM `p.d.raw`) AS d WHERE t.a = d.a",
        "UPDATE `p.d.victim` SET a = 1 FROM `p.d.raw` WHERE victim.a = raw.a",
        "INSERT INTO `p.d.victim` (a) VALUES ((SELECT COUNT(*) FROM `p.d.raw`))",
        "CREATE TABLE `p.d.victim` CLONE `p.d.raw`",
    ],
)
def test_reads_become_edges_and_the_written_table_does_not(sql):
    pipeline = _pipeline(sql)
    reads = pipeline.table_reads()["p.d.m"]
    assert "p.d.raw" in reads and not any("victim" in name for name in reads), reads
    assert "p.d.m" in pipeline.downstream["p.d.raw"]


def test_columns_are_still_not_traced_so_the_model_stays_blind():
    pipeline = _pipeline("DELETE FROM `p.d.victim` WHERE a IN (SELECT a FROM `p.d.raw`)")
    codes = {d.code for d in pipeline.all_diagnostics() if d.model == "p.d.m"}
    assert "unknown_reads" in codes
    assert "p.d.m" not in pipeline.explain_lineage() and not pipeline.dead_columns().get("p.d.raw")


def test_writes_only_statements_add_no_edges():
    for sql in ("TRUNCATE TABLE `p.d.victim`", "DROP TABLE `p.d.victim`", "ALTER TABLE `p.d.victim` ADD COLUMN z INT64"):
        assert not _pipeline(sql).upstream["p.d.m"], sql


def test_dropping_a_read_table_reaches_the_dml_model():
    impact = _pipeline("DELETE FROM `p.d.victim` WHERE a IN (SELECT a FROM `p.d.raw`)").assess_change("drop_table", "p.d.raw")
    assert any(a.model == "p.d.m" for a in [*impact.affected, *impact.unknown]) and impact.safe_to_delete == "unknown"


def test_a_script_mixing_a_query_and_a_delete_reads_both():
    sql = "DELETE FROM `p.d.victim` WHERE a IN (SELECT a FROM `p.d.other`);\nSELECT a FROM `p.d.raw`"
    pipeline = _pipeline(sql, other="SELECT 2 AS a")
    assert {"p.d.raw", "p.d.other"} <= set(pipeline.table_reads()["p.d.m"])
