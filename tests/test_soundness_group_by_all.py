"""GROUP BY ALL infers its keys from the select list (determinism and robustness audit, DR-01).

Both provers used to ignore the ``all`` flag: an aggregate-only ``GROUP BY ALL`` read as a grouped query with no
row on empty input, so ``COUNT(*) .. GROUP BY ALL`` was proved equal to ``GROUP BY a`` and refuted against the
global ``COUNT(*)``. The keys are now spelled out (as positions) before proving; shapes whose keys depend on the
engine or on outer scopes are declined.
"""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct, expand_group_by_all
from kumosql.bounded_equivalence import BoundedStatus, check_bounded, schema_from_prover
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

SCHEMA = {"t": ["a", "b", "c"]}
PROVERS = (prove_equivalent_smt, prove_equivalent_algebraic)


def _duckdb_rows(sql: str, rows: list[tuple]) -> list[tuple]:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER)")
    if rows:
        db.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    return sorted(db.execute(sql).fetchall(), key=repr)


@pytest.mark.parametrize("prove", PROVERS)
def test_aggregate_only_group_by_all_is_not_proved_equal_to_a_grouped_query(prove):
    left = "SELECT COUNT(*) AS n FROM t WHERE a = 1 GROUP BY ALL"
    right = "SELECT COUNT(*) AS n FROM t WHERE a = 1 GROUP BY a"
    assert _duckdb_rows(left, []) == [(0,)] and _duckdb_rows(right, []) == []
    result = prove(left, right, schema=SCHEMA)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
    if result.counterexample is not None:
        assert result.counterexample.tables["t"] == []
        assert list(result.counterexample.left_rows) == [(0,)] and list(result.counterexample.right_rows) == []


@pytest.mark.parametrize("prove", PROVERS)
def test_aggregate_only_group_by_all_is_not_a_constant_key(prove):
    left, right = "SELECT COUNT(*) AS n FROM t GROUP BY ALL", "SELECT COUNT(*) AS n FROM t GROUP BY 1 + 0"
    assert _duckdb_rows(left, []) == [(0,)] and _duckdb_rows(right, []) == []
    assert prove(left, right, schema=SCHEMA).status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("prove", PROVERS)
def test_aggregate_only_group_by_all_is_the_global_aggregate(prove):
    left = "SELECT COUNT(*) AS n FROM t GROUP BY ALL"
    right = "SELECT COUNT(*) AS n FROM t"
    assert _duckdb_rows(left, []) == _duckdb_rows(right, []) == [(0,)]
    assert prove(left, right, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("prove", PROVERS)
@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT a, COUNT(*) AS n FROM t GROUP BY ALL", "SELECT a, COUNT(*) AS n FROM t GROUP BY a"),
        ("SELECT a + 1 AS k, b, SUM(c) AS s FROM t GROUP BY ALL", "SELECT a + 1 AS k, b, SUM(c) AS s FROM t GROUP BY a + 1, b"),
        ("SELECT 'x' AS tag, a, COUNT(*) AS n FROM t GROUP BY ALL", "SELECT 'x' AS tag, a, COUNT(*) AS n FROM t GROUP BY a"),
    ],
)
def test_group_by_all_groups_by_the_non_aggregate_items(prove, left, right):
    rows = [(1, 2, 3), (1, 2, 4), (None, 2, 5), (2, None, None)]
    assert _duckdb_rows(left, rows) == _duckdb_rows(right, rows)
    assert prove(left, right, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS k, COUNT(*) AS n FROM t GROUP BY ALL",  # only a constant: a key in DuckDB, not in BigQuery
        "SELECT 1 AS k FROM t GROUP BY ALL",
        "SELECT * FROM t GROUP BY ALL",
        "SELECT a, SUM(b) OVER () AS w FROM t GROUP BY ALL",
        "SELECT a, (SELECT 1) AS s, COUNT(*) AS n FROM t GROUP BY ALL",
        "SELECT a, MY_UDF(b) AS u, COUNT(*) AS n FROM t GROUP BY ALL",
        "SELECT b, COUNT(*) + a AS n FROM t GROUP BY ALL",
        "SELECT a FROM t WHERE a IN (SELECT b FROM t GROUP BY ALL)",  # may read outer columns
    ],
)
def test_group_by_all_shapes_that_are_declined(sql):
    with pytest.raises(UnmodeledConstruct):
        expand_group_by_all(sqlglot.parse_one(sql, read="bigquery"))
    for prove in PROVERS:
        result = prove(sql, sql, schema=SCHEMA)
        assert result.status is SmtStatus.NOT_PROVEN and "GROUP BY ALL" in result.reason


def test_group_by_all_in_derived_tables_and_ctes_is_spelled_out():
    tree = expand_group_by_all(sqlglot.parse_one(
        "WITH c AS (SELECT a, b, COUNT(*) AS n FROM t GROUP BY ALL) SELECT x.a FROM (SELECT a, MAX(n) AS m FROM c GROUP BY ALL) AS x",
        read="bigquery",
    ))
    assert sorted(g.sql() for g in tree.find_all(sqlglot.exp.Group)) == ["GROUP BY 1", "GROUP BY 1, 2"]


@pytest.mark.parametrize("prove", PROVERS)
def test_a_key_column_may_also_appear_next_to_an_aggregate(prove):
    # BigQuery accepts this (a is a key); DuckDB's binder does not, so it is not executed here.
    left, right = "SELECT a, COUNT(*) + a AS n FROM t GROUP BY ALL", "SELECT a, COUNT(*) + a AS n FROM t GROUP BY a"
    assert prove(left, right, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT


def test_bounded_checker_reads_group_by_all_keys():
    schema = schema_from_prover({"t": ["a"]}, types={"t": {"a": "INT64"}})
    keyed = check_bounded("SELECT a, COUNT(*) AS n FROM t GROUP BY ALL", "SELECT a, COUNT(*) AS n FROM t GROUP BY a", schema)
    assert keyed.status is BoundedStatus.BOUNDED_EQUIVALENT
    empty = check_bounded(
        "SELECT COUNT(*) AS n FROM t WHERE a = 1 GROUP BY ALL", "SELECT COUNT(*) AS n FROM t WHERE a = 1 GROUP BY a", schema
    )
    assert empty.status is BoundedStatus.DIFFERENT
