import sqlglot

from kumosql.date_ranges import extract_to_ranges
from kumosql.dedup_join_rules import drop_unread_outer_join
from kumosql.empty_rules import canonical_empty, is_empty, propagate_empty


def _sql(tree):
    return tree.sql(dialect="mysql")


def test_empty_sources_propagate_but_global_aggregates_keep_their_row():
    tree = propagate_empty(sqlglot.parse_one("SELECT a.x FROM (SELECT x FROM t WHERE FALSE) AS a JOIN u ON a.x = u.x"))
    assert is_empty(tree)
    assert not is_empty(sqlglot.parse_one("SELECT COUNT(*) FROM (SELECT x FROM t WHERE FALSE) AS a"))
    assert not is_empty(sqlglot.parse_one("SELECT u.x FROM (SELECT x FROM t WHERE 1 = 0) AS a RIGHT JOIN u ON a.x = u.x"))


def test_left_join_to_an_empty_relation_pads_with_nulls():
    tree = propagate_empty(sqlglot.parse_one("SELECT t.x, e.y FROM t LEFT JOIN (SELECT y FROM u WHERE FALSE) AS e ON t.x = e.y"))
    assert _sql(tree) == "SELECT t.x, NULL FROM t"


def test_exists_over_an_empty_relation_is_false():
    tree = propagate_empty(sqlglot.parse_one("SELECT x FROM t WHERE EXISTS (SELECT 1 FROM (SELECT 1 AS c WHERE FALSE) AS e WHERE e.c = t.x)"))
    assert "EXISTS" not in _sql(tree)


def test_canonical_empty_keeps_output_names():
    tree = canonical_empty(sqlglot.parse_one("SELECT a AS p, b FROM t WHERE FALSE"))
    assert [e.alias_or_name for e in tree.expressions] == ["p", "b"]


def test_order_by_without_limit_in_derived_table_is_dropped():
    tree = propagate_empty(sqlglot.parse_one("SELECT d.x FROM (SELECT x FROM t ORDER BY x) AS d"))
    assert "ORDER" not in _sql(tree)
    tree = propagate_empty(sqlglot.parse_one("SELECT d.x FROM (SELECT x FROM t ORDER BY x LIMIT 2) AS d"))
    assert "ORDER" in _sql(tree)


def test_extract_year_and_month_become_ranges():
    tree = extract_to_ranges(sqlglot.parse_one("SELECT 1 FROM t WHERE EXTRACT(YEAR FROM d) = 2014 AND EXTRACT(MONTH FROM d) = 12 AND x = 1"))
    assert _sql(tree) == "SELECT 1 FROM t WHERE x = 1 AND (d >= CAST('2014-12-01' AS DATE) AND d < CAST('2015-01-01' AS DATE))"


def test_unread_outer_join_dropped_only_when_duplicates_cannot_matter():
    blind = sqlglot.parse_one("SELECT DISTINCT a.x FROM a LEFT JOIN b ON a.k = b.k")
    assert _sql(drop_unread_outer_join(blind)) == "SELECT DISTINCT a.x FROM a"
    assert drop_unread_outer_join(sqlglot.parse_one("SELECT a.x FROM a LEFT JOIN b ON a.k = b.k")) is None
    assert drop_unread_outer_join(sqlglot.parse_one("SELECT DISTINCT a.x FROM a LEFT JOIN b ON a.k = b.k WHERE b.y = 1")) is None
    assert drop_unread_outer_join(sqlglot.parse_one("SELECT a.x, COUNT(*) FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.x")) is None
    grouped = sqlglot.parse_one("SELECT d.x, MAX(d.y) FROM (SELECT a.x, a.y FROM a RIGHT JOIN b ON a.k = b.k) AS d GROUP BY d.x")
    assert drop_unread_outer_join(grouped) is None  # the RIGHT JOIN's kept side is b, and a is read
    kept = sqlglot.parse_one("SELECT d.k FROM (SELECT b.k FROM a RIGHT JOIN b ON a.k = b.k) AS d GROUP BY d.k")
    assert _sql(drop_unread_outer_join(kept)) == "SELECT d.k FROM (SELECT b.k FROM b) AS d GROUP BY d.k"


def test_filter_above_a_derived_left_join_on_the_padded_side_makes_it_inner():
    from kumosql.outer_filters import strengthen_derived_outer_join

    tree = sqlglot.parse_one("SELECT d.x FROM (SELECT a.x, b.y AS c9 FROM a LEFT JOIN b ON a.k = b.k) AS d WHERE NOT d.c9 IS NULL")
    assert "LEFT" not in _sql(strengthen_derived_outer_join(tree))
    kept = sqlglot.parse_one("SELECT d.x FROM (SELECT a.x, b.y AS c9 FROM a LEFT JOIN b ON a.k = b.k) AS d WHERE NOT d.x IS NULL")
    assert strengthen_derived_outer_join(kept) is None
    grouped = sqlglot.parse_one("SELECT d.x FROM (SELECT a.x, MAX(b.y) AS c9 FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.x) AS d WHERE d.c9 > 1")
    assert strengthen_derived_outer_join(grouped) is None


def test_duplicate_removal_in_a_source_is_redundant_under_a_duplicate_blind_select():
    from kumosql.dedup_join_rules import strip_distinct_sources

    tree = sqlglot.parse_one("SELECT d.k FROM (SELECT k FROM t GROUP BY k) AS d JOIN u ON d.k = u.k GROUP BY d.k")
    assert _sql(strip_distinct_sources(tree)) == "SELECT d.k FROM (SELECT k FROM t) AS d JOIN u ON d.k = u.k GROUP BY d.k"
    counted = sqlglot.parse_one("SELECT d.k, COUNT(*) FROM (SELECT k FROM t GROUP BY k) AS d GROUP BY d.k")
    assert strip_distinct_sources(counted) is None
    aggregated = sqlglot.parse_one("SELECT DISTINCT d.k FROM (SELECT k, SUM(x) AS s FROM t GROUP BY k) AS d")
    assert strip_distinct_sources(aggregated) is None


def test_constant_group_key_is_dropped_not_turned_into_an_ordinal():
    from kumosql.algebraic_equivalence import normalize

    out = normalize("SELECT d.c0, MAX(d.c1) FROM (SELECT 4 AS c0, x AS c1 FROM t) AS d GROUP BY d.c0", dialect="mysql")
    assert "GROUP BY 4" not in out


def test_mysql_cast_to_char_of_a_varchar_column_is_identity():
    from kumosql.algebraic_equivalence import normalize

    out = normalize("SELECT CAST(t.name AS CHAR) AS n FROM t", dialect="mysql", schema={"t": ["name"]}, types={"t": {"name": "VARCHAR(20)"}})
    assert "CAST" not in out
