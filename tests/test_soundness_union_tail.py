"""Wrong proofs that dropped a LIMIT or OFFSET over a whole UNION, kept as regression cases.

``x IN (A UNION ALL B LIMIT 0)`` is never true, yet splitting it into ``x IN (A) OR x IN (B)`` lost the
LIMIT; ``SELECT DISTINCT a FROM (A UNION ALL B LIMIT 1)`` read as ``A UNION DISTINCT B`` lost it the same
way. Each pair returns different rows on the database below, so neither prover may call it equivalent.
Near misses without such a tail stay proven, so the fix cannot just decline every union.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import prove_equivalent_smt

DDL = {"t": "CREATE TABLE t (x INTEGER, y INTEGER)", "u": "CREATE TABLE u (k INTEGER)"}
ROWS = {"t": [(2, 4), (2, 5)], "u": [(1,)]}
SPLIT = "SELECT (2 IN (SELECT x FROM t) OR 2 IN (SELECT k FROM u)) AS e"


def _bags(left: str, right: str, dialect: str) -> tuple[Counter, Counter]:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for name, create in DDL.items():
        db.execute(create)
        for row in ROWS[name]:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    run = lambda sql: Counter(db.execute(sqlglot.transpile(sql, read=dialect, write="duckdb")[0]).fetchall())  # noqa: E731
    return run(left), run(right)


WRONG_PROOFS = [
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u LIMIT 0) AS e", SPLIT, id="S009-005-in-over-union-limit-0"),
    pytest.param(
        "SELECT 2 NOT IN (SELECT x FROM t UNION ALL SELECT k FROM u LIMIT 0) AS e",
        "SELECT NOT (2 IN (SELECT x FROM t) OR 2 IN (SELECT k FROM u)) AS e",
        id="not-in-over-union-limit-0",
    ),
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u ORDER BY 1 LIMIT 1) AS e", SPLIT, id="in-over-union-order-limit"),
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u ORDER BY 1 DESC LIMIT 5 OFFSET 2) AS e", SPLIT, id="in-over-union-offset"),
    pytest.param(
        "SELECT y FROM t WHERE x IN (SELECT x FROM t UNION ALL SELECT k FROM u LIMIT 0)",
        "SELECT y FROM t WHERE x IN (SELECT x FROM t) OR x IN (SELECT k FROM u)",
        id="in-over-union-limit-in-where",
    ),
    pytest.param(
        "SELECT 2 IN (SELECT k FROM u UNION ALL (SELECT k FROM u UNION ALL SELECT x FROM t LIMIT 0)) AS e",
        "SELECT (2 IN (SELECT k FROM u) OR 2 IN (SELECT k FROM u) OR 2 IN (SELECT x FROM t)) AS e",
        id="in-over-nested-union-limit",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM (SELECT x AS a FROM t UNION ALL SELECT k FROM u LIMIT 1) AS d",
        "SELECT x AS a FROM t UNION DISTINCT SELECT k FROM u",
        id="distinct-over-union-all-limit",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM (SELECT x AS a FROM t UNION ALL SELECT k FROM u ORDER BY a LIMIT 1) AS d",
        "SELECT x AS a FROM t UNION DISTINCT SELECT k FROM u",
        id="distinct-over-union-all-order-limit",
    ),
]


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
@pytest.mark.parametrize("left,right", WRONG_PROOFS)
def test_union_tails_are_not_dropped(left, right, dialect):
    lhs, rhs = _bags(left, right, dialect)
    assert lhs != rhs
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, dialect=dialect).proven, prove.__name__


STILL_PROVEN = [
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u) AS e", SPLIT, id="in-over-union-all"),
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION DISTINCT SELECT k FROM u) AS e", SPLIT, id="in-over-union-distinct"),
    pytest.param(
        "SELECT 2 NOT IN (SELECT x FROM t UNION ALL SELECT k FROM u) AS e",
        "SELECT NOT (2 IN (SELECT x FROM t) OR 2 IN (SELECT k FROM u)) AS e",
        id="not-in-over-union-all",
    ),
    pytest.param(
        "SELECT y FROM t WHERE x IN (SELECT x FROM t UNION ALL SELECT k FROM u)",
        "SELECT y FROM t WHERE x IN (SELECT x FROM t) OR x IN (SELECT k FROM u)",
        id="in-over-union-in-where",
    ),
    pytest.param(
        "SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u UNION ALL SELECT y FROM t) AS e",
        "SELECT (2 IN (SELECT x FROM t) OR 2 IN (SELECT k FROM u) OR 2 IN (SELECT y FROM t)) AS e",
        id="in-over-three-branches",
    ),
    pytest.param("SELECT 2 IN ((SELECT x FROM t ORDER BY x) UNION ALL SELECT k FROM u) AS e", SPLIT, id="in-over-union-branch-order"),
    pytest.param("SELECT 2 IN (SELECT x FROM t UNION ALL SELECT k FROM u ORDER BY 1) AS e", SPLIT, id="in-over-union-order-without-limit"),
    pytest.param(
        "SELECT DISTINCT a FROM (SELECT x AS a FROM t UNION ALL SELECT k FROM u) AS d",
        "SELECT x AS a FROM t UNION DISTINCT SELECT k FROM u",
        id="distinct-over-union-all",
    ),
]


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_bare_unions_stay_proven(left, right, dialect):
    lhs, rhs = _bags(left, right, dialect)
    assert lhs == rhs
    assert prove_equivalent_algebraic(left, right, dialect=dialect).proven
