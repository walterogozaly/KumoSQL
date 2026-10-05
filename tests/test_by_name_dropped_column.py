"""A false proof from ``positionalize`` when a BY NAME branch drops a column (issue #518).

``A LEFT UNION ALL BY NAME B`` keeps only A's columns, and ``INNER`` / ``CORRESPONDING BY`` only the shared ones,
so a branch can lose columns. The rewrite to a positional set operation removed them from the branch's select list
in place, which is only right while the select does not depend on them: ``SELECT DISTINCT y AS x, id AS z`` keeps one
row per pair, but ``SELECT DISTINCT y AS x`` keeps one row per ``y``, and an ``ORDER BY z`` or ``QUALIFY`` on a dropped
alias pointed at nothing. Such a branch is now selected from by name, so its DISTINCT, LIMIT and filters run first.
Each pair below returns different rows (DuckDB, optimizer off).
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.set_operations import positional_sql_pair

SCHEMA = {"t": {"id": "INT64", "x": "INT64", "y": "INT64"}}
ROWS = [(1, 1, 5), (2, 2, 5), (3, 3, 6)]

# (BY NAME query, what BigQuery returns for it spelled positionally, the in-place rewrite that lost the DISTINCT)
CASES = [
    pytest.param(
        "SELECT x FROM t LEFT UNION ALL BY NAME SELECT DISTINCT y AS x, id AS z FROM t",
        "SELECT x FROM t UNION ALL SELECT x FROM (SELECT DISTINCT y AS x, id AS z FROM t)",
        "SELECT x FROM t UNION ALL SELECT DISTINCT y AS x FROM t",
        id="left-distinct-branch",
    ),
    pytest.param(
        "SELECT DISTINCT y, id FROM t INNER UNION ALL BY NAME SELECT x AS y FROM t",
        "SELECT y FROM (SELECT DISTINCT y, id FROM t) UNION ALL SELECT x AS y FROM t",
        "SELECT DISTINCT y FROM t UNION ALL SELECT x AS y FROM t",
        id="inner-distinct-first-branch",
    ),
    pytest.param(
        "SELECT x FROM t UNION ALL CORRESPONDING BY (x) SELECT DISTINCT y AS x, id AS z FROM t",
        "SELECT x FROM t UNION ALL SELECT x FROM (SELECT DISTINCT y AS x, id AS z FROM t)",
        "SELECT x FROM t UNION ALL SELECT DISTINCT y AS x FROM t",
        id="corresponding-by-distinct-branch",
    ),
]


def _rows(sql: str) -> Counter:
    db = duckdb.connect()
    db.execute("CREATE TABLE t(id BIGINT, x BIGINT, y BIGINT)")
    db.executemany("INSERT INTO t VALUES (?, ?, ?)", ROWS)
    return Counter(run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


@pytest.mark.parametrize("by_name, positional, lossy", CASES)
def test_a_branch_that_loses_a_column_keeps_its_distinct(by_name, positional, lossy):
    skip_if_unparseable(by_name, positional, lossy)
    assert _rows(positional) != _rows(lossy), "the database must separate the two spellings"
    rewritten, _, problem = positional_sql_pair(by_name, by_name)
    assert problem is None
    assert _rows(rewritten) == _rows(positional)
    assert not _proven(by_name, lossy)
    assert _proven(by_name, positional)


def test_an_order_by_on_a_dropped_alias_still_runs_before_the_cut():
    by_name = "SELECT x FROM t INNER UNION ALL BY NAME (SELECT y AS x, id AS z FROM t ORDER BY z DESC LIMIT 1)"
    positional = "SELECT x FROM t UNION ALL SELECT x FROM (SELECT y AS x, id AS z FROM t ORDER BY z DESC LIMIT 1)"
    skip_if_unparseable(by_name, positional)
    rewritten, _, problem = positional_sql_pair(by_name, by_name)
    assert problem is None
    assert _rows(rewritten) == _rows(positional)


def test_dropping_a_plain_column_stays_a_plain_projection():
    by_name = "SELECT x FROM t INNER UNION ALL BY NAME SELECT y AS x, id AS z FROM t"
    rewritten, _, problem = positional_sql_pair(by_name, by_name)
    assert problem is None
    assert "_by_name_" not in rewritten
    assert _proven(by_name, "SELECT x FROM t UNION ALL SELECT y AS x FROM t")
