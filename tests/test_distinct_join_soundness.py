"""False proofs in the distinct-join-to-EXISTS rule (issue #518), kept as regression cases.

A derived table that only dedups a key (``SELECT DISTINCT tid FROM p``) joined to a table is a semi-join, and the rule
rewrites it to ``EXISTS``. When that join was not the first one, the rule dropped the other conditions of its ON clause,
so ``... JOIN d ON d.tid = t.id AND t.x > 0`` was proven equal to the same query without ``AND t.x > 0``. Each wrong pair
returns different rows on the database next to it (DuckDB, optimizer off, see ``kumosql.duckdb_load.run_unoptimized``);
the near misses are equivalent and stay proven.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

DDL = {
    "t": "id BIGINT, x BIGINT, y BIGINT",
    "p": "id BIGINT, tid BIGINT, n BIGINT",
    "u": "k BIGINT, w BIGINT",
}
SCHEMA = {name: [c.split()[0] for c in columns.split(", ")] for name, columns in DDL.items()}
CONSTRAINTS = {
    "t": TableConstraints(keys=(("id",),), not_null=frozenset({"id"})),
    "p": TableConstraints(keys=(("id",),), not_null=frozenset({"id", "tid"})),
    "u": TableConstraints(keys=(("k",),), not_null=frozenset({"k"})),
}
ROWS = {
    "t": [(1, 5, 1), (2, -5, 1), (3, 5, 2)],
    "p": [(10, 1, 0), (11, 2, 0), (12, 2, 0), (13, 3, 0)],
    "u": [(1, 7), (2, -7)],
}
DERIVED = "(SELECT tid FROM p GROUP BY tid)"
DISTINCT = "(SELECT DISTINCT tid FROM p)"
FLAGGED = "(SELECT p.tid, 1 AS c FROM p GROUP BY p.tid)"


def _both(pair):
    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        for row in ROWS[name]:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    left, right = run_unoptimized(db, *pair)
    return Counter(left), Counter(right)


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=CONSTRAINTS, dialect="bigquery")


WRONG_PAIRS = [
    pytest.param(
        f"SELECT t.id FROM t JOIN {DISTINCT} AS d ON d.tid = t.id AND t.x > 0",
        f"SELECT t.id FROM t JOIN {DISTINCT} AS d ON d.tid = t.id",
        id="distinct-derived-table-with-a-filter-on-the-other-table",
    ),
    pytest.param(
        f"SELECT t.id FROM t JOIN {DERIVED} AS d ON d.tid = t.id AND t.x > 0",
        f"SELECT t.id FROM t JOIN {DERIVED} AS d ON d.tid = t.id",
        id="group-by-derived-table-with-a-filter-on-the-other-table",
    ),
    pytest.param(
        f"SELECT t.id FROM t JOIN u ON u.k = t.y JOIN {DERIVED} AS d ON d.tid = t.id AND u.w > 0",
        f"SELECT t.id FROM t JOIN u ON u.k = t.y JOIN {DERIVED} AS d ON d.tid = t.id",
        id="filter-on-a-third-table",
    ),
    pytest.param(
        f"SELECT t.id FROM t JOIN {FLAGGED} AS q ON q.tid = t.id AND q.c IS NULL",
        f"SELECT t.id FROM t JOIN {FLAGGED} AS q ON q.tid = t.id",
        id="filter-on-the-derived-table-itself",
    ),
]

STILL_PROVEN = [
    pytest.param(
        f"SELECT t.id FROM t JOIN {DERIVED} AS d ON d.tid = t.id AND t.x > 0",
        "SELECT t.id FROM t WHERE t.x > 0 AND EXISTS (SELECT 1 FROM p WHERE p.tid = t.id)",
        id="the-filter-moved-to-where-next-to-exists",
    ),
    pytest.param(
        f"SELECT t.id FROM t JOIN {DERIVED} AS d ON d.tid = t.id AND t.x > 0",
        f"SELECT t.id FROM t JOIN {DERIVED} AS d ON d.tid = t.id WHERE t.x > 0",
        id="the-filter-in-on-or-in-where",
    ),
    pytest.param(
        f"SELECT t.id FROM t JOIN u ON u.k = t.y JOIN {DERIVED} AS d ON d.tid = t.id AND u.w > 0",
        f"SELECT t.id FROM t JOIN u ON u.k = t.y JOIN {DERIVED} AS d ON d.tid = t.id WHERE u.w > 0",
        id="third-table-filter-in-on-or-in-where",
    ),
]


@pytest.mark.parametrize(("left", "right"), WRONG_PAIRS)
def test_the_database_tells_these_pairs_apart(left, right):
    a, b = _both((left, right))
    assert a != b


@pytest.mark.parametrize(("left", "right"), WRONG_PAIRS)
def test_pairs_that_differ_are_never_proven(left, right):
    assert not _prove(left, right).proven


@pytest.mark.parametrize(("left", "right"), STILL_PROVEN)
def test_near_misses_stay_proven_and_agree(left, right):
    assert _prove(left, right).proven
    a, b = _both((left, right))
    assert a == b
