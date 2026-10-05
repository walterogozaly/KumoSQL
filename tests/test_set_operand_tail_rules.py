"""False proofs from set-operation rules that stepped through the parentheses of an operand and lost its tail (issue #518).

In sqlglot ``((SELECT x FROM t) ORDER BY x LIMIT 1)`` keeps its ORDER BY / LIMIT on the parentheses (a ``Subquery``
with no alias). ``merge_same_source`` and ``set_operation_to_exists`` (``setop_rules``) and
``push_filter_into_set_operation`` (``union_filter_rules``) unwrapped such parentheses to reach the select, so a
filter moved below the cut or the cut vanished. Every pair below returns different rows (DuckDB, optimizer off,
shows it on the database in ``ROWS``), so the prover may not call it equivalent; the near misses without the tail
stay proven, so the fix is not just a wider refusal.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.setop_rules import merge_same_source, set_operation_to_exists
from kumosql.union_filter_rules import push_filter_into_set_operation

SCHEMA = {"t": {"x": "INT64", "y": "INT64"}, "u": {"k": "INT64"}}
ROWS = {"t": [(1, 1), (2, 1), (3, -1), (-1, -1), (5, 0)], "u": [(2,), (3,), (7,)]}
CUT = "((SELECT x FROM t{where}) ORDER BY x LIMIT 1)"


def _bag(db, sql: str) -> Counter:
    return Counter(run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _differ(left: str, right: str) -> bool:
    db = duckdb.connect()
    db.execute("CREATE TABLE t(x BIGINT, y BIGINT)")
    db.execute("CREATE TABLE u(k BIGINT)")
    for table, rows in ROWS.items():
        db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in rows[0])})", rows)
    return _bag(db, left) != _bag(db, right)


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


def _tail(where: str = "") -> str:
    return CUT.format(where=where)


# (tailed query, the query it was wrongly merged or pushed into)
WRONG_PROOFS = [
    pytest.param(
        f"{_tail(' WHERE y > 0')} UNION DISTINCT SELECT x FROM t WHERE y < 0",
        "SELECT DISTINCT x FROM t WHERE (y > 0) OR (y < 0)",
        id="union-to-filter",
    ),
    pytest.param(
        f"{_tail()} INTERSECT DISTINCT SELECT x FROM t WHERE x < 5",
        "SELECT DISTINCT x FROM t WHERE x < 5",
        id="intersect-to-filter",
    ),
    pytest.param(
        f"{_tail()} INTERSECT DISTINCT SELECT k FROM u",
        "SELECT DISTINCT x FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k IS NOT DISTINCT FROM t.x)",
        id="intersect-to-exists",
    ),
    pytest.param(
        f"SELECT k FROM u EXCEPT DISTINCT {_tail()}",
        "SELECT DISTINCT k FROM u WHERE NOT EXISTS (SELECT 1 FROM t WHERE t.x IS NOT DISTINCT FROM u.k)",
        id="except-to-not-exists",
    ),
    pytest.param(
        f"SELECT k FROM (SELECT k FROM u EXCEPT DISTINCT {_tail()}) AS d WHERE d.k > 0",
        "SELECT k FROM (SELECT k FROM u WHERE k > 0 EXCEPT DISTINCT SELECT x FROM t WHERE x > 0) AS d",
        id="filter-pushed-below-the-cut",
    ),
    pytest.param(
        f"SELECT k FROM (SELECT k FROM u INTERSECT DISTINCT {_tail()}) AS d WHERE d.k > 2",
        "SELECT k FROM (SELECT k FROM u WHERE k > 2 INTERSECT DISTINCT SELECT x FROM t WHERE x > 2) AS d",
        id="intersect-filter-pushed-below-the-cut",
    ),
]


@pytest.mark.parametrize("left, right", WRONG_PROOFS)
def test_a_tail_on_an_operand_is_not_dropped(left, right):
    skip_if_unparseable(left, right)
    assert _differ(left, right), "the database must separate the pair"
    assert not _proven(left, right)


# The same pairs without the tail are equivalent and stay proven.
NEAR_MISSES = [
    pytest.param(
        "(SELECT x FROM t WHERE y > 0) UNION DISTINCT SELECT x FROM t WHERE y < 0",
        "SELECT DISTINCT x FROM t WHERE (y > 0) OR (y < 0)",
        id="union-to-filter",
    ),
    pytest.param(
        "(SELECT x FROM t) INTERSECT DISTINCT SELECT x FROM t WHERE x < 5",
        "SELECT DISTINCT x FROM t WHERE x < 5",
        id="intersect-to-filter",
    ),
    pytest.param(
        "SELECT k FROM (SELECT k FROM u EXCEPT DISTINCT (SELECT x FROM t)) AS d WHERE d.k > 0",
        "SELECT k FROM (SELECT k FROM u WHERE k > 0 EXCEPT DISTINCT SELECT x FROM t WHERE x > 0) AS d",
        id="filter-pushed-into-the-branches",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_the_same_pair_without_a_tail_still_proves(left, right):
    assert not _differ(left, right)
    assert _proven(left, right)


def _select(sql: str):
    return sqlglot.parse_one(sql, read="bigquery")


def test_the_rules_leave_a_tailed_operand_alone():
    for sql in (
        f"{_tail(' WHERE y > 0')} UNION DISTINCT SELECT x FROM t WHERE y < 0",
        f"{_tail()} INTERSECT DISTINCT SELECT x FROM t WHERE x < 5",
    ):
        assert merge_same_source(_select(sql)) is None
        assert set_operation_to_exists(_select(sql)) is None
    assert push_filter_into_set_operation(_select(f"SELECT x FROM ({_tail()} UNION ALL SELECT k FROM u) AS d WHERE d.x > 0")) is None


def test_the_rules_still_read_plain_parentheses():
    assert merge_same_source(_select("(SELECT x FROM t WHERE y > 0) UNION DISTINCT (SELECT x FROM t WHERE y < 0)")) is not None
    assert push_filter_into_set_operation(_select("SELECT x FROM ((SELECT x FROM t) UNION ALL SELECT k FROM u) AS d WHERE d.x > 0")) is not None
