"""Floors for the open-source BigQuery projects in ``tests/fixtures/bq_corpora`` (``tools/bq_corpus_bench.py``)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bq_corpus_bench as bench  # noqa: E402

from kumosql.pipeline import load_sqlx_project  # noqa: E402
from kumosql.scripts import analyse_script  # noqa: E402


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def test_real_projects_load_clean_and_keep_their_floors():
    results = bench.run_all()
    totals = bench.totals(results)
    assert [f for r in results.values() for f in r["failures"]] == []
    assert totals["dependencies_found"] == totals["dependencies_expected"] >= 217
    assert totals["statements_matched"] >= 120
    assert totals["columns_traced"] >= 1358
    assert totals["files"] >= 202


def test_a_long_header_comment_does_not_hide_the_statement():
    header = "/*\n" + "Licence text. " * 60 + "\n*/\n-- " + "x" * 700 + "\n"
    statement = analyse_script(header + "CREATE VIEW d.v AS SELECT a FROM d.t").statements[0]
    assert (statement.kind, statement.disposition) == ("create_view", "kept")


def test_statements_inside_called_procedures_are_counted_apart():
    pipeline = load_sqlx_project(bench.CORPORA / "snowplow-web")
    messages = [d.message for d in pipeline.all_diagnostics() if d.code == "unparsed_operation" and d.model.endswith("03_commit_custom")]
    assert messages and messages[0].startswith("7 statements inside procedures they call could not be read")
