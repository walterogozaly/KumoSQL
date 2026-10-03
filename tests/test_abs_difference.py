import pytest

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import NUMERIC_DIFFERENCE_ASSUMPTION

pytest.importorskip("z3")

SCHEMA = {"point": ["x"], "t": ["x", "y", "s"]}

SHORTEST = "SELECT MIN(ABS(a.x - b.x)) AS shortest FROM point AS a, point AS b WHERE a.x <> b.x"


def _prove(left, right, **options):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql", timeout_ms=4000, **options)


@pytest.mark.parametrize(
    "other",
    [
        "SELECT MIN(p2.x - p1.x) AS shortest FROM point AS p1 JOIN point AS p2 ON p1.x < p2.x",
        "SELECT MIN(a.x - b.x) AS shortest FROM point AS a LEFT JOIN point AS b ON a.x > b.x",
        "SELECT MIN(ABS(a.x - b.x)) AS shortest FROM point AS a, point AS b WHERE a.x > b.x",
        "SELECT MIN(c.s) AS shortest FROM (SELECT ABS(b.x - a.x) AS s FROM point AS a CROSS JOIN point AS b WHERE ABS(b.x - a.x) > 0) AS c",
    ],
)
def test_shortest_distance_between_points(other):
    result = _prove(SHORTEST, other)
    assert result.proven
    assert NUMERIC_DIFFERENCE_ASSUMPTION in result.assumptions


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT ABS(x - y) FROM t", "SELECT ABS(y - x) FROM t"),
        ("SELECT ABS(x - y) FROM t WHERE x < y", "SELECT y - x FROM t WHERE x < y"),
        ("SELECT x FROM t WHERE ABS(x - y) = 0", "SELECT x FROM t WHERE x = y"),
        ("SELECT DISTINCT a.x FROM t AS a, t AS b WHERE ABS(a.x - b.x) > 0", "SELECT DISTINCT a.x FROM t AS a, t AS b WHERE a.x <> b.x"),
        # x <> y splits into x < y and y < x, each landing on the right side with the operands swapped.
        (
            "SELECT DISTINCT ABS(a.x - b.x) FROM t AS a, t AS b WHERE a.x <> b.x",
            "SELECT DISTINCT b.x - a.x FROM t AS a, t AS b WHERE a.x < b.x",
        ),
    ],
)
def test_abs_of_a_difference(left, right):
    assert _prove(left, right).proven


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT MIN(a.x - b.x) FROM t AS a, t AS b WHERE a.x <> b.x", "SELECT MIN(ABS(a.x - b.x)) FROM t AS a, t AS b WHERE a.x <> b.x"),
        ("SELECT MAX(ABS(a.x - b.x)) FROM t AS a, t AS b", "SELECT MAX(a.x - b.x) FROM t AS a, t AS b WHERE a.x > b.x"),
        ("SELECT DISTINCT ABS(a.x - b.x) FROM t AS a, t AS b WHERE a.x <> b.x", "SELECT DISTINCT a.x - b.x FROM t AS a, t AS b WHERE a.x < b.x"),
        ("SELECT ABS(x - y) FROM t WHERE x < y", "SELECT x - y FROM t WHERE x < y"),
        ("SELECT ABS(x - y) FROM t WHERE x <> y", "SELECT ABS(x) FROM t WHERE x <> y"),
        # Not symmetric: the largest x has no larger partner.
        ("SELECT DISTINCT a.x FROM t AS a, t AS b WHERE ABS(a.x - b.x) > 0", "SELECT DISTINCT a.x FROM t AS a, t AS b WHERE a.x > b.x"),
        ("SELECT DISTINCT a.s FROM t AS a, t AS b WHERE a.s <> b.s", "SELECT DISTINCT a.s FROM t AS a, t AS b WHERE a.s < b.s"),
        ("SELECT DISTINCT a.x FROM t AS a, t AS b WHERE a.x <> b.x", "SELECT DISTINCT a.x FROM t AS a, t AS b WHERE a.x < b.x"),
    ],
)
def test_different_queries_stay_unproven(left, right):
    assert not _prove(left, right).proven


def test_exact_arithmetic_reads_abs_as_the_absolute_value():
    assert _prove("SELECT ABS(x) FROM t WHERE x < 0", "SELECT -x FROM t WHERE x < 0", exact_arithmetic=True).proven
    assert not _prove("SELECT ABS(x) FROM t WHERE x < 0", "SELECT x FROM t WHERE x < 0", exact_arithmetic=True).proven
