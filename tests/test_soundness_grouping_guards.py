"""Wrong proofs behind guards that never saw ROLLUP, CUBE, GROUPING SETS, ``GROUP BY ()`` or DISTINCT ON.

sqlglot keeps ``ROLLUP (x)``, ``CUBE (x)`` and ``GROUPING SETS (..)`` as items of ``group.expressions``, so a
guard reading ``group.args.get("rollup")`` never fires, and ``GROUP BY ()`` is a key-less ``Tuple`` item. The
normalizer spells most grouping sets out as a ``UNION ALL`` first, but not a list that repeats a set (or one
with an expression key), and rules then read the select as a plain grouping: one row per group, no row over
no input. A truthy ``args.get("distinct")`` likewise reads ``DISTINCT ON (k)`` (one row per ``k``, which can
repeat output rows) as duplicate removal. Each pair returns different rows on the database next to it
(DuckDB, optimizer off), so no entry point may call it equivalent; near misses stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import extended_grouping
from kumosql.containment import check_containment
from kumosql.duckdb_load import run_unoptimized
from kumosql.query_optimizer import Catalog, rule_drop_inner_order, search_deletions
from kumosql.smt_equivalence import TableConstraints, prove_equivalent_smt

SCHEMA = {"t": ["x", "y", "z"], "u": ["k", "v"]}
X_KEY = {"t": TableConstraints(not_null=frozenset({"x"}), keys=(("x",),))}
EMPTY = {"t": [], "u": []}
TWO_ROWS = {"t": [(1, 5, 0), (2, 6, 0)], "u": []}
SAME_Y = {"t": [(1, 5, 0), (2, 5, 0)], "u": []}
ONE_X = {"t": [(1, 5, 0), (1, 6, 0)], "u": []}

# A derived table of per-x counts (never NULL), summed again by the grouping under test.
COUNTS = "(SELECT x, COUNT(*) AS c FROM t GROUP BY x) AS s"
ANTI_JOIN = "FROM u LEFT JOIN t AS o ON o.x = u.k WHERE NOT EXISTS (SELECT 1 FROM t AS s WHERE s.x = u.k)"


def _bags(sqls: list[str], rows: dict[str, list[tuple]], dialect: str) -> list[Counter]:
    db = duckdb.connect()
    db.execute("CREATE TABLE t (x INTEGER, y INTEGER, z INTEGER)")
    db.execute("CREATE TABLE u (k INTEGER, v INTEGER)")
    for name, table_rows in rows.items():
        for row in table_rows:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    queries = [sql if dialect == "duckdb" else sqlglot.transpile(sql, read=dialect, write="duckdb")[0] for sql in sqls]
    return [Counter(result) for result in run_unoptimized(db, *queries)]


# (left, right, constraints, rows on which they differ)
WRONG_PROOFS = [
    # grouped_sums: COALESCE(SUM(count), 0) is SUM(count) only when every group has a row
    pytest.param(
        f"SELECT s.x, COALESCE(SUM(s.c), 0) AS n FROM {COUNTS} GROUP BY GROUPING SETS ((s.x), (), ())",
        f"SELECT s.x, SUM(s.c) AS n FROM {COUNTS} GROUP BY GROUPING SETS ((s.x), (), ())",
        None,
        EMPTY,
        id="grouped-sum-coalesce-repeated-empty-set",
    ),
    pytest.param(
        f"SELECT s.x, COALESCE(SUM(s.c), 0) AS n FROM {COUNTS} GROUP BY ROLLUP (s.x), ROLLUP (s.x)",
        f"SELECT s.x, SUM(s.c) AS n FROM {COUNTS} GROUP BY ROLLUP (s.x), ROLLUP (s.x)",
        None,
        EMPTY,
        id="grouped-sum-coalesce-repeated-rollup",
    ),
    pytest.param(
        f"SELECT COALESCE(SUM(s.c), 0) AS n FROM {COUNTS} GROUP BY ()",
        f"SELECT SUM(s.c) AS n FROM {COUNTS} GROUP BY ()",
        None,
        EMPTY,
        id="grouped-sum-coalesce-empty-grouping",
    ),
    # grouped_outer_joins: dropping the NULL key o.x leaves only grouping sets that hold the empty set
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n {ANTI_JOIN} GROUP BY o.x, GROUPING SETS ((u.k), (), ())",
        "SELECT u.k, COUNT(*) AS n FROM u WHERE NOT EXISTS (SELECT 1 FROM t AS s WHERE s.x = u.k) GROUP BY GROUPING SETS ((u.k), (), ())",
        None,
        EMPTY,
        id="anti-joined-null-key-dropped-from-grouping-sets",
    ),
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n {ANTI_JOIN} GROUP BY o.x, ROLLUP (u.k), ROLLUP (u.k)",
        "SELECT u.k, COUNT(*) AS n FROM u WHERE NOT EXISTS (SELECT 1 FROM t AS s WHERE s.x = u.k) GROUP BY ROLLUP (u.k), ROLLUP (u.k)",
        None,
        EMPTY,
        id="anti-joined-null-key-dropped-from-rollups",
    ),
    # keyed_rules: grouping by a key does not make each row its own group when a set repeats
    pytest.param(
        "SELECT t.x FROM t GROUP BY t.x, GROUPING SETS ((), ())", "SELECT t.x FROM t", X_KEY, TWO_ROWS, id="keyed-grouping-repeated-set"
    ),
    pytest.param(
        "SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x, GROUPING SETS ((t.y), (t.y))",
        "SELECT t.x, 1 AS n FROM t",
        X_KEY,
        TWO_ROWS,
        id="keyed-grouping-count-repeated-set",
    ),
    pytest.param(
        "SELECT t.x FROM t GROUP BY t.x, ROLLUP (t.y), ROLLUP (t.y)", "SELECT t.x FROM t", X_KEY, TWO_ROWS, id="keyed-grouping-repeated-rollup"
    ),
    # empty_rules: WHERE FALSE leaves the grand-total rows of the empty grouping set
    pytest.param(
        "SELECT x, COUNT(*) AS n FROM t WHERE FALSE GROUP BY GROUPING SETS ((x), (), ())",
        "SELECT m.x, m.n FROM (SELECT x, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((x), (), ())) AS m WHERE FALSE",
        None,
        TWO_ROWS,
        id="where-false-under-grouping-sets-is-not-empty",
    ),
    pytest.param(
        "SELECT COUNT(*) AS n FROM t WHERE FALSE GROUP BY ()",
        "SELECT m.n FROM (SELECT COUNT(*) AS n FROM t GROUP BY ()) AS m WHERE FALSE",
        None,
        TWO_ROWS,
        id="where-false-under-empty-grouping-is-not-empty",
    ),
    # smt_equivalence._selects_a_set: a DISTINCT ON derived table can repeat a row
    pytest.param(
        "SELECT d.y FROM (SELECT DISTINCT ON (x) y FROM t ORDER BY x, y) AS d",
        "SELECT DISTINCT d.y FROM (SELECT DISTINCT ON (x) y FROM t ORDER BY x, y) AS d",
        None,
        SAME_Y,
        id="distinct-on-derived-table-is-not-a-set",
    ),
]


@pytest.mark.parametrize("left,right,constraints,rows", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, constraints, rows):
    first, second = _bags([left, right], rows, "duckdb")
    assert first != second
    options = {"schema": SCHEMA, "dialect": "duckdb", **({"constraints": constraints} if constraints else {})}
    assert not prove_equivalent_algebraic(left, right, **options).proven
    assert not prove_equivalent_smt(left, right, **options).proven


STILL_PROVEN = [
    pytest.param(
        f"SELECT s.x, COALESCE(SUM(s.c), 0) AS n FROM {COUNTS} GROUP BY s.x",
        f"SELECT s.x, SUM(s.c) AS n FROM {COUNTS} GROUP BY s.x",
        None,
        id="grouped-sum-coalesce-plain-grouping",
    ),
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n {ANTI_JOIN} GROUP BY o.x, u.k",
        "SELECT u.k, COUNT(*) AS n FROM u WHERE NOT EXISTS (SELECT 1 FROM t AS s WHERE s.x = u.k) GROUP BY u.k",
        None,
        id="anti-joined-null-key-dropped-from-plain-grouping",
    ),
    pytest.param("SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x", "SELECT t.x, 1 AS n FROM t", X_KEY, id="keyed-plain-grouping"),
    pytest.param(
        "SELECT x, COUNT(*) AS n FROM t WHERE FALSE GROUP BY x",
        "SELECT m.x, m.n FROM (SELECT x, COUNT(*) AS n FROM t GROUP BY x) AS m WHERE FALSE",
        None,
        id="where-false-under-plain-grouping-is-empty",
    ),
    pytest.param(
        "SELECT d.y FROM (SELECT DISTINCT y FROM t) AS d", "SELECT DISTINCT d.y FROM (SELECT DISTINCT y FROM t) AS d", None, id="distinct-derived-table-is-a-set"
    ),
    pytest.param(
        "SELECT x, COUNT(*) AS n FROM t GROUP BY x HAVING x > 1",
        "SELECT x, COUNT(*) AS n FROM t WHERE x > 1 GROUP BY x",
        None,
        id="key-having-to-where",
    ),
    pytest.param(
        "SELECT x, COUNT(*) AS n FROM t GROUP BY ROLLUP (x)",
        "SELECT x, COUNT(*) AS n FROM t GROUP BY x UNION ALL SELECT NULL AS x, COUNT(*) AS n FROM t",
        None,
        id="rollup-spelled-out",
    ),
]


@pytest.mark.parametrize("left,right,constraints", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right, constraints):
    for rows in (EMPTY, TWO_ROWS, SAME_Y):
        first, second = _bags([left, right], rows, "duckdb")
        assert first == second
    options = {"schema": SCHEMA, "dialect": "duckdb", **({"constraints": constraints} if constraints else {})}
    assert prove_equivalent_algebraic(left, right, **options).proven


def test_empty_grouping_set_is_an_extended_grouping():
    def group(sql):
        return sqlglot.parse_one(sql, read="duckdb").args.get("group")

    for sql in ("SELECT COUNT(*) FROM t GROUP BY ()", "SELECT x FROM t GROUP BY x, ()", "SELECT x FROM t GROUP BY ROLLUP (x)"):
        assert extended_grouping(group(sql))
    for sql in ("SELECT x FROM t GROUP BY x", "SELECT x FROM t GROUP BY (x, y)", "SELECT COUNT(*) FROM t"):
        assert not extended_grouping(group(sql))


CATALOG = Catalog(columns={"t": ["x", "y", "z"]}, not_null={"t": {"x"}}, keys={"t": [("x",)]})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.x, COUNT(*) AS n FROM t GROUP BY t.x, GROUPING SETS ((), ())",
        "SELECT t.x FROM t GROUP BY t.x, GROUPING SETS ((t.y), (t.y))",
        "SELECT t.x FROM t GROUP BY t.x, ROLLUP (t.y), ROLLUP (t.y)",
    ],
)
def test_search_deletions_keeps_repeated_grouping_sets(sql):
    text, steps = search_deletions(sql, CATALOG, dialect="postgres", budget_s=20)
    first, second = _bags([sql, text], TWO_ROWS, "postgres")
    assert first == second, steps


def test_search_deletions_still_drops_a_keyed_grouping():
    text, steps = search_deletions("SELECT t.x, t.y FROM t GROUP BY t.x, t.y", CATALOG, dialect="postgres", budget_s=20)
    assert steps and "GROUP BY" not in text.upper()


def test_drop_inner_order_reads_a_set_operation_distinct_flag():
    # a UNION's DISTINCT is a bool, not a Distinct node; this used to raise AttributeError
    tree = sqlglot.parse_one("SELECT d.x FROM (SELECT x FROM t UNION SELECT y FROM t ORDER BY 1) AS d", read="postgres")
    assert rule_drop_inner_order(tree)
    assert tree.sql(dialect="postgres") == "SELECT d.x FROM (SELECT x FROM t UNION SELECT y FROM t) AS d"
    kept = sqlglot.parse_one("SELECT d.y FROM (SELECT DISTINCT ON (x) y FROM t ORDER BY x, y) AS d", read="postgres")
    assert not rule_drop_inner_order(kept)


NOT_CONTAINED = [
    pytest.param("SELECT COUNT(*) AS n FROM t WHERE FALSE", "SELECT COUNT(*) AS n FROM t", id="global-aggregate"),
    pytest.param("SELECT COUNT(*) AS n FROM t WHERE 1 = 0", "SELECT COUNT(*) AS n FROM t", id="global-aggregate-false-comparison"),
    pytest.param("SELECT COUNT(*) AS n FROM t WHERE FALSE GROUP BY ()", "SELECT COUNT(*) AS n FROM t GROUP BY ()", id="empty-grouping"),
    pytest.param(
        "SELECT x, COUNT(*) AS n FROM t WHERE FALSE GROUP BY ROLLUP (x)", "SELECT x, COUNT(*) AS n FROM t GROUP BY ROLLUP (x)", id="rollup"
    ),
    pytest.param(
        "SELECT DISTINCT ON (x) y FROM t WHERE (6 = y) ORDER BY x, y", "SELECT DISTINCT ON (x) y FROM t ORDER BY x, y", id="distinct-on"
    ),
]


@pytest.mark.parametrize("q1,q2", NOT_CONTAINED)
def test_prefilter_does_not_claim_containment_past_a_grand_total_or_distinct_on(q1, q2):
    first, second = _bags([q1, q2], ONE_X, "postgres")
    assert not set(first) <= set(second)
    for semantics in ("set", "bag"):
        assert check_containment(q1, q2, schema=SCHEMA, semantics=semantics, dialect="postgres").status != "contained"


def test_prefilter_still_proves_a_filtered_plain_grouping():
    q1, q2 = "SELECT x, COUNT(*) AS n FROM t WHERE x = 1 GROUP BY x", "SELECT x, COUNT(*) AS n FROM t GROUP BY x"
    result = check_containment(q1, q2, schema=SCHEMA, semantics="bag", dialect="postgres")
    assert (result.status, result.method) == ("contained", "pre-filter")
