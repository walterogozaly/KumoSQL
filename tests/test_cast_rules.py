from collections import Counter

import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.cast_rules import fold_casts_and_constant_cases
from kumosql.duckdb_load import run_unoptimized

TYPES = {"t": {"i": "int", "b": "bigint", "s": "varchar(20)", "d": "date", "n": "decimal(5,2)", "ti": "tinyint"}}
SCHEMA = {"t": ["i", "b", "s", "d", "n", "ti"], "u": ["k", "x"], "v": ["k", "y"]}


def _fold(sql, dialect="mysql"):
    tree = sqlglot.parse_one(sql, read=dialect)
    out = fold_casts_and_constant_cases(tree, TYPES, dialect)
    return out.sql(dialect=dialect) if out is not None else None


def _proven(left, right, dialect="mysql"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven


def test_integer_casts_to_wide_integer_types_are_dropped():
    assert _fold("SELECT CAST(i AS BIGINT), CAST(SUM(i) AS INTEGER), CAST(i + b AS SIGNED) FROM t") == "SELECT i, SUM(i), i + b FROM t"
    assert _fold("SELECT CAST(COUNT(*) * i AS INTEGER) FROM t") == "SELECT COUNT(*) * i FROM t"


def test_casts_that_can_change_a_value_stay():
    assert _fold("SELECT CAST(i AS TINYINT) FROM t") is None  # a real range check
    assert _fold("SELECT CAST(s AS SIGNED) FROM t") is None
    assert _fold("SELECT CAST(i AS BOOLEAN) FROM t") is None  # 2 is not TRUE everywhere
    assert _fold("SELECT CAST(n AS SIGNED) FROM t") is None
    assert _fold("SELECT CAST(i / 2 AS SIGNED) FROM t") is None
    assert _fold("SELECT CAST(unknown_col AS SIGNED) FROM t") is None
    assert _fold("SELECT CAST(x AS SIGNED) FROM other") is None


def test_ti_cast_to_its_own_width_and_dates_and_booleans():
    assert _fold("SELECT CAST(ti AS TINYINT), CAST(d AS DATE), CAST(i = 1 AS BOOLEAN) FROM t") == "SELECT ti, d, i = 1 FROM t"


def test_types_are_followed_through_derived_tables():
    assert _fold("SELECT CAST(q.c AS BIGINT) FROM (SELECT i + 1 AS c FROM t) AS q") == "SELECT q.c FROM (SELECT i + 1 AS c FROM t) AS q"
    assert _fold("SELECT CAST(q.c AS BIGINT) FROM (SELECT s AS c FROM t) AS q") is None


def test_literals_fold_only_when_they_fit_exactly():
    assert _fold("SELECT CAST('12' AS SIGNED), CAST(5 AS DECIMAL(11, 1)), CAST(1 AS BIGINT) FROM t") == "SELECT 12, 5.0, 1 FROM t"
    assert _fold("SELECT CAST(300 AS TINYINT) FROM t") is None
    assert _fold("SELECT CAST('1e2' AS SIGNED) FROM t") is None
    assert _fold("SELECT CAST(5.25 AS DECIMAL(11, 1)) FROM t") is None
    assert _fold("SELECT CAST(123 AS DECIMAL(3, 1)) FROM t") is None


def test_nested_identical_casts_collapse():
    assert _fold("SELECT CAST(CAST(s AS DATE) AS DATE) FROM t") == "SELECT CAST(s AS DATE) FROM t"


def test_constant_case_conditions():
    assert _fold("SELECT CASE WHEN i = 1 THEN 1 WHEN NOT 1 IS NULL THEN 2 ELSE NULL END FROM t") == "SELECT CASE WHEN i = 1 THEN 1 ELSE 2 END FROM t"
    assert _fold("SELECT CASE WHEN NULL IS NULL THEN s ELSE 'x' END FROM t") == "SELECT s FROM t"
    assert _fold("SELECT CASE WHEN 1 IS NULL THEN s END FROM t") == "SELECT NULL FROM t"


def test_strict_integer_comparisons_become_non_strict():
    assert _fold("SELECT i FROM t GROUP BY i HAVING COUNT(*) > 1 AND 5 > COUNT(s) AND COUNT(*) < 3") == "SELECT i FROM t GROUP BY i HAVING COUNT(*) >= 2 AND 4 >= COUNT(s) AND COUNT(*) <= 2"
    assert _fold("SELECT i FROM t GROUP BY i HAVING COUNT(*) > 1.5") is None
    assert _fold("SELECT i FROM t WHERE i > 3") is None  # x > 50 OR x <= 50 must stay complementary for the SMT


def test_round_gets_its_default_precision():
    assert _fold("SELECT ROUND(n) FROM t") == "SELECT ROUND(n, 0) FROM t"


def test_times_one_only_where_it_cannot_change_a_value():
    assert _fold("SELECT AVG(1.0 * x), SUM(x) * 1.0 / COUNT(*) FROM u") == "SELECT AVG(x), SUM(x) / COUNT(*) FROM u"
    assert _fold("SELECT x * 1 FROM u") is None  # x may be a string
    assert _fold("SELECT SUM(x) * 1.0 / COUNT(*) FROM u", dialect="postgres") is None  # integer division there


def test_proofs_end_to_end():
    assert _proven("SELECT CAST(SUM(i) AS INTEGER) AS c FROM t", "SELECT SUM(i) AS c FROM t")
    assert _proven("SELECT i FROM t GROUP BY i HAVING COUNT(*) > 1", "SELECT i FROM t GROUP BY i HAVING COUNT(*) >= 2")
    assert not _proven("SELECT CAST(i AS TINYINT) FROM t", "SELECT i FROM t")
    assert not _proven("SELECT n FROM t WHERE n > 1", "SELECT n FROM t WHERE n >= 2")


def test_natural_join_is_using_over_shared_columns():
    assert _proven("SELECT * FROM u NATURAL JOIN v", "SELECT u.k, u.x, v.y FROM u JOIN v ON u.k = v.k")
    assert _proven("SELECT k, y FROM u NATURAL LEFT JOIN v", "SELECT u.k, v.y FROM u LEFT JOIN v ON u.k = v.k")
    assert _proven("SELECT * FROM u NATURAL JOIN (SELECT y FROM v) AS w", "SELECT u.k, u.x, w.y FROM u CROSS JOIN (SELECT y FROM v) AS w")
    assert not _proven("SELECT u.x FROM u NATURAL JOIN v", "SELECT u.x FROM u CROSS JOIN v")


def test_using_over_a_derived_table_lists_the_merged_column_once_first():
    star = "SELECT * FROM (SELECT * FROM u WHERE x < 3) AS d JOIN v USING (k)"
    assert _proven(star, "SELECT d.k, d.x, v.y FROM (SELECT k, x FROM u WHERE x < 3) AS d JOIN v ON d.k = v.k")
    assert not _proven(star, "SELECT d.k, d.x, v.k, v.y FROM (SELECT k, x FROM u WHERE x < 3) AS d JOIN v ON d.k = v.k")
    unaliased = "SELECT * FROM (SELECT * FROM u WHERE x < 3) JOIN v USING (k)"
    assert _proven(unaliased, "SELECT u.k, u.x, v.y FROM u JOIN v ON u.k = v.k WHERE u.x < 3")


def test_exact_numeric_casts_keep_order_and_nulls():
    assert _fold("SELECT i FROM t ORDER BY CAST(i AS DOUBLE) IS NULL, CAST(i AS DOUBLE)") == "SELECT i FROM t ORDER BY i IS NULL, i"
    assert _fold("SELECT b FROM t ORDER BY CAST(b AS DOUBLE)") is None  # BIGINT is not exact in a double
    assert _fold("SELECT i FROM t ORDER BY CAST(i AS DECIMAL(5, 2))") is None  # too narrow
    assert _fold("SELECT i FROM t ORDER BY CAST(i AS FLOAT64)", dialect="bigquery") is None  # BigQuery's INT is INT64
    assert _fold("SELECT ti FROM t GROUP BY ti ORDER BY CAST(SUM(ti) AS DOUBLE)") is None  # a sum of many rows has no bound
    assert _fold("SELECT i FROM t ORDER BY CAST(i * ti AS DOUBLE)") is None  # up to 13 digits
    assert _fold("SELECT ti FROM t ORDER BY CAST(ti * ti + 1 AS DOUBLE)") == "SELECT ti FROM t ORDER BY ti * ti + 1"


def _duckdb_bags(ddl, rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute(ddl)
    db.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in rows[0])})", rows)
    return [Counter(result) for result in run_unoptimized(db, *queries)]


# BigQuery's integer literals are INT64; DuckDB reads 2000000000 as a 32-bit INTEGER.
_BIG = "CAST(2000000000 AS BIGINT)"

# A cast dropped from an ORDER BY key must neither tie nor reorder rows. Each pair below differs on
# DuckDB (with the declared type replayed as DuckDB's equal type; a tie-breaking second key under
# LIMIT 1 shows a tie the cast makes), so it must stay unproven.
# (left, right, dialect, declared type of t.x and t.y, DuckDB table, rows, DuckDB spelling or None to transpile)
TIES_MADE_BY_A_CAST = [
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(CASE WHEN x = 0 THEN 2000000000 * 2000000000 ELSE 2000000000 * 2000000000 - 1 END AS FLOAT64), x LIMIT 1",
        "SELECT x FROM t ORDER BY CASE WHEN x = 0 THEN 2000000000 * 2000000000 ELSE 2000000000 * 2000000000 - 1 END, x LIMIT 1",
        "bigquery", "INT64", "CREATE TABLE t (x BIGINT)", [(0,), (1,)],
        (
            f"SELECT x FROM t ORDER BY CAST(CASE WHEN x = 0 THEN {_BIG} * {_BIG} ELSE {_BIG} * {_BIG} - 1 END AS DOUBLE), x LIMIT 1",
            f"SELECT x FROM t ORDER BY CASE WHEN x = 0 THEN {_BIG} * {_BIG} ELSE {_BIG} * {_BIG} - 1 END, x LIMIT 1",
        ),
        id="s007-004-a-product-outgrows-its-operands",
    ),
    pytest.param(
        # MySQL multiplies INT columns as BIGINT, so the DuckDB table holds the INT values as BIGINT.
        "SELECT y FROM t ORDER BY CAST(x * 8388608 + y AS DOUBLE), y DESC LIMIT 1",
        "SELECT y FROM t ORDER BY x * 8388608 + y, y DESC LIMIT 1",
        "mysql", "INT", "CREATE TABLE t (x BIGINT, y BIGINT)", [(2**31 - 1, 0), (2**31 - 1, 1)], None,
        id="s007-004-int-column-arithmetic-outgrows-a-double",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(x AS FLOAT), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "duckdb", "INT", "CREATE TABLE t (x INTEGER)", [(2**24,), (2**24 + 1,)], None,
        id="s007-005-duckdb-float-is-32-bit",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(x AS FLOAT(24)), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "postgres", "int4", "CREATE TABLE t (x INTEGER)", [(2**24,), (2**24 + 1,)],
        ("SELECT x FROM t ORDER BY CAST(x AS REAL), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x, x DESC LIMIT 1"),
        id="s007-005-postgres-float-24-is-real",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(x AS FLOAT64), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "bigquery", "INT", "CREATE TABLE t (x BIGINT)", [(2**53,), (2**53 + 1,)], None,
        id="s007-003-bigquery-int-is-int64",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(CAST(x AS INT) AS FLOAT64), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "bigquery", "INT64", "CREATE TABLE t (x BIGINT)", [(2**53,), (2**53 + 1,)],
        ("SELECT x FROM t ORDER BY CAST(CAST(x AS BIGINT) AS DOUBLE), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x, x DESC LIMIT 1"),
        id="s007-003-bigquery-cast-to-int-is-int64",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(x AS DOUBLE PRECISION), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "postgres", "int8", "CREATE TABLE t (x BIGINT)", [(2**53,), (2**53 + 1,)], None,
        id="s007-003-postgres-int8-is-bigint",
    ),
    pytest.param(
        "SELECT x FROM t ORDER BY CAST(x AS DOUBLE), x DESC LIMIT 1",
        "SELECT x FROM t ORDER BY x, x DESC LIMIT 1",
        "mysql", "INT8", "CREATE TABLE t (x BIGINT)", [(2**53,), (2**53 + 1,)], None,
        id="s007-003-mysql-int8-is-bigint",
    ),
]


@pytest.mark.parametrize("left, right, dialect, declared, ddl, rows, replay", TIES_MADE_BY_A_CAST)
def test_casts_that_can_tie_values_stay_in_order_by(left, right, dialect, declared, ddl, rows, replay):
    replay = replay or [sqlglot.transpile(q, read=dialect, write="duckdb")[0] for q in (left, right)]
    before, after = _duckdb_bags(ddl, rows, *replay)
    assert before != after
    assert not prove_equivalent_algebraic(left, right, schema={"t": ["x", "y"]}, types={"t": {"x": declared, "y": declared}}, dialect=dialect, compare_names=False).proven


# Near misses: the cast is exact on every value of the declared type, so these stay proven.
EXACT_CASTS = [
    pytest.param("SELECT x FROM t ORDER BY CAST(x AS DOUBLE PRECISION), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x LIMIT 1", "postgres", "int4", id="postgres-int4-in-a-double"),
    pytest.param("SELECT x FROM t ORDER BY CAST(x AS DOUBLE), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x LIMIT 1", "duckdb", "INTEGER", id="duckdb-integer-in-a-double"),
    pytest.param("SELECT CAST(x AS INT64) AS c FROM t", "SELECT x AS c FROM t", "bigquery", "INT", id="bigquery-int-is-already-int64"),
    pytest.param("SELECT x FROM t ORDER BY CAST(x * x AS DOUBLE), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x * x, x DESC LIMIT 1", "mysql", "SMALLINT", id="smallint-product-in-a-double"),
    pytest.param("SELECT x FROM t ORDER BY CAST(x AS FLOAT), x DESC LIMIT 1", "SELECT x FROM t ORDER BY x LIMIT 1", "duckdb", "SMALLINT", id="duckdb-smallint-in-a-float"),
]


@pytest.mark.parametrize("left, right, dialect, declared", EXACT_CASTS)
def test_exact_casts_stay_proven(left, right, dialect, declared):
    assert prove_equivalent_algebraic(left, right, schema={"t": ["x"]}, types={"t": {"x": declared}}, dialect=dialect, compare_names=False).proven
