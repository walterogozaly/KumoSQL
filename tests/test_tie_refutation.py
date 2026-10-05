"""Refutation on one database must not rest on how a tie happened to be broken (``kumosql.tie_data``).

Pairs that agree on every database where the rows cannot tie, and differ only in which tied row, ANY_VALUE
pick or LIMIT cut each query keeps, stay unknown. Pairs that differ whatever the tie-break still refute.
"""

import pytest

pytest.importorskip("duckdb")

from kumosql import tie_data  # noqa: E402
from kumosql.bounded_equivalence import BColumn, BoundedSchema, BTable, DuckDBReplay  # noqa: E402
from kumosql.executed_refutation import search_counterexample  # noqa: E402
from kumosql.refute import SqliteRunner, find_targeted_difference  # noqa: E402
from kumosql.result_equivalence import (  # noqa: E402
    DataRules,
    DatasetRunner,
    SyntheticDataset,
    SyntheticTable,
)
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

COLUMNS = {"id": "INT64", "g": "INT64", "ts": "INT64", "v": "INT64"}
SCHEMA = {"t": COLUMNS}
RULES = {"t": DataRules(frozenset(COLUMNS), (("id",),))}
COMPARE = {"check_column_names": False}

# Each pair agrees whenever no two rows tie, and each query's rows are among the other's possible results
# on a database with ties.
LATEST_ROW = "SELECT g, ts, v FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY ts DESC) = 1"
LATEST_MIN = (
    "SELECT t.g, t.ts, MIN(t.v) AS v FROM t JOIN (SELECT g, MAX(ts) AS ts FROM t GROUP BY g) m "
    "ON t.g = m.g AND t.ts = m.ts GROUP BY t.g, t.ts"
)
TIE_ARTEFACTS = {
    "row_number_vs_min": (LATEST_ROW, LATEST_MIN),
    "limit_vs_ordered_limit": (
        "SELECT v FROM t ORDER BY ts LIMIT 1",
        "SELECT v FROM t WHERE ts = (SELECT MIN(ts) FROM t) ORDER BY v LIMIT 1",
    ),
    "any_value_vs_min": ("SELECT g, ANY_VALUE(v) FROM t GROUP BY g", "SELECT g, MIN(v) FROM t GROUP BY g"),
    "first_value_vs_min": (
        "SELECT DISTINCT g, FIRST_VALUE(v) OVER (PARTITION BY g ORDER BY ts) FROM t",
        "SELECT g, MIN(v) FROM t JOIN (SELECT g AS g2, MIN(ts) AS m FROM t GROUP BY g) q ON g = g2 AND ts = m GROUP BY g",
    ),
}
# Different whichever tied row is kept.
DIFFERENT = {
    "ascending_vs_descending_limit": ("SELECT v FROM t ORDER BY ts LIMIT 1", "SELECT v FROM t ORDER BY ts DESC LIMIT 1"),
    "limit_vs_shifted_limit": ("SELECT v FROM t ORDER BY ts LIMIT 1", "SELECT v + 100 FROM t ORDER BY ts, v LIMIT 1"),
    "any_value_vs_max_plus_one": ("SELECT g, ANY_VALUE(v) FROM t GROUP BY g", "SELECT g, MAX(v) + 1 FROM t GROUP BY g"),
}


def _dataset(rows):
    return SyntheticDataset(0, {"t": SyntheticTable(tuple(COLUMNS.items()), tuple(rows))})


@pytest.mark.parametrize("name", sorted(TIE_ARTEFACTS))
def test_a_tie_artefact_is_not_refuted(name):
    left, right = TIE_ARTEFACTS[name]
    # the pair really does differ on some database, so the old check refuted it
    assert find_targeted_difference(left, right, SCHEMA, RULES, tie_aware=False, budget=30) is not None
    skipped = []
    assert find_targeted_difference(left, right, SCHEMA, RULES, tie_artefacts=skipped, budget=30) is None
    assert skipped, "the databases that differed were skipped as tie artefacts"


@pytest.mark.parametrize("name", sorted(DIFFERENT))
def test_a_difference_under_every_tie_break_still_refutes(name):
    left, right = DIFFERENT[name]
    found = find_targeted_difference(left, right, SCHEMA, RULES, budget=30)
    assert found is not None
    with DatasetRunner(SCHEMA) as runner:
        assert tie_data.tie_verdict(runner, left, right, found.dataset, COMPARE).status == "different"


def test_the_refuting_database_of_a_tie_prone_pair_is_tie_free():
    left, right = "SELECT g, ANY_VALUE(v) FROM t GROUP BY g", "SELECT g, MAX(v) + 1 FROM t GROUP BY g"
    found = find_targeted_difference(left, right, SCHEMA, RULES, budget=30)
    rows = found.dataset.tables["t"].rows
    assert len({(r[1], r[3]) for r in rows}) == len({r[1] for r in rows}), "one value per group: the pick is not free"


# ----- the data-level probe ----------------------------------------------------------------------


def test_profile_sees_a_tie_only_when_tied_rows_differ_in_what_is_read():
    latest = "SELECT g, ts, v FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY ts DESC) = 1"
    ts_only = "SELECT g, ts FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY ts DESC) = 1"
    tied = _dataset([(1, 1, 5, 10), (2, 1, 5, 20)])
    apart = _dataset([(1, 1, 5, 10), (2, 1, 6, 20)])
    with DatasetRunner(SCHEMA) as runner:
        assert not tie_data.profile(runner, latest, tied, COMPARE).determined
        assert tie_data.profile(runner, latest, apart, COMPARE).determined
        assert tie_data.profile(runner, ts_only, tied, COMPARE).determined  # tied rows look the same


def test_profile_covers_a_tied_cut_and_a_free_pick():
    tied = _dataset([(1, 1, 5, 10), (2, 1, 5, 20)])
    with DatasetRunner(SCHEMA) as runner:
        cut = tie_data.profile(runner, "SELECT v FROM t ORDER BY ts LIMIT 1", tied, COMPARE)
        assert {o.rows for o in cut.outputs} == {((10,),), ((20,),)}
        pick = tie_data.profile(runner, "SELECT g, ANY_VALUE(v) FROM t GROUP BY g", tied, COMPARE)
        assert not pick.complete and "free" in " ".join(pick.reasons)
        same = _dataset([(1, 1, 5, 10), (2, 1, 5, 10)])
        assert tie_data.profile(runner, "SELECT g, ANY_VALUE(v) FROM t GROUP BY g", same, COMPARE).determined


def test_a_nested_cut_is_probed_too():
    variants, unread = tie_data.cut_variants("SELECT v FROM (SELECT v, ts FROM t ORDER BY ts LIMIT 1) q", "duckdb")
    assert len(variants) == 2 and not unread and "ORDER BY ts, 1 ASC, 2 ASC" in variants[0]
    tied = _dataset([(1, 1, 5, 10), (2, 1, 5, 20)])
    with DatasetRunner(SCHEMA) as runner:
        profile = tie_data.profile(runner, "SELECT v FROM (SELECT v, ts FROM t ORDER BY ts LIMIT 1) q", tied, COMPARE)
        assert len(profile.outputs) == 2


def test_storage_orders_cover_small_tables_and_sample_large_ones():
    small = {"t": [(1,), (2,), (3,)]}
    assert len({tuple(o["t"]) for o in tie_data.storage_orders(small)}) == 6
    large = {"a": [(i,) for i in range(9)], "b": [(i,) for i in range(9)]}
    orders = list(tie_data.storage_orders(large, limit=20))
    assert 2 <= len(orders) <= 20 and orders[0] == {"a": tuple(large["a"]), "b": tuple(large["b"])}


def _profile(*results, complete=True):
    return tie_data.TieProfile(tuple(results), complete)


def test_verdict_table():
    same = lambda a, b: a == b  # noqa: E731
    verdict = tie_data.refutation_verdict
    assert verdict(_profile(1), _profile(2), same).status == "different"
    assert verdict(_profile(1), _profile(1), same).status == "same"
    # one determined, the other different under every tie-break tried
    assert verdict(_profile(1), _profile(2, 3), same).status == "different"
    assert verdict(_profile(2, 3), _profile(1), same).status == "different"
    # one tie-break makes them agree
    assert verdict(_profile(1), _profile(2, 1), same).status == "unknown"
    # both tied, or the other's tie-breaks are not all observed (a free pick)
    assert verdict(_profile(1, 2), _profile(3, 4), same).status == "unknown"
    assert verdict(_profile(1), _profile(2, complete=False), same).status == "unknown"
    assert verdict(_profile(), _profile(1), same).status == "unknown"


# ----- the other refuters ------------------------------------------------------------------------


def test_sqlite_engine_is_probed_too():
    left, right = "SELECT v FROM t ORDER BY ts LIMIT 1", "SELECT v FROM t WHERE ts = (SELECT MIN(ts) FROM t) ORDER BY v LIMIT 1"
    assert find_targeted_difference(left, right, SCHEMA, RULES, engine="sqlite", dialect="sqlite", tie_aware=False) is not None
    assert find_targeted_difference(left, right, SCHEMA, RULES, engine="sqlite", dialect="sqlite") is None
    assert find_targeted_difference("SELECT v FROM t ORDER BY ts LIMIT 1", "SELECT v FROM t ORDER BY ts DESC LIMIT 1", SCHEMA, RULES, engine="sqlite", dialect="sqlite")
    with SqliteRunner(SCHEMA) as runner:
        tied = _dataset([(1, 1, 5, 10), (2, 1, 5, 20)])
        assert len(tie_data.profile(runner, left, tied, COMPARE).outputs) == 2


def _bounded_replay(left, right):
    schema = BoundedSchema({"t": BTable("t", [BColumn(c, "INT64") for c in COLUMNS])})
    return DuckDBReplay(schema, left, right, "bigquery")


def test_the_bounded_replay_declines_a_tie_artefact():
    left, right = TIE_ARTEFACTS["limit_vs_ordered_limit"]
    replay = _bounded_replay(left, right)
    assert replay.differ({"t": [(1, 1, 5, 10), (2, 1, 5, 20)]}) is not True  # the tied rows decide
    # a tie-free database where they still differ is not possible here: the pair is equivalent without ties
    assert replay.differ({"t": [(1, 1, 5, 20), (2, 1, 6, 10)]}) is False
    different = _bounded_replay(*DIFFERENT["ascending_vs_descending_limit"])
    assert different.differ({"t": [(1, 1, 5, 20), (2, 1, 6, 10)]}) is True
    assert different.differ({"t": [(1, 1, 5, 20), (2, 1, 5, 10)]}) is not True  # both cut on the tied rows


def test_the_executed_search_declines_a_tie_artefact_and_keeps_a_real_difference():
    types = {"t": COLUMNS}
    schema = {"t": list(COLUMNS)}
    constraints = {"t": TableConstraints(not_null=frozenset(COLUMNS), keys=(("id",),))}
    running = "SELECT id, SUM(v) OVER (ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM t"
    by_id = "SELECT id, SUM(v) OVER (ORDER BY ts, id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM t"
    shifted = "SELECT id, SUM(v) OVER (ORDER BY ts, id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) + 1 AS s FROM t"
    assert search_counterexample(running, by_id, schema=schema, types=types, constraints=constraints) is None
    assert search_counterexample(running, shifted, schema=schema, types=types, constraints=constraints) is not None
