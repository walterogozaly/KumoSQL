"""A filtering inner join under DISTINCT read as EXISTS (``kumosql.semijoin_rules``)."""

import pytest

from kumosql.semijoin_rules import semijoin_reading


def test_filtering_join_becomes_exists_with_its_where_conjuncts():
    assert semijoin_reading("SELECT DISTINCT t.a AS k FROM t JOIN u ON t.a = u.b WHERE u.c > 1 AND t.b = 2") == (
        "SELECT DISTINCT t.a AS k FROM t WHERE t.b = 2 AND EXISTS(SELECT 1 FROM u WHERE t.a = u.b AND u.c > 1)"
    )


def test_grouping_with_min_max_only_is_duplicate_blind():
    assert semijoin_reading("SELECT t.a AS k, MAX(t.b) AS m FROM t, u WHERE t.a = u.b GROUP BY t.a") == (
        "SELECT t.a AS k, MAX(t.b) AS m FROM t WHERE EXISTS(SELECT 1 FROM u WHERE t.a = u.b) GROUP BY t.a"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.a AS k FROM t JOIN u ON t.a = u.b",  # repeats are visible
        "SELECT t.a AS k, COUNT(*) AS n FROM t JOIN u ON t.a = u.b GROUP BY t.a",  # COUNT sees repeats
        "SELECT DISTINCT t.a AS k, u.c FROM t JOIN u ON t.a = u.b",  # reads u
        "SELECT DISTINCT t.a FROM t JOIN u ON t.a = u.b RIGHT JOIN v ON v.x = t.a",  # keeps rows the test drops
        "SELECT DISTINCT t.a FROM t LEFT JOIN u ON t.a = u.b",  # not an inner join
        "SELECT DISTINCT a FROM t JOIN u ON t.a = u.b",  # unqualified column
        "SELECT DISTINCT * FROM t JOIN u ON t.a = u.b",
    ],
)
def test_left_alone(sql):
    assert semijoin_reading(sql) is None


def test_chained_filtering_joins_nest():
    assert semijoin_reading("SELECT DISTINCT t.a FROM t JOIN u ON t.a = u.b JOIN v ON v.x = u.c") == (
        "SELECT DISTINCT t.a FROM t WHERE EXISTS(SELECT 1 FROM u WHERE t.a = u.b AND EXISTS(SELECT 1 FROM v WHERE v.x = u.c))"
    )


def test_proves_distinct_join_against_in():
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    schema = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
    proven = prove_equivalent_algebraic(
        "SELECT DISTINCT t.a AS k FROM t JOIN u ON t.a = u.b",
        "SELECT DISTINCT t.a AS k FROM t WHERE t.a IN (SELECT u.b FROM u)",
        schema=schema,
        compare_names=False,
    )
    assert proven.proven
    # without DISTINCT the join repeats rows: never proved
    assert not prove_equivalent_algebraic(
        "SELECT t.a AS k FROM t JOIN u ON t.a = u.b",
        "SELECT t.a AS k FROM t WHERE t.a IN (SELECT u.b FROM u)",
        schema=schema,
        compare_names=False,
    ).proven
