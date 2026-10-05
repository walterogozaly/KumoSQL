"""False proofs from struct field reads (issue #518), kept as regression cases.

sqlglot reads the three-part column ``a.s.f`` as column ``f`` of a table ``s`` in a dataset ``a``. When ``a`` is a
source of the query, it is the struct column ``s`` of ``a`` and its field ``f``. The outer-join rules attributed the
test ``a.s.f > 1`` to a source named ``s`` and turned ``a LEFT JOIN s`` into an inner join, which loses the rows of ``a``
that have no match. DuckDB (optimizer off, see ``kumosql.duckdb_load.run_unoptimized``) is the witness.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import run_unoptimized  # noqa: E402

SCHEMA = {"a": ["id", "k", "s"], "s": ["k", "w"], "u": ["k", "w"]}


def _both(pair):
    db = duckdb.connect()
    db.execute("CREATE TABLE a (id BIGINT, k BIGINT, s STRUCT(f BIGINT))")
    db.execute("CREATE TABLE s (k BIGINT, w BIGINT)")
    db.execute("CREATE TABLE u (k BIGINT, w BIGINT)")
    db.execute("INSERT INTO a VALUES (1, 1, {'f': 5}), (2, 2, {'f': 7}), (3, NULL, {'f': 9})")
    db.execute("INSERT INTO s VALUES (1, 100)")
    db.execute("INSERT INTO u VALUES (1, 100)")
    left, right = run_unoptimized(db, *pair)
    return Counter(left), Counter(right)


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery")


WRONG_PAIRS = [
    pytest.param(
        "SELECT a.id FROM a LEFT JOIN s ON a.k = s.k WHERE a.s.f > 1",
        "SELECT a.id FROM a JOIN s ON a.k = s.k WHERE a.s.f > 1",
        id="struct-field-of-the-preserved-side-named-like-the-padded-source",
    ),
    pytest.param(
        "SELECT a.id, s.w FROM a LEFT JOIN s ON a.k = s.k WHERE a.s.f > 1",
        "SELECT a.id, s.w FROM a JOIN s ON a.k = s.k WHERE a.s.f > 1",
        id="same-with-a-padded-column-selected",
    ),
]

STILL_PROVEN = [
    pytest.param(
        "SELECT a.id FROM a LEFT JOIN u ON a.k = u.k WHERE u.w > 1",
        "SELECT a.id FROM a JOIN u ON a.k = u.k WHERE u.w > 1",
        id="a-test-on-the-padded-side-makes-the-join-inner",
    ),
    pytest.param(
        "SELECT a.id FROM a LEFT JOIN u ON a.k = u.k WHERE a.k > 1",
        "SELECT a.id FROM a LEFT JOIN u ON a.k = u.k WHERE a.k > 1 AND TRUE",
        id="a-test-on-the-preserved-side-keeps-the-outer-join",
    ),
]


@pytest.mark.parametrize(("left", "right"), WRONG_PAIRS)
def test_the_database_tells_these_pairs_apart(left, right):
    a, b = _both((left, right))
    assert a != b


@pytest.mark.parametrize(("left", "right"), WRONG_PAIRS)
def test_pairs_that_differ_are_never_proven(left, right):
    assert not _prove(left, right).proven


@pytest.mark.parametrize(("left", "right"), STILL_PROVEN)
def test_near_misses_stay_proven_and_agree(left, right):
    assert _prove(left, right).proven
    a, b = _both((left, right))
    assert a == b
