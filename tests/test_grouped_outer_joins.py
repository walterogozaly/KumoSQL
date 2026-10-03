"""Grouping pushed through an outer join, and outer joins an anti-join test rules out (grouped_outer_joins).

Each equivalent pair must be proved and agree on random DuckDB databases; each near miss must stay
unproved and carries a witness database on which the two queries differ (DuckDB, optimizer off).
"""

import random
from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.grouped_outer_joins import group_distinct_outer_join, null_extend_anti_joined  # noqa: E402

SCHEMA = {"a": ["x", "z"], "b": ["y", "w"], "c": ["y", "w"]}
DDL = {"a": "x INT, z INT", "b": "y INT, w INT", "c": "y INT, w INT"}

GROUPED = "SELECT a.x, b.y FROM a LEFT JOIN b ON a.x = b.y GROUP BY a.x, b.y"
PUSHED = "SELECT s.x, t.y FROM (SELECT x FROM a GROUP BY x) AS s LEFT JOIN (SELECT y FROM b GROUP BY y) AS t ON s.x = t.y"
ANTI = "NOT EXISTS (SELECT 1 FROM b AS e WHERE e.y = a.x)"

EQUIVALENT = [
    pytest.param(GROUPED, PUSHED, id="pushed-grouping"),
    pytest.param(
        "SELECT a.x, b.y FROM b RIGHT JOIN a ON a.x = b.y GROUP BY a.x, b.y",
        "SELECT s.x, t.y FROM (SELECT DISTINCT y FROM b) AS t RIGHT JOIN (SELECT DISTINCT x FROM a) AS s ON s.x = t.y",
        id="pushed-distinct-right-join",
    ),
    pytest.param(
        f"SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE {ANTI} GROUP BY a.x, e.y",
        f"SELECT a.x, NULL FROM a WHERE {ANTI} GROUP BY a.x",
        id="anti-join-null-extends",
    ),
    pytest.param(
        f"{GROUPED} UNION ALL SELECT a.x, e.y FROM b AS e RIGHT JOIN a ON e.y = a.x WHERE {ANTI} GROUP BY a.x, e.y",
        f"{PUSHED} UNION ALL SELECT s.x, t.y FROM (SELECT y FROM b GROUP BY y) AS t RIGHT JOIN (SELECT x FROM a GROUP BY x) AS s ON s.x = t.y "
        "WHERE NOT EXISTS (SELECT 1 FROM (SELECT y FROM b GROUP BY y) AS t WHERE t.y = s.x) GROUP BY s.x, t.y",
        id="union-of-both",
    ),
]

# (left, right, witness rows on which they differ)
DIFFERENT = [
    pytest.param(  # a hidden group key: x repeats in the pushed grouping
        GROUPED,
        "SELECT s.x, t.y FROM (SELECT x FROM a GROUP BY x, z) AS s LEFT JOIN (SELECT y FROM b GROUP BY y) AS t ON s.x = t.y",
        {"a": [(1, 1), (1, 2)]},
        id="hidden-group-key",
    ),
    pytest.param(  # the null-supplying side is not read in full: one row of a meets two rows of b
        "SELECT a.x FROM a LEFT JOIN b ON a.x = b.y GROUP BY a.x",
        "SELECT s.x FROM (SELECT x FROM a GROUP BY x) AS s LEFT JOIN (SELECT y, w FROM b GROUP BY y, w) AS t ON s.x = t.y",
        {"a": [(1, 0)], "b": [(1, 1), (1, 2)]},
        id="partly-read-side",
    ),
    pytest.param(  # the preserved input is not grouped
        GROUPED,
        "SELECT s.x, t.y FROM (SELECT x FROM a) AS s LEFT JOIN (SELECT y FROM b GROUP BY y) AS t ON s.x = t.y",
        {"a": [(1, 0), (1, 5)]},
        id="ungrouped-input",
    ),
    pytest.param(  # the anti-join test is narrower than the ON condition
        "SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x AND f.w > 0)",
        "SELECT a.x, NULL FROM a WHERE NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x AND f.w > 0)",
        {"a": [(1, 0)], "b": [(1, 0)]},
        id="narrower-anti-test",
    ),
    pytest.param(  # the anti-join test reads another table
        "SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE NOT EXISTS (SELECT 1 FROM c WHERE c.y = a.x)",
        "SELECT a.x, NULL FROM a WHERE NOT EXISTS (SELECT 1 FROM c WHERE c.y = a.x)",
        {"a": [(1, 0)], "b": [(1, 0)]},
        id="anti-test-on-other-table",
    ),
    pytest.param(  # the anti-join test is one side of an OR, not a conjunct of WHERE
        "SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE a.z = 0 OR NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x)",
        "SELECT a.x, NULL FROM a WHERE a.z = 0 OR NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x)",
        {"a": [(1, 0)], "b": [(1, 0)]},
        id="anti-test-under-or",
    ),
]


def _database(rows):
    db = duckdb.connect()
    for table, ddl in DDL.items():
        db.execute(f"CREATE TABLE {table} ({ddl})")
        insert_rows(db, table, rows.get(table, []))
    return db


def _random_rows(rng):
    values = [None, 1, 2, 3]
    return {t: [(rng.choice(values), rng.choice(values)) for _ in range(rng.randint(0, 5))] for t in DDL}


def _prove(left, right, dialect):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect=dialect)


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pair_is_proved(left, right):
    for dialect in ("mysql", "duckdb"):
        result = _prove(left, right, dialect)
        assert result.proven, (dialect, result.reason)


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pair_agrees_on_random_databases(left, right):
    rng = random.Random(7)
    for _ in range(80):
        first, second = run_unoptimized(_database(_random_rows(rng)), left, right)
        assert Counter(first) == Counter(second), (first, second)


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
def test_witness_separates_the_pair(left, right, witness):
    first, second = run_unoptimized(_database(witness), left, right)
    assert Counter(first) != Counter(second)


@pytest.mark.parametrize("left, right, witness", DIFFERENT)
def test_different_pair_is_never_proved(left, right, witness):
    for dialect in ("mysql", "duckdb"):
        result = _prove(left, right, dialect)
        assert not result.proven, (dialect, result.reason)


def _select(sql):
    return sqlglot.parse_one(sql, read="mysql")


def test_grouping_is_not_added_inside_a_derived_table():
    inner = _select(f"SELECT q.x FROM ({PUSHED}) AS q").find(sqlglot.exp.Subquery).this
    assert group_distinct_outer_join(inner) is None
    assert group_distinct_outer_join(_select(PUSHED)) is not None


def test_grouping_needs_plain_column_outputs():
    assert group_distinct_outer_join(_select(PUSHED.replace("SELECT s.x, t.y", "SELECT s.x + 1, t.y"))) is None
    assert group_distinct_outer_join(_select(PUSHED.replace("LEFT JOIN", "JOIN"))) is None
    assert group_distinct_outer_join(_select(PUSHED.replace("GROUP BY y", "GROUP BY y HAVING COUNT(*) > 1"))) is None


def test_anti_join_rule_leaves_shadowing_and_unqualified_reads_alone():
    base = "SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE NOT EXISTS (SELECT 1 FROM b AS e WHERE e.y = a.x)"
    assert null_extend_anti_joined(_select(base)) is not None
    # an unqualified column could belong to the dropped table
    assert null_extend_anti_joined(_select(base.replace("SELECT a.x, e.y", "SELECT a.x, w"))) is None
    # the joined table read from inside another subquery
    assert null_extend_anti_joined(_select(base + " AND a.z IN (SELECT c.w FROM c WHERE c.y = e.w)")) is None
    # a different alias inside that still reads the joined alias is a correlated read of the join
    assert null_extend_anti_joined(_select(
        "SELECT a.x, e.y FROM a LEFT JOIN b AS e ON e.y = a.x WHERE NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x AND e.w = f.w)"
    )) is None
    # GROUP BY only on the dropped table's column cannot be dropped to nothing
    assert null_extend_anti_joined(_select(
        "SELECT e.y, COUNT(*) FROM a LEFT JOIN b AS e ON e.y = a.x WHERE NOT EXISTS (SELECT 1 FROM b AS f WHERE f.y = a.x) GROUP BY e.y"
    )) is None
