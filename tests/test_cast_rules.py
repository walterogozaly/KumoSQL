import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.cast_rules import fold_casts_and_constant_cases

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
    assert _fold("SELECT i FROM t WHERE i > 3 AND 5 > b AND i < -2") == "SELECT i FROM t WHERE i >= 4 AND 4 >= b AND i <= -3"
    assert _fold("SELECT i FROM t GROUP BY i HAVING COUNT(*) > 1") == "SELECT i FROM t GROUP BY i HAVING COUNT(*) >= 2"
    assert _fold("SELECT i FROM t WHERE n > 3 AND s > 3 AND i > 3.5") is None


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
