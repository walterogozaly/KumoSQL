"""LEFT JOIN read as inner: by a foreign key, by HAVING, and nested join groups read as derived tables."""

import pytest
import sqlglot

from kumosql.join_rewrites import _fk_left_join_to_inner, _having_left_join_to_inner, _map_equalities_into_left_join, _nested_join_to_derived

SCHEMA = {"a": ["id", "k", "x"], "b": ["id", "k", "y"], "c": ["id", "k", "z"]}
FK = {"a": [(("k",), "b", ("id",))]}
NOT_NULL = {"a": frozenset({"id", "k"})}


def _sql(node):
    return node.sql(dialect="bigquery") if node is not None else None


def _fk(sql, not_null=NOT_NULL, foreign_keys=FK):
    return _sql(_fk_left_join_to_inner(sqlglot.parse_one(sql, read="bigquery"), not_null, foreign_keys))


def _having(sql):
    return _sql(_having_left_join_to_inner(sqlglot.parse_one(sql, read="bigquery")))


def _nested(sql):
    return _sql(_nested_join_to_derived(sqlglot.parse_one(sql, read="bigquery"), SCHEMA))


def test_foreign_key_makes_the_left_join_inner():
    assert _fk("SELECT a.id, b.y FROM a LEFT JOIN b ON a.k = b.id") == "SELECT a.id, b.y FROM a JOIN b ON a.k = b.id"
    assert _fk("SELECT a.id, b.y FROM a LEFT JOIN b ON b.id = a.k") == "SELECT a.id, b.y FROM a JOIN b ON b.id = a.k"
    assert _fk("SELECT a.id FROM c JOIN a ON a.id = c.k LEFT JOIN b ON a.k = b.id") == "SELECT a.id FROM c JOIN a ON a.id = c.k JOIN b ON a.k = b.id"


@pytest.mark.parametrize(
    "sql, not_null",
    [
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.id", {}),  # a.k may be NULL
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.id AND b.y > 0", NOT_NULL),  # the parent row may fail b.y > 0
        ("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", NOT_NULL),  # not the referenced column
        ("SELECT a.id FROM b LEFT JOIN a ON a.k = b.id", NOT_NULL),  # the parent is the preserved side
        ("SELECT a.id FROM c LEFT JOIN a ON a.id = c.k LEFT JOIN b ON a.k = b.id", NOT_NULL),  # a itself null-extended
        ("SELECT a.id FROM a LEFT JOIN (SELECT * FROM b WHERE y > 0) AS b ON a.k = b.id", NOT_NULL),  # filtered parent
    ],
)
def test_foreign_key_rule_refuses(sql, not_null):
    assert _fk(sql, not_null) is None


def test_having_that_needs_a_match_makes_the_left_join_inner():
    out = _having("SELECT a.id, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING COUNT(b.k) > 0")
    assert out == "SELECT a.id, COUNT(b.k) AS n FROM a JOIN b ON a.k = b.k GROUP BY a.id HAVING COUNT(b.k) > 0"
    assert _having("SELECT a.x, MAX(b.y) AS m FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.x HAVING SUM(b.y) + COUNT(b.k) > 2") is not None
    assert _having("SELECT COUNT(DISTINCT b.y) AS n FROM a LEFT JOIN b ON a.k = b.k HAVING NOT COUNT(b.k) = 0") is not None
    # only the join whose rows the aggregates skip is made inner
    out = _having("SELECT a.id, COUNT(c.k) AS n FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON c.k = a.x GROUP BY a.id HAVING COUNT(c.k) > 0")
    assert "LEFT JOIN b" in out and "LEFT JOIN c" not in out


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.id, COUNT(*) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING COUNT(*) > 0",  # counts null-extended rows
        "SELECT a.x, SUM(a.id) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.x HAVING COUNT(b.k) > 0",  # reads the preserved side
        "SELECT a.id, SUM(COALESCE(b.y, 1)) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING SUM(COALESCE(b.y, 1)) > 0",
        "SELECT a.id, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING COUNT(b.k) >= 0",  # TRUE with no match
        "SELECT a.id, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING COUNT(b.k) < 5",
        "SELECT a.id, MAX(b.y) AS m FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id HAVING MAX(b.y) IS NULL",
        "SELECT a.x, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.x HAVING COUNT(b.k) > 0 OR a.x > 1",
        "SELECT a.id, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k RIGHT JOIN c ON c.k = a.x GROUP BY a.id HAVING COUNT(b.k) > 0",
        "SELECT a.id, COUNT(b.k) AS n FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.id",
    ],
)
def test_having_rule_refuses(sql):
    assert _having(sql) is None


def test_nested_join_group_becomes_a_derived_table():
    out = _nested("SELECT a.id, b.y, c.z FROM c LEFT JOIN (a JOIN b ON a.k = b.k) ON c.k = b.id")
    assert out == (
        "SELECT _g0.a__id AS id, _g0.b__y AS y, c.z FROM c LEFT JOIN "
        "(SELECT a.id AS a__id, b.y AS b__y, b.id AS b__id FROM a JOIN b ON a.k = b.k) AS _g0 ON c.k = _g0.b__id"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, c.z FROM c LEFT JOIN (a JOIN b ON a.k = b.k) ON c.k = b.id",  # bare column
        "SELECT * FROM c LEFT JOIN (a JOIN b ON a.k = b.k) ON c.k = b.id",
        "SELECT a.id FROM c LEFT JOIN (a JOIN b ON a.k = c.k) ON c.k = b.id",  # group reads an outer source
        "SELECT a.id FROM c LEFT JOIN (a JOIN d ON a.k = d.k) ON c.k = d.id",  # unknown member columns
        "SELECT a.id FROM c LEFT JOIN (a JOIN b USING (k)) ON c.k = b.id",
    ],
)
def test_nested_join_rule_refuses(sql):
    assert _nested(sql) is None


def _mapped(sql):
    return _sql(_map_equalities_into_left_join(sqlglot.parse_one(sql, read="bigquery")))


def test_where_test_on_a_joined_column_also_filters_the_far_side():
    assert _mapped("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.k = 2") == "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k AND b.k = 2 WHERE a.k = 2"
    assert _mapped("SELECT a.id FROM a LEFT JOIN b ON b.k = a.k WHERE a.k IN (1, 2) AND a.x > 0") == (
        "SELECT a.id FROM a LEFT JOIN b ON b.k = a.k AND b.k IN (1, 2) WHERE a.k IN (1, 2) AND a.x > 0"
    )
    assert _mapped("SELECT a.id FROM a LEFT JOIN b ON a.k = b.k AND b.k = 2 WHERE a.k = 2") is None  # already there


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.k = 2 OR a.x = 1",  # not a conjunct
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.x = 2",  # not a joined column
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.k = 2",  # the far side's own column
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.k = a.x",  # not a literal test
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE a.k IN (SELECT z FROM c)",
        "SELECT a.id FROM a FULL JOIN b ON a.k = b.k WHERE a.k = 2",
    ],
)
def test_equality_mapping_refuses(sql):
    assert _mapped(sql) is None
