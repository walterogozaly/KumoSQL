"""TestSuiteEval's hand-labelled ESM false negatives (tools/spider_esm_bench.py): zero wrong proofs and floors on a pinned sample.

The data is downloaded on first use from pinned commits (TestSuiteEval has no licence); the tests that need it skip
when it cannot be fetched. ``FLOORS`` only ever goes up. The full run goes through ``python tools/spider_esm_bench.py``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("spider_esm_bench", _tools / "spider_esm_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["spider_esm_bench"] = bench
_spec.loader.exec_module(bench)
import spider_data  # noqa: E402

# Pairs of the development part (never the held-out fifth) that the floors are measured on: the first SAMPLE of them
SAMPLE = 30
# measured 2 proven, 24 refuted, 4 unknown on this sample; the margin absorbs a slow machine
FLOORS = {"proven": 2, "refuted": 22}


def _pairs():
    try:
        return bench.load_pairs()
    except OSError as error:
        pytest.skip(f"Spider data not available: {error}")


def _schema(*keys):
    """A two-table schema: ``parent(id key, name)``, ``child(id key, pid -> parent.id, note)``."""

    return spider_data.parse_schemas(
        [
            {
                "db_id": "toy",
                "table_names_original": ["parent", "child"],
                "column_names_original": [[-1, "*"], [0, "id"], [0, "name"], [1, "id"], [1, "pid"], [1, "note"]],
                "column_types": ["text", "number", "text", "number", "number", "text"],
                "primary_keys": [1, 3],
                "foreign_keys": [[4, 1]],
            }
        ]
    )["toy"]


def test_the_pinned_files_have_the_published_counts():
    _pairs()  # skips the test when the data cannot be fetched
    rows = bench.load_rows()
    pairs = bench.load_pairs(rows)
    assert len(rows) == 558 and len(pairs) == 359 and sum(len(p.rows) for p in pairs) == 558
    assert sum(p.held_out for p in pairs) == 66
    assert bench.check_sources() == []


def test_schemas_list_types_keys_and_foreign_keys():
    schema = _schema()
    assert schema.tables == {"parent": {"id": "INTEGER", "name": "TEXT"}, "child": {"id": "INTEGER", "pid": "INTEGER", "note": "TEXT"}}
    assert schema.keys == {"parent": ("id",), "child": ("id",)}
    assert schema.foreign == (("child", "pid", "parent", "id"),)
    assert schema.unique == {}  # a foreign key onto the listed key adds no other unique column


def test_a_composite_key_is_listed_by_its_first_column_only_and_never_given_to_the_prover(monkeypatch):
    """Spider lists only the first column of a composite key: the prover must be given no key at all."""

    _pairs()
    schema = spider_data.load_schemas()["concert_singer"]
    assert schema.keys["singer_in_concert"] == ("concert_id",)
    seen = []
    monkeypatch.setattr(bench.solver, "prove", lambda sql1, sql2, tables: seen.append(tables) or "unknown")
    pair = bench.Pair("concert_singer", "", "", (0,), (), schema)
    assert not bench.prove(pair, "SELECT name FROM singer", "SELECT name FROM singer")
    assert seen == [schema.tables]  # the tables and their types only


def test_generated_databases_respect_keys_and_foreign_keys():
    import spider_check as check

    schema = _schema()
    assert check.valid(schema, {"parent": [[1, "a"]], "child": [[1, 1, "x"]]})
    assert not check.valid(schema, {"parent": [[1, "a"], [1, "b"]], "child": []})  # a repeated key
    assert not check.valid(schema, {"parent": [[None, "a"]], "child": []})  # a NULL key
    assert not check.valid(schema, {"parent": [[1, "a"]], "child": [[1, 2, "x"]]})  # a reference to no row
    assert check.valid(schema, {"parent": [[1, "a"]], "child": [[1, None, "x"]]})  # NULL references are allowed


def test_a_difference_that_only_a_tie_decides_is_not_counted():
    """Two queries that take the first of tied rows differently never differ on a database where the order has ties."""

    import spider_check as check

    schema = _schema()
    assert check.differs(schema, "SELECT name FROM parent ORDER BY id LIMIT 1", "SELECT name FROM parent ORDER BY id DESC LIMIT 1") == "differs"
    assert check.differs(schema, "SELECT note FROM child ORDER BY pid LIMIT 1", "SELECT note FROM child ORDER BY pid DESC LIMIT 1") == "differs"
    assert check.differs(schema, "SELECT note FROM child ORDER BY pid LIMIT 1", "SELECT note FROM child ORDER BY pid LIMIT 1") == "agree"


def test_value_placeholders_are_plugged_with_the_golds_values():
    gold = "SELECT note FROM child WHERE pid = 3 AND note = 'a' ORDER BY id LIMIT 2"
    assert bench.needs_values(gold, "SELECT note FROM child WHERE pid = 1 AND note = 'terminal' LIMIT 2")
    assert not bench.needs_values(gold, "SELECT note FROM child WHERE note = 'a' AND pid = 3 LIMIT 2")
    plugs = bench.plug_values(gold, "SELECT note FROM child WHERE pid = 1 AND note = 'terminal' LIMIT 2")
    assert len(plugs) == 4 and all("LIMIT 2" in p for p in plugs)  # two slots, two values; the LIMIT count is no slot
    assert any("pid = 3" in p and "note = 'a'" in p for p in plugs)
    assert bench.plug_values(gold, "SELECT note FROM child WHERE " + " AND ".join(f"pid = {i}" for i in range(9))) is None


def test_the_metrics_clean_up_and_distinct_removal_are_applied():
    assert bench.respace("SELECT a FROM t WHERE a > = 1 AND b < = 2 AND c ! = 3") == "SELECT a FROM t WHERE a >= 1 AND b <= 2 AND c != 3"
    assert "DISTINCT" not in bench.strip_distinct("SELECT DISTINCT a FROM t").upper()
    assert "DISTINCT" not in bench.strip_distinct("SELECT count(DISTINCT a) FROM t").upper()
    assert bench.strip_distinct("SELECT a FROM t") is None


def test_an_equivalent_pair_is_proven_and_a_label_dispute_is_refuted():
    schema = _schema()
    pair = bench.Pair("toy", "", "", (0,), (), schema)
    pair.gold = "SELECT note FROM child WHERE pid = 1 AND id > 0"
    pair.pred = "SELECT child.note FROM child WHERE child.id > 0 AND child.pid = 1"
    proven = bench.decide_pair(pair)
    assert proven["outcome"] == "proven" and not proven["wrong"]
    # a join with the parent drops a child whose pid is NULL: the schema allows it, so the two differ
    pair.gold = "SELECT note FROM child"
    pair.pred = "SELECT child.note FROM child JOIN parent ON child.pid = parent.id"
    refuted = bench.decide_pair(pair)
    assert refuted["outcome"] == "refuted" and not refuted["wrong"] and refuted["witness"]


def test_a_pair_that_runs_past_its_limit_is_unknown_not_proven():
    schema = _schema()
    pair = bench.Pair("toy", "SELECT note FROM child", "SELECT note FROM child", (0,), (), schema)
    result = bench.decide_guarded(pair, timeout=0)
    assert result["outcome"] == "unknown" and result["how"] == "timeout" and not result["wrong"]


def test_pinned_sample_has_no_wrong_proof():
    pairs = [p for p in _pairs() if not p.held_out][:SAMPLE]
    results = bench.run(pairs, jobs=2)
    assert not [r["id"] for r in results if r["wrong"]]
    summary = bench.summarize(results)["all"]
    assert summary["proven"] >= FLOORS["proven"] and summary["refuted"] >= FLOORS["refuted"], summary
    assert not [r["id"] for r in results if r["how"] in ("timeout", "crash")], "a pair ran past its limit"
