"""Open false proofs: ``_union_all_branches`` reads through the parentheses of a cut operand (issue #518).

``algebraic_equivalence._union_all_branches`` steps through every ``Subquery``, so ``((SELECT x FROM t) ORDER BY x LIMIT 1)
UNION ALL ...`` is read as the select of ``t`` with the tail gone. Its callers (``_aligned_branches`` and with it
``_distribute``, ``_prune_union_all``, the aggregate splits in ``aggregate_rules``) then work on all of ``t``. The fix is to
return ``None`` for a ``Subquery`` with an ``order``, ``limit`` or ``offset``, as ``setop_rules._unwrap`` and
``union_filter_rules._branches`` do; it is in the algebraic core, which this sweep did not edit, so the tests that show
the bug are marked ``xfail(strict=True)``: the thread that fixes the function removes the marker (strict makes a fix
that forgets to fail loudly). Every pair returns different rows (DuckDB, optimizer off).
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized

SCHEMA = {"t": {"x": "INT64"}, "u": {"w": "INT64"}, "p": {"tid": "INT64"}}
ROWS = {"t": [(3,), (1,), (2,)], "u": [(5,)], "p": [(1,), (2,), (3,), (5,)]}
CUT = "((SELECT x FROM t){tail})"
PLAIN = "(SELECT x FROM t)"


def _bag(db, sql: str) -> Counter:
    return Counter(run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _differ(left: str, right: str) -> bool:
    db = duckdb.connect()
    for table, column in (("t", "x"), ("u", "w"), ("p", "tid")):
        db.execute(f"CREATE TABLE {table}({column} BIGINT)")
        db.executemany(f"INSERT INTO {table} VALUES (?)", ROWS[table])
    return _bag(db, left) != _bag(db, right)


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


# (reader of a derived UNION ALL whose first operand is cut, the same reader without the cut)
READERS = [
    pytest.param("SELECT d.x FROM ({union}) AS d", id="plain-select"),
    pytest.param("SELECT d.x, COUNT(*) AS c FROM ({union}) AS d GROUP BY d.x", id="group-by"),
    pytest.param("SELECT SUM(d.x) AS s FROM ({union}) AS d", id="global-aggregate"),
    pytest.param("SELECT d.x FROM ({union}) AS d JOIN p ON p.tid = d.x", id="join"),
]
TAILS = [" LIMIT 1", " ORDER BY x LIMIT 1"]


@pytest.mark.xfail(strict=True, reason="algebraic_equivalence._union_all_branches reads through a cut Subquery (issue #518)")
@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("tail", TAILS)
def test_a_cut_operand_of_a_union_all_is_not_the_whole_table(reader, tail):
    cut = reader.format(union=f"{CUT.format(tail=tail)} UNION ALL SELECT w FROM u")
    whole = reader.format(union=f"{PLAIN} UNION ALL SELECT w FROM u")
    skip_if_unparseable(cut, whole)
    assert _differ(cut, whole), "the database must separate the pair"
    assert not _proven(cut, whole)


@pytest.mark.parametrize("reader", READERS)
def test_the_same_readers_without_a_cut_still_prove(reader):
    plain = reader.format(union=f"{PLAIN} UNION ALL SELECT w FROM u")
    wrapped = reader.format(union=f"(({PLAIN})) UNION ALL SELECT w FROM u")
    assert not _differ(plain, wrapped)
    assert _proven(plain, wrapped)
