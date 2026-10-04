"""COUNT of a column that every branch of a derived union keeps non-NULL is COUNT(*) (union_non_null_counts)."""

import duckdb
import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402
from kumosql.union_non_null_counts import count_over_non_null_union  # noqa: E402

SCHEMA = {"t": ["a", "b"], "u": ["a", "b"]}
TYPES = {table: {column: "INT64" for column in ("a", "b")} for table in SCHEMA}
ROWS = {
    "t": "(1, NULL), (1, 2), (2, NULL), (NULL, 3), (2, 2), (2, 2)",
    "u": "(1, NULL), (NULL, NULL), (3, 3)",
}
GUARDED = "SELECT a AS k, a AS v FROM t WHERE a IS NOT NULL UNION ALL SELECT a AS k, b AS v FROM t WHERE b IS NOT NULL UNION ALL SELECT a AS k, 0 AS v FROM t"
UNGUARDED = "SELECT a AS k, a AS v FROM t WHERE a IS NOT NULL UNION ALL SELECT a AS k, b AS v FROM t"


def _rule(sql):
    out = count_over_non_null_union(sqlglot.parse_one(sql, read="bigquery"))
    return None if out is None else out.sql(dialect="bigquery")


def _same(sql):
    return sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False).status


def _differ(left, right):
    db = duckdb.connect()
    for table, rows in ROWS.items():
        db.execute(f"CREATE TABLE {table}(a BIGINT, b BIGINT)")
        db.execute(f"INSERT INTO {table} VALUES {rows}")
    a, b = run_unoptimized(db, left, right)
    return sorted(a, key=repr) != sorted(b, key=repr)


def test_count_of_a_guarded_union_column_is_count_star():
    sql = f"SELECT x.k, COUNT(x.v) AS n FROM ({GUARDED}) AS x GROUP BY x.k"
    assert _rule(sql) == _same(f"SELECT x.k, COUNT(*) AS n FROM ({GUARDED}) AS x GROUP BY x.k")
    star = f"SELECT x.k, COUNT(*) AS n FROM ({GUARDED}) AS x GROUP BY x.k"
    assert _prove(sql, star) is SmtStatus.PROVEN_EQUIVALENT
    assert not _differ(sql, star)


def test_having_count_of_the_column_is_read_too():
    sql = f"SELECT x.k FROM ({GUARDED}) AS x GROUP BY x.k HAVING COUNT(x.v) > 2"
    assert _rule(sql) == _same(f"SELECT x.k FROM ({GUARDED}) AS x GROUP BY x.k HAVING COUNT(*) > 2")


@pytest.mark.parametrize(
    "sql",
    [
        # a branch without the guard can emit NULL
        f"SELECT x.k, COUNT(x.v) AS n FROM ({UNGUARDED}) AS x GROUP BY x.k",
        # the guard is on another expression than the one output
        "SELECT x.k, COUNT(x.v) AS n FROM (SELECT a AS k, b AS v FROM t WHERE a IS NOT NULL UNION ALL SELECT a AS k, 0 AS v FROM t) AS x GROUP BY x.k",
        # an OR is not a conjunct, a NULL-keeping IS NULL is not a guard
        "SELECT x.k, COUNT(x.v) AS n FROM (SELECT a AS k, b AS v FROM t WHERE b IS NOT NULL OR a = 1 UNION ALL SELECT a AS k, 0 AS v FROM t) AS x GROUP BY x.k",
        "SELECT x.k, COUNT(x.v) AS n FROM (SELECT a AS k, b AS v FROM t WHERE b IS NULL UNION ALL SELECT a AS k, 0 AS v FROM t) AS x GROUP BY x.k",
        # COUNT(DISTINCT ..) counts values, a join can pad the union with NULLs
        f"SELECT x.k, COUNT(DISTINCT x.v) AS n FROM ({GUARDED}) AS x GROUP BY x.k",
        f"SELECT x.k, COUNT(y.b) AS n FROM ({GUARDED}) AS x LEFT JOIN u AS y ON x.k = y.a GROUP BY x.k",
        # a grouped branch: its WHERE does not constrain its output values
        "SELECT x.k, COUNT(x.v) AS n FROM (SELECT a AS k, MAX(b) AS v FROM t WHERE b IS NOT NULL GROUP BY a UNION ALL SELECT a AS k, 0 AS v FROM t) AS x GROUP BY x.k",
    ],
)
def test_near_misses_are_left_alone(sql):
    assert _rule(sql) is None


def test_near_miss_pairs_are_not_proved_and_really_differ():
    left = f"SELECT x.k, COUNT(x.v) AS n FROM ({UNGUARDED}) AS x GROUP BY x.k"
    right = f"SELECT x.k, COUNT(*) AS n FROM ({UNGUARDED}) AS x GROUP BY x.k"
    assert _differ(left, right)
    assert _prove(left, right) is not SmtStatus.PROVEN_EQUIVALENT
    left = f"SELECT x.k, COUNT(DISTINCT x.v) AS n FROM ({GUARDED}) AS x GROUP BY x.k"
    right = f"SELECT x.k, COUNT(*) AS n FROM ({GUARDED}) AS x GROUP BY x.k"
    assert _differ(left, right)
    assert _prove(left, right) is not SmtStatus.PROVEN_EQUIVALENT
