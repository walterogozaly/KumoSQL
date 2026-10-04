"""Pruning the unread columns of a UNION ALL must not strip a branch of its only aggregate.

``SELECT MIN(x) AS k, 1 AS c FROM t`` returns one row over an empty or any ``t``; without the aggregate it is
``SELECT 1 AS c FROM t``, a row per input row. ``_prune_union_all`` dropped unread columns in every branch
without looking (the plain-select path of ``_prune_derived`` already kept an aggregate). Found by the rule-level
differential fuzzer (``tools/rule_fuzz.py``).
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402

SCHEMA = {"t": ["id", "x", "y"]}


def _rows(sql: str) -> Counter:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id BIGINT, x BIGINT, y BIGINT)")
    con.execute("INSERT INTO t VALUES (NULL, -1, 3), (1, 1, 2), (3, 3, 0)")
    return Counter(run_unoptimized(con, sql)[0])


def _proves(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA).proven


LEFT = (
    "SELECT q.c3 FROM (SELECT MIN(t.x) AS k, 1 AS c3 FROM t UNION ALL "
    "SELECT MIN(t.x) AS k, 0 AS c3 FROM t GROUP BY t.y) AS q"
)
RIGHT = (
    "SELECT q.c3 FROM (SELECT t.x AS k, 1 AS c3 FROM t UNION ALL "
    "SELECT MIN(t.x) AS k, 0 AS c3 FROM t GROUP BY t.y) AS q"
)


def test_witness_returns_different_rows():
    assert _rows(LEFT) != _rows(RIGHT)


def test_a_global_aggregate_branch_does_not_equal_a_row_per_input_row():
    assert not _proves(LEFT, RIGHT)
    assert not _proves(RIGHT, LEFT)


def test_both_branches_global_aggregates_with_other_aggregate_unread():
    left = "SELECT q.c FROM (SELECT MAX(t.x) AS k, 1 AS c FROM t UNION ALL SELECT MAX(t.y) AS k, 2 AS c FROM t) AS q"
    right = "SELECT q.c FROM (SELECT t.x AS k, 1 AS c FROM t UNION ALL SELECT MAX(t.y) AS k, 2 AS c FROM t) AS q"
    assert _rows(left) != _rows(right)
    assert not _proves(left, right)


def test_near_miss_unread_aggregate_column_may_differ_when_the_kept_column_is_an_aggregate():
    left = "SELECT q.n FROM (SELECT COUNT(*) AS n, MIN(t.x) AS k FROM t UNION ALL SELECT COUNT(*) AS n, MAX(t.x) AS k FROM t) AS q"
    right = "SELECT q.n FROM (SELECT COUNT(*) AS n, MIN(t.y) AS k FROM t UNION ALL SELECT COUNT(*) AS n, 7 AS k FROM t) AS q"
    assert _rows(left) == _rows(right)
    assert _proves(left, right)


def test_near_miss_plain_branches_still_prune_unread_columns():
    left = "SELECT q.c FROM (SELECT t.x AS k, 1 AS c FROM t UNION ALL SELECT t.y AS k, 2 AS c FROM t) AS q"
    right = "SELECT q.c FROM (SELECT t.id AS k, 1 AS c FROM t UNION ALL SELECT t.x AS k, 2 AS c FROM t) AS q"
    assert _rows(left) == _rows(right)
    assert _proves(left, right)
