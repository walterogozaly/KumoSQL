import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.null_rejecting_joins import left_join_to_inner, rejected_tables

SCHEMA = {"s": ["id", "pid", "buyer"], "p": ["pid", "name", "price"], "u": ["pid", "day", "units"]}


def _rule(sql):
    out = left_join_to_inner(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def _proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


@pytest.mark.parametrize(
    "condition, expected",
    [
        ("p.name = 'x'", {"p"}),
        ("s.day BETWEEN p.lo AND p.hi", {"s", "p"}),
        ("p.price + 1 > s.id", {"p", "s"}),
        ("UPPER(p.name) LIKE 'A%'", {"p"}),
        ("p.name IN ('a', 'b')", {"p"}),
        ("NOT p.name IS NULL", {"p"}),
        ("p.name = 'a' OR p.price > 1", {"p"}),
        ("p.name = 'a' OR s.id > 1", set()),
        ("p.name = 'a' AND s.id > 1", {"p", "s"}),
        ("p.name IS NULL", set()),
        ("p.name <=> 'a'", set()),
        ("COALESCE(p.name, 'a') = 'a'", set()),
        ("NOT p.name IN (SELECT name FROM q)", set()),
        ("p.name IN (SELECT name FROM q)", set()),
        ("NOT p.name = 'a'", set()),
    ],
)
def test_rejected_tables(condition, expected):
    tree = sqlglot.parse_one(f"SELECT 1 FROM s WHERE {condition}", read="mysql")
    assert rejected_tables(tree.args["where"].this) == expected


def test_left_join_with_null_rejecting_where_becomes_inner():
    out = _rule("SELECT s.buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE p.name = 'x'")
    assert out == "SELECT s.buyer FROM s JOIN p ON p.pid = s.pid WHERE p.name = 'x'"
    assert _rule("SELECT s.buyer FROM s LEFT OUTER JOIN p ON p.pid = s.pid WHERE p.name = 'x'") == out


def test_filters_on_the_preserved_side_or_not_rejecting_leave_the_join():
    assert _rule("SELECT s.buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE s.buyer = 'x'") is None
    assert _rule("SELECT s.buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE p.name IS NULL") is None
    assert _rule("SELECT s.buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE p.name = 'x' OR s.id = 1") is None
    assert _rule("SELECT buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE name = 'x'") is None  # unqualified
    assert _rule("SELECT s.buyer FROM s LEFT JOIN p ON p.pid = s.pid RIGHT JOIN u ON u.pid = s.pid WHERE p.name = 'x'") is None
    assert _rule("SELECT s.buyer FROM s LEFT JOIN p USING (pid) WHERE p.name = 'x'") is None


def test_rejecting_filter_over_a_derived_table_reaches_its_join():
    sql = "SELECT t.buyer FROM (SELECT s.buyer, p.name AS pname FROM s LEFT JOIN p ON s.pid = p.pid) AS t WHERE t.pname = 'x'"
    assert _rule(sql) == "SELECT t.buyer FROM (SELECT s.buyer, p.name AS pname FROM s JOIN p ON s.pid = p.pid) AS t WHERE t.pname = 'x'"
    # A bare column names the derived table's output when it is the only source.
    assert "LEFT" not in _rule(sql.replace("t.pname = 'x'", "pname = 'x'"))
    # An output that hides NULLs, or a derived table that aggregates, is left alone.
    assert _rule(sql.replace("p.name AS pname", "COALESCE(p.name, 'x') AS pname")) is None
    assert _rule(sql.replace("p.name AS pname FROM s LEFT JOIN p ON s.pid = p.pid", "MAX(p.name) AS pname FROM s LEFT JOIN p ON s.pid = p.pid GROUP BY s.buyer")) is None
    # The same name in the outer select does not count as the inner table.
    assert _rule("SELECT t.buyer FROM (SELECT s.buyer, s.id FROM s LEFT JOIN p ON s.pid = p.pid) AS t JOIN p ON p.pid = t.id WHERE p.name = 'x'") is None


def test_prover_reads_left_join_with_rejecting_filter_as_inner():
    inner = "SELECT DISTINCT s.buyer FROM p JOIN s ON s.pid = p.pid WHERE p.name = 'x' AND s.buyer NOT IN (SELECT s.buyer FROM p JOIN s ON s.pid = p.pid WHERE p.name = 'y')"
    left = "SELECT DISTINCT buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE name = 'x' AND buyer NOT IN (SELECT buyer FROM s LEFT JOIN p ON p.pid = s.pid WHERE name = 'y')"
    assert _proved(inner, left)
    grouped_inner = "SELECT u.pid, SUM(u.units * p.price) FROM p JOIN u ON p.pid = u.pid AND u.day BETWEEN p.price AND p.price + 1 GROUP BY u.pid"
    grouped_left = "SELECT u.pid, SUM(u.units * p.price) FROM u LEFT JOIN p ON p.pid = u.pid WHERE u.day BETWEEN p.price AND p.price + 1 GROUP BY u.pid"
    assert _proved(grouped_inner, grouped_left)
    cte = "WITH t AS (SELECT s.buyer, p.name FROM s LEFT JOIN p ON s.pid = p.pid) SELECT buyer FROM t WHERE name = 'x'"
    assert _proved("SELECT s.buyer FROM s JOIN p ON s.pid = p.pid WHERE p.name = 'x'", cte)


def test_prover_keeps_left_join_when_the_filter_admits_null_extended_rows():
    inner = "SELECT s.buyer FROM s JOIN p ON p.pid = s.pid WHERE p.name = 'x' OR s.id = 1"
    assert not _proved(inner, inner.replace("JOIN p", "LEFT JOIN p"))
    inner = "SELECT s.buyer FROM s JOIN p ON p.pid = s.pid WHERE COALESCE(p.name, 'x') = 'x'"
    assert not _proved(inner, inner.replace("JOIN p", "LEFT JOIN p"))
    inner = "SELECT s.buyer FROM s JOIN p ON p.pid = s.pid WHERE NOT p.name IN (SELECT name FROM p WHERE price > 1)"
    assert not _proved(inner, inner.replace("JOIN p ON", "LEFT JOIN p ON"))
