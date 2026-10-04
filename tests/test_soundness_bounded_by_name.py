"""Wrong bounded verdicts on set operations, kept as regression cases.

The bounded compiler matched set-operation branches by position, so a ``BY NAME`` /
``CORRESPONDING`` operation whose branches list their columns in different orders was compiled
as if aligned by position, and a ``LIMIT``/``OFFSET`` on the operation itself was dropped. Each
pair below returns different rows on the database next to it (DuckDB shows it), so
``check_bounded`` must not call it ``BOUNDED_EQUIVALENT``. The near misses are equivalent and must
not be refuted, so the fix cannot just decline everything.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql.bounded_equivalence import BColumn, BoundedSchema, BoundedStatus, BTable, check_bounded  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402


def _schema() -> BoundedSchema:
    return BoundedSchema({"t": BTable("t", [BColumn("x", "INT64"), BColumn("y", "INT64")])})


def _bags_differ(left: str, right: str, rows: list[tuple]) -> bool:
    db = duckdb.connect()
    db.execute("CREATE TABLE t (x BIGINT, y BIGINT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", rows)
    duck = lambda sql: sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]  # noqa: E731
    a, b = run_unoptimized(db, duck(left), duck(right))
    return Counter(a) != Counter(b)


def _parses(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return False
    return True


# sqlglot 26 cannot parse outer (FULL, LEFT, INNER) BY NAME or CORRESPONDING set operations
OUTER_BY_NAME = pytest.mark.skipif(
    not _parses("SELECT 1 AS a FULL UNION ALL BY NAME SELECT 1 AS a"), reason="this sqlglot version cannot parse outer BY NAME"
)


# (left, right, DuckDB stand-in for left or None, rows of t on which they differ). DuckDB has no
# INTERSECT/EXCEPT BY NAME, so those cases spell out the BigQuery meaning positionally for the check.
WRONG = [
    pytest.param(
        "SELECT x AS a, y AS b FROM t UNION DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
        "SELECT DISTINCT x AS a, y AS b FROM t",
        None,
        [(1, 2)],
        id="union-distinct-by-name-swapped-aliases",
    ),
    pytest.param(
        "SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT x AS b, y AS a FROM t",
        "SELECT x AS a, y AS b FROM t UNION ALL SELECT x, y FROM t",
        None,
        [(1, 2)],
        id="union-all-by-name-swapped-aliases",
    ),
    pytest.param(
        "SELECT x AS a, y AS b FROM t INTERSECT DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
        "SELECT DISTINCT x AS a, y AS b FROM t",
        "SELECT x AS a, y AS b FROM t INTERSECT DISTINCT SELECT y AS a, x AS b FROM t",
        [(1, 2)],
        id="intersect-by-name-swapped-aliases",
    ),
    pytest.param(
        "SELECT x AS a, y AS b FROM t EXCEPT DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
        "SELECT x AS a, y AS b FROM t WHERE FALSE",
        "SELECT x AS a, y AS b FROM t EXCEPT DISTINCT SELECT y AS a, x AS b FROM t",
        [(1, 2)],
        id="except-by-name-swapped-aliases",
    ),
    pytest.param(
        "SELECT x AS a, y AS b FROM t UNION ALL STRICT CORRESPONDING SELECT x AS b, y AS a FROM t",
        "SELECT x AS a, y AS b FROM t UNION ALL SELECT x, y FROM t",
        None,
        [(1, 2)],
        id="union-all-strict-corresponding",
        marks=OUTER_BY_NAME,
    ),
    pytest.param(
        "SELECT x FROM t UNION ALL SELECT y FROM t LIMIT 1",
        "SELECT x FROM t UNION ALL SELECT y FROM t",
        None,
        [(1, 2)],
        id="limit-of-the-set-operation",
    ),
    pytest.param(
        "SELECT x FROM t UNION ALL SELECT y FROM t ORDER BY 1 LIMIT 5 OFFSET 1",
        "SELECT x FROM t UNION ALL SELECT y FROM t",
        None,
        [(1, 2)],
        id="offset-of-the-set-operation",
    ),
]


@pytest.mark.parametrize("left, right, duck_left, rows", WRONG)
def test_set_operation_pair_is_not_bounded_equivalent(left, right, duck_left, rows):
    assert _bags_differ(duck_left or left, right, rows)
    result = check_bounded(left, right, _schema(), rows=2)
    assert result.status is not BoundedStatus.BOUNDED_EQUIVALENT, result.reason


@pytest.mark.parametrize(
    "left, right",
    [
        pytest.param(
            "SELECT x AS a, y AS b FROM t UNION DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
            "SELECT DISTINCT x AS a, y AS b FROM t",
            id="union-by-name",
        ),
        pytest.param(
            "SELECT x AS a, y AS b FROM t INTERSECT DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
            "SELECT DISTINCT x AS a, y AS b FROM t",
            id="intersect-by-name",
        ),
    ],
)
def test_by_name_difference_is_found(left, right):
    result = check_bounded(left, right, _schema(), rows=2)
    assert result.status is BoundedStatus.DIFFERENT, result.reason
    assert result.bound == 1


@pytest.mark.parametrize(
    "left, right",
    [
        # Same names in another order: BY NAME aligns them, so these equal the positional query.
        pytest.param(
            "SELECT x AS a, y AS b FROM t UNION DISTINCT BY NAME SELECT y AS b, x AS a FROM t",
            "SELECT DISTINCT x AS a, y AS b FROM t",
            id="union-by-name-reordered-columns",
        ),
        pytest.param(
            "SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT y AS b, x AS a FROM t",
            "SELECT x AS a, y AS b FROM t UNION ALL SELECT x, y FROM t",
            id="union-all-by-name-reordered-columns",
        ),
        pytest.param(
            "SELECT x AS a, y AS b FROM t UNION DISTINCT BY NAME SELECT x AS b, y AS a FROM t",
            "SELECT x AS a, y AS b FROM t UNION DISTINCT SELECT y, x FROM t",
            id="union-by-name-swapped-aliases-positional-twin",
        ),
        pytest.param(
            "SELECT x AS a, y AS b FROM t EXCEPT DISTINCT BY NAME SELECT y AS b, x AS a FROM t",
            "SELECT x AS a, y AS b FROM t WHERE FALSE",
            id="except-by-name-reordered-columns",
        ),
        pytest.param(
            "SELECT x AS a, y AS b FROM t FULL UNION ALL BY NAME SELECT x AS a FROM t",
            "SELECT x AS a, y AS b FROM t UNION ALL SELECT x, NULL FROM t",
            id="full-union-by-name-pads-with-null",
            marks=OUTER_BY_NAME,
        ),
    ],
)
def test_equivalent_by_name_pair_is_not_refuted(left, right):
    result = check_bounded(left, right, _schema(), rows=2)
    assert result.status is not BoundedStatus.DIFFERENT, result.counterexample
    assert result.status is BoundedStatus.BOUNDED_EQUIVALENT, result.reason


@pytest.mark.parametrize(
    "left, right",
    [
        # Bounded-equivalent on master and still: positional set operations, ORDER BY alone.
        pytest.param("SELECT x, y FROM t UNION DISTINCT SELECT x, y FROM t", "SELECT DISTINCT x, y FROM t", id="union-distinct-self"),
        pytest.param("SELECT x FROM t UNION ALL SELECT y FROM t", "SELECT y FROM t UNION ALL SELECT x FROM t", id="union-all-commutes"),
        pytest.param("SELECT x FROM t UNION ALL SELECT y FROM t ORDER BY 1", "SELECT y FROM t UNION ALL SELECT x FROM t", id="order-by-alone"),
        pytest.param("SELECT x FROM t INTERSECT DISTINCT SELECT x FROM t", "SELECT DISTINCT x FROM t", id="intersect-self"),
        pytest.param("SELECT x FROM t EXCEPT DISTINCT SELECT y FROM t WHERE FALSE", "SELECT DISTINCT x FROM t", id="except-empty"),
    ],
)
def test_positional_near_miss_stays_bounded_equivalent(left, right):
    result = check_bounded(left, right, _schema(), rows=2)
    assert result.status is BoundedStatus.BOUNDED_EQUIVALENT, result.reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT y AS b, x AS a FROM t",
        pytest.param("SELECT x AS a FROM t LEFT UNION ALL BY NAME SELECT x AS a FROM t", marks=OUTER_BY_NAME),
        pytest.param("SELECT x AS a, y AS b FROM t UNION ALL CORRESPONDING BY (b) SELECT y AS b, x AS a FROM t", marks=OUTER_BY_NAME),
        "SELECT x FROM t UNION ALL SELECT y FROM t LIMIT 1",
    ],
)
def test_compiler_refuses_unaligned_set_operation_modifiers(sql):
    # Callers that skip the public entry point's alignment get Unsupported, not a positional reading.
    compiler = be.Compiler(be.SymbolicDatabase(_schema(), 1))
    with pytest.raises(be.Unsupported):
        compiler.compile(sql)
