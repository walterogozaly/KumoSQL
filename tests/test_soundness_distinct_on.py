"""DISTINCT, DISTINCT ON and grouping sets are three different things; wrong proofs that mixed them up.

``DISTINCT ON (k)`` keeps one row per ``k``, the first in its ``ORDER BY``: it picks values, so it is not
duplicate removal, and its ORDER BY is not a result order a bag comparison may drop. A ``GROUP BY`` with
ROLLUP, CUBE or GROUPING SETS outputs a row per grouping set (a repeated set repeats its rows, and the
empty set gives a row even over no input), so its keys are not one row per value. sqlglot keeps those
extensions as items of ``group.expressions``, where the older ``group.args`` guards never saw them.

Each pair returns different rows on the database next to it (DuckDB, optimizer off), so no prover may
call it equivalent. Near misses that are equivalent stay proven, so a fix cannot just decline more.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.canonical_rules import canonicalize
from kumosql.duckdb_load import run_unoptimized
from kumosql.equivalence import prove_equivalent
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"t": ["x", "y"], "u": ["k"]}
X_NOT_NULL = {"t": TableConstraints(not_null=frozenset({"x"}))}


def _bags(sqls: list[str], rows: dict[str, list[tuple]], dialect: str, x_not_null: bool = False) -> list[Counter]:
    db = duckdb.connect()
    db.execute(f"CREATE TABLE t (x INTEGER{' NOT NULL' if x_not_null else ''}, y INTEGER)")
    db.execute("CREATE TABLE u (k INTEGER)")
    for name, table_rows in rows.items():
        for row in table_rows:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    queries = [sqlglot.transpile(sql, read=dialect, write="duckdb")[0] for sql in sqls]
    return [Counter(result) for result in run_unoptimized(db, *queries)]


def _bags_differ(left: str, right: str, rows: dict[str, list[tuple]], dialect: str, x_not_null: bool = False) -> bool:
    first, second = _bags([left, right], rows, dialect, x_not_null)
    return first != second


# (left, right, prover options, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT DISTINCT ON (x) y FROM t) d",
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT y FROM t) AS d",
        {"dialect": "duckdb"},
        {"t": [(1, 1), (1, 2)]},
        id="S009-009-distinct-on-source-is-not-duplicate-removal",
    ),
    pytest.param(
        "SELECT DISTINCT ON (x) y FROM t ORDER BY x, y",
        "SELECT DISTINCT ON (x) y FROM t",
        {"dialect": "duckdb", "schema": SCHEMA},
        {"t": [(1, 2), (1, 1)]},
        id="S009-012-distinct-on-keeps-its-order",
    ),
    pytest.param(
        "SELECT * FROM (SELECT DISTINCT ON (x) y FROM t ORDER BY x, y) AS d",
        "SELECT * FROM (SELECT DISTINCT ON (x) y FROM t) AS d",
        {"dialect": "duckdb", "schema": SCHEMA},
        {"t": [(1, 2), (1, 1)]},
        id="derived-distinct-on-keeps-its-order",
    ),
    pytest.param(
        # Without an ORDER BY the row kept is arbitrary: the filter can see one that fails it.
        "SELECT d.x, d.y FROM (SELECT DISTINCT ON (x) x, y FROM t) d WHERE d.y = 1",
        "SELECT d.x, d.y FROM (SELECT DISTINCT ON (x) x, y FROM t WHERE y = 1) d",
        {"dialect": "duckdb", "schema": SCHEMA},
        {"t": [(1, 2), (1, 1)]},
        id="filter-on-a-non-key-output-stays-above-distinct-on",
    ),
    pytest.param(
        "SELECT k FROM u WHERE EXISTS (SELECT 1 FROM t GROUP BY GROUPING SETS ((x + 0), ()))",
        "SELECT k FROM u WHERE EXISTS (SELECT 1 FROM t)",
        {},
        {"u": [(1,)]},
        id="empty-grouping-set-has-a-row-in-an-exists",
    ),
    pytest.param(
        "SELECT COUNT(*) AS c FROM t GROUP BY ROLLUP (TRUE)",
        "SELECT COUNT(*) AS c FROM t GROUP BY TRUE",
        {"group_by_constants": True},
        {},
        id="rollup-of-a-constant-is-not-a-constant-grouping",
    ),
]


@pytest.mark.parametrize("left,right,options,rows", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, options, rows):
    skip_if_unparseable(left, right, dialect=options.get("dialect", "bigquery"))
    assert _bags_differ(left, right, rows, options.get("dialect", "bigquery"))
    assert not prove_equivalent_algebraic(left, right, **options).proven


def test_a_null_guard_on_an_aggregate_stays_under_an_empty_grouping_set():
    # x is NOT NULL, yet the empty grouping set's group over no rows has MAX(x) NULL
    left = "SELECT MAX(x) AS m FROM t GROUP BY GROUPING SETS ((y + 0), ()) HAVING MAX(x) IS NOT NULL"
    right = "SELECT MAX(x) AS m FROM t GROUP BY GROUPING SETS ((y + 0), ())"
    skip_if_unparseable(left, right)
    assert _bags_differ(left, right, {}, "bigquery", x_not_null=True)
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=X_NOT_NULL).proven


def test_the_structural_prover_keeps_the_order_of_distinct_on():
    left, right = "SELECT DISTINCT ON (x) y FROM t ORDER BY x, y", "SELECT DISTINCT ON (x) y FROM t"
    assert _bags_differ(left, right, {"t": [(1, 2), (1, 1)]}, "duckdb")
    assert not prove_equivalent(left, right).proven
    assert prove_equivalent(left, left).proven
    assert prove_equivalent("SELECT x, y FROM t ORDER BY x, y", "SELECT x, y FROM t").proven


# The local canonical rules (``canonicalize``): the rewrite must return the rows the query does.
CANONICAL_KEEPS_ROWS = [
    pytest.param("SELECT DISTINCT ON (x) y FROM t ORDER BY x, y", "duckdb", {"t": [(1, 2), (1, 1)]}, id="S009-012-order-under-distinct-on"),
    pytest.param("SELECT DISTINCT x FROM t GROUP BY x WITH ROLLUP", "mysql", {"t": [(None, 1)]}, id="S009-010-distinct-over-rollup"),
    pytest.param("SELECT DISTINCT x FROM t GROUP BY x, GROUPING SETS ((y), (y))", "bigquery", {"t": [(2, 4), (2, 5)]}, id="S009-010-repeated-grouping-sets"),
    pytest.param("SELECT COUNT(*) AS c FROM t GROUP BY ROLLUP (x) HAVING COUNT(*) > 0", "bigquery", {}, id="having-count-under-rollup"),
]


@pytest.mark.parametrize("sql,dialect,rows", CANONICAL_KEEPS_ROWS)
def test_canonical_rules_keep_the_rows(sql, dialect, rows):
    rewritten = canonicalize(sql, dialect)
    if dialect == "mysql":  # DuckDB spells MySQL's WITH ROLLUP as ROLLUP (..)
        sql, rewritten, dialect = (q.replace("GROUP BY x WITH ROLLUP", "GROUP BY ROLLUP (x)") for q in (sql, rewritten, "duckdb"))
    before, after = _bags([sql, rewritten], rows, dialect)
    assert before == after, rewritten


def test_canonical_rules_still_apply_to_plain_selects():
    assert canonicalize("SELECT x, y FROM t ORDER BY x, y", "duckdb") == "SELECT x, y FROM t"
    assert canonicalize("SELECT DISTINCT x FROM t GROUP BY x") == "SELECT x FROM t GROUP BY x"
    assert canonicalize("SELECT x, COUNT(*) AS c FROM t GROUP BY x HAVING COUNT(*) > 0") == "SELECT x, COUNT(*) AS c FROM t GROUP BY x"


STILL_PROVEN = [
    pytest.param(
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT DISTINCT y FROM t) d",
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT y FROM t) AS d",
        {},
        id="distinct-source-under-a-distinct-count",
    ),
    pytest.param(
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT DISTINCT y FROM t) d",
        "SELECT COUNT(DISTINCT d.y) AS n FROM (SELECT y FROM t) AS d",
        {"dialect": "duckdb"},
        id="distinct-source-under-a-distinct-count-duckdb",
    ),
    pytest.param(
        "SELECT MAX(d.y) AS n FROM (SELECT y FROM t GROUP BY y) d",
        "SELECT MAX(d.y) AS n FROM (SELECT y FROM t) AS d",
        {},
        id="grouped-source-under-max",
    ),
    pytest.param(
        "SELECT DISTINCT x, COUNT(*) AS c FROM t GROUP BY x",
        "SELECT x, COUNT(*) AS c FROM t GROUP BY x",
        {},
        id="distinct-over-plain-group-keys",
    ),
    pytest.param("SELECT x, y FROM t ORDER BY x, y", "SELECT x, y FROM t", {}, id="order-without-limit"),
    pytest.param("SELECT x, y FROM t ORDER BY x, y", "SELECT x, y FROM t", {"dialect": "duckdb"}, id="order-without-limit-duckdb"),
    pytest.param(
        "SELECT * FROM (SELECT x, y FROM t ORDER BY y) AS d",
        "SELECT * FROM (SELECT x, y FROM t) AS d",
        {"dialect": "duckdb", "schema": SCHEMA},
        id="derived-order-without-limit",
    ),
    pytest.param(
        "SELECT d.y FROM (SELECT DISTINCT y FROM t) d WHERE d.y = 1",
        "SELECT d.y FROM (SELECT DISTINCT y FROM t WHERE y = 1) d",
        {},
        id="filter-into-a-distinct-source",
    ),
    pytest.param(
        "SELECT d.x, d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y) d",
        "SELECT d.x, d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y) d",
        {"dialect": "duckdb", "schema": SCHEMA},
        id="same-distinct-on-query",
    ),
    pytest.param(
        "SELECT k FROM u WHERE EXISTS (SELECT 1 FROM t GROUP BY x)",
        "SELECT k FROM u WHERE EXISTS (SELECT 1 FROM t)",
        {},
        id="plain-group-in-an-exists",
    ),
    pytest.param(
        "SELECT COUNT(*) AS c FROM t GROUP BY 1, x",
        "SELECT COUNT(*) AS c FROM t GROUP BY x",
        {"group_by_constants": True},
        id="constant-grouping-dropped",
    ),
    pytest.param(
        "SELECT x, COUNT(*) AS c FROM t GROUP BY ROLLUP (x)",
        "SELECT x, COUNT(*) AS c FROM t GROUP BY x UNION ALL SELECT NULL AS x, COUNT(*) AS c FROM t",
        {},
        id="rollup-as-a-union",
    ),
]


@pytest.mark.parametrize("left,right,options", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right, options):
    assert prove_equivalent_algebraic(left, right, **options).proven
