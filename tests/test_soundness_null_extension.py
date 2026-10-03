"""A derived table's computed output must not be inlined where a later outer join can NULL-extend it.

``t JOIN (SELECT COALESCE(u.k, 7) AS v FROM u) d ON TRUE RIGHT JOIN u b ON FALSE`` keeps ``b``'s rows
with ``d.v`` NULL, while the inlined ``COALESCE(d.k, 7)`` gives 7 on them (S009-003). Every pair below
returns different rows on the database next to it, so no prover may call it equivalent. The near misses
(no join that pads the derived table, or an expression that is NULL on a padded row) stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import prove_equivalent_smt

DDL = ("CREATE TABLE t (x INTEGER, y INTEGER)", "CREATE TABLE u (k INTEGER)")
ROWS = {"t": [(2, 4), (2, 5)], "u": [(1,)]}
CO = "(SELECT COALESCE(u.k, 7) AS v FROM u)"
INLINED = "COALESCE(d.k, 7) AS v"


def _bags_differ(left: str, right: str) -> bool:
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    for create in DDL:
        db.execute(create)
    for name, rows in ROWS.items():
        for row in rows:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    queries = [sqlglot.transpile(sql, read="bigquery", write="duckdb")[0] for sql in (left, right)]
    a, b = run_unoptimized(db, *queries)
    return Counter(a) != Counter(b)


WRONG_PROOFS = [
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t JOIN {CO} d ON TRUE RIGHT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t JOIN u AS d ON TRUE RIGHT JOIN u AS b ON FALSE",
        id="S009-003-inner-joined-then-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t JOIN {CO} d ON TRUE FULL JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t JOIN u AS d ON TRUE FULL JOIN u AS b ON FALSE",
        id="inner-joined-then-full-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t CROSS JOIN {CO} d RIGHT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t CROSS JOIN u AS d RIGHT JOIN u AS b ON FALSE",
        id="cross-joined-then-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t, {CO} d RIGHT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t, u AS d RIGHT JOIN u AS b ON FALSE",
        id="comma-joined-then-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t RIGHT JOIN {CO} d ON TRUE FULL JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t RIGHT JOIN u AS d ON TRUE FULL JOIN u AS b ON FALSE",
        id="preserved-by-its-own-right-join-then-full-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t JOIN {CO} d ON TRUE LEFT JOIN u c ON TRUE RIGHT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t JOIN u AS d ON TRUE LEFT JOIN u c ON TRUE RIGHT JOIN u AS b ON FALSE",
        id="right-join-after-an-unrelated-left-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM {CO} d JOIN t ON TRUE RIGHT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM u AS d JOIN t ON TRUE RIGHT JOIN u AS b ON FALSE",
        id="from-item-then-right-join",
    ),
    pytest.param(
        "SELECT t.x, d.v, b.k FROM t JOIN (SELECT CASE WHEN u.k IS NULL THEN 1 ELSE 0 END AS v FROM u) d ON TRUE"
        " RIGHT JOIN u b ON FALSE",
        "SELECT t.x, CASE WHEN d.k IS NULL THEN 1 ELSE 0 END AS v, b.k FROM t JOIN u AS d ON TRUE RIGHT JOIN u AS b ON FALSE",
        id="case-over-is-null-then-right-join",
    ),
    pytest.param(
        f"SELECT t.x, b.k FROM t JOIN {CO} d ON TRUE RIGHT JOIN u b ON FALSE WHERE d.v IS NOT NULL",
        "SELECT t.x, b.k FROM t JOIN u AS d ON TRUE RIGHT JOIN u AS b ON FALSE WHERE COALESCE(d.k, 7) IS NOT NULL",
        id="read-in-where-after-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v FROM t JOIN (u b LEFT JOIN {CO} d ON FALSE) ON TRUE",
        f"SELECT t.x, {INLINED} FROM t JOIN (u b LEFT JOIN u AS d ON FALSE) ON TRUE",
        id="left-joined-inside-a-parenthesized-join",
    ),
]


@pytest.mark.parametrize("left, right", WRONG_PROOFS)
def test_padded_derived_expression_is_not_proven(left: str, right: str) -> None:
    assert _bags_differ(left, right)
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        for a, b in ((left, right), (right, left)):
            result = prove(a, b, dialect="bigquery", timeout_ms=3000)
            assert not result.status.name.startswith(("PROVEN", "BOUNDED")), (prove.__name__, result.reason)


NEAR_MISSES = [
    pytest.param(
        f"SELECT t.x, d.v FROM t JOIN {CO} d ON TRUE",
        f"SELECT t.x, {INLINED} FROM t JOIN u AS d ON TRUE",
        id="coalesce-inner-joined-no-outer-join",
    ),
    pytest.param(
        "SELECT t.x, d.v, b.k FROM t JOIN (SELECT u.k + 1 AS v FROM u) d ON TRUE RIGHT JOIN u b ON FALSE",
        "SELECT t.x, d.k + 1 AS v, b.k FROM t JOIN u AS d ON TRUE RIGHT JOIN u AS b ON FALSE",
        id="strict-expression-below-a-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v, b.k FROM t JOIN {CO} d ON TRUE LEFT JOIN u b ON FALSE",
        f"SELECT t.x, {INLINED}, b.k FROM t JOIN u AS d ON TRUE LEFT JOIN u AS b ON FALSE",
        id="later-left-join-pads-only-its-own-side",
    ),
    pytest.param(
        f"SELECT t.x, d.v FROM t RIGHT JOIN {CO} d ON t.x = d.v",
        f"SELECT t.x, {INLINED} FROM t RIGHT JOIN u AS d ON t.x = COALESCE(d.k, 7)",
        id="preserved-side-of-its-own-right-join",
    ),
    pytest.param(
        f"SELECT t.x, d.v FROM {CO} d LEFT JOIN t ON t.x = d.v",
        f"SELECT t.x, {INLINED} FROM u AS d LEFT JOIN t ON t.x = COALESCE(d.k, 7)",
        id="from-item-preserved-by-a-left-join",
    ),
    pytest.param(
        f"SELECT o.v, b.k FROM (SELECT d.v FROM t JOIN {CO} d ON TRUE) o RIGHT JOIN u b ON FALSE",
        "SELECT o.v, b.k FROM (SELECT COALESCE(d.k, 7) AS v FROM t JOIN u AS d ON TRUE) o RIGHT JOIN u b ON FALSE",
        id="computed-inside-a-derived-table-the-outer-query-pads",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_unpadded_derived_expression_stays_proven(left: str, right: str) -> None:
    assert not _bags_differ(left, right)
    result = prove_equivalent_algebraic(left, right, dialect="bigquery", timeout_ms=3000)
    assert result.status.name == "PROVEN_EQUIVALENT", result.reason
