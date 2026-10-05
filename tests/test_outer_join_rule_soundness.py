"""False proofs in the outer-join rules (issue #518), kept as regression cases.

Each pair returns different rows on the database next to it (DuckDB, optimizer off, see
``kumosql.duckdb_load.run_unoptimized``), so the prover may not call it equivalent. The near misses are equivalent
and stay proven, so a fix cannot just decline more.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402
from kumosql.join_rewrites import _fk_left_join_to_inner  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

DDL = {
    "t": "id BIGINT, x BIGINT, y BIGINT",
    "p": "id BIGINT, tid BIGINT, n BIGINT",
    "u": "k BIGINT, w BIGINT",
}
SCHEMA = {name: [c.split()[0] for c in columns.split(", ")] for name, columns in DDL.items()}
# p.tid references t.id and is NOT NULL; t.id is the key of t
FK = {
    "t": TableConstraints(keys=(("id",),), not_null=frozenset({"id"})),
    "p": TableConstraints(keys=(("id",),), not_null=frozenset({"id", "tid"}), foreign_keys=((("tid",), "t", ("id",)),)),
}


def _rows(pair, rows):
    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        for row in rows.get(name, []):
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    left, right = run_unoptimized(db, *pair)
    return Counter(left), Counter(right)


def _prove(left, right, constraints=None):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=constraints, dialect="bigquery")


# (left, right, rows on which they differ, constraints)
WRONG_PROOFS = [
    pytest.param(
        # the ON clause equates p.tid with t.x as well as with the referenced t.id: the parent row may fail the first
        "SELECT p.id, t.x FROM p LEFT JOIN t ON p.tid = t.x AND p.tid = t.id",
        "SELECT p.id, t.x FROM p JOIN t ON p.tid = t.x AND p.tid = t.id",
        {"t": [(1, 5, 0)], "p": [(10, 1, 0)]},
        FK,
        id="left-join-fk-with-a-second-parent-column",
    ),
]


@pytest.mark.parametrize("left,right,rows,constraints", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, rows, constraints):
    ran_left, ran_right = _rows((left, right), rows)
    assert ran_left != ran_right
    result = _prove(left, right, constraints)
    assert not result.proven, result.reason


STILL_PROVEN = [
    pytest.param(
        "SELECT p.id, t.x FROM p LEFT JOIN t ON p.tid = t.id",
        "SELECT p.id, t.x FROM p JOIN t ON p.tid = t.id",
        id="left-join-fk",
    ),
    pytest.param(
        "SELECT p.id, t.x FROM p LEFT JOIN t ON p.tid = t.id AND p.tid = t.id",
        "SELECT p.id, t.x FROM p JOIN t ON p.tid = t.id",
        id="left-join-fk-repeated-equality",
    ),
]


@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_near_misses_stay_proven(left, right):
    result = _prove(left, right, FK)
    assert result.proven, result.reason


def test_foreign_key_rule_needs_the_on_clause_to_equate_exactly_the_key():
    fk = {"p": [(("tid",), "t", ("id",))]}
    not_null = {"p": frozenset({"id", "tid"})}

    def rule(sql):
        return _fk_left_join_to_inner(sqlglot.parse_one(sql, read="bigquery"), not_null, fk)

    assert rule("SELECT p.id FROM p LEFT JOIN t ON p.tid = t.id") is not None
    assert rule("SELECT p.id FROM p LEFT JOIN t ON p.tid = t.id AND p.tid = t.id") is not None
    assert rule("SELECT p.id FROM p LEFT JOIN t ON p.tid = t.x AND p.tid = t.id") is None
    assert rule("SELECT p.id FROM p LEFT JOIN t ON p.tid = t.id AND p.tid = t.x") is None
