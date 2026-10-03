"""Wrong proofs that drop or ignore a query's only (global) aggregate.

An aggregate without GROUP BY returns exactly one row, even over no input. Pruning its last aggregate
output turns it into a plain select with a row per input row, and reading ``WHERE FALSE`` under it as "no
rows" forgets the row it still returns. Each pair below returns different rows (DuckDB shows it on the
database next to it), so the prover may not call it equivalent; near misses that are equivalent stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import _fold_trivia, _prune_derived, _unwrap_projection, normalize, prove_equivalent_algebraic
from kumosql.canonical_rules import canonicalize

DDL = ("CREATE TABLE t (x INTEGER, y INTEGER)", "CREATE TABLE u (k INTEGER)")
WITNESS = {"t": [(2, 4), (2, 5)], "u": [(1,)]}
EMPTY = {"t": [], "u": []}


def _bags(left: str, right: str, rows: dict[str, list[tuple]]) -> tuple[Counter, Counter]:
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    for create in DDL:
        db.execute(create)
    for name, values in rows.items():
        for row in values:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    to_duckdb = lambda sql: sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]  # noqa: E731
    left_rows, right_rows = run_unoptimized(db, to_duckdb(left), to_duckdb(right))
    return Counter(left_rows), Counter(right_rows)


# (left, right, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d", "SELECT 7 AS c FROM t", WITNESS, id="S009-001-unread-count-pruned"
    ),
    pytest.param("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d", "SELECT 7 AS c FROM t", EMPTY, id="S009-001-empty-input"),
    pytest.param(
        "SELECT 0 IN (SELECT COUNT(*) FROM t WHERE FALSE) AS e", "SELECT FALSE AS e", WITNESS, id="S009-002-in-over-empty-input-count"
    ),
    pytest.param(
        "SELECT EXISTS(SELECT COUNT(*) FROM t WHERE FALSE) AS e", "SELECT FALSE AS e", WITNESS, id="S009-011-exists-over-empty-input-count"
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t",
        {"t": [(1, 1), (2, 2), (3, 3)]},
        id="S007-001-pruned-after-the-redundant-limit",
    ),
    pytest.param("SELECT d.c FROM (SELECT MAX(x) AS m, 7 AS c FROM t) d", "SELECT 7 AS c FROM t", WITNESS, id="unread-max-pruned"),
    pytest.param("SELECT d.c FROM (SELECT COUNT(x) AS n, 7 AS c FROM t) d", "SELECT 7 AS c FROM t", WITNESS, id="unread-count-column-pruned"),
    pytest.param(
        "SELECT d.c + 1 AS c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d", "SELECT 8 AS c FROM t", WITNESS, id="computed-over-constant-output"
    ),
    pytest.param(
        "SELECT e.c FROM (SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d) e", "SELECT 7 AS c FROM t", WITNESS, id="nested-derived"
    ),
    pytest.param("WITH d AS (SELECT COUNT(*) AS n, 7 AS c FROM t) SELECT c FROM d", "SELECT 7 AS c FROM t", WITNESS, id="through-a-cte"),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d",
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t GROUP BY x) d",
        {"t": [(1, 1), (2, 2)]},
        id="global-against-grouped",
    ),
    pytest.param(
        "SELECT x FROM t WHERE x IN (SELECT COUNT(*) + 2 FROM u WHERE FALSE)",
        "SELECT x FROM t WHERE FALSE",
        WITNESS,
        id="where-in-over-empty-input-count",
    ),
    # an outer select with no aggregate folded into a global aggregate (R019, a real optimizer bug pair)
    pytest.param("SELECT 'US' AS c FROM t", "SELECT 'US' AS c FROM (SELECT COUNT(*) AS seed FROM t) g", WITNESS, id="R019-constant-over-count"),
    pytest.param("SELECT 'US' AS c FROM t", "SELECT 'US' AS c FROM (SELECT COUNT(*) AS seed FROM t) g", EMPTY, id="R019-empty-input"),
    pytest.param("SELECT 1 AS a FROM (SELECT MAX(x) AS m FROM t) g", "SELECT 1 AS a FROM t", WITNESS, id="R019-constant-over-max"),
]


@pytest.mark.parametrize("left,right,rows", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, rows):
    left_bag, right_bag = _bags(left, right, rows)
    assert left_bag != right_bag
    assert not prove_equivalent_algebraic(left, right, dialect="bigquery").proven


STILL_PROVEN = [
    pytest.param("SELECT d.n FROM (SELECT COUNT(*) AS n, SUM(x) AS s FROM t) d", "SELECT COUNT(*) AS n FROM t", id="non-last-aggregate-pruned"),
    pytest.param("SELECT d.x FROM (SELECT x, COUNT(*) AS n FROM t GROUP BY x) d", "SELECT x FROM t GROUP BY x", id="grouped-outputs-pruned"),
    pytest.param("SELECT EXISTS(SELECT x FROM t WHERE FALSE) AS e", "SELECT FALSE AS e", id="exists-over-empty-plain-select"),
    pytest.param("SELECT 0 IN (SELECT x FROM t WHERE FALSE) AS e", "SELECT FALSE AS e", id="in-over-empty-plain-select"),
    pytest.param("SELECT EXISTS(SELECT COUNT(*) FROM t WHERE FALSE) AS e", "SELECT TRUE AS e", id="exists-over-global-aggregate"),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d",
        "SELECT d.c FROM (SELECT SUM(x) AS s, 7 AS c FROM t) d",
        id="either-aggregate-keeps-the-one-row",
    ),
    pytest.param("SELECT u.k FROM u, (SELECT COUNT(*) AS n FROM t) d", "SELECT u.k FROM u", id="cross-join-with-one-row"),
    pytest.param("SELECT d.n FROM (SELECT COUNT(*) AS n FROM t WHERE FALSE) d", "SELECT 0 AS n", id="count-over-no-input"),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d WHERE d.n > 0",
        "SELECT 7 AS c FROM t HAVING COUNT(*) > 0",
        id="filter-on-the-aggregate-becomes-having",
    ),
]


@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right):
    for rows in (WITNESS, EMPTY):
        left_bag, right_bag = _bags(left, right, rows)
        assert left_bag == right_bag
    assert prove_equivalent_algebraic(left, right, dialect="bigquery").proven


def _select(sql: str) -> sqlglot.exp.Select:
    return sqlglot.parse_one(sql, read="bigquery")


def test_pruning_keeps_an_aggregate_of_a_global_aggregate():
    assert normalize("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d", dialect="bigquery") != "SELECT 7 AS c FROM t"
    assert _unwrap_projection(_select("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d")) is None
    assert _unwrap_projection(_select("SELECT d.n FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d")).sql() == "SELECT COUNT(*) AS n FROM t"
    kept = _prune_derived(_select("SELECT 7 AS c FROM (SELECT 7 AS c, SUM(x) AS s, COUNT(*) AS n FROM t) AS d"))
    assert kept.sql() == "SELECT 7 AS c FROM (SELECT 7 AS c, SUM(x) AS s FROM t) AS d"
    pruned = _prune_derived(_select("SELECT d.s FROM (SELECT COUNT(*) AS n, SUM(x) AS s, 7 AS c FROM t) AS d"))
    assert pruned.sql() == "SELECT d.s FROM (SELECT SUM(x) AS s FROM t) AS d"


def test_canonical_merge_keeps_the_global_aggregate():
    assert canonicalize("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t) d") != "SELECT 7 AS c FROM t"
    assert canonicalize("SELECT d.n FROM (SELECT COUNT(*) AS n, SUM(x) AS s FROM t) d") == "SELECT COUNT(*) AS n FROM t"


def test_membership_over_a_global_aggregate_is_not_folded_to_false():
    for sql in ("SELECT 0 IN (SELECT COUNT(*) FROM t WHERE FALSE) AS e", "SELECT EXISTS(SELECT COUNT(*) FROM t WHERE FALSE) AS e"):
        assert _fold_trivia(_select(sql)).sql(dialect="bigquery") == sql
    for sql in (
        "SELECT EXISTS(SELECT x FROM t WHERE FALSE) AS e",
        "SELECT 0 IN (SELECT x FROM t WHERE FALSE) AS e",
        "SELECT EXISTS(SELECT COUNT(*) FROM t LIMIT 0) AS e",
    ):
        assert _fold_trivia(_select(sql)).sql(dialect="bigquery") == "SELECT FALSE AS e"
