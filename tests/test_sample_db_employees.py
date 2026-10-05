"""Employees in the sample databases eval: a run-time download checked against upstream, workload, pairs, 0 wrong.

Employees (datacharmer/test_db, CC BY-SA 3.0, 167 MB) is never committed: ``tools/sample_db_employees.py`` downloads
the pinned files and loads them into a DuckDB file in a cache folder. The tests that need the data are marked ``slow``
(the first run downloads 167 MB and builds the database, about two minutes; each later run opens it read-only) and skip
when GitHub cannot be reached. The tests that need no download (the pins, the INSERT scanner, the checksum chain, the
authored files, the results files) run in the quick suite. The full run is
``python tools/sample_db_bench.py --database employees --write-results``.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

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
import sample_db_employees as emp  # noqa: E402

employees = bench.DOWNLOADED["employees"]
FOLDER = ROOT / "tests" / "fixtures" / "sample_databases" / "employees"

# floors only ever go up (recorded run: 32 of 35 proved, 40 of 41 refuted)
FLOORS = {"proven": 31, "refuted": 38}
# workload queries the pipeline and lift_subqueries change: upstream views and functions, authored
SUBSET = [
    "emp-view-current-dept-emp",
    "emp-func-emp-name",
    "emp-a01-distinct-on-key",
    "emp-a03-not-in-with-nulls",
    "emp-a07-cte-with-unused",
    "emp-a08-derived-table-trivial-predicate",
]


def _data():
    """The downloaded folder; the test is skipped when GitHub cannot be reached."""

    try:
        return emp.fetch()
    except emp.DataUnavailable as error:
        pytest.skip(f"the pinned Employees files cannot be fetched: {error}")


# ---------------------------------------------------------------- no download needed


def test_the_data_is_never_committed_and_is_pinned_to_one_commit():
    assert employees.downloaded and "employees" not in bench.ADAPTERS
    assert emp.COMMIT == "e324b56193ca506ab7cc1ab143a9153d8c4535d7"
    assert set(emp.FILES) >= {"employees.sql", "objects.sql", *emp.LOAD_ORDER}
    assert all(len(d) == 64 and int(d, 16) >= 0 for d in emp.FILES.values())
    assert {u.path for u in employees.upstream} == set(emp.FILES)
    # nothing but the adapted DDL, the authored files and the notice is in the repository
    assert sorted(p.name for p in FOLDER.rglob("*") if p.is_file()) == [
        "NOTICE.md",
        "pairs.json",
        "schema.sql",
        "workload.json",
    ]
    assert not list(FOLDER.rglob("*.dump")) and not list(FOLDER.rglob("employees.sql"))


def test_the_download_is_verified_before_it_is_cached(tmp_path, monkeypatch):
    name = "departments-test"
    monkeypatch.setitem(emp.FILES, name, hashlib.sha256(b"whole file").hexdigest())
    # a transfer cut off, or a file that is not the pinned one, is never renamed into place
    pieces = {"data": b"whole fil"}

    class Response:
        headers = {"Content-Length": "10"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            data, pieces["data"] = pieces["data"], b""
            return data

    monkeypatch.setattr(emp.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(emp.DataUnavailable):
        emp.download(name, tmp_path, attempts=2)
    assert not (tmp_path / name).exists() and not list(tmp_path.iterdir())
    # a cached file that is not the pinned one is an error, not a skip
    (tmp_path / name).write_bytes(b"something else")
    with pytest.raises(emp.PinMismatch):
        emp.download(name, tmp_path)
    (tmp_path / name).write_bytes(b"whole file")
    assert emp.download(name, tmp_path) == tmp_path / name


def test_the_insert_scanner_counts_every_tuple_and_refuses_what_it_does_not_understand():
    text = (
        "INSERT INTO `t` VALUES (1,'a, (b)','1990-01-01'),(2,'it''s','1990-01-02');\n"
        "INSERT INTO `t` VALUES (3,'c','1990-01-03');\n"
        "INSERT INTO `u` VALUES ('d001','Sales');\n"
    )
    assert emp.count_rows(text) == {"t": 3, "u": 1}
    with pytest.raises(ValueError):
        emp.count_rows(text + "DELETE FROM t;\n")
    with pytest.raises(ValueError):
        emp.count_rows("INSERT INTO `t` VALUES (1,'a\\'b');\n")


def test_the_checksum_chain_follows_mysqls_concat_ws():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE departments (dept_no VARCHAR, dept_name VARCHAR)")
    con.execute(
        "INSERT INTO departments VALUES ('d002', 'Finance'), ('d001', 'Marketing')"
    )
    records, md5, sha = emp.chain_checksums(con, "departments")
    crc = ""
    for row in (("d001", "Marketing"), ("d002", "Finance")):
        crc = hashlib.md5(f"{crc}#{'#'.join(row)}".encode()).hexdigest()
    assert records == 2 and md5 == crc and len(sha) == 64
    # a NULL is skipped by CONCAT_WS, not printed
    con.execute(
        "CREATE TABLE titles (emp_no INT, title VARCHAR, from_date DATE, to_date DATE)"
    )
    con.execute("INSERT INTO titles VALUES (1, 'Engineer', DATE '1990-01-01', NULL)")
    assert (
        emp.chain_checksums(con, "titles")[1]
        == hashlib.md5(b"#1#Engineer#1990-01-01").hexdigest()
    )


def test_the_adapted_schema_declares_the_keys_the_pairs_are_about():
    schema = employees.schema()
    assert list(schema) == list(emp.TABLES)
    assert schema["titles"].primary_key == ("emp_no", "title", "from_date")
    assert schema["salaries"].primary_key == ("emp_no", "from_date")
    assert schema["dept_emp"].primary_key == ("emp_no", "dept_no")
    assert (
        schema["titles"].not_null >= {"emp_no", "title", "from_date"}
        and "to_date" not in schema["titles"].not_null
    )
    assert all(
        schema[t].columns[c] == "DATE"
        for t, c in (("titles", "from_date"), ("salaries", "from_date"))
    )


def test_the_authored_files_are_well_formed():
    workload = employees.workload()
    ids = [q["id"] for q in workload]
    assert len(ids) == len(set(ids)) and all(i.startswith("emp-") for i in ids)
    origins = {q["origin"] for q in workload}
    assert origins == {
        "upstream-view",
        "upstream-procedure",
        "upstream-test",
        "authored",
    }
    assert all("adaptation" in q for q in workload if q["origin"] != "authored")
    pairs = employees.pairs()
    pair_ids = [p["id"] for p in pairs]
    assert len(pair_ids) == len(set(pair_ids)) and all(
        i.startswith("emp-") for i in pair_ids
    )
    for pair in pairs:
        assert (
            pair["label"] in ("equivalent", "different")
            and pair["left"]
            and pair["right"]
        )
        if pair["label"] == "different":
            assert pair.get("witness") is not None, pair["id"]
        if "sibling" in pair:
            assert pair["sibling"] in pair_ids, pair["id"]
    assert sum(p["label"] == "equivalent" for p in pairs) >= 25
    assert sum(bool(p.get("drop")) for p in pairs) >= 10
    # every query of the workload is held out or not by the same hash as in the other databases
    assert 0 < sum(bench.held_out(f"employees:{i}") for i in ids) < len(ids) // 3


def test_the_results_files_report_zero_wrong():
    for name in (
        "sample-databases-employees-rewrites",
        "sample-databases-employees-pairs",
    ):
        row = json.loads((ROOT / "benchmarks" / "results" / f"{name}.json").read_text())
        assert row["docs"] == "docs/evals/sample-databases-employees.md"
        assert row["command"].endswith("--database employees --write-results")
        assert ", 0 wrong" in row["score"] or row["score"].startswith("0 wrong")


# ---------------------------------------------------------------- needs the downloaded data (slow lane)


@pytest.mark.slow
def test_the_load_matches_upstream():
    _data()
    report = bench.check_database(employees)
    assert report["problems"] == [], report["problems"]
    assert report["tables"] == 6 and report["rows"] == 3919015
    assert report["declared"] == {"primary_keys": 6, "foreign_keys": 6, "not_null": 23}
    assert report["upstream_views"] == 4


@pytest.mark.slow
def test_the_upstream_views_are_in_the_workload():
    _data()
    views = employees.upstream_views()
    assert sorted(views) == [
        "current_dept_emp",
        "dept_emp_latest_date",
        "v_full_departments",
        "v_full_employees",
    ]
    assert sorted(
        q["name"] for q in employees.workload() if q["origin"] == "upstream-view"
    ) == sorted(views)


@pytest.mark.slow
def test_every_employees_pair_is_decided_without_a_wrong_answer():
    _data()
    rows = bench.run_pairs([employees], jobs=2)
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [
        r["id"] for r in rows if r["witness_ok"] is False
    ]
    assert (
        summary["proven"] >= FLOORS["proven"]
        and summary["refuted"] >= FLOORS["refuted"]
    )
    # a pair annotated with a prover bug is the replay gate catching an illegal counterexample: it must stay
    # unknown, never a refutation or a proof
    annotated = {p["id"] for p in employees.pairs() if p.get("known_prover_bug")}
    assert {r["id"] for r in rows if r.get("prover_bug")} <= annotated
    assert all(
        r["outcome"] == "unknown"
        for r in rows
        if r["id"] in annotated and r.get("prover_bug")
    )


@pytest.mark.slow
def test_rewrites_keep_the_results_on_the_real_data():
    _data()
    rows = bench.rewrite_cases(("employees", SUBSET, False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET) and summary["unsupported"] == 0
    assert summary["verified"] > 0
