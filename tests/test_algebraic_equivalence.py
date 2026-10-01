import random
import sqlite3
from collections import Counter

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic

U = "(SELECT a, k FROM A UNION ALL SELECT a, k FROM B)"

PROVEN = [
    pytest.param(
        f"SELECT u.a, c.x FROM {U} AS u JOIN C AS c ON u.k = c.k",
        "SELECT a.a, c.x FROM A AS a JOIN C AS c ON a.k = c.k UNION ALL SELECT b.a, c.x FROM B AS b JOIN C AS c ON b.k = c.k",
        id="join-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT u.a FROM {U} AS u WHERE u.a > 1",
        "SELECT a FROM A WHERE a > 1 UNION ALL SELECT a FROM B WHERE a > 1",
        id="filter-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A UNION ALL SELECT SUM(a) AS s FROM B) AS v",
        id="sum-of-union-all-is-sum-of-partial-sums",
    ),
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT SUM(n) AS n FROM (SELECT COUNT(*) AS n FROM B UNION ALL SELECT COUNT(*) AS n FROM A) AS v",
        id="count-of-union-all-is-sum-of-partial-counts",
    ),
    pytest.param(
        f"SELECT MAX(u.a) AS m FROM {U} AS u",
        "SELECT MAX(m) AS m FROM (SELECT MAX(a) AS m FROM A UNION ALL SELECT MAX(a) AS m FROM B) AS v",
        id="max-of-union-all",
    ),
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n FROM {U} AS u GROUP BY u.k",
        "SELECT v.k, SUM(v.n) AS n FROM (SELECT k, COUNT(*) AS n FROM A GROUP BY k "
        "UNION ALL SELECT k, COUNT(*) AS n FROM B GROUP BY k) AS v GROUP BY v.k",
        id="grouped-count-of-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u WHERE u.a > 1",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A WHERE a > 1 UNION ALL SELECT SUM(a) AS s FROM B WHERE a > 1) AS v",
        id="filtered-sum-of-union-all",
    ),
]

NOT_PROVEN = [
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT COUNT(*) AS n FROM A UNION ALL SELECT COUNT(*) AS n FROM B",
        id="count-of-union-is-not-union-of-counts",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(a) AS s FROM A",
        id="dropping-a-branch",
    ),
    pytest.param(
        f"SELECT AVG(u.a) AS s FROM {U} AS u",
        "SELECT AVG(s) AS s FROM (SELECT AVG(a) AS s FROM A UNION ALL SELECT AVG(a) AS s FROM B) AS v",
        id="average-of-averages",
    ),
    pytest.param(
        "SELECT DISTINCT u.a FROM (SELECT a FROM A UNION ALL SELECT a FROM B) AS u",
        "SELECT DISTINCT a FROM A UNION ALL SELECT DISTINCT a FROM B",
        id="distinct-does-not-distribute",
    ),
]


@pytest.mark.parametrize("left,right", PROVEN)
def test_algebraic_identities_are_proven(left, right):
    assert prove_equivalent_algebraic(left, right).proven


@pytest.mark.parametrize("left,right", NOT_PROVEN)
def test_non_identities_are_not_proven(left, right):
    assert not prove_equivalent_algebraic(left, right).proven


def test_normalize_is_idempotent():
    for case in PROVEN:
        for sql in case.values:
            once = normalize(sql)
            assert normalize(once) == once


@pytest.mark.parametrize("sql", [v for case in PROVEN + NOT_PROVEN for v in case.values])
def test_normalization_preserves_results_on_random_databases(sql):
    rng = random.Random(7)
    normalized = normalize(sql)
    for _ in range(40):
        db = sqlite3.connect(":memory:")
        for table in ("A", "B", "C"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 0, 1, 3, 5])):
                row = [rng.choice([None, 0, 1, 2, 3]) for _ in range(3)]
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized
