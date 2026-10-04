"""Hash/group order can hide arbitrary aggregate picks from row shuffles."""

import random

import pytest

pytest.importorskip("duckdb")

from kumosql import counterexample as cx


def _stable(left, rows):
    spec = cx.Spec({"t": cx.Table("t", [cx.Column("x", "INT")])})
    searcher = cx.Searcher(spec, left, "SELECT 99 AS x", dialect="duckdb")
    try:
        data = {"t": [(x,) for x in rows]}
        searcher._load(data)
        before = searcher._rows(searcher.left_sql)
        after = searcher._rows(searcher.right_sql)
        assert before != after
        return searcher._stable(data, before, after, random.Random(0))
    finally:
        searcher.db.close()


@pytest.mark.parametrize("aggregate", ["ANY_VALUE", "FIRST", "LAST"])
def test_a_sorted_hash_group_does_not_make_an_arbitrary_pick_stable(aggregate):
    sql = f"SELECT {aggregate}(x) AS x FROM (SELECT x FROM t GROUP BY x) q"
    assert not _stable(sql, [1, 2])


@pytest.mark.parametrize("aggregate", ["ANY_VALUE", "FIRST", "LAST"])
@pytest.mark.parametrize("rows", [[1, 1], [None, None], []])
def test_a_group_with_one_possible_value_keeps_a_real_difference(aggregate, rows):
    assert _stable(f"SELECT {aggregate}(x) AS x FROM t", rows)


def test_an_arbitrary_pick_used_as_a_window_is_declined():
    assert not _stable("SELECT ANY_VALUE(x) OVER () AS x FROM t", [1, 2])


def test_a_mixed_null_group_is_conservatively_declined():
    assert not _stable("SELECT ANY_VALUE(x) AS x FROM t", [None, 1])


def test_an_arbitrary_scalar_subquery_inside_a_pick_is_guarded_first():
    sql = "SELECT ANY_VALUE((SELECT ANY_VALUE(x) FROM (SELECT x FROM t GROUP BY x) q)) FROM t"
    assert not _stable(sql, [1, 2])


def test_an_ordinary_aggregate_keeps_a_real_difference():
    assert _stable("SELECT SUM(x) AS x FROM t", [1, 2, None])


@pytest.mark.parametrize("sql", [
    "SELECT FIRST(x ORDER BY x) FROM t",
    "SELECT ANY_VALUE(x) FILTER (WHERE x > 0) FROM t",
    "SELECT ANY_VALUE(DISTINCT x) FROM t",
])
def test_decorated_picks_are_conservatively_declined(sql):
    from kumosql.counterexample_stability import guard_arbitrary_picks

    assert guard_arbitrary_picks(sql) is None
