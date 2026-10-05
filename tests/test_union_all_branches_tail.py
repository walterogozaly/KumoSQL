"""``_union_all_branches`` does not read through a cut operand of a UNION ALL (issue #518).

``algebraic_equivalence._union_all_branches`` used to step through every ``Subquery``, so ``((SELECT x FROM t) ORDER BY x LIMIT 1)
UNION ALL ...`` was read as the select of ``t`` with the tail gone. Its callers (``_aligned_branches`` and with it
``_distribute``, ``_prune_union_all``, the aggregate splits in ``aggregate_rules``) then worked on all of ``t``, and the prover
proved the cut and the uncut query equal (also what the rule fuzzer found as a ``_distribute`` firing that returned one row
more). It now returns ``None`` for a ``Subquery`` with a ``limit`` or ``offset``, as ``setop_rules._unwrap`` and
``union_filter_rules._branches`` do; an ORDER BY alone keeps every row, so that parenthesis is still read through. Every
cut pair returns different rows (DuckDB, optimizer off).
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
TAILS = [" LIMIT 1", " ORDER BY x LIMIT 1", " LIMIT 1 OFFSET 1"]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("tail", TAILS)
def test_a_cut_operand_of_a_union_all_is_not_the_whole_table(reader, tail):
    cut = reader.format(union=f"{CUT.format(tail=tail)} UNION ALL SELECT w FROM u")
    whole = reader.format(union=f"{PLAIN} UNION ALL SELECT w FROM u")
    skip_if_unparseable(cut, whole)
    assert _differ(cut, whole), "the database must separate the pair"
    assert not _proven(cut, whole)


@pytest.mark.parametrize("reader", READERS)
def test_a_cut_operand_on_the_right_or_inside_more_parentheses_is_not_the_whole_table(reader):
    for cut, whole in (
        ("SELECT w AS x FROM u UNION ALL ((SELECT x FROM t) ORDER BY x LIMIT 1)", "SELECT w AS x FROM u UNION ALL (SELECT x FROM t)"),
        ("(((SELECT x FROM t) ORDER BY x LIMIT 1)) UNION ALL SELECT w FROM u", "(SELECT x FROM t) UNION ALL SELECT w FROM u"),
    ):
        cut, whole = reader.format(union=cut), reader.format(union=whole)
        skip_if_unparseable(cut, whole)
        assert _differ(cut, whole), "the database must separate the pair"
        assert not _proven(cut, whole)


@pytest.mark.parametrize("reader", READERS)
def test_an_ordered_operand_is_still_read_through(reader):
    plain = reader.format(union=f"{PLAIN} UNION ALL SELECT w FROM u")
    ordered = reader.format(union="((SELECT x FROM t) ORDER BY x) UNION ALL SELECT w FROM u")
    assert not _differ(plain, ordered)
    assert _proven(plain, ordered)


@pytest.mark.parametrize("reader", READERS)
def test_the_same_readers_without_a_cut_still_prove(reader):
    plain = reader.format(union=f"{PLAIN} UNION ALL SELECT w FROM u")
    wrapped = reader.format(union=f"(({PLAIN})) UNION ALL SELECT w FROM u")
    assert not _differ(plain, wrapped)
    assert _proven(plain, wrapped)
