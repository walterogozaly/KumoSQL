"""DISTINCT and regrouping rules (``kumosql.distinct_rules``): proofs they unlock, and pairs they must not prove."""

import random
import sqlite3
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.distinct_rules import drop_dedup_read_as_set, drop_membership_dedup, merge_grouped_source, regroup_distinct
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"t": ["a", "b", "c"], "s": ["a", "b", "d"]}


def prove(left, right, **kwargs):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql", **kwargs)


PROVEN = [
    pytest.param(
        "SELECT a FROM t WHERE b NOT IN (SELECT DISTINCT b FROM s WHERE d > 1)",
        "SELECT a FROM t WHERE b NOT IN (SELECT b FROM s WHERE d > 1)",
        id="distinct-inside-not-in",
    ),
    pytest.param(
        "SELECT a FROM t WHERE b IN (SELECT b FROM s GROUP BY b)",
        "SELECT a FROM t WHERE b IN (SELECT DISTINCT b FROM s)",
        id="key-only-group-inside-in",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM t GROUP BY a, b HAVING COUNT(DISTINCT c) > 1",
        "SELECT DISTINCT a FROM (SELECT a, b, COUNT(DISTINCT c) AS n FROM t GROUP BY a, b) AS g WHERE n > 1",
        id="distinct-over-grouped-derived",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM t GROUP BY a, b HAVING COUNT(DISTINCT c) > 1",
        "SELECT x FROM (SELECT a AS x FROM t GROUP BY a, b HAVING COUNT(DISTINCT c) > 1) AS g GROUP BY x",
        id="group-only-over-grouped-derived",
    ),
    pytest.param(
        "SELECT SUM(DISTINCT a), COUNT(DISTINCT a), MAX(a) FROM t",
        "SELECT SUM(a), COUNT(a), MAX(a) FROM (SELECT a FROM t GROUP BY a) AS g",
        id="global-regroup-distinct",
    ),
    pytest.param(
        "SELECT COUNT(*), COUNT(DISTINCT a) FROM t",
        "SELECT CAST(COALESCE(SUM(n), 0) AS SIGNED), COUNT(a) FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS g",
        id="global-regroup-count-star",
    ),
    pytest.param(
        "SELECT SUM(b), SUM(DISTINCT a) FROM t",
        "SELECT SUM(p), SUM(a) FROM (SELECT a, SUM(b) AS p FROM t GROUP BY a) AS g",
        id="global-regroup-partial-sum",
    ),
    pytest.param(
        "SELECT b, SUM(a) DIV COUNT(a), COUNT(DISTINCT a) FROM t GROUP BY b",
        "SELECT b, SUM(p) DIV SUM(n), COUNT(a) FROM (SELECT b, a, SUM(a) AS p, COUNT(a) AS n FROM t GROUP BY b, a) AS g GROUP BY b",
        id="grouped-regroup-inside-expression",
    ),
    pytest.param(
        "SELECT s.d FROM t LEFT JOIN s ON t.b = s.b GROUP BY s.d",
        "SELECT y.d FROM (SELECT DISTINCT b FROM t) AS x LEFT JOIN (SELECT b, d FROM s GROUP BY b, d) AS y ON x.b = y.b GROUP BY y.d",
        id="distinct-sources-under-left-join",
    ),
    pytest.param(
        "SELECT t.a, s.d FROM t FULL JOIN s ON t.b = s.b GROUP BY t.a, s.d",
        "SELECT x.a, y.d FROM (SELECT DISTINCT a, b FROM t) AS x FULL JOIN (SELECT b, d FROM s GROUP BY b, d) AS y ON x.b = y.b "
        "GROUP BY x.a, y.d",
        id="distinct-sources-under-full-join",
    ),
    pytest.param(
        "SELECT t.a FROM t JOIN (SELECT DISTINCT b FROM s WHERE d > 1) AS x ON t.b = x.b",
        "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM s WHERE s.d > 1 AND s.b = t.b)",
        id="join-with-distinct-derived-is-exists",
    ),
    pytest.param(
        "SELECT DISTINCT(a), b, COUNT(DISTINCT(c)) FROM t GROUP BY a, b",
        "SELECT a, b, COUNT(DISTINCT c) FROM t GROUP BY a, b",
        id="parenthesized-columns-and-distinct-over-group-keys",
    ),
    pytest.param(
        "SELECT a, SUM(p) FROM (SELECT a, SUM(c) AS p FROM t GROUP BY a, b HAVING b = 1) AS g GROUP BY a",
        "SELECT a, SUM(c) FROM t GROUP BY a, b HAVING b = 1",
        id="regroup-over-a-key-fixed-to-a-constant",
    ),
    pytest.param(
        "SELECT a, MIN(m), SUM(n) FROM (SELECT a, MIN(c) AS m, COUNT(*) AS n FROM t GROUP BY a, b, c) AS g GROUP BY a",
        "SELECT a, MIN(c), COUNT(*) FROM t GROUP BY a",
        id="two-level-aggregate-merge",
    ),
    pytest.param(
        "SELECT DISTINCT a FROM t GROUP BY a, b",
        "SELECT x FROM (SELECT a AS x, b FROM t GROUP BY a, b) AS g GROUP BY x",
        id="group-without-aggregates-under-distinct",
    ),
]


@pytest.mark.parametrize("left, right", PROVEN)
def test_distinct_rules_prove(left, right):
    result = prove(left, right)
    assert result.proven, result.reason


NOT_PROVEN = [
    pytest.param(
        "SELECT SUM(a) FROM t",
        "SELECT SUM(a) FROM (SELECT a FROM t GROUP BY a) AS g",
        id="sum-over-distinct-values-is-not-sum",
    ),
    pytest.param(
        "SELECT COUNT(*) FROM t",
        "SELECT SUM(n) FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS g",
        id="global-sum-of-counts-is-null-on-empty-input",
    ),
    pytest.param(
        "SELECT COUNT(a) FROM t",
        "SELECT COALESCE(SUM(n), 0) FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS g",
        id="count-of-nullable-column-is-not-count-star",
    ),
    pytest.param(
        "SELECT s.d, COUNT(*) FROM t LEFT JOIN s ON t.b = s.b GROUP BY s.d",
        "SELECT y.d, COUNT(*) FROM (SELECT DISTINCT b FROM t) AS x LEFT JOIN s AS y ON x.b = y.b GROUP BY y.d",
        id="count-sees-repeats-through-left-join",
    ),
    pytest.param(
        "SELECT t.a FROM t LEFT JOIN s ON t.b = s.b",
        "SELECT x.a FROM (SELECT DISTINCT a, b FROM t) AS x LEFT JOIN s ON x.b = s.b",
        id="bag-output-keeps-repeats",
    ),
    pytest.param(
        "SELECT a FROM t WHERE b IN (SELECT b FROM s ORDER BY d LIMIT 1)",
        "SELECT a FROM t WHERE b IN (SELECT DISTINCT b FROM s ORDER BY d LIMIT 1)",
        id="limit-sees-repeats-inside-in",
    ),
    pytest.param(
        "SELECT t.a FROM t JOIN (SELECT DISTINCT b, d FROM s) AS x ON t.b = x.b",
        "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM s WHERE s.b = t.b)",
        id="join-on-some-columns-of-a-distinct-derived-can-repeat",
    ),
    pytest.param(
        "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a, b",
        "SELECT a, COUNT(*) FROM t GROUP BY a, b",
        id="distinct-over-groups-missing-a-key",
    ),
    pytest.param(
        "SELECT a, SUM(p) FROM (SELECT a, SUM(c) AS p FROM t GROUP BY a, b HAVING SUM(c) > 1) AS g GROUP BY a",
        "SELECT a, SUM(c) FROM t GROUP BY a HAVING SUM(c) > 1",
        id="having-on-finer-groups-is-not-having-on-coarse-groups",
    ),
]


@pytest.mark.parametrize("left, right", NOT_PROVEN)
def test_distinct_rules_do_not_prove(left, right):
    assert not prove(left, right).proven


def test_rules_leave_non_matching_shapes_alone():
    def select(sql, path=()):
        node = sqlglot.parse_one(sql, read="mysql")
        for step in path:
            node = node.find(step) if not isinstance(step, int) else list(node.find_all(sqlglot.exp.Select))[step]
        return node

    # MySQL picks any c per group: reading it twice need not give the same value.
    assert merge_grouped_source(select("SELECT DISTINCT a FROM (SELECT a, c FROM t GROUP BY a) AS g WHERE c > 1")) is None
    # A LIMIT inside the membership test sees repeats.
    limited = select("SELECT a FROM t WHERE b IN (SELECT DISTINCT b FROM s LIMIT 2)", (1,))
    assert drop_membership_dedup(limited) is None
    # An inner-join reader is _push_distinct_into_sources' shape; dropping would undo it.
    inner = select("SELECT DISTINCT x.a FROM (SELECT DISTINCT a, b FROM t) AS x JOIN s ON x.b = s.b", (1,))
    assert drop_dedup_read_as_set(inner) is None
    # A global SUM of partial counts is NULL on empty input, not the count.
    assert regroup_distinct(select("SELECT SUM(n) FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS g")) is None


RANDOM_PAIRS = [p.values for p in PROVEN]


@pytest.mark.parametrize("left, right", RANDOM_PAIRS)
def test_proven_pairs_agree_on_random_databases(left, right):
    rng = random.Random(7)
    for _ in range(40):
        db = sqlite3.connect(":memory:")
        for table, columns in SCHEMA.items():
            db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
            rows = [tuple(rng.choice([None, 1, 2, 3]) for _ in columns) for _ in range(rng.randint(0, 6))]
            db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", rows)
        runnable = [sqlglot.transpile(sql, read="mysql", write="sqlite")[0] for sql in (left, right)]
        try:
            results = [Counter(db.execute(sql).fetchall()) for sql in runnable]
        except sqlite3.OperationalError:  # FULL JOIN needs SQLite 3.39
            pytest.skip("SQLite cannot run this pair")
        assert results[0] == results[1], (left, right)


def test_count_of_a_not_null_column_is_the_regrouped_count_star():
    constraints = {"t": TableConstraints(not_null=frozenset({"a"}))}
    result = prove(
        "SELECT COUNT(a) FROM t",
        "SELECT COALESCE(SUM(n), 0) FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a) AS g",
        constraints=constraints,
    )
    assert result.proven, result.reason
