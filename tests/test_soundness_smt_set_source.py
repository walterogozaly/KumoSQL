"""A DISTINCT (or key-only GROUP BY) derived table read by the SMT encoding as an existence test.

Its columns used to be modeled as never NULL before ``resolve_set_sources`` checked that a join pins
them to non-NULL outer columns, so ``d.x IS NULL`` pruned a block that has a NULL row (S008-001 to 003),
and a constant NULL column made the test never hold. Each wrong pair returns different rows in DuckDB on
the database next to it; the equivalent near misses stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints, prove_equivalent_smt

SCHEMA = {"t": ["x", "y"], "u": ["x", "y"]}
DDL = {"t": "CREATE TABLE t (x BIGINT, y BIGINT)", "u": "CREATE TABLE u (x BIGINT, y BIGINT)"}
NULL_GROUP = {"t": [(None, 0), (None, 0), (1, 0)]}
NOT_NULL = {"t": TableConstraints(not_null=frozenset({"x", "y"}))}


def _bags_differ(left: str, right: str, rows: dict[str, list[tuple]]) -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for name, create in DDL.items():
        db.execute(create)
        for row in rows.get(name, []):
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    run = lambda sql: Counter(db.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]).fetchall())  # noqa: E731
    return run(left) != run(right)


# (left, right, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT d.x FROM (SELECT DISTINCT x FROM t) d WHERE d.x IS NULL",
        "SELECT x FROM t WHERE FALSE",
        NULL_GROUP,
        id="S008-001-distinct-null-row",
    ),
    pytest.param(
        "SELECT d.x FROM (SELECT x FROM t GROUP BY x) d WHERE d.x IS NULL",
        "SELECT x FROM t WHERE FALSE",
        NULL_GROUP,
        id="S008-002-null-group",
    ),
    pytest.param(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT x FROM t) d WHERE d.x IS NULL",
        "SELECT 0 AS n",
        NULL_GROUP,
        id="S008-003-count-over-the-null-row",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x, NULL AS z FROM t WHERE t.y = 1) d ON u.x = d.x",
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x, NULL AS z FROM t WHERE t.y = 2) d ON u.x = d.x",
        {"t": [(1, 1)], "u": [(1, 5)]},
        id="constant-null-column-of-the-joined-set",
    ),
]


@pytest.mark.parametrize("left,right,rows", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, rows):
    assert _bags_differ(left, right, rows)
    assert not prove_equivalent_smt(left, right, schema=SCHEMA).proven
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA).proven


# (left, right, constraints): equivalent, and proven by the SMT encoding before the fix
STILL_PROVEN = [
    pytest.param(
        "SELECT d.x FROM (SELECT DISTINCT x FROM t) d WHERE d.x IS NULL", "SELECT x FROM t WHERE FALSE", NOT_NULL,
        id="not-null-column-has-no-null-row",
    ),
    pytest.param(
        "SELECT d.x FROM (SELECT x FROM t GROUP BY x) d WHERE d.x IS NULL", "SELECT x FROM t WHERE FALSE", NOT_NULL,
        id="not-null-column-has-no-null-group",
    ),
    pytest.param(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT x FROM t) d WHERE d.x IS NULL", "SELECT 0 AS n", NOT_NULL,
        id="not-null-column-counts-zero",
    ),
    pytest.param(
        "SELECT d.x FROM (SELECT DISTINCT x FROM t WHERE x IS NOT NULL) d WHERE d.x IS NULL",
        "SELECT x FROM t WHERE FALSE",
        None,
        id="filtered-to-non-null",
    ),
    pytest.param(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT x FROM t WHERE x > 0) d WHERE d.x IS NULL",
        "SELECT 0 AS n",
        None,
        id="compared-so-never-null",
    ),
    pytest.param(
        "SELECT d.x FROM (SELECT DISTINCT x FROM t) d WHERE 1 = 0", "SELECT x FROM t WHERE FALSE", None,
        id="false-filter-over-a-set",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x FROM t) d ON u.x = d.x",
        "SELECT u.y FROM u WHERE u.x IN (SELECT x FROM t)",
        None,
        id="distinct-join-is-a-semijoin",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT x FROM t GROUP BY x) d ON u.x = d.x",
        "SELECT u.y FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.x = u.x)",
        None,
        id="grouped-join-is-a-semijoin",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x, y FROM t) d ON u.x = d.x AND u.y = d.y",
        "SELECT u.y FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.x = u.x AND t.y = u.y)",
        None,
        id="join-on-every-column",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x, 1 AS z FROM t) d ON u.x = d.x",
        "SELECT u.y FROM u WHERE u.x IN (SELECT x FROM t)",
        None,
        id="constant-column-is-pinned",
    ),
    pytest.param(
        "SELECT u.y FROM u JOIN (SELECT DISTINCT x FROM t WHERE y > 3) d ON u.x = d.x WHERE u.y > 0",
        "SELECT u.y FROM u WHERE u.y > 0 AND u.x IN (SELECT x FROM t WHERE y > 3)",
        None,
        id="filtered-semijoin",
    ),
]


@pytest.mark.parametrize("left,right,constraints", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right, constraints):
    assert prove_equivalent_smt(left, right, schema=SCHEMA, constraints=constraints).proven
