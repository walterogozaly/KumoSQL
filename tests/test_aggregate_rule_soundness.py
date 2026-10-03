"""False proofs in the aggregate, eager-aggregation and DISTINCT rules, kept as regression cases.

The first six came from an outside audit of ``aggregate_rules``, ``eager_aggregation`` and
``distinct_rules`` (S004-001 to S004-006); the rest are the same mistakes found elsewhere while fixing
them. Each pair returns different rows on the database next to it (DuckDB, optimizer off), so neither
prover may call it equivalent. The near misses below are equivalent and stay proven, so a fix cannot
just decline more.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.ast_utils import extended_grouping  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints, prove_equivalent_smt  # noqa: E402

DDL = {
    "t": "x BIGINT",
    "t2": "k BIGINT, x BIGINT",
    "tp": "k BIGINT, p BIGINT, x BIGINT",
    "a": "k BIGINT, j BIGINT, x BIGINT",
    "b": "k BIGINT, j BIGINT, x BIGINT",
    "c": "id BIGINT",
}
SCHEMA = {name: [c.split()[0] for c in columns.split(", ")] for name, columns in DDL.items()}
C_KEY = {"c": TableConstraints(keys=(("id",),), not_null=frozenset({"id"}))}
UNION = "(SELECT k, j, x FROM a UNION ALL SELECT k, j, x FROM b) AS u"
TWO_J_GROUPS = {"a": [(1, 1, 10), (1, 2, 20)], "b": [(1, 1, 1)]}

# (left, right, rows on which they differ, constraints)
WRONG_PROOFS = [
    pytest.param(
        "SELECT DISTINCT COUNT(*) AS n FROM (SELECT DISTINCT x FROM t) AS d",
        "SELECT DISTINCT COUNT(*) AS n FROM (SELECT x FROM t) AS d",
        {"t": [(1,), (1,)]},
        None,
        id="S004-001-outer-distinct-does-not-dedup-count-input",
    ),
    pytest.param(
        "SELECT k, COUNT(DISTINCT k) AS n FROM t2 GROUP BY k"
        " HAVING CASE WHEN COUNT(DISTINCT k) > 0 THEN 9007199254740993 ELSE 9007199254740992 END = 9007199254740992",
        "SELECT k, CASE WHEN k IS NULL THEN 0 ELSE 1 END AS n FROM t2 GROUP BY k HAVING TRUE",
        {"t2": [(1, 1)]},
        None,
        id="S004-002-case-literals-compared-as-floats",
    ),
    pytest.param(
        "SELECT p.k, p.s * q.c AS v FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k, j) AS p"
        " JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k",
        "SELECT a.k AS k, SUM(a.x) AS v FROM a, b WHERE a.k = b.k GROUP BY a.k, b.k",
        TWO_J_GROUPS,
        None,
        id="S004-003-flatten-loses-hidden-group-key",
    ),
    pytest.param(
        "SELECT c.id, g.s FROM c JOIN (SELECT k, SUM(x) AS s FROM a GROUP BY k, j) AS g ON c.id = g.k",
        "SELECT c.id, SUM(a.x) AS s FROM c, a WHERE c.id = a.k GROUP BY c.id",
        {"c": [(1,)], "a": [(1, 1, 10), (1, 2, 20)]},
        C_KEY,
        id="S004-004-pull-up-loses-hidden-group-key",
    ),
    pytest.param(
        f"SELECT u.k, SUM(u.x) + 1 AS v FROM {UNION} GROUP BY u.k, u.j",
        "SELECT k, SUM(p) + 1 AS v FROM (SELECT k, SUM(x) AS p FROM a GROUP BY k, j"
        " UNION ALL SELECT k, SUM(x) AS p FROM b GROUP BY k, j) AS w GROUP BY k",
        {"a": [(1, 1, 10), (1, 2, 20)]},
        None,
        id="S004-005-compound-split-loses-hidden-group-key",
    ),
    pytest.param(
        "SELECT k FROM tp GROUP BY k HAVING COUNT(CASE WHEN p = 1 THEN 1 END) > 0 ORDER BY COUNT(*) DESC LIMIT 1",
        "SELECT k FROM tp WHERE p = 1 GROUP BY k ORDER BY COUNT(*) DESC LIMIT 1",
        {"tp": [(1, 1, 0), (1, 0, 0), (1, 0, 0), (2, 1, 0), (2, 1, 0)]},
        None,
        id="S004-006-having-filter-pushed-past-order-by-count",
    ),
    pytest.param(
        "SELECT k FROM tp GROUP BY k HAVING SUM(CASE WHEN p = 1 THEN 1 ELSE 0 END) >= 1 ORDER BY SUM(x) DESC LIMIT 1",
        "SELECT k FROM tp WHERE p = 1 GROUP BY k ORDER BY SUM(x) DESC LIMIT 1",
        {"tp": [(1, 1, 1), (1, 0, 5), (2, 1, 3)]},
        None,
        id="having-filter-pushed-past-order-by-sum",
    ),
    pytest.param(
        "SELECT DISTINCT SUM(x) AS n FROM (SELECT DISTINCT x FROM t) AS d",
        "SELECT DISTINCT SUM(x) AS n FROM t",
        {"t": [(1,), (1,)]},
        None,
        id="outer-distinct-does-not-dedup-sum-input",
    ),
    pytest.param(
        "SELECT DISTINCT COUNT(*) AS n FROM a LEFT JOIN b ON a.k = b.k",
        "SELECT DISTINCT COUNT(*) AS n FROM a",
        {"a": [(1, 1, 1)], "b": [(1, 1, 1), (1, 2, 2)]},
        None,
        id="unread-outer-join-dropped-under-distinct-count",
    ),
    pytest.param(
        "SELECT DISTINCT COUNT(*) AS n FROM a JOIN b ON a.k = b.k",
        "SELECT DISTINCT COUNT(*) AS n FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.k = a.k)",
        {"a": [(1, 1, 1)], "b": [(1, 1, 1), (1, 2, 2)]},
        None,
        id="exists-read-as-join-under-distinct-count",
    ),
    pytest.param(
        "SELECT DISTINCT a.k, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY a.k",
        "SELECT DISTINCT a.k, COUNT(*) AS n FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.k = a.k) GROUP BY a.k",
        {"a": [(1, 1, 1)], "b": [(1, 1, 1), (1, 2, 2)]},
        None,
        id="exists-read-as-join-under-distinct-grouped-count",
    ),
    pytest.param(
        "SELECT DISTINCT SUM(a.x) AS n FROM a JOIN b ON a.k = b.k",
        "SELECT DISTINCT SUM(a.x) AS n FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.k = a.k)",
        {"a": [(1, 1, 1)], "b": [(1, 1, 1), (1, 2, 2)]},
        None,
        id="exists-read-as-join-under-distinct-sum",
    ),
    pytest.param(
        f"SELECT u.k, SUM(u.x) AS v FROM {UNION} GROUP BY u.k, u.j",
        f"SELECT u.k, SUM(u.x) AS v FROM {UNION} GROUP BY u.k",
        {"a": [(1, 1, 10), (1, 2, 20)]},
        None,
        id="aggregate-split-keeps-hidden-group-key",
    ),
    pytest.param(
        "SELECT p.k, q.c FROM (SELECT k FROM a GROUP BY k, j) AS p JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k",
        "SELECT p.k, q.c FROM (SELECT k FROM a GROUP BY k) AS p JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k",
        TWO_J_GROUPS,
        None,
        id="flatten-key-only-source-with-hidden-key",
    ),
    pytest.param(
        "SELECT DISTINCT 1 AS a FROM t GROUP BY ROLLUP (x)",
        "SELECT DISTINCT 1 AS a FROM t",
        {},
        None,
        id="rollup-grand-total-exists-over-no-rows",
    ),
    pytest.param(
        "SELECT x + 0 AS y, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((x + 0), ()) HAVING COUNT(*) > 0",
        "SELECT x + 0 AS y, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((x + 0), ())",
        {},
        None,
        id="grouping-sets-empty-group-has-no-rows",
    ),
    pytest.param(
        "SELECT b.k, SUM(p.c) AS v FROM b JOIN (SELECT k, COUNT(*) AS c FROM a GROUP BY k) AS p ON b.k = p.k GROUP BY ROLLUP (b.k)",
        "SELECT b.k, COUNT(*) AS v FROM b JOIN a ON b.k = a.k GROUP BY ROLLUP (b.k)",
        {},
        None,
        id="rollup-sum-of-counts-is-null-over-no-rows",
    ),
]


def _rows(sql_pair: tuple[str, str], rows: dict[str, list[tuple]]) -> tuple[Counter, Counter]:
    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        for row in rows.get(name, []):
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    left, right = run_unoptimized(db, *sql_pair)
    return Counter(left), Counter(right)


@pytest.mark.parametrize("left,right,rows,constraints", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, rows, constraints):
    ran_left, ran_right = _rows((left, right), rows)
    assert ran_left != ran_right
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        result = prove(left, right, schema=SCHEMA, constraints=constraints)
        assert not result.proven, (prove.__name__, result.reason)


STILL_PROVEN = [
    pytest.param(
        "SELECT DISTINCT MIN(x) AS n FROM (SELECT DISTINCT x FROM t) AS d", "SELECT DISTINCT MIN(x) AS n FROM t", None, id="distinct-min"
    ),
    pytest.param(
        "SELECT DISTINCT COUNT(DISTINCT x) AS n FROM (SELECT DISTINCT x FROM t) AS d",
        "SELECT DISTINCT COUNT(DISTINCT x) AS n FROM t",
        None,
        id="distinct-count-distinct",
    ),
    pytest.param("SELECT DISTINCT x FROM (SELECT DISTINCT x FROM t2) AS d", "SELECT DISTINCT x FROM t2", None, id="distinct-plain"),
    pytest.param("SELECT DISTINCT a.k FROM a LEFT JOIN b ON a.k = b.k", "SELECT DISTINCT a.k FROM a", None, id="unread-outer-join"),
    pytest.param(
        "SELECT DISTINCT MAX(a.x) AS n FROM a JOIN b ON a.k = b.k",
        "SELECT DISTINCT MAX(a.x) AS n FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.k = a.k)",
        None,
        id="exists-as-join-under-max",
    ),
    pytest.param(
        "SELECT DISTINCT a.x FROM a JOIN b ON a.k = b.k",
        "SELECT DISTINCT a.x FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.k = a.k)",
        None,
        id="exists-as-join-plain",
    ),
    pytest.param(
        f"SELECT u.k, SUM(u.x) + 1 AS v FROM {UNION} GROUP BY u.k, u.j",
        "SELECT k, SUM(s) + 1 AS v FROM (SELECT k, j, SUM(x) AS s FROM a GROUP BY k, j"
        " UNION ALL SELECT k, j, SUM(x) AS s FROM b GROUP BY k, j) AS w GROUP BY k, j",
        None,
        id="compound-split-with-hidden-key-carried",
    ),
    pytest.param(
        f"SELECT u.k, SUM(u.x) + 1 AS v FROM {UNION} GROUP BY u.k",
        "SELECT k, SUM(s) + 1 AS v FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k UNION ALL SELECT k, SUM(x) AS s FROM b GROUP BY k) AS w GROUP BY k",
        None,
        id="compound-split",
    ),
    pytest.param(
        "SELECT p.k, p.s * q.c AS v FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k) AS p"
        " JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k",
        "SELECT a.k, SUM(a.x) AS v FROM a JOIN b ON a.k = b.k GROUP BY a.k, b.k",
        None,
        id="flatten-grouped-join",
    ),
    pytest.param(
        "SELECT c.id, g.s FROM c JOIN (SELECT k, SUM(x) AS s FROM a GROUP BY k) AS g ON c.id = g.k",
        "SELECT c.id, SUM(a.x) AS s FROM c JOIN a ON c.id = a.k GROUP BY c.id",
        C_KEY,
        id="pull-up-aggregate",
    ),
    pytest.param(
        "SELECT b.k, SUM(p.s) AS v FROM b JOIN (SELECT k, SUM(x) AS s FROM a GROUP BY k, j) AS p ON b.k = p.k GROUP BY b.k",
        "SELECT b.k, SUM(p.s) AS v FROM b JOIN (SELECT k, SUM(x) AS s FROM a GROUP BY k) AS p ON b.k = p.k GROUP BY b.k",
        None,
        id="unnest-grouped-source-with-hidden-key",
    ),
    pytest.param(
        "SELECT k FROM tp GROUP BY k HAVING COUNT(CASE WHEN p = 1 THEN 1 END) > 0",
        "SELECT k FROM tp WHERE p = 1 GROUP BY k",
        None,
        id="having-existence-to-where",
    ),
    pytest.param(
        "SELECT k FROM tp GROUP BY k HAVING COUNT(CASE WHEN p = 1 THEN 1 END) > 0 ORDER BY k LIMIT 1",
        "SELECT k FROM tp WHERE p = 1 GROUP BY k ORDER BY k LIMIT 1",
        None,
        id="having-existence-to-where-ordered-by-key",
    ),
    pytest.param(
        "SELECT k, COUNT(DISTINCT k) AS n FROM t2 GROUP BY k HAVING CASE WHEN COUNT(DISTINCT k) > 0 THEN 3 ELSE 2 END > 5",
        "SELECT k, CASE WHEN k IS NULL THEN 0 ELSE 1 END AS n FROM t2 GROUP BY k HAVING FALSE",
        None,
        id="case-comparison-folds",
    ),
]


@pytest.mark.parametrize("left,right,constraints", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right, constraints):
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=constraints).proven


def test_case_literals_of_mixed_types_are_not_folded():
    """BigQuery compares INT64 with FLOAT64 as floats (equal here), DuckDB with DECIMAL exactly (not equal)."""

    sql = (
        "SELECT k FROM t2 GROUP BY k"
        " HAVING CASE WHEN COUNT(DISTINCT k) > 0 THEN 9007199254740993 ELSE 9007199254740992 END = 9007199254740992.0"
    )
    assert "TRUE" not in normalize(sql, schema=SCHEMA).split("HAVING")[-1]


def test_joined_aggregates_reading_an_output_alias_are_not_merged():
    """Merging renames the outputs, so a HAVING that reads ``s`` by its alias would be left dangling."""

    sql = (
        "SELECT d1.k, d1.s, d2.m FROM (SELECT k, SUM(x) AS s FROM t2 GROUP BY k HAVING s > 5) AS d1"
        " JOIN (SELECT k, MAX(x) AS m FROM t2 GROUP BY k) AS d2 ON d1.k IS NOT DISTINCT FROM d2.k"
    )
    assert "HAVING s > 5) AS d1" in normalize(sql, schema=SCHEMA)


@pytest.mark.parametrize(
    "sql,extended",
    [
        ("SELECT x FROM t GROUP BY ROLLUP (x)", True),
        ("SELECT x FROM t GROUP BY CUBE (x)", True),
        ("SELECT x FROM t GROUP BY GROUPING SETS ((x), ())", True),
        ("SELECT x FROM t GROUP BY x", False),
    ],
)
def test_extended_grouping_sees_rollup_cube_and_grouping_sets(sql, extended):
    assert extended_grouping(sqlglot.parse_one(sql, read="bigquery").args["group"]) is extended
