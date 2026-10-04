"""A ``SELECT *`` branch over a table with unknown columns only makes unknown the columns it may fill.

Before, any star branch in a set operation turned every output column of the model unknown, each with
every column the model reads as its sources, and marked everything downstream incomplete. A branch is
exact before its first ``*`` (and after its last, once a branch without a star gives the width), so the
other columns keep their lineage. A column a star branch may fill stays unknown, with the sources the
other branches give. ``--fetch-schema`` also reaches declared sources with no declared columns.
"""

import pytest
from sqlglot import exp

from kumosql import ColumnRef, bigquery_catalog, schema_fetch
from kumosql.lineage_soundness import star_branch_view
from kumosql.pipeline import Pipeline
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.pipeline_types import Model, Target

SCHEMA = {"a": {"a": "INT64", "b": "INT64", "c": "INT64"}}


def pipeline(sql: str, **extra_models) -> Pipeline:
    models = {"b": Model(Target(name="b"), "table", sql)}
    models["c"] = Model(Target(name="c"), "table", "SELECT SUM(x) AS total FROM `b`")
    for name, query in extra_models.items():
        models[name] = Model(Target(name=name), "table", query)
    return Pipeline(models, {"a": Target(name="a")}, SCHEMA)


def record(p: Pipeline, table: str, column: str):
    return p._analyse().records[ColumnRef(table, column)]


def sources(rec) -> set[str]:
    return {f"{s.table}.{s.column}" for s in rec.sources}


UNION_STAR = "SELECT a + b AS x, a, b FROM a UNION ALL SELECT * FROM `ext`"


def test_star_branch_keeps_narrow_sources_instead_of_everything_the_model_reads():
    p = pipeline(UNION_STAR)
    x, a, b = record(p, "b", "x"), record(p, "b", "a"), record(p, "b", "b")
    assert sources(x) == {"a.a", "a.b"}
    assert sources(a) == {"a.a"} and sources(b) == {"a.b"}  # not widened to the other column
    # every position of a union is also filled by the star branch, so none can be called known
    assert {x.status, a.status, b.status} == {"unknown"} and x.reason == "unexpanded_star"
    # the aggregate downstream still traces to x
    total = record(p, "c", "total")
    assert total.status == "traced" and sources(total) == {"b.x"}
    trace = p.trace_column(ColumnRef("c", "total"))
    assert not trace.complete


def test_gap_names_the_star_branch_and_stays_blocking():
    p = pipeline(UNION_STAR)
    gap = [d for d in p._analyse().diagnostics if d.code == "unexpanded_star"]
    assert len(gap) == 1 and "set operation has a SELECT * branch" in gap[0].message
    assert not p.completeness()["complete"]


def test_columns_before_the_star_and_after_it_are_traced():
    head = pipeline("SELECT a, a + b AS x, c FROM a UNION ALL SELECT 1, 2, * FROM `ext`")
    assert record(head, "b", "a").status == "traced" and sources(record(head, "b", "a")) == {"a.a"}
    assert record(head, "b", "x").status == "traced" and sources(record(head, "b", "x")) == {"a.a", "a.b"}
    assert record(head, "b", "c").status == "unknown" and sources(record(head, "b", "c")) == {"a.c"}

    tail = pipeline("SELECT a, a + b AS x, c FROM a UNION ALL SELECT *, 3 FROM `ext`")
    assert record(tail, "b", "c").status == "traced"
    assert record(tail, "b", "a").status == "unknown" and record(tail, "b", "x").status == "unknown"


def test_star_in_the_first_branch_names_the_known_positions():
    p = pipeline("SELECT 1 AS k, *, a FROM `ext` UNION ALL SELECT a, a + b, c FROM a")
    assert record(p, "b", "k").status == "traced" and sources(record(p, "b", "k")) == {"a.a"}
    last = record(p, "b", "a")
    assert last.status == "traced" and sources(last) == {"a.c", "ext.a"}
    assert record(p, "b", "*").status == "unknown"


def test_star_set_operation_in_a_cte_stays_unknown_for_every_column():
    p = pipeline("WITH u AS (SELECT a, a + b AS x FROM a UNION ALL SELECT * FROM `ext`) SELECT a, x FROM u")
    assert {record(p, "b", "a").status, record(p, "b", "x").status} == {"unknown"}


def test_all_branches_with_a_star_leave_the_width_unknown():
    p = pipeline("SELECT * FROM `ext` UNION ALL SELECT * FROM `ext2`")
    assert record(p, "b", "*").status == "unknown"


@pytest.mark.parametrize("head,tail,width", [(h, t, n) for n in (3, 4, 5) for h in range(n) for t in range(n - h)])
def test_exactly_the_positions_a_star_may_fill_are_tainted(head, tail, width):
    cols = [f"c{i}" for i in range(width)]
    first = ", ".join(f"a AS {c}" for c in cols)
    branch = ", ".join([f"{i} AS h{i}" for i in range(head)] + ["*"] + [f"{i} AS t{i}" for i in range(tail)])
    query = pipeline(f"SELECT {first} FROM a UNION ALL SELECT {branch} FROM `ext`")._analyse().parsed["b"]
    # parse only: the view of the raw query is enough for the position arithmetic
    view = star_branch_view(query, cols)
    if view is None:
        pytest.skip("star branch not eligible")
    expected = {cols[i] for i in range(width) if not (i < head or i >= width - tail)}
    assert view.tainted == expected
    assert not any(isinstance(n, exp.Star) for n in view.query.find_all(exp.Star) if isinstance(n.parent, exp.Select))


def test_changes_to_the_star_table_reach_the_model_as_unknown_not_silent():
    p = pipeline(UNION_STAR)
    drop = p.assess_change("drop_column", "ext", "q").to_json()
    assert [u["model"] for u in drop["unknown"]][:1] == ["b"] and drop["unknown"][0]["reason"] == "unexpanded_star"
    assert drop["affected"] == []
    # the known branch is still resolved exactly
    known = p.assess_change("drop_column", "a", "a").to_json()
    assert [(m["model"], m["effect"]) for m in known["affected"]] == [("b", "breaks"), ("c", "indirect")]
    assert p.assess_change("drop_column", "a", "c").to_json()["affected"] == []


# --- --fetch-schema: declared sources, and what it reports --------------------------------------------------


@pytest.fixture(autouse=True)
def clean_catalog():
    bigquery_catalog.clear_cache()
    schema_fetch._DENIED.clear()
    schema_fetch._NO_CREDENTIALS.clear()
    yield
    bigquery_catalog.clear_cache()


def fake_bigquery(monkeypatch, tables, denied=()):
    calls = []

    def get_table(project, dataset, table):
        full = f"{project}.{dataset}.{table}"
        calls.append(full)
        if full in denied:
            raise bigquery_catalog.CatalogError("no access", 403)
        if full not in tables:
            raise bigquery_catalog.CatalogError("not found", 404)
        return {"id": table, "schema": [{"name": c, "type": "INTEGER" if c == "id" else "STRING"} for c in tables[full]]}

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "token")
    monkeypatch.setattr(bigquery_catalog, "get_table", get_table)
    return calls


def dataform_project(root, declared=("ext",)):
    definitions = root / "definitions"
    definitions.mkdir()
    (root / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (definitions / "a.sqlx").write_text('config { type: "declaration", name: "a" }\n')
    for name in declared:
        (definitions / f"{name}.sqlx").write_text(f'config {{ type: "declaration", name: "{name}" }}\n')
    (definitions / "b.sqlx").write_text(
        'config { type: "table" }\nSELECT a + b AS x, a, b FROM ${ref("a")}\nUNION ALL\nSELECT * FROM ${ref("ext")}\n'
    )
    (definitions / "c.sqlx").write_text('config { type: "table" }\nSELECT SUM(x) AS total FROM ${ref("b")}\n')
    return load_sqlx_project(root, source_schema={"p.d.a": {"a": "INT64", "b": "INT64", "c": "INT64"}})


def test_a_declared_source_without_columns_is_looked_up_and_expands_the_star(monkeypatch, tmp_path):
    calls = fake_bigquery(monkeypatch, {"p.d.ext": ["id", "z", "w"]})
    p = dataform_project(tmp_path)
    analysis = p._analyse()
    assert calls == ["p.d.ext"]
    assert analysis.schema_lookup["fetched"] == 1 and analysis.schema_lookup["failed"] == 0
    assert not [d for d in analysis.diagnostics if d.code == "unexpanded_star"]
    x = analysis.records[ColumnRef("p.d.b", "x")]
    # the fetched columns took part: ext.id fills the same position as a + b
    assert x.status == "traced" and {f"{s.table}.{s.column}" for s in x.sources} == {"p.d.a.a", "p.d.a.b", "p.d.ext.id"}
    assert p.trace_column(ColumnRef("p.d.c", "total")).complete
    report = p.report()
    assert report["schema_lookup"]["fetched"] == 1


def test_fetch_off_is_reported_and_leaves_the_star_a_gap(monkeypatch, tmp_path):
    calls = fake_bigquery(monkeypatch, {"p.d.ext": ["id", "z", "w"]})
    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "0")
    p = dataform_project(tmp_path)
    stats = p._analyse().schema_lookup
    assert calls == [] and stats["fetch"] is False and stats["skipped_by_reason"] == {"fetch_off": 1}
    assert "fetching is off" in schema_fetch.summary(stats)
    assert ("p.d.b", "unexpanded_star") in {(d.model, d.code) for d in p._analyse().diagnostics}


@pytest.mark.parametrize(
    "denied,tables,reason",
    [({"p.d.ext"}, {}, "denied"), (set(), {}, "not_found")],
)
def test_failed_fetches_are_counted_by_reason(monkeypatch, tmp_path, denied, tables, reason):
    fake_bigquery(monkeypatch, tables, denied=denied)
    stats = dataform_project(tmp_path)._analyse().schema_lookup
    assert stats["fetched"] == 0 and stats["failed"] == 1 and stats["failed_by_reason"] == {reason: 1}
    line = schema_fetch.summary(stats)
    assert "fetched 0 of 1 tables, 1 failed" in line and "p.d.ext" not in line


def test_no_credentials_is_reported_not_silent(monkeypatch, tmp_path):
    fake_bigquery(monkeypatch, {"p.d.ext": ["id"]})
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: (_ for _ in ()).throw(RuntimeError("no credentials")))
    stats = dataform_project(tmp_path)._analyse().schema_lookup
    assert stats["skipped_by_reason"] == {"no_credentials": 1}
    assert "no BigQuery credentials" in schema_fetch.summary(stats)


def test_a_table_name_without_a_project_is_reported(monkeypatch):
    fake_bigquery(monkeypatch, {})
    _, stats = schema_fetch.resolve(["ext.raw"], default_project="")
    assert stats["skipped_by_reason"] == {"no_project": 1} and stats["unknown"] == 1
