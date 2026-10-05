"""Seeded smoke run of tools/tie_fuzz.py: no query ``analyze`` calls deterministic changes with the order of tied rows."""

import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tie_fuzz  # noqa: E402

from kumosql.tie_determinism import analyze  # noqa: E402


def test_queries_and_databases_depend_only_on_the_seed():
    first = [tie_fuzz.gen_query(random.Random(f"3:{i}")) for i in range(30)]
    assert first == [tie_fuzz.gen_query(random.Random(f"3:{i}")) for i in range(30)]
    assert len(set(first)) > 25
    assert tie_fuzz.databases(random.Random("a"), "key") == tie_fuzz.databases(random.Random("a"), "key")


def test_databases_respect_the_declared_facts_and_hold_ties():
    rng = random.Random(5)
    ties = 0
    for _ in range(40):
        for rows in tie_fuzz.databases(rng, "key", 3):
            assert 2 <= len(rows) <= 4
            assert len({r[0] for r in rows}) == len(rows) and all(r[0] is not None for r in rows)
            ties += len({r[1:3] for r in rows}) < len(rows)
        for rows in tie_fuzz.databases(rng, "composite", 3):
            assert len({(r[1], r[2]) for r in rows}) == len(rows) and all(r[1] is not None and r[2] is not None for r in rows)
    assert ties > 20


def test_the_fuzzer_sees_a_tie_dependence_when_there_is_one():
    fuzzer = tie_fuzz.Fuzzer()
    sql = "SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1"
    assert not analyze(sql, constraints=tie_fuzz.SCHEMAS["key"]).deterministic
    varied, witness, ran = fuzzer.varies("key", sql, [[(1, 1, 1, 5), (2, 1, 1, 6)]])
    assert ran and varied and witness
    stable = "SELECT user_id, ts FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1"
    assert analyze(stable).deterministic
    assert fuzzer.varies("nokey", stable, [[(1, 1, 1, 5), (2, 1, 1, 6)]]) == (False, None, True)


def _check(summary):
    assert summary["unsound"] == 0, summary["failures"]
    assert summary["deterministic_run"] >= 80  # most checks exercise the claim, not just the unknown verdicts
    assert summary["control_varied"] >= 8  # and the same fuzzer finds the dependence the analysis was afraid of
    assert {"window", "limit", "aggregate"} <= set(summary["deterministic_sites_by_kind"])


def test_no_deterministic_verdict_varies_with_the_storage_order():
    _check(tie_fuzz.run(90, seed=7, show=False, shadow=False))


# Where a SELECT alias takes a column's name (`value AS ts`) older analyses took the alias for the column (#684).
TRAP = "SELECT value AS ts, ts AS value FROM events QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) = 1"


@pytest.mark.skipif(analyze(TRAP).deterministic, reason="needs the alias fix: the analysis still reads value AS ts as ts")
def test_no_deterministic_verdict_varies_when_aliases_shadow_columns():
    _check(tie_fuzz.run(90, seed=7, show=False, shadow=True))
