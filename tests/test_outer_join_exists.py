"""An uncorrelated EXISTS in a LEFT JOIN's ON clause read as a filter on the padded side.

Every proved pair is also run on random DuckDB databases (NULLs, duplicates, empty tables); every
pair that must stay unproven has a DuckDB witness found the same way, confirmed with the optimizer off.
"""

import random
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.outer_join_exists import move_exists_into_padded_side

SCHEMA = {"a": ["k", "x"], "b": ["k", "y"], "c": ["k", "z"]}

Q = "SELECT 1 FROM c WHERE c.z < 2"
GROUPED_TRUE = "(SELECT s2.v AS v FROM (SELECT TRUE AS v FROM (SELECT c.k AS k FROM c WHERE c.z < 2) AS s) AS s2 GROUP BY s2.v)"


def _differ(left: str, right: str, trials: int = 200) -> bool:
    """Whether some random database tells the two queries apart.

    DuckDB cannot run an EXISTS in an outer join's ON clause, so both queries run (optimizer off)
    with ``EXISTS (Q)``, which reads no outer row, replaced by its value on that database.
    """

    rng = random.Random(11)
    con = duckdb.connect()
    for table, columns in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(c + ' INTEGER' for c in columns)})")
    for _ in range(trials):
        for table, columns in SCHEMA.items():
            con.execute(f"DELETE FROM {table}")
            for _ in range(rng.randint(0, 3)):
                values = [rng.choice([None, 0, 1, 2, 3]) for _ in columns]
                con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", values)
        [[(flag,)]] = run_unoptimized(con, f"SELECT EXISTS ({Q})")
        value = "TRUE" if flag else "FALSE"
        first, second = run_unoptimized(con, left.replace(f"EXISTS ({Q})", value), right.replace(f"EXISTS ({Q})", value))
        if Counter(first) != Counter(second):
            return True
    return False


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False).proven


EQUIVALENT = [
    pytest.param(
        f"SELECT a.x FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y, t.v AS v FROM b JOIN {GROUPED_TRUE} AS t ON TRUE) AS d ON TRUE",
        id="calcite-grouped-true-expansion",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        f"SELECT a.x, d.y FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE EXISTS ({Q})) AS d ON a.k = d.k",
        id="exists-beside-join-key",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON NOT EXISTS ({Q}) AND a.k = b.k",
        f"SELECT a.x, d.y FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE NOT EXISTS ({Q})) AS d ON a.k = d.k",
        id="not-exists",
    ),
]


NOT_EQUIVALENT = [
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        "SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k",
        id="exists-dropped",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k WHERE EXISTS ({Q})",
        id="exists-moved-to-where-drops-unpadded-rows",
    ),
    pytest.param(
        f"SELECT a.x FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x FROM a JOIN (SELECT b.k AS k, t.v AS v FROM b JOIN {GROUPED_TRUE} AS t ON TRUE) AS d ON TRUE",
        id="inner-join-does-not-pad",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON NOT EXISTS ({Q})",
        id="negated",
    ),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_proves_and_agrees_on_data(left, right):
    assert not _differ(left, right)
    assert _proven(left, right)


@pytest.mark.parametrize("left, right", NOT_EQUIVALENT)
def test_near_misses_stay_unproven(left, right):
    assert _differ(left, right)
    assert not _proven(left, right)


def _rule(sql: str):
    return move_exists_into_padded_side(sqlglot.parse_one(sql), SCHEMA)


def test_rule_moves_only_uncorrelated_exists():
    rewritten = _rule(f"SELECT a.x FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})")
    assert rewritten is not None
    assert rewritten.sql() == "SELECT a.x FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE EXISTS(SELECT 1 FROM c WHERE c.z < 2)) AS b ON a.k = b.k"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT a.x FROM a LEFT JOIN b ON a.k = b.k AND EXISTS (SELECT 1 FROM c WHERE c.k = a.k)", id="reads-preserved-side"),
        pytest.param("SELECT a.x FROM a LEFT JOIN b ON EXISTS (SELECT 1 FROM c WHERE c.k = b.k)", id="reads-padded-side"),
        pytest.param(f"SELECT a.x FROM a JOIN b ON EXISTS ({Q})", id="inner-join"),
        pytest.param(f"SELECT a.x FROM a RIGHT JOIN b ON EXISTS ({Q})", id="right-join"),
        pytest.param(f"SELECT a.x FROM a LEFT JOIN b ON a.k = b.k OR EXISTS ({Q})", id="disjunct"),
        pytest.param(f"SELECT a.x FROM a LEFT JOIN zz ON EXISTS ({Q})", id="unknown-columns"),
    ],
)
def test_rule_leaves_other_conditions(sql):
    assert _rule(sql) is None
