"""A false proof from ``drop_dedup_read_as_set`` through a ``UNION ALL`` that cuts its rows (issue #518).

``SELECT DISTINCT d.x FROM (A UNION ALL B ORDER BY 1 LIMIT n) AS d`` reads ``A`` as a set only when repeats cannot
show. The rule climbed through the ``UNION ALL`` to the outer ``DISTINCT`` and dropped the ``DISTINCT`` of ``A``, but a
``LIMIT`` or ``OFFSET`` on that union counts rows: repeats of ``A`` use up the cut and push other rows out. The pair
below returns different rows (DuckDB, optimizer off); without the cut the same drop is valid and still proves.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.distinct_rules import drop_dedup_read_as_set
from kumosql.duckdb_load import run_unoptimized

SCHEMA = {"t": {"x": "INT64"}, "u": {"k": "INT64"}}
ROWS = {"t": [(1,), (1,), (1,), (2,)], "u": [(5,), (6,)]}
READ = "SELECT DISTINCT d.x FROM ({union}) AS d"
KEPT = "(SELECT DISTINCT x FROM t) UNION ALL (SELECT k FROM u)"
DROPPED = "(SELECT x FROM t) UNION ALL (SELECT k FROM u)"


def _bag(db, sql: str) -> Counter:
    return Counter(run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _differ(left: str, right: str) -> bool:
    db = duckdb.connect()
    db.execute("CREATE TABLE t(x BIGINT)")
    db.execute("CREATE TABLE u(k BIGINT)")
    for table, rows in ROWS.items():
        db.executemany(f"INSERT INTO {table} VALUES (?)", rows)
    return _bag(db, left) != _bag(db, right)


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


@pytest.mark.parametrize("cut", ["ORDER BY 1 LIMIT 3", "ORDER BY 1 LIMIT 3 OFFSET 1", "ORDER BY 1 OFFSET 2"])
def test_a_cut_union_all_keeps_the_repeats_of_its_branches(cut):
    left, right = READ.format(union=f"{KEPT} {cut}"), READ.format(union=f"{DROPPED} {cut}")
    skip_if_unparseable(left, right)
    assert _differ(left, right), "the database must separate the pair"
    assert not _proven(left, right)


def test_the_rule_leaves_the_distinct_of_a_cut_branch_alone():
    for cut in ("ORDER BY 1 LIMIT 3", "LIMIT 2", "ORDER BY 1 OFFSET 1"):
        tree = sqlglot.parse_one(READ.format(union=f"{KEPT} {cut}"), read="bigquery")
        branch = next(s for s in tree.find_all(sqlglot.exp.Select) if s.args.get("distinct") and s is not tree)
        assert drop_dedup_read_as_set(branch) is None


@pytest.mark.parametrize("tail", ["", " ORDER BY 1"])
def test_without_a_cut_the_distinct_is_dropped_and_the_pair_proves(tail):
    left, right = READ.format(union=f"{KEPT}{tail}"), READ.format(union=f"{DROPPED}{tail}")
    assert not _differ(left, right)
    assert _proven(left, right)
    tree = sqlglot.parse_one(left, read="bigquery")
    branch = next(s for s in tree.find_all(sqlglot.exp.Select) if s.args.get("distinct") and s is not tree)
    assert drop_dedup_read_as_set(branch) is not None
