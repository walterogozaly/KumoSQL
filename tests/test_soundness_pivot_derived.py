"""PIVOT and UNPIVOT modifiers must not disappear inside derived-table rewrites."""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.ast_utils import UnmodeledConstruct  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402

SCHEMA = {"t": ["id", "x", "y"]}
PIVOT = "SELECT * FROM (SELECT y, x FROM t) PIVOT (SUM(x) FOR y IN (1, 2))"
UNPIVOT = "SELECT * FROM (SELECT COUNT(*) AS c, MAX(x) AS m FROM t) AS d UNPIVOT (v FOR k IN (c, m))"
CASES = [
    pytest.param(PIVOT, "SELECT y, x FROM t", id="pivot-is-not-its-source"),
    pytest.param(
        PIVOT,
        "SELECT * FROM (SELECT y, x FROM t) PIVOT (SUM(x) FOR y IN (3, 4))",
        id="pivot-over-other-values",
    ),
    pytest.param(UNPIVOT, UNPIVOT + " LIMIT 1", id="unpivot-limit-changes-rows"),
    pytest.param(UNPIVOT, "SELECT COUNT(*) AS c, MAX(x) AS m FROM t", id="unpivot-is-not-its-source"),
    pytest.param("SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))", "SELECT * FROM t", id="table-pivot"),
    pytest.param("SELECT id FROM t UNPIVOT (v FOR k IN (x, y))", "SELECT id FROM t", id="table-unpivot"),
]


def _rows(*queries: str) -> list[Counter]:
    db = duckdb.connect()
    db.execute("CREATE TABLE t (id INT, x INT, y INT)")
    db.execute("INSERT INTO t VALUES (1, 4, 1), (2, 3, 2), (3, 2, 3)")
    return [Counter(rows) for rows in run_unoptimized(db, *queries)]


def _prove(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery")


@pytest.mark.parametrize(("left", "right"), CASES)
def test_pivot_and_unpivot_differences_are_not_proven(left, right):
    left_rows, right_rows = _rows(left, right)
    assert left_rows != right_rows
    assert not _prove(left, right).proven


@pytest.mark.parametrize("sql", [PIVOT, UNPIVOT, "SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))"])
def test_normalize_declines_a_pivot(sql):
    with pytest.raises(UnmodeledConstruct):
        normalize(sql, schema=SCHEMA)


def test_case_based_pivot_rewrite_still_proves():
    left = "SELECT id, SUM(CASE WHEN y = 1 THEN x END) AS a FROM t GROUP BY id"
    right = "SELECT id, SUM(IF(y = 1, x, NULL)) AS a FROM t GROUP BY id"
    assert _prove(left, right).proven
