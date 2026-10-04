"""``GROUP BY ()`` is a grand total: one row even over no input, so it is not a plain grouping to drop.

``_drop_group_in_membership_tests`` turned ``EXISTS (SELECT 1 FROM u GROUP BY ())`` into ``EXISTS (SELECT 1 FROM u)``,
which is FALSE over an empty ``u`` where the original is TRUE. ``ast_utils.extended_grouping`` (the shared guard)
did not count the empty grouping. Found by the rule-level fuzzer (``tools/rule_fuzz.py``).
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.ast_utils import extended_grouping  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402

SCHEMA = {"t": ["id", "x"], "u": ["k", "w"]}


def _rows(sql: str, u_rows: list) -> Counter:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id BIGINT, x BIGINT)")
    con.execute("CREATE TABLE u (k BIGINT, w BIGINT)")
    con.execute("INSERT INTO t VALUES (1, 2), (2, NULL)")
    for row in u_rows:
        con.execute("INSERT INTO u VALUES (?, ?)", row)
    return Counter(run_unoptimized(con, sql)[0])


@pytest.mark.parametrize("group,expected", [("()", True), ("x", False), ("x, y", False), ("ROLLUP (x)", True), ("GROUPING SETS ((x), ())", True)])
def test_extended_grouping_counts_the_empty_grouping(group, expected):
    tree = sqlglot.parse_one(f"SELECT 1 FROM u GROUP BY {group}", read="bigquery")
    assert extended_grouping(tree.args["group"]) is expected


WITH_TOTAL = "SELECT t.x FROM t WHERE EXISTS (SELECT 1 FROM u GROUP BY ())"
WITHOUT = "SELECT t.x FROM t WHERE EXISTS (SELECT 1 FROM u)"


def test_witness_differs_over_an_empty_table():
    assert _rows(WITH_TOTAL, []) != _rows(WITHOUT, [])


def test_grand_total_exists_is_not_the_plain_exists():
    assert not prove_equivalent_algebraic(WITH_TOTAL, WITHOUT, schema=SCHEMA).proven
    assert not prove_equivalent_algebraic(WITHOUT, WITH_TOTAL, schema=SCHEMA).proven


def test_near_miss_grouping_by_the_selected_key_is_still_dropped():
    left = "SELECT t.id FROM t WHERE t.id IN (SELECT u.k FROM u GROUP BY u.k)"
    right = "SELECT t.id FROM t WHERE t.id IN (SELECT u.k FROM u)"
    assert _rows(left, [(1, 1), (1, 2)]) == _rows(right, [(1, 1), (1, 2)])
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA).proven


def test_near_miss_exists_over_a_keyed_grouping_is_still_dropped():
    left = "SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.id GROUP BY u.k)"
    right = "SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.id)"
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA).proven
