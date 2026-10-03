"""COUNT over a regrouped aggregate counts the inner rows, it does not repeat the inner value.

``_collapse_aggregate`` drops a regrouping whose groups each hold one inner row. SUM, MIN and MAX of
that one value return it, but COUNT of it is 1 (or 0 when it is NULL), so an outer COUNT over an inner
COUNT is not the inner query. Found by the soundness fuzzer; DuckDB separates each pair below.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import prove_equivalent_smt

ROWS = [(0, 1, 5), (1, 1, 6), (2, 2, None)]

WRONG_PROOFS = [
    pytest.param("SELECT COUNT(x) AS n FROM t", "SELECT COUNT(g.n) AS n FROM (SELECT COUNT(x) AS n FROM t) AS g", id="global-count-of-count"),
    pytest.param(
        "SELECT x, COUNT(y) AS m FROM t GROUP BY x",
        "SELECT d.x, COUNT(d.n) AS m FROM (SELECT x, COUNT(y) AS n FROM t GROUP BY x) AS d GROUP BY d.x",
        id="grouped-count-of-count",
    ),
    pytest.param(
        "SELECT x, COUNT(*) AS m FROM t GROUP BY x",
        "SELECT d.x, COUNT(d.n) AS m FROM (SELECT x, COUNT(*) AS n FROM t GROUP BY x) AS d GROUP BY d.x",
        id="grouped-count-of-count-star",
    ),
    pytest.param(
        "SELECT 1 + COUNT(x) AS n FROM t", "SELECT 1 + COUNT(g.n) AS n FROM (SELECT COUNT(x) AS n FROM t) AS g", id="count-of-count-in-arithmetic"
    ),
]


def _bags(left: str, right: str) -> tuple[Counter, Counter]:
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE t (id INTEGER, x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?, ?)", ROWS)
    to_duckdb = lambda sql: sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]  # noqa: E731
    left_rows, right_rows = run_unoptimized(db, to_duckdb(left), to_duckdb(right))
    return Counter(left_rows), Counter(right_rows)


@pytest.mark.parametrize("left,right", WRONG_PROOFS)
def test_count_of_count_is_never_proven(left, right):
    left_bag, right_bag = _bags(left, right)
    assert left_bag != right_bag
    assert not prove_equivalent_algebraic(left, right, dialect="bigquery").proven
    assert not prove_equivalent_smt(left, right, dialect="bigquery").proven


@pytest.mark.parametrize(
    "left,right",
    [
        pytest.param(
            "SELECT x, COUNT(y) AS m FROM t GROUP BY x",
            "SELECT d.x, SUM(d.n) AS m FROM (SELECT x, COUNT(y) AS n FROM t GROUP BY x) AS d GROUP BY d.x",
            id="sum-of-count",
        ),
        pytest.param(
            "SELECT x, MAX(y) AS m FROM t GROUP BY x",
            "SELECT d.x, MAX(d.n) AS m FROM (SELECT x, MAX(y) AS n FROM t GROUP BY x) AS d GROUP BY d.x",
            id="max-of-max",
        ),
    ],
)
def test_one_value_regroupings_stay_proven(left, right):
    left_bag, right_bag = _bags(left, right)
    assert left_bag == right_bag
    assert prove_equivalent_algebraic(left, right, dialect="bigquery").proven
