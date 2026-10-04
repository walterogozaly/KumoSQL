"""``x IN (SELECT y ..)`` becomes ``EXISTS`` only where ``x`` cannot be NULL, which a global aggregate breaks.

Declared NOT NULL columns make the IN test two-valued, but a bare column in an aggregate without GROUP BY
(accepted by SQLite and MySQL) reads NULL when there are no input rows: ``NULL IN (1)`` is NULL while the
``EXISTS`` form is FALSE. SQLite shows the difference on an empty ``t``.
"""

import sqlite3

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints

CONSTRAINTS = {"t": TableConstraints(not_null=frozenset({"x"})), "u": TableConstraints(not_null=frozenset({"y"}))}
EXISTS_FORM = "SELECT COUNT(*) AS c, EXISTS(SELECT 1 FROM u WHERE u.y = t.x) AS d FROM t"


def _rows(sql: str) -> list[tuple]:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (x INTEGER NOT NULL)")
    db.execute("CREATE TABLE u (y INTEGER NOT NULL)")
    db.execute("INSERT INTO u VALUES (1)")
    return db.execute(sql).fetchall()


@pytest.mark.parametrize(
    "left",
    [
        pytest.param("WITH q AS (SELECT t.x AS x FROM t) SELECT COUNT(*) AS c, q.x IN (SELECT u.y FROM u) AS d FROM q", id="through-a-cte"),
        pytest.param("SELECT COUNT(*) AS c, t.x IN (SELECT u.y FROM u) AS d FROM t", id="direct"),
    ],
)
@pytest.mark.parametrize("dialect", ["sqlite", "mysql"])
def test_in_beside_a_global_aggregate_is_not_exists(left, dialect):
    assert _rows(left) != _rows(EXISTS_FORM)
    assert not prove_equivalent_algebraic(left, EXISTS_FORM, dialect=dialect, constraints=CONSTRAINTS).proven


@pytest.mark.parametrize(
    "left,right",
    [
        pytest.param("SELECT t.x IN (SELECT u.y FROM u) AS d FROM t", "SELECT EXISTS(SELECT 1 FROM u WHERE u.y = t.x) AS d FROM t", id="no-aggregate"),
        pytest.param(
            "SELECT t.x, COUNT(*) AS c, t.x IN (SELECT u.y FROM u) AS d FROM t GROUP BY t.x",
            "SELECT t.x, COUNT(*) AS c, EXISTS(SELECT 1 FROM u WHERE u.y = t.x) AS d FROM t GROUP BY t.x",
            id="grouped-key",
        ),
    ],
)
def test_in_without_a_global_aggregate_stays_proven(left, right):
    assert prove_equivalent_algebraic(left, right, dialect="sqlite", constraints=CONSTRAINTS).proven
