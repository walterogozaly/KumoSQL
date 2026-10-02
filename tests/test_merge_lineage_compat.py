"""Column tracing of a MERGE works on every supported sqlglot version (26.0.0 has no ``copy`` argument on ``lineage``)."""

from kumosql import load_sqlx_project

MERGE = (
    "MERGE `p.d.tgt` T USING (SELECT k, v FROM `p.d.src`) S ON T.k = S.k\n"
    "WHEN MATCHED THEN UPDATE SET v = S.v\n"
    "WHEN NOT MATCHED THEN INSERT (k, v) VALUES (S.k, S.v)"
)


def test_merge_columns_trace_without_lineage_errors(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n", encoding="utf-8")
    (tmp_path / "definitions" / "a.sqlx").write_text('config { type: "table" }\nSELECT 1 AS k\n', encoding="utf-8")
    (tmp_path / "definitions" / "m.sqlx").write_text('config { type: "incremental" }\n' + MERGE + "\n", encoding="utf-8")
    analysis = load_sqlx_project(tmp_path)._analyse()
    assert not [d for d in analysis.diagnostics if d.code in ("lineage_error", "qualify_error")]
    sources = {(ref.column, tuple(sorted(str(s) for s in record.sources))) for ref, record in analysis.records.items() if ref.table.endswith(".m")}
    assert sources == {("k", ("p.d.src.k",)), ("v", ("p.d.src.v",))}
