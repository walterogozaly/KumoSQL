"""AdventureWorks in the sample databases eval: the load checked against upstream, workload, pairs with 0 wrong.

The data (91 MB of CSV files in a release asset) is downloaded at run time from the pinned release and never committed, so
every test that needs it skips when GitHub cannot be reached. What is committed (the install script, the licence, the pin
list of the zip's files) is checked offline. The full run is
``python tools/sample_db_bench.py --database adventureworks --write-results``.
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
_spec = importlib.util.spec_from_file_location("sample_db_bench", ROOT / "tools" / "sample_db_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["sample_db_bench"] = bench
_spec.loader.exec_module(bench)

aw = bench.DOWNLOADED["adventureworks"]
import sample_databases_adventureworks as adapter_module  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_databases" / "adventureworks"

# floors only ever go up (recorded run in benchmarks/results/sample-databases-adventureworks-pairs.json)
FLOORS = {"proven": 0, "refuted": 0}
# workload queries the pipeline and lift_subqueries change: views, functions, procedures, authored
SUBSET = [
    "aw-view-vEmployee",
    "aw-view-vSalesPersonSalesByFiscalYears",
    "aw-func-ufnGetStock",
    "aw-proc-uspGetBillOfMaterials",
    "aw-a05",
]

needs_data = pytest.mark.skipif(not aw.available(), reason="the AdventureWorks release asset cannot be downloaded here")


# ------------------------------------------------------------ offline: what is committed and how the script is read


def test_the_committed_files_are_the_pinned_ones():
    script = (FIXTURE / "upstream" / "instawdb.sql").read_bytes()
    assert hashlib.sha256(script).hexdigest() == adapter_module.SCRIPT_SHA256
    licence = (FIXTURE / "upstream" / "license.txt").read_bytes()
    assert hashlib.sha256(licence).hexdigest() == adapter_module.LICENCE_SHA256
    assert b"MIT License" in licence
    members = aw.member_digests()
    assert len(members) == 70 and members["instawdb.sql"] == adapter_module.SCRIPT_SHA256
    assert sum(name.endswith(".csv") for name in members) == 69
    # the script is the one the pins name, and no pin is a loose file that is missing
    assert {pin.local for pin in aw.upstream} == {
        "upstream/instawdb.sql",
        "upstream/license.txt",
        f"release/{adapter_module.ASSET}",
    }
    assert aw.downloaded and "adventureworks" in bench.DOWNLOADED and "adventureworks" not in bench.ADAPTERS


def test_bulk_insert_statements_name_a_file_per_table():
    bulk = aw.bulk_inserts()
    assert len(bulk) == 69  # 68 BULK INSERTs plus AWBuildVersion, which the script fills with an INSERT
    assert bulk["Person"] == ("Person.csv", "+|", "&|\n")
    assert bulk["Address"][1:] == ("\t", "\n")  # the other files are tab and line feed separated
    assert bulk["AWBuildVersion"] == ("AWBuildVersion.csv", "\t", "\n")
    assert set(bulk) | set(aw._EMPTY) == set(aw.schema())


def test_record_readers():
    reader = adapter_module
    assert reader.split_records("a+|b&|\nc+|d&|\n", "&|\n") == ["a+|b", "c+|d"]
    # a line feed row terminator also matches a carriage return and line feed
    assert reader.split_records("a\tb\r\nc\td\r\n", "\n") == ["a\tb", "c\td"]
    # a text column holding line feeds: lines continue until the record has all its fields
    assert reader.join_continuations(["1\tfirst", "second\tdate", "2\tthird\td2"], "\t", 3) == [
        "1\tfirst\nsecond\tdate",
        "2\tthird\td2",
    ]
    assert reader.read_bulk_inserts(
        "BULK INSERT [Person].[Person] FROM '$(SqlSamplesSourceDataPath)Person.csv' WITH (CHECK_CONSTRAINTS, CODEPAGE='65001', "
        "DATAFILETYPE='widechar', FIELDTERMINATOR='+|', ROWTERMINATOR='&|\\n', KEEPIDENTITY, TABLOCK);"
    ) == {"Person": ("Person.csv", "+|", "&|\n")}
    assert reader.column_reader("NUMERIC(19, 4)")("1.50").as_tuple().exponent == -2
    assert reader.column_reader("BOOL")("0") is False and reader.column_reader("BOOL")("1") is True


def test_the_script_is_read_as_declared():
    tables = aw.upstream_tables()
    assert len(tables) == 70 and "DatabaseLog" not in tables
    assert tables["SalesOrderDetail"].primary_key == ("SalesOrderID", "SalesOrderDetailID")
    assert (("SpecialOfferID", "ProductID"), "SpecialOfferProduct", ("SpecialOfferID", "ProductID")) in tables[
        "SalesOrderDetail"
    ].foreign_keys
    # computed columns upstream declares ISNULL(expression, constant) are NOT NULL, the two hierarchyid levels are not
    assert "TotalDue" in tables["SalesOrderHeader"].not_null and "OrganizationLevel" not in tables["Employee"].not_null
    assert aw.schema()["Employee"].primary_key == ("BusinessEntityID",)


def test_every_upstream_view_is_adapted_or_declined_with_a_reason():
    views = aw.upstream_views()
    assert len(views) == 20
    workload = json.loads((FIXTURE / "workload.json").read_text(encoding="utf-8"))
    used = {q["name"] for q in workload["queries"] if q["origin"] == "upstream-view"}
    declined = {d["upstream"] for d in workload["declined"] if d["kind"] == "view"}
    assert used | declined == set(views) and not used & declined
    assert all(d["reason"] for d in workload["declined"])
    # what is declined reads XML with XQuery
    assert all("XQuery" in d["reason"] for d in workload["declined"] if d["kind"] == "view")
    for query in workload["queries"]:
        assert query["origin"] == "authored" or query["adaptation"], query["id"]


def test_pairs_are_wellformed():
    pairs = aw.pairs()
    ids = [p["id"] for p in pairs]
    assert len(ids) == len(set(ids)) and len(pairs) >= 60
    for p in pairs:
        assert p["label"] in ("equivalent", "different") and p["why"]
        assert p["label"] == "equivalent" or p.get("witness"), p["id"]
        assert "sibling" not in p or p["sibling"] in ids
    # every equivalent pair has a sibling that keeps the same shape but breaks the guarantee
    siblings = {p["sibling"] for p in pairs if "sibling" in p}
    assert sum(p["label"] == "equivalent" and p["id"] in siblings for p in pairs) >= 20
    assert sum(bool(p.get("drop")) for p in pairs) >= 12


# ------------------------------------------------------------ the data


@needs_data
def test_the_release_zip_is_the_pinned_one():
    assert aw.check_members() == []


@needs_data
def test_database_loads_as_upstream_declares_it():
    report = bench.check_database(aw)
    assert report["problems"] == []
    assert report["tables"] == 70 and report["rows"] == 759240 and report["upstream_views"] == 20
    assert report["declared"] == {"primary_keys": 70, "foreign_keys": 90, "not_null": 404}
    assert report["assertions"] >= 8


@needs_data
def test_values_are_as_the_files_write_them():
    con = aw.connect()
    try:
        # nchar keeps its padding, an empty string is written as one NUL byte, a hierarchyid is text of its hex
        assert con.execute("SELECT Revision, FileExtension FROM Document WHERE Title = 'Documents'").fetchone() == ("0    ", "")
        assert con.execute("SELECT COUNT(*) FROM Employee WHERE OrganizationNode IS NULL").fetchone() == (1,)
        assert con.execute("SELECT COUNT(*) FROM ProductReview WHERE Comments LIKE '%' || chr(10) || '%'").fetchone() == (4,)
        assert con.execute("SELECT COUNT(*) FROM Product").fetchone() == (504,)
    finally:
        con.close()


@needs_data
def test_every_adventureworks_pair_is_decided_without_a_wrong_answer():
    rows = bench.run_pairs([aw], jobs=2)
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [r["id"] for r in rows if r["witness_ok"] is False]
    assert summary["proven"] >= FLOORS["proven"] and summary["refuted"] >= FLOORS["refuted"]
    assert summary["constraint_siblings_refuted"] == summary["constraint_siblings"] or all(
        r["outcome"] == "unknown" for r in rows if r["drop"] and r["outcome"] != "refuted"
    )
    annotated = {p["id"] for p in aw.pairs() if p.get("known_prover_bug")}
    assert {r["id"] for r in rows if r.get("prover_bug")} <= annotated


@needs_data
def test_rewrites_keep_the_results_on_the_real_data():
    rows = bench.rewrite_cases(("adventureworks", SUBSET, False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET) and summary["unsupported"] == 0
    assert summary["verified"] > 0


def test_results_files_report_zero_wrong():
    for name in ("sample-databases-adventureworks-rewrites", "sample-databases-adventureworks-pairs"):
        row = json.loads((ROOT / "benchmarks" / "results" / f"{name}.json").read_text())
        assert row["docs"] == "docs/evals/sample-databases-adventureworks.md"
        assert row["command"].endswith("--database adventureworks --write-results")
        assert ", 0 wrong" in row["score"] or row["score"].startswith("0 wrong")
    # the other databases' rows keep their own files and numbers
    legacy = json.loads((ROOT / "benchmarks" / "results" / "sample-databases-rewrites.json").read_text())
    assert legacy["size"] == 515
