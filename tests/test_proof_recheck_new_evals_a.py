"""The ``new-evals-a`` re-check adapters (tools/recheck/new_evals_a.py): each proves a pair as its eval does."""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from recheck import engine  # noqa: E402
from recheck import new_evals_a as family  # noqa: E402

NAMES = {
    "quite-rewrites", "quite-negatives", "logos-core-proof", "querybooster", "dbgpt-rules", "documented-rewrites",
    "optimizer-bugs", "jaffle-shop-refactors", "cosette-adapted", "arcwise-corrections",
}


def test_every_eval_of_the_family_is_registered():
    assert NAMES <= set(family.ADAPTERS)


def test_run_capped_stops_a_slow_call_and_keeps_the_callers_timer():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, 30, 5)
    try:
        start = time.time()
        with pytest.raises(family._Deadline):
            family.run_capped(0.2, time.sleep, 5)
        assert time.time() - start < 2
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 20 < remaining <= 30 and interval == 5
        assert family.run_capped(5, lambda: "done") == "done"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_a_proven_adapted_cosette_pair_becomes_a_case_and_an_unproven_one_does_not():
    adapter = family.ADAPTERS["cosette-adapted"]
    items = {i["pair"]: i for i in adapter.items()}
    assert len(items) == 9
    case = adapter.case(items["SelfJoin0"])
    assert case is not None and case.tables and case.dialect == "mysql"
    assert adapter.case(items["SelfJoin0-no-distinct"]) is None
    assert engine.recheck(case, budget=60, seconds=20)["verdict"] == "survived"


def test_dbgpt_style_cases_carry_their_declared_tables():
    adapter = family.ADAPTERS["dbgpt-rules"]
    items = {i["pair"]: i for i in adapter.items()}
    assert len(items) == 36
    case = adapter.case(items["1"])
    assert case is not None and case.dialect == "postgres"
    assert all(column.sql_type in ("INTEGER", "VARCHAR", "BOOLEAN", "TIMESTAMP") for table in case.tables.values() for column in table.columns)
    assert engine.recheck(case, budget=60, seconds=20)["verdict"] == "survived"


def test_documented_rewrites_that_change_results_are_not_proved():
    adapter = family.ADAPTERS["documented-rewrites"]
    items = {i["pair"]: i for i in adapter.items()}
    assert len(items) == 25
    for pair in ("R012b-09", "R012-037"):  # labelled not_equivalent
        assert items[pair]["label"] == "not_equivalent"
        assert adapter.case(items[pair]) is None


def test_optimizer_bug_pairs_are_never_proved():
    adapter = family.ADAPTERS["optimizer-bugs"]
    items = adapter.items()
    assert len(items) >= 24
    assert all(adapter.case(item) is None for item in items[:3])


def test_a_proven_jaffle_refactor_is_composed_from_the_seed_tables():
    adapter = family.ADAPTERS["jaffle-shop-refactors"]
    items = {i["pair"]: i for i in adapter.items()}
    assert len(items) == 8
    case = adapter.case(items["right_join_order_payments::orders"])
    assert case is not None
    assert set(case.tables) == {"raw_customers", "raw_orders", "raw_payments"}
    assert case.left.startswith("WITH ") and "an__orders" in case.right
    assert engine.recheck(case, budget=60, seconds=20)["verdict"] == "survived"
    assert adapter.case(items["inline_staging_into_customers::customers"]) is None  # the prover does not prove it


def _needs(fetch):
    try:
        fetch()
    except Exception as error:  # noqa: BLE001 - no network: the data is downloaded at run time and never bundled
        pytest.skip(f"benchmark data unavailable: {type(error).__name__}")


def test_quite_pairs_are_split_by_flag_and_replayed_as_postgres():
    import quite_bench as q

    _needs(q.fetch)
    equal, unequal = family.ADAPTERS["quite-rewrites"], family.ADAPTERS["quite-negatives"]
    a, b = equal.items(), unequal.items()
    assert len(a) > 2000 and len(b) > 400
    assert not {i["pair"] for i in a} & {i["pair"] for i in b}
    small = min(b, key=lambda i: len(i["original"]) + len(i["rewritten"]))
    tables = unequal.engine_tables(small["benchmark"])
    assert tables and all(isinstance(t, engine.Table) for t in tables.values())
    assert q.DUCKDB_SETTINGS[0] == "SET integer_division = true"


def test_quite_proofs_are_replayed_with_the_evals_settings():
    import quite_bench as q

    _needs(q.fetch)
    adapter = family.ADAPTERS["quite-rewrites"]
    same = [i for i in adapter.items() if " ".join(i["original"].split()) == " ".join(i["rewritten"].split()) and len(i["original"]) < 400]
    if not same:
        pytest.skip("no short identical-text pair")
    case = adapter.case(same[0])
    assert case is not None  # a query is equivalent to its own text
    assert case.setup == q.DUCKDB_SETTINGS and case.dialect == "postgres"


def test_logos_and_querybooster_list_their_pairs():
    import logos_bench as lb
    import querybooster_bench as qb

    _needs(lb.core_root)
    _needs(qb.fetch)
    logos = family.ADAPTERS["logos-core-proof"].items()
    assert len(logos) == 74 and {i["pair"] for i in logos if i["case"] == "tpcds-variants/query014"} == {
        "tpcds-variants/query014#0", "tpcds-variants/query014#1"}
    assert len(family.ADAPTERS["querybooster"].items()) == 68
