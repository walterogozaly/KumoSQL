import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.set_split_rules import split_distinct_select

pytest.importorskip("z3")

SCHEMA = {
    "pairs": ["a", "b"],
    "votes": ["voter", "item"],
    "visits": ["guest", "door", "arrive", "leave"],
    "t": ["x", "y", "z"],
    "u": ["x", "y"],
}

# Items voted for by people paired with person 7, minus the items person 7 voted for.
CASE_KEY = (
    "SELECT DISTINCT v.item FROM (SELECT CASE WHEN a = 7 THEN b WHEN b = 7 THEN a ELSE NULL END AS who FROM pairs) AS p "
    "JOIN votes AS v ON p.who = v.voter WHERE v.item NOT IN (SELECT item FROM votes WHERE voter = 7)"
)
SAME_AS_CASE_KEY = [
    "SELECT DISTINCT item FROM votes WHERE (voter IN (SELECT b FROM pairs WHERE a = 7) OR voter IN (SELECT a FROM pairs WHERE b = 7)) "
    "AND item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    "SELECT DISTINCT v.item FROM (SELECT b AS who FROM pairs WHERE a = 7 UNION SELECT a AS who FROM pairs WHERE b = 7) AS f "
    "JOIN votes AS v ON f.who = v.voter WHERE v.item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    "WITH f AS (SELECT a, b FROM pairs UNION ALL SELECT b, a FROM pairs) SELECT DISTINCT item FROM votes "
    "WHERE voter IN (SELECT b FROM f WHERE a = 7) AND item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    "SELECT DISTINCT v.item FROM pairs AS p JOIN votes AS v ON p.b = v.voter WHERE p.a = 7 AND v.item NOT IN (SELECT item FROM votes WHERE voter = 7) "
    "UNION SELECT DISTINCT v.item FROM pairs AS p JOIN votes AS v ON p.a = v.voter WHERE p.b = 7 AND v.item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    "SELECT DISTINCT v.item FROM votes AS v WHERE v.voter IN (SELECT CASE WHEN a = 7 THEN b WHEN b = 7 THEN a END FROM pairs) "
    "AND v.item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    "SELECT DISTINCT v.item FROM (SELECT DISTINCT (CASE WHEN a = 7 THEN b WHEN b = 7 THEN a END) AS who FROM pairs) AS p "
    "JOIN votes AS v ON p.who = v.voter WHERE v.item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    # Person 7 counts as their own pair here, but every item person 7 voted for is removed anyway.
    "SELECT DISTINCT v.item FROM pairs AS p JOIN votes AS v ON (p.a = 7 OR p.b = 7) AND (p.a = v.voter OR p.b = v.voter) "
    "WHERE v.item NOT IN (SELECT item FROM votes WHERE voter = 7)",
]

# Guests seen at a different door while one of their own visits was open.
OVERLAP = (
    "SELECT DISTINCT g1.guest FROM visits AS g1 JOIN visits AS g2 ON g1.guest = g2.guest AND g1.door <> g2.door "
    "AND g2.arrive BETWEEN g1.arrive AND g1.leave"
)
SAME_AS_OVERLAP = [
    "SELECT DISTINCT p.guest FROM visits AS p JOIN visits AS q ON p.guest = q.guest AND p.door <> q.door "
    "WHERE p.arrive BETWEEN q.arrive AND q.leave OR q.arrive BETWEEN p.arrive AND p.leave",
    "SELECT DISTINCT p.guest FROM visits AS p JOIN visits AS q ON p.guest = q.guest AND p.door <> q.door "
    "AND p.arrive BETWEEN q.arrive AND q.leave",
]


def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql", timeout_ms=4000).proven


def _rule(sql):
    out = split_distinct_select(sqlglot.parse_one(sql, read="mysql"))
    return out.sql(dialect="mysql") if out is not None else None


def test_case_key_splits_into_one_branch_per_arm():
    out = _rule("SELECT DISTINCT v.item FROM (SELECT CASE WHEN a = 7 THEN b WHEN b = 7 THEN a END AS who FROM pairs) AS p JOIN votes AS v ON p.who = v.voter")
    # Both arms pin a and b to 7, so the second arm needs no "first arm not taken" guard.
    assert out == (
        "SELECT DISTINCT v.item FROM (SELECT b AS who FROM pairs WHERE a = 7 UNION ALL SELECT a AS who FROM pairs WHERE b = 7) AS p "
        "JOIN votes AS v ON p.who = v.voter"
    )


def test_case_key_keeps_the_guard_when_both_arms_can_differ():
    out = _rule("SELECT DISTINCT p.who FROM (SELECT CASE WHEN x = 1 THEN y WHEN z = 1 THEN x END AS who FROM t) AS p JOIN u ON p.who = u.x")
    assert "NOT ((x = 1) IS TRUE)" in out


def test_case_key_needs_a_join_that_drops_null_keys_and_a_distinct_outer_query():
    assert _rule("SELECT DISTINCT p.who FROM (SELECT CASE WHEN x = 1 THEN y END AS who FROM t) AS p JOIN u ON p.who > u.x") is None
    assert _rule("SELECT p.who FROM (SELECT CASE WHEN x = 1 THEN y END AS who FROM t) AS p JOIN u ON p.who = u.x") is None
    assert _rule("SELECT DISTINCT p.who FROM (SELECT CASE WHEN x = 1 THEN y ELSE z END AS who FROM t) AS p JOIN u ON p.who = u.x") is None
    assert _rule("SELECT DISTINCT p.who FROM (SELECT CASE WHEN x = 1 THEN y END AS who FROM t) AS p LEFT JOIN u ON p.who = u.x") is None


def test_distinct_over_a_derived_union_distributes():
    out = _rule("SELECT DISTINCT u.y FROM (SELECT x FROM t UNION ALL SELECT y AS z FROM t) AS d JOIN u ON d.x = u.x")
    assert out == (
        "SELECT u.y FROM (SELECT x FROM t) AS d JOIN u ON d.x = u.x UNION SELECT u.y FROM (SELECT y AS x FROM t) AS d JOIN u ON d.x = u.x"
    )


def test_in_over_a_filtered_derived_union_splits():
    out = _rule("SELECT x FROM u WHERE x IN (SELECT y FROM (SELECT x, y FROM t UNION SELECT y, x FROM t) AS d WHERE x = 1)")
    assert out == (
        "SELECT x FROM u WHERE (x IN (SELECT y FROM (SELECT x, y FROM t) AS d WHERE x = 1) "
        "OR x IN (SELECT y FROM (SELECT y AS x, x AS y FROM t) AS d WHERE x = 1))"
    )


def test_exists_over_a_filtered_derived_union_splits():
    out = _rule("SELECT x FROM u WHERE EXISTS (SELECT 1 FROM (SELECT x FROM t UNION SELECT y FROM t) AS d WHERE d.x = u.y)")
    assert out == (
        "SELECT x FROM u WHERE (EXISTS(SELECT 1 FROM (SELECT x FROM t) AS d WHERE d.x = u.y) "
        "OR EXISTS(SELECT 1 FROM (SELECT y AS x FROM t) AS d WHERE d.x = u.y))"
    )


@pytest.mark.parametrize("other", SAME_AS_CASE_KEY)
def test_case_join_key_matches_union_and_or_forms(other):
    assert _proven(CASE_KEY, other)


@pytest.mark.parametrize("other", SAME_AS_OVERLAP)
def test_self_join_symmetry_under_distinct(other):
    assert _proven(OVERLAP, other)


@pytest.mark.parametrize(
    "left, right",
    [
        # The guard matters: a row with x = 1 and z = 1 gives only y on the left.
        (
            "SELECT DISTINCT u.y FROM (SELECT CASE WHEN x = 1 THEN y WHEN z = 1 THEN x END AS k FROM t) AS d JOIN u ON d.k = u.x",
            "SELECT DISTINCT u.y FROM (SELECT y AS k FROM t WHERE x = 1 UNION ALL SELECT x AS k FROM t WHERE z = 1) AS d JOIN u ON d.k = u.x",
        ),
        # Without DISTINCT the (7, 7) pair is counted once on the left and twice on the right.
        (
            "SELECT v.item FROM (SELECT CASE WHEN a = 7 THEN b WHEN b = 7 THEN a END AS who FROM pairs) AS p JOIN votes AS v ON p.who = v.voter",
            "SELECT v.item FROM (SELECT b AS who FROM pairs WHERE a = 7 UNION ALL SELECT a AS who FROM pairs WHERE b = 7) AS p JOIN votes AS v ON p.who = v.voter",
        ),
        # One disjunct is not the whole OR.
        ("SELECT DISTINCT x FROM t WHERE y = 1 OR z = 1", "SELECT DISTINCT x FROM t WHERE y = 1"),
        # The output is not symmetric in the two visits.
        (
            "SELECT DISTINCT g1.door FROM visits AS g1 JOIN visits AS g2 ON g1.guest = g2.guest AND g2.arrive BETWEEN g1.arrive AND g1.leave",
            "SELECT DISTINCT p.door FROM visits AS p JOIN visits AS q ON p.guest = q.guest "
            "WHERE p.arrive BETWEEN q.arrive AND q.leave OR q.arrive BETWEEN p.arrive AND p.leave",
        ),
        # Without DISTINCT, a DISTINCT derived table is not the bag it came from.
        ("SELECT t.x FROM t JOIN (SELECT DISTINCT y FROM u) AS d ON t.x < d.y", "SELECT t.x FROM t JOIN u AS d ON t.x < d.y"),
        # Person 7's own votes are removed, person 8's are not.
        (
            "SELECT item FROM votes WHERE voter = 8 AND item NOT IN (SELECT item FROM votes WHERE voter = 7)",
            "SELECT item FROM votes WHERE FALSE",
        ),
        # The CASE's NULLs make NOT IN unknown on the left only.
        (
            "SELECT x FROM u WHERE x NOT IN (SELECT CASE WHEN x = 1 THEN y END FROM t)",
            "SELECT x FROM u WHERE x NOT IN (SELECT y FROM t WHERE x = 1)",
        ),
        (
            "SELECT x FROM u WHERE EXISTS (SELECT 1 FROM (SELECT x FROM t UNION SELECT y FROM t) AS d WHERE d.x = u.y)",
            "SELECT x FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.x = u.y)",
        ),
        (
            "SELECT x FROM u WHERE x IN (SELECT y FROM (SELECT x, y FROM t UNION SELECT y, x FROM t) AS d WHERE x = 1)",
            "SELECT x FROM u WHERE x IN (SELECT y FROM t WHERE x = 1)",
        ),
    ],
)
def test_different_queries_stay_unproven(left, right):
    assert not _proven(left, right)


def test_a_row_witnesses_a_subquery_over_its_own_table():
    assert _proven(
        "SELECT DISTINCT item FROM votes WHERE voter IN (7, 8) AND item NOT IN (SELECT item FROM votes WHERE voter = 7)",
        "SELECT DISTINCT item FROM votes WHERE voter = 8 AND item NOT IN (SELECT item FROM votes WHERE voter = 7)",
    )
