"""IBM FIBEN in the sample databases eval: the pinned download, the load, the adapted queries, rewrites and pairs with 0 wrong.

FIBEN's data (an 80 MB ``data.zip``, 400 MB of CSV) is not in the repository: it is downloaded at run time from the pinned
commit into ``KUMOSQL_BENCH_DATA`` (default ``~/.cache/kumosql-bench``) and checked against the SHA-256 of the archive and of
every file. The tests that need it skip when GitHub cannot be reached; the ones that do not (pins, the regenerated schema,
the query file, the adaptation, the download guards) always run. The full run is
``python tools/sample_db_bench.py --database fiben --write-results``.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import urllib.error

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location(
    "sample_db_bench", ROOT / "tools" / "sample_db_bench.py"
)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sample_db_bench"] = bench
_spec.loader.exec_module(bench)

fiben = bench.ADAPTERS["fiben"]
sdf = bench.fiben  # tools/sample_db_fiben.py

# floors only ever go up
FLOORS = {"proven": 0, "refuted": 0}
# workload queries the pipeline and lift_subqueries change (see the docs page)
SUBSET: list[str] = []


@pytest.fixture(scope="module")
def con():
    """The whole database, loaded once (about a minute); the test is skipped when the data cannot be had."""

    try:
        sdf.data_dir()
    except OSError as error:
        pytest.skip(f"FIBEN data not available: {error}")
    connection = fiben.connect()
    yield connection
    connection.close()


# ---------------------------------------------------------------- no data needed


def test_the_committed_upstream_files_are_the_pinned_ones():
    for pin in fiben.upstream:
        assert bench.sha256(fiben.folder / pin.local) == pin.sha256, pin.local
        assert pin.commit == sdf.COMMIT and pin.repo == sdf.REPO
    licence = (fiben.folder / "upstream" / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence and "Version 2.0" in licence


def test_the_adapted_schema_is_the_upstream_schema():
    ddl = (fiben.folder / "upstream" / "FIBEN.sql").read_text(encoding="utf-8")
    text = sdf.adapted_schema_text(ddl, bench.read_ddl)
    assert (fiben.folder / "adapted" / "schema.sql").read_text(encoding="utf-8") == text
    upstream, adapted = fiben.upstream_tables(), fiben.schema()
    assert len(adapted) == 152 and set(upstream) == set(adapted)
    assert sum(len(t.foreign_keys) for t in adapted.values()) == 159
    assert all(t.primary_key for t in adapted.values())
    for name, table in adapted.items():
        assert list(table.columns) == list(upstream[name].columns)
        assert table.foreign_keys == upstream[name].foreign_keys
    assert sorted(adapted) == sorted(
        (fiben.folder / "upstream" / "tablelist.txt").read_text().split()
    )


def test_the_manifest_names_every_file_of_the_archive():
    files = sdf.manifest()
    assert sorted(files) == sorted(fiben.schema())
    assert sum(f["rows"] for f in files.values()) == 11_668_125
    assert fiben.published_counts == {t: f["rows"] for t, f in files.items()}
    empty = [t for t, f in files.items() if not f["rows"]]
    assert len(empty) == 111 and all(files[t]["bytes"] == 0 for t in empty)


def test_the_query_file_is_as_upstream_describes_it():
    entries = sdf.upstream_queries()
    assert len(entries) == 300
    assert len(sdf.targets(entries)) == 237
    assert sum(e["queryType"].lower() != "non-nested" for e in entries) == 170
    # a paraphrase repeats the target of another entry
    assert {e["uniqueQueryID"] for e in entries if e["isParaphrased"]} <= set(
        sdf.targets(entries)
    )


def test_every_upstream_target_is_scored_or_listed_with_its_reason():
    workload = json.loads((fiben.folder / "workload.json").read_text(encoding="utf-8"))
    scored = [q["upstream_id"] for q in workload["queries"] if q["origin"] == "upstream-query"]
    left_out = [q["upstream_id"] for q in workload["not_scored"]]
    assert sorted(scored + left_out) == sorted(sdf.targets())
    assert {q["reason"] for q in workload["not_scored"]} <= {
        "duplicate",
        "broken upstream",
        "adaptation",
        "empty result",
        "result not determined",
    }
    ids = [q["id"] for q in workload["queries"]]
    assert len(ids) == len(set(ids))
    for query in workload["queries"]:
        assert query["origin"] in ("upstream-query", "authored")
        assert query["adaptation"], query["id"]


def test_the_adaptation_is_syntax_and_type_coercion_only():
    types = {
        "PERSON": {"PERSONID": "INT64", "HASLASTNAME": "STRING"},
        "ACCOUNT": {"ACCOUNTID": "INT64", "HASSTARTDATE": "DATETIME"},
    }
    sql, changes = sdf.adapt_query(
        'SELECT YEAR(a.HASSTARTDATE) AS y FROM FIBEN.ACCOUNT AS a, FIBEN."PERSON" AS p '
        "WHERE p.PERSONID = '42' AND p.HASLASTNAME = '42' ORDER BY y FETCH FIRST 3 ROWS ONLY",
        types,
    )
    assert sql == (
        "SELECT EXTRACT(YEAR FROM a.HASSTARTDATE) AS y FROM ACCOUNT AS a, PERSON AS p "
        "WHERE p.PERSONID = 42 AND p.HASLASTNAME = '42' ORDER BY y LIMIT 3"
    )
    assert "schema qualifier FIBEN dropped" in changes
    assert "FETCH FIRST n ROWS ONLY -> LIMIT n" in changes
    # a literal compared with a STRING column stays a string; a query without anything to adapt comes back unchanged
    assert sdf.adapt_query("SELECT PERSONID FROM PERSON", types)[1] == []


def test_the_download_is_checked_and_refuses_what_is_not_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(sdf, "CACHE", tmp_path)

    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(sdf.urllib.request, "urlopen", unreachable)
    with pytest.raises(sdf.Unavailable):
        sdf.data_dir()
    assert not sdf.available()

    class Pointer:  # what a Git LFS pointer, or a changed file, looks like
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __init__(self):
            self.chunks = [b"version https://git-lfs.github.com/spec/v1\n"]

        def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    monkeypatch.setattr(sdf.urllib.request, "urlopen", lambda *a, **k: Pointer())
    with pytest.raises(sdf.Unavailable, match="not the pinned"):
        sdf.data_dir()
    assert list(tmp_path.rglob("*.part")) == [] and not (tmp_path / sdf.ARCHIVE).exists()


def test_a_cached_file_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sdf, "CACHE", tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    files = sdf.manifest()
    for table, spec in files.items():
        (data / f"{table}.csv").write_bytes(b"x" * spec["bytes"])  # right size, wrong content
    monkeypatch.setattr(sdf, "_download", lambda: pytest.fail("a complete cache is not downloaded again"))
    biggest = next(t for t, f in files.items() if f["bytes"])
    with pytest.raises(sdf.Unavailable, match="not the pinned"):
        sdf.data_dir()
    assert hashlib.sha256(b"x" * files[biggest]["bytes"]).hexdigest() != files[biggest]["sha256"]


def test_a_workload_that_returns_other_rows_is_reported(monkeypatch):
    class Fake:
        def execute(self, sql):
            return self

        def fetchall(self):
            return [(1,)]

    monkeypatch.setattr(fiben, "workload", lambda: [
        {"id": "fb-x", "origin": "upstream-query", "sql": "SELECT 1", "rows": 2, "digest": "0"}
    ])
    problems = sdf.check_workload(fiben, Fake())
    assert len(problems) == 1 and "fb-x" in problems[0]


# ---------------------------------------------------------------- the data


def test_the_database_loads_as_the_pinned_files_declare_it(con):
    report = bench.check_database(fiben, con)
    assert report["problems"] == []
    assert report["tables"] == 152 and report["rows"] == 11_668_125
    assert report["declared"]["primary_keys"] == 152 and report["declared"]["foreign_keys"] == 159


def test_every_scored_query_returns_the_rows_of_the_original(con):
    # check_workload also runs inside check_database; here a failure names the query
    assert sdf.check_workload(fiben, con) == []
    scored = [q for q in fiben.workload() if q["origin"] == "upstream-query"]
    assert all(q["rows"] > 0 for q in scored)


def test_the_pairs_are_decided_without_a_wrong_answer(con):
    pairs = fiben.pairs()
    rows = [bench.decide_pair("fiben", pair, con) for pair in pairs]
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [r["id"] for r in rows if r["witness_ok"] is False]
    assert summary["pairs"] == len(pairs)
    assert summary["proven"] >= FLOORS["proven"] and summary["refuted"] >= FLOORS["refuted"]
    assert summary["constraint_siblings_refuted"] == summary["constraint_siblings"]


def test_rewrites_keep_the_results_on_the_real_data():
    pytest.importorskip("sqlglot")
    try:
        sdf.data_dir()
    except OSError as error:
        pytest.skip(f"FIBEN data not available: {error}")
    import engine_suites as es

    before = es.QUERY_TIMEOUT_S
    rows = bench.rewrite_cases(("fiben", SUBSET, False))
    assert es.QUERY_TIMEOUT_S == before  # FIBEN's longer timeout is for its own run only
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET) and summary["unsupported"] == 0
    assert summary["verified"] > 0


def test_results_files_report_zero_wrong():
    for name in ("sample-databases-fiben-rewrites", "sample-databases-fiben-pairs"):
        row = json.loads((ROOT / "benchmarks" / "results" / f"{name}.json").read_text())
        assert row["docs"] == "docs/evals/sample-databases-fiben.md"
        assert row["command"].endswith("--database fiben --write-results")
        assert ", 0 wrong" in row["score"] or row["score"].startswith("0 wrong")
