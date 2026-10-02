import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.union_filter_rules import push_filter_into_set_operation

SCHEMA = {"products": ["pid", "store1", "store2"], "t": ["a", "b"], "u": ["c", "d"]}


def _rule(sql):
    out = push_filter_into_set_operation(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def _proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven


def test_filter_moves_into_every_branch_by_position():
    out = _rule("SELECT s.a FROM (SELECT a, b FROM t UNION ALL SELECT c, d + 1 FROM u WHERE c > 0) AS s WHERE s.b > 1 AND s.a = 2")
    assert out == "SELECT s.a FROM (SELECT a, b FROM t WHERE b > 1 AND a = 2 UNION ALL SELECT c, d + 1 FROM u WHERE c > 0 AND (d + 1) > 1 AND c = 2) AS s"


def test_identity_projection_left_after_the_move_is_the_set_operation():
    assert _rule("SELECT s.a, s.b FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE s.b > 1") == (
        "SELECT a, b FROM t WHERE b > 1 UNION SELECT c, d FROM u WHERE d > 1"
    )
    assert _rule("SELECT s.b, s.a FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE b > 1") == (
        "SELECT b, a FROM t WHERE b > 1 UNION SELECT d, c FROM u WHERE d > 1"
    )


def test_conjuncts_it_cannot_move_stay_outside():
    out = _rule("SELECT s.a FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE s.a > 1 AND s.b IN (SELECT d FROM u)")
    assert out == "SELECT s.a FROM (SELECT a, b FROM t WHERE a > 1 UNION SELECT c, d FROM u WHERE c > 1) AS s WHERE s.b IN (SELECT d FROM u)"
    assert _rule("SELECT s.a FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE RAND() > 0.5") is None


def test_grouped_limited_or_joined_set_operations_are_left_alone():
    assert _rule("SELECT s.a FROM (SELECT a, SUM(b) AS b FROM t GROUP BY a UNION SELECT c, d FROM u) AS s WHERE s.b > 1") is None
    assert _rule("SELECT s.a FROM (SELECT a, b FROM t UNION SELECT c, d FROM u LIMIT 3) AS s WHERE s.b > 1") is None
    assert _rule("SELECT s.a FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s JOIN t ON t.a = s.a WHERE s.b > 1") is None


def test_prover_matches_filter_outside_and_inside_a_union():
    outside = "SELECT * FROM (SELECT pid, 'S1' AS store, store1 AS price FROM products UNION SELECT pid, 'S2' AS store, store2 AS price FROM products) AS x WHERE price IS NOT NULL"
    inside = "SELECT pid, 'S1' AS store, store1 AS price FROM products WHERE store1 IS NOT NULL UNION SELECT pid, 'S2', store2 FROM products WHERE store2 IS NOT NULL"
    assert _proved(outside, inside)
    reordered = "SELECT y.pid, y.store, y.price FROM (SELECT pid, store1 AS price, 'S1' AS store FROM products UNION SELECT pid, store2, 'S2' FROM products) AS y WHERE y.price IS NOT NULL"
    assert _proved(outside, reordered)
    assert not _proved(outside, inside.replace("WHERE store2 IS NOT NULL", ""))


def test_order_by_on_the_outputs_moves_to_the_set_operation():
    assert _rule("SELECT * FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE b > 1 ORDER BY s.a") == (
        "SELECT * FROM (SELECT a, b FROM t WHERE b > 1 UNION SELECT c, d FROM u WHERE d > 1) AS s ORDER BY s.a"
    )
    assert _rule("SELECT s.a, s.b FROM (SELECT a, b FROM t UNION SELECT c, d FROM u) AS s WHERE b > 1 ORDER BY s.a DESC") == (
        "SELECT a, b FROM t WHERE b > 1 UNION SELECT c, d FROM u WHERE d > 1 ORDER BY a DESC"
    )
    outside = "SELECT * FROM (SELECT pid, store1 AS price FROM products UNION SELECT pid, store2 FROM products) AS x WHERE price IS NOT NULL"
    assert _proved(outside, outside + " ORDER BY pid")
