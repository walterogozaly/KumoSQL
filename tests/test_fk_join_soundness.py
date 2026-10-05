"""False proofs in the foreign-key join elimination (issue #518), each with near misses that must still hold.

1. ``p JOIN t ON p.tid = t.x AND p.tid = t.id`` was read as the foreign-key join ``p.tid = t.id``: the second
   equality overwrote the first in the rule's column map, so the join was dropped although the parent row can
   fail ``p.tid = t.x``.
2. ``a LEFT JOIN p ON .. JOIN t AS par ON p.tid = par.id`` dropped the join to ``par`` because ``p.tid`` is
   NOT NULL and references ``t.id``, but the rows ``p`` is null-extended into have a NULL ``tid`` and the inner join
   removes them.

DuckDB (optimizer off, ``kumosql.duckdb_load.run_unoptimized``) is the witness.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402
from kumosql.fk_rules import drop_fk_join  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

DDL = {"t": "id BIGINT, x BIGINT", "p": "id BIGINT, tid BIGINT, other BIGINT"}
SCHEMA = {name: [c.split()[0] for c in columns.split(", ")] for name, columns in DDL.items()}
FK = {
    "t": TableConstraints(keys=(("id",),), not_null=frozenset({"id"})),
    "p": TableConstraints(
        keys=(("id",),), not_null=frozenset({"id", "tid", "other"}), foreign_keys=((("tid",), "t", ("id",)), (("other",), "t", ("id",)))
    ),
}
# t.x = 5 for id 1 but x = id for id 2; p 10 points at t 1 and 2 (tid, other); t 3 has no p row
ROWS = {"t": [(1, 5), (2, 2), (3, 3)], "p": [(10, 1, 2), (11, 2, 1), (12, 2, 2)]}


def _run(*queries):
    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        for row in ROWS[name]:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    return [Counter(rows) for rows in run_unoptimized(db, *queries)]


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=FK, dialect="bigquery")


def _rule(sql):
    parsed = sqlglot.parse_one(sql, read="bigquery")
    return drop_fk_join(parsed, {"t": [("id",)]}, {"p": frozenset({"tid", "other"})}, {"p": [(("tid",), "t", ("id",))]})


WRONG = [
    pytest.param(
        "SELECT p.id FROM p JOIN t ON p.tid = t.x AND p.tid = t.id",  # the parent row may fail p.tid = t.x
        "SELECT p.id FROM p",
        id="second-parent-column",
    ),
    pytest.param("SELECT p.id FROM p JOIN t ON p.tid = t.id AND p.tid = t.x", "SELECT p.id FROM p", id="second-parent-column-last"),
    pytest.param("SELECT p.id FROM p JOIN t ON p.tid = t.id AND p.other = t.id", "SELECT p.id FROM p", id="two-child-columns"),
    pytest.param("SELECT p.id FROM p JOIN t ON p.other = t.id AND p.tid = t.id", "SELECT p.id FROM p", id="two-child-columns-first"),
    pytest.param("SELECT p.id FROM t JOIN p ON p.tid = t.x AND p.tid = t.id", "SELECT p.id FROM p", id="parent-first-in-from"),
    pytest.param(
        "SELECT a.id FROM t AS a LEFT JOIN p ON p.tid = a.id JOIN t AS par ON p.tid = par.id",
        "SELECT a.id FROM t AS a LEFT JOIN p ON p.tid = a.id",
        id="child-null-extended-by-its-left-join",
    ),
    pytest.param(
        "SELECT a.id FROM p RIGHT JOIN t AS a ON p.tid = a.id JOIN t AS par ON p.tid = par.id",
        "SELECT a.id FROM p RIGHT JOIN t AS a ON p.tid = a.id",
        id="child-null-extended-by-a-later-right-join",
    ),
    pytest.param(
        "SELECT a.id FROM p FULL JOIN t AS a ON p.tid = a.id JOIN t AS par ON p.tid = par.id",
        "SELECT a.id FROM p FULL JOIN t AS a ON p.tid = a.id",
        id="child-null-extended-by-a-full-join",
    ),
]


@pytest.mark.parametrize("left,right", WRONG)
def test_pairs_that_differ_are_never_proven(left, right):
    ran_left, ran_right = _run(left, right)
    assert ran_left != ran_right
    assert not _prove(left, right).proven
    assert _rule(left) is None


STILL_PROVEN = [
    # exactly the foreign key, once or twice
    pytest.param("SELECT p.id FROM p JOIN t ON p.tid = t.id", "SELECT p.id FROM p", id="foreign-key"),
    pytest.param("SELECT p.id FROM p JOIN t ON p.tid = t.id AND p.tid = t.id", "SELECT p.id FROM p", id="foreign-key-repeated"),
    # the child is a real row: an inner chain, or a WHERE test that removes the null-extended rows
    pytest.param(
        "SELECT p.id FROM p JOIN t AS a ON p.tid = a.id JOIN t AS par ON p.tid = par.id",
        "SELECT p.id FROM p JOIN t AS a ON p.tid = a.id",
        id="child-in-an-inner-chain",
    ),
    pytest.param(
        "SELECT a.id FROM t AS a LEFT JOIN p ON p.tid = a.id JOIN t AS par ON p.tid = par.id WHERE p.id IS NOT NULL",
        "SELECT a.id FROM t AS a LEFT JOIN p ON p.tid = a.id WHERE p.id IS NOT NULL",
        id="where-removes-the-null-extended-rows",
    ),
]


@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_near_misses_stay_proven(left, right):
    ran_left, ran_right = _run(left, right)
    assert ran_left == ran_right
    assert _prove(left, right).proven
