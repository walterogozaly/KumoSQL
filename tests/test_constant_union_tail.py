"""A false proof from ``distribute_over_constant_union`` when the union keeps only some rows (issue #518).

``SELECT UPPER(x) FROM (SELECT 'a' AS x UNION ALL SELECT 'b' ORDER BY x DESC LIMIT 1) AS d`` reads each constant row
of the union once and so is the union of the select over each row. The rule ignored an ``ORDER BY`` / ``LIMIT`` /
``OFFSET`` on the union and on the parentheses around a branch, so the cut vanished. Each pair below returns different
rows (DuckDB, optimizer off); the same pairs without the tail stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.literal_fold_rules import distribute_over_constant_union

READ = "SELECT UPPER(d.x) AS u FROM ({union}) AS d"
BOTH = "SELECT 'a' AS x UNION {kind} SELECT 'b'"


def _bag(sql: str) -> Counter:
    return Counter(run_unoptimized(duckdb.connect(), sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0])


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, dialect="bigquery", timeout_ms=3000).proven


# (the union with its tail, the union as if nothing cut it)
CUT = [
    pytest.param("SELECT 'a' AS x UNION ALL SELECT 'b' ORDER BY x DESC LIMIT 1", BOTH.format(kind="ALL"), id="union-all-limit"),
    pytest.param("SELECT 'a' AS x UNION DISTINCT SELECT 'b' ORDER BY x LIMIT 1 OFFSET 1", BOTH.format(kind="DISTINCT"), id="union-distinct-offset"),
    pytest.param("SELECT 'a' AS x UNION ALL SELECT 'b' ORDER BY x OFFSET 1", BOTH.format(kind="ALL"), id="offset-only"),
]
# the same cut on the parentheses of a branch; the prover still proves this one through the open ``_union_all_branches`` bug
# (``tests/test_union_all_branches_tail.py``), so only the rule is checked
CUT_PARENTHESES = "(SELECT 'a' AS x) UNION ALL ((SELECT 'b') LIMIT 0)"


@pytest.mark.parametrize("cut, whole", CUT)
def test_a_cut_union_of_constants_keeps_its_cut(cut, whole):
    left, right = READ.format(union=cut), READ.format(union=whole)
    skip_if_unparseable(left, right)
    assert _bag(left) != _bag(right), "the database must separate the pair"
    assert not _proven(left, right)


def test_the_rule_leaves_a_cut_union_alone():
    for cut in [p.values[0] for p in CUT] + [CUT_PARENTHESES]:
        assert distribute_over_constant_union(sqlglot.parse_one(READ.format(union=cut), read="bigquery")) is None


@pytest.mark.parametrize("kind", ["ALL", "DISTINCT"])
def test_an_uncut_union_of_constants_is_still_distributed(kind):
    whole = READ.format(union=BOTH.format(kind=kind))
    spelled = "SELECT 'A' AS u UNION ALL SELECT 'B'"
    assert _bag(whole) == _bag(spelled)
    assert distribute_over_constant_union(sqlglot.parse_one(whole, read="bigquery")) is not None
    assert _proven(whole, spelled)
