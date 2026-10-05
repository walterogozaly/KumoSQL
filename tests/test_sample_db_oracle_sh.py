"""Sample databases eval, Oracle Sales History (SH): the run-time download, the CSV load, pairs and rewrites.

The full run is ``python tools/sample_db_bench.py --database oracle_sh --write-results``. The six CSV files (91 MB) are
not committed: they are downloaded from the pinned commit of ``oracle-samples/db-sample-schemas`` into a cache and
checked against their SHA-256, and every test that needs them skips when GitHub cannot be reached. The offline tests
(pins, scripts, licence, the CSV reader on small files, the results files) run without the network.
"""

import datetime
import decimal
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("sample_db_bench_oracle_sh", ROOT / "tools" / "sample_db_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["sample_db_bench_oracle_sh"] = bench
_spec.loader.exec_module(bench)
import sample_db_oracle_sh as sh  # noqa: E402

ADAPTER = bench.ADAPTERS["oracle_sh"]

# a pinned subset of the workload through the rewrite pipeline and lift_subqueries
SUBSET = [
    "sh-view-cal-month-sales-mv",
    "sh-view-fweek-pscat-sales-mv",
    "sh-a10-not-in-with-nulls",
    "sh-a21-cte-chain-unused-cte",
]
# queries the proof-gated optimizer changes on the declared keys (DISTINCT on a primary key)
KEYED = ["sh-a05-distinct-on-primary-key"]
# sibling pairs that drop a key from the declarations, and the DATE-keyed dimension, all refuted on a legal database
DATE_KEY_PAIRS = {
    "sh-fk-join-elimination-time",
    "sh-fk-join-elimination-time-without-fk",
    "sh-fk-join-elimination-time-without-key",
}
# floors only ever go up (recorded run: 28/32 proved, 48/50 refuted)
FLOORS = {"proven": 28, "refuted": 46}


@pytest.fixture(scope="module")
def data():
    """The six CSV files (downloaded and verified once); the test skips when GitHub cannot be reached."""

    if not ADAPTER.available():
        pytest.skip("the pinned Oracle SH CSV files cannot be fetched (no network)")
    return sh.fetch_all()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_database_has_results_files_of_its_own():
    assert "oracle_sh" in bench.ADAPTERS and "oracle_sh" not in bench.COMBINED and ADAPTER.remote
    slots = [o for n, a in bench.ADAPTERS.items() if n not in bench.COMBINED for o in (a.results_order, a.results_order + 1)]
    assert ADAPTER.results_order and len(slots) == len(set(slots))


def test_every_upstream_file_is_pinned_and_the_committed_ones_are_unchanged():
    pins = {u.local: u for u in ADAPTER.upstream}
    assert {u.commit for u in pins.values()} == {sh.COMMIT} and {u.repo for u in pins.values()} == {sh.REPO}
    committed = {name for name, u in pins.items() if not u.remote}
    assert committed == {"upstream/sh_create.sql", "upstream/sh_populate.sql", "upstream/sh_install.sql", "upstream/README.md", "upstream/LICENSE.txt"}
    for name in committed:
        assert digest(ADAPTER.folder / name) == pins[name].sha256, name
    # the six CSV files are pinned here with their digest and are never committed
    assert {u.local: u.sha256 for u in pins.values() if u.remote} == sh.CSV_FILES
    assert not list(ADAPTER.folder.rglob("*.csv"))
    assert (ADAPTER.folder / "upstream" / "LICENSE.txt").read_text().startswith("MIT License") or "Permission is hereby granted" in (
        ADAPTER.folder / "upstream" / "LICENSE.txt"
    ).read_text()
    assert (ADAPTER.folder / "adapted" / "schema.sql").read_text().startswith("-- ADAPTED, not upstream.")


def test_the_scripts_say_which_file_fills_which_table():
    loads = ADAPTER.csv_loads()
    assert sorted(loads.values()) == sorted(sh.CSV_FILES)
    assert loads == {
        "costs": "costs.csv",
        "customers": "customers.csv",
        "promotions": "promotions.csv",
        "sales": "sales.csv",
        "supplementary_demographics": "supplementary_demographics.csv",
        "times": "times.csv",
    }
    inserted = ADAPTER.upstream_rows()
    assert sorted(inserted) == ["channels", "countries", "products"]  # the three tables the script INSERTs


def test_the_declarations_are_the_oracle_scripts():
    tables = ADAPTER.upstream_tables()
    assert sorted(tables) == sorted(ADAPTER.published_counts)
    assert tables["sales"].primary_key == () and tables["costs"].primary_key == ()  # facts: no key upstream
    assert sorted(f[1] for f in tables["sales"].foreign_keys) == ["channels", "customers", "products", "promotions", "times"]
    assert sorted(f[1] for f in tables["costs"].foreign_keys) == ["channels", "products", "promotions", "times"]
    assert tables["customers"].foreign_keys == [(("country_id",), "countries", ("country_id",))]
    assert tables["times"].primary_key == ("time_id",)
    assert sorted(ADAPTER.upstream_views()) == ["cal_month_sales_mv", "fweek_pscat_sales_mv", "profits"]
    for query in ADAPTER.workload():
        assert query["origin"] in ("upstream-view", "authored")
        assert query["origin"] == "authored" or (query["adaptation"] and query["name"] in ADAPTER.upstream_views())


def test_a_changed_declaration_is_caught(data):
    class Drifted(bench.OracleSH):
        def schema(self):
            schema = super().schema()
            schema["sales"].foreign_keys.pop()
            schema["customers"].not_null.discard("cust_email")
            schema["customers"].not_null.discard("cust_first_name")
            return schema

    problems = bench.check_database(Drifted())["problems"]
    assert any("sales: foreign keys" in p for p in problems)
    assert any("customers: NOT NULL" in p for p in problems)


def test_the_csv_loader_reads_the_files_as_oracle_does(tmp_path):
    import duckdb

    path = tmp_path / "t.csv"
    # blanks around a number are Oracle's padding, an empty field is NULL, a quoted field keeps its comma and quote
    path.write_text('ID,D,AMOUNT,NAME\n 7,2020-02-29,12.5,"a, ""b"""\n8,,   ,\n', encoding="utf-8")
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id BIGINT, d DATE, amount DECIMAL(10, 2), name VARCHAR)")
    sh.load_csv(con, "t", {"id": "BIGINT", "d": "DATE", "amount": "DECIMAL(10, 2)", "name": "VARCHAR"}, path)
    assert con.execute("SELECT * FROM t ORDER BY id").fetchall() == [
        (7, datetime.date(2020, 2, 29), decimal.Decimal("12.50"), 'a, "b"'),
        (8, None, None, None),
    ]
    assert sh.count_records(path) == 2 and sh.header(path) == ["ID", "D", "AMOUNT", "NAME"]
    # a value a cast would round or reinterpret is refused, and so is a column the table does not have
    for text, kind in (("1.234", "DECIMAL(10, 2)"), ("1.5", "BIGINT"), ("29-02-2020", "DATE")):
        bad = tmp_path / "bad.csv"
        bad.write_text(f"X\n{text}\n", encoding="utf-8")
        con.execute("CREATE OR REPLACE TABLE b (x " + kind + ")")
        with pytest.raises(ValueError):
            sh.load_csv(con, "b", {"x": kind}, bad)
    bad.write_text("Y\n1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        sh.load_csv(con, "b", {"x": "DECIMAL(10, 2)"}, bad)


def test_a_file_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / sh.COMMIT[:12]
    folder.mkdir()
    (folder / "times.csv").write_text("not the pinned file", encoding="utf-8")
    with pytest.raises(ValueError):
        sh.download("times.csv", cache=tmp_path)
    assert sh.download("times.csv", cache=tmp_path, verify=False).read_text() == "not the pinned file"  # the caller compares the digest
    # nothing is fetched when the network is down: OSError, which the tests turn into a skip
    monkeypatch.setattr(sh.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")))
    with pytest.raises(OSError):
        sh.download("costs.csv", cache=tmp_path)
    assert not list(folder.glob("costs.csv*"))  # no half file is left behind


def test_the_downloaded_files_are_the_pinned_ones(data):
    for name, digest_ in sh.CSV_FILES.items():
        assert digest(data[name]) == digest_


def test_the_load_matches_the_install_script_counts(data):
    report = bench.check_database(ADAPTER)
    assert report["problems"] == []
    assert report["counts"] == ADAPTER.published_counts
    assert report["tables"] == 9 and report["rows"] == sum(ADAPTER.published_counts.values()) == 1_063_396
    assert report["declared"]["primary_keys"] == 7


def test_values_of_the_loaded_database_are_the_files(data):
    con = ADAPTER.connect()
    one = lambda sql: con.execute(sql).fetchone()  # noqa: E731
    assert one("SELECT COUNT(*), SUM(amount_sold), MIN(time_id), MAX(time_id), SUM(quantity_sold) FROM sales") == (
        918_843,
        decimal.Decimal("98205831.21"),
        datetime.date(2019, 1, 1),
        datetime.date(2022, 12, 31),
        918_843,
    )
    assert one("SELECT COUNT(*) FROM sales WHERE channel_id = 9") == (2074,)
    # an empty field is NULL, as Oracle's LOAD reads it
    assert one("SELECT COUNT(*) FROM customers WHERE cust_marital_status IS NULL") == (17506,)
    assert one("SELECT promo_name, promo_cost, promo_begin_date FROM promotions WHERE promo_id = 999") == (
        "NO PROMOTION #",
        decimal.Decimal("0.00"),
        datetime.date(9999, 1, 1),
    )
    assert one("SELECT channel_desc, channel_class FROM channels WHERE channel_id = 9") == ("Tele Sales", "Direct")
    # sales has no key upstream, but no two rows repeat all five foreign keys
    assert one("SELECT COUNT(*) FROM (SELECT 1 FROM sales GROUP BY prod_id, cust_id, time_id, channel_id, promo_id HAVING COUNT(*) > 1)") == (0,)


@pytest.mark.parametrize("subset", [SUBSET])
def test_rewrites_keep_the_results_on_the_real_oracle_data(data, subset):
    rows = bench.rewrite_cases(("oracle_sh", subset, False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(subset) and summary["unsupported"] == 0
    assert summary["verified"] > 0


def test_the_optimizer_uses_the_declared_keys_on_the_real_oracle_data(data):
    rows = bench.rewrite_cases(("oracle_sh", KEYED, True))
    assert not [r for r in rows if r["status"] == "wrong"]
    changed = {r["query"] for r in rows if r["stage"] == "optimizer" and r["status"] == "transformed"}
    assert changed == set(KEYED)  # DISTINCT dropped on the primary key


def test_every_pair_is_decided_without_a_wrong_answer(data):
    rows = bench.run_pairs([ADAPTER], jobs=2)
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [r["id"] for r in rows if r["witness_ok"] is False]
    assert summary["proven"] >= FLOORS["proven"] and summary["refuted"] >= FLOORS["refuted"], summary
    # a counterexample that does not replay on a legal database (a prover bug, such as a repeated DATE key) is never
    # counted as a refutation: none is recorded
    assert [r["id"] for r in rows if r.get("prover_bug")] == []
    # the DATE-keyed dimension: refuted with a counterexample that replays on a legal database
    for row in rows:
        if row["id"] in DATE_KEY_PAIRS and row["label"] == "different":
            assert row["outcome"] == "refuted" and row["counterexample_ok"] is True, row
    # every sibling that drops a guarantee is refuted on a database that keeps the other guarantees, bar the listed ones
    undecided = {r["id"] for r in rows if r["drop"] and r["outcome"] != "refuted"}
    assert undecided == set(), undecided


def test_the_results_files_say_what_they_measured():
    results = ROOT / "benchmarks" / "results"
    rewrites = json.loads((results / "sample-databases-oracle_sh-rewrites.json").read_text())
    pairs = json.loads((results / "sample-databases-oracle_sh-pairs.json").read_text())
    for row in (rewrites, pairs):
        assert row["command"] == "python tools/sample_db_bench.py --database oracle_sh --write-results"
        assert row["docs"] == "docs/evals/sample-databases.md"
    assert rewrites["score"].startswith("0 wrong") and "6660bad68c" in rewrites["caveats"]
    assert pairs["score"].endswith(", 0 wrong") and "known failure" not in pairs["caveats"].lower()
    for row in (rewrites, pairs):
        assert "run time" in row["caveats"] or "downloaded" in row["caveats"]
