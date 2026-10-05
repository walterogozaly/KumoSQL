"""Join reduction and identity rules: outer joins that every row survives, lookups of the same row, constants.

Each proved pair is also run on random DuckDB databases that hold NULLs, duplicates, empty tables and
respect the declared keys and NOT NULL columns. Each near miss has a DuckDB witness (confirmed with the
optimizer off) and must stay unproven, so a rule that over-reached would show here.
"""

import random
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.identity_rules import DECIMAL_FIT_ASSUMPTION, identity_rules, union_all_repeats
from kumosql.join_reduction import Facts, constant_outer_join, lookup_self_join, reject_in_inner_on, twin_outer_join
from kumosql.smt_equivalence import TableConstraints

# t: k key; a NOT NULL; b, c, f nullable.  u: composite key.  w: nullable UNIQUE id.
TABLES = {
    "t": ["k", "a", "b", "c", "f"],
    "u": ["k1", "k2", "v"],
    "w": ["id", "x"],
}
TYPES = {
    "t": {"k": "INT", "a": "INT", "b": "INT", "c": "INT", "f": "DOUBLE"},
    "u": {"k1": "INT", "k2": "INT", "v": "INT"},
    "w": {"id": "INT", "x": "INT"},
}
NOT_NULL = {"t": frozenset({"k", "a"}), "u": frozenset({"k1", "k2"}), "w": frozenset()}
KEYS = {"t": [("k",)], "u": [("k1", "k2")], "w": [("id",)]}
CONSTRAINTS = {name: TableConstraints(not_null=NOT_NULL[name], keys=tuple(KEYS[name])) for name in TABLES}
FACTS = Facts(NOT_NULL, KEYS)


def _database(rng: random.Random, con) -> None:
    for name, columns in TABLES.items():
        con.execute(f"DELETE FROM {name}")
        seen = set()
        for _ in range(rng.randint(0, 5)):
            row = []
            for column in columns:
                if column == "f":
                    row.append(rng.choice([None, 0.0, 1.5, 2.5]))
                elif column in NOT_NULL[name] or name == "t" and column == "k":
                    row.append(rng.choice([0, 1, 2, 3]))
                else:
                    row.append(rng.choice([None, 0, 1, 2, 3]))
            key = tuple(row[columns.index(c)] for c in KEYS[name][0])
            if name == "w" and key[0] is None:
                key = None  # a UNIQUE key admits many NULLs
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            con.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in columns)})", row)


def _connection():
    con = duckdb.connect()
    for name, columns in TABLES.items():
        con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {TYPES[name][c]}' for c in columns)})")
    return con


def _bag(rows):
    return Counter(tuple(round(v, 6) if isinstance(v, float) else (float(v) if hasattr(v, "as_integer_ratio") is False and v is not None and not isinstance(v, (int, str, bool)) else v) for v in row) for row in rows)


def _witness(left: str, right: str, trials: int = 300) -> bool:
    rng = random.Random(7)
    con = _connection()
    for _ in range(trials):
        _database(rng, con)
        if _bag(con.execute(left).fetchall()) != _bag(con.execute(right).fetchall()):
            a, b = run_unoptimized(con, left, right)
            if _bag(a) != _bag(b):
                return True
    return False


def _proof(left: str, right: str):
    return prove_equivalent_algebraic(
        left, right, schema=TABLES, constraints=CONSTRAINTS, types=TYPES, dialect="mysql", compare_names=False
    )


EQUIVALENT = [
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.k = y.k",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.k = y.k",
        id="left-self-join-on-key",
    ),
    pytest.param(
        "SELECT x.k, y.k, y.b FROM t AS x FULL JOIN t AS y ON x.a = y.a",
        "SELECT x.k, y.k, y.b FROM t AS x JOIN t AS y ON x.a = y.a",
        id="full-self-join-on-not-null-column-needs-no-key",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x FULL JOIN (SELECT k, b FROM t WHERE c > 1) AS y ON x.k = y.k",
        "SELECT x.k, y.b FROM t AS x LEFT JOIN (SELECT k, b FROM t WHERE c > 1) AS y ON x.k = y.k",
        id="full-join-with-a-filtered-copy-is-left",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM (SELECT k, a FROM t WHERE c > 1) AS x FULL JOIN t AS y ON x.k = y.k",
        "SELECT x.k, y.b FROM (SELECT k, a FROM t WHERE c > 1) AS x RIGHT JOIN t AS y ON x.k = y.k",
        id="full-join-with-a-filtered-left-copy-is-right",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.k = y.k AND x.a = x.a AND y.a = y.a",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.k = y.k",
        id="reflexive-conjuncts-on-not-null-columns",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.b IS NOT DISTINCT FROM y.b",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.b IS NOT DISTINCT FROM y.b",
        id="null-safe-equality-holds-for-nulls-too",
    ),
    pytest.param(
        "SELECT x.k, y.c FROM (SELECT t.k, t.a FROM t JOIN t AS t2 ON t.k = t2.k) AS x FULL JOIN t AS y ON x.k = y.k",
        "SELECT x.k, y.c FROM (SELECT t.k, t.a FROM t JOIN t AS t2 ON t.k = t2.k) AS x JOIN t AS y ON x.k = y.k",
        id="a-copy-made-by-a-key-self-join",
    ),
    pytest.param(
        "SELECT x.k, p.b, q.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k AND x.b > 1 LEFT JOIN t AS q ON p.k IS NOT DISTINCT FROM q.k",
        "SELECT x.k, p.b, p.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k AND x.b > 1",
        id="lookup-of-the-same-row-by-key",
    ),
    pytest.param(
        "SELECT x.k1, p.v, q.v FROM u AS x LEFT JOIN u AS p ON x.k1 = p.k1 AND x.k2 = p.k2 AND x.v > 1 LEFT JOIN u AS q ON p.k1 = q.k1 AND p.k2 = q.k2",
        "SELECT x.k1, p.v, p.v FROM u AS x LEFT JOIN u AS p ON x.k1 = p.k1 AND x.k2 = p.k2 AND x.v > 1",
        id="lookup-by-a-composite-key",
    ),
    pytest.param(
        "SELECT x.k, p.v FROM t AS x LEFT JOIN u AS p ON x.k = p.k1 JOIN w ON p.v = w.x",
        "SELECT x.k, p.v FROM t AS x JOIN u AS p ON x.k = p.k1 JOIN w ON p.v = w.x",
        id="inner-join-on-rejects-the-padded-rows",
    ),
    pytest.param(
        "SELECT x.k, d.v FROM t AS x LEFT JOIN u AS p ON TRUE JOIN (SELECT k1, v FROM u) AS d ON x.k = 1 AND d.v > 2",
        "SELECT x.k, d.v FROM t AS x JOIN u AS p ON TRUE JOIN (SELECT k1, v FROM u) AS d ON x.k = 1 AND d.v > 2",
        id="constant-condition-on-the-left-side-only-keeps-padding",
        marks=pytest.mark.xfail(reason="documents that a filter on the preserved side does not reject padding", strict=True),
    ),
    pytest.param(
        "SELECT l.c, r.c FROM (SELECT 1 AS c) AS l FULL JOIN (SELECT 2 AS c UNION ALL SELECT 3) AS r ON TRUE",
        "SELECT l.c, r.c FROM (SELECT 1 AS c) AS l JOIN (SELECT 2 AS c UNION ALL SELECT 3) AS r ON TRUE",
        id="on-true-against-constant-rows",
    ),
    pytest.param(
        "SELECT l.c, r.c FROM (SELECT 1 AS c) AS l LEFT JOIN (SELECT 2 AS c) AS r ON l.c = r.c",
        "SELECT 1 AS c, NULL AS d",
        id="constant-condition-never-matches",
    ),
    pytest.param(
        "SELECT k FROM t WHERE (a > 1) IS TRUE",
        "SELECT k FROM t WHERE a > 1",
        id="is-true-in-a-filter",
    ),
    pytest.param(
        "SELECT k FROM t WHERE NOT ((a = k) IS NOT DISTINCT FROM FALSE)",
        "SELECT k FROM t WHERE a = k",
        id="is-not-false-of-a-comparison-of-not-null-columns",
    ),
    pytest.param(
        "SELECT t.k, u.v FROM t JOIN u ON NOT ((t.b = u.v) IS NOT DISTINCT FROM FALSE) WHERE t.b > 0 AND u.v > 0",
        "SELECT t.k, u.v FROM t JOIN u ON t.b = u.v WHERE t.b > 0 AND u.v > 0",
        id="is-not-false-when-a-filter-keeps-the-columns-non-null",
    ),
    pytest.param(
        "SELECT SUM(DISTINCT k) FROM t GROUP BY b",
        "SELECT SUM(k) FROM t GROUP BY b",
        id="distinct-sum-of-a-key",
    ),
    pytest.param(
        "SELECT COUNT(DISTINCT k2) FROM u GROUP BY k1",
        "SELECT COUNT(k2) FROM u GROUP BY k1",
        id="distinct-count-with-the-rest-of-the-key-grouped",
    ),
    pytest.param(
        "SELECT MIN(v) FROM (SELECT k1, v FROM u UNION ALL SELECT k1, v FROM u) AS d GROUP BY k1",
        "SELECT MIN(v) FROM u GROUP BY k1",
        id="min-does-not-see-repeats",
    ),
    pytest.param(
        "SELECT DISTINCT k1 FROM (SELECT k1 FROM u UNION ALL SELECT k1 FROM u) AS d",
        "SELECT DISTINCT k1 FROM u",
        id="distinct-does-not-see-repeats",
    ),
    pytest.param(
        "SELECT CAST(a AS DECIMAL(19, 2)) AS d FROM t",
        "SELECT a AS d FROM t",
        id="integer-cast-to-decimal",
    ),
]

NEAR_MISSES = [
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.b = y.b",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.b = y.b",
        id="self-join-on-a-nullable-column-pads-the-null-rows",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.k = y.k AND x.a > y.a",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.k = y.k AND x.a > y.a",
        id="a-condition-the-twin-does-not-meet",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN t AS y ON x.k = y.k AND y.c > 1",
        "SELECT x.k, y.b FROM t AS x JOIN t AS y ON x.k = y.k AND y.c > 1",
        id="a-filter-on-the-joined-side-in-the-on",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM t AS x LEFT JOIN (SELECT k, b FROM t WHERE c > 1) AS y ON x.k = y.k",
        "SELECT x.k, y.b FROM t AS x JOIN (SELECT k, b FROM t WHERE c > 1) AS y ON x.k = y.k",
        id="the-joined-copy-is-filtered",
    ),
    pytest.param(
        "SELECT x.k, y.b FROM (SELECT k, a FROM t WHERE c > 1) AS x FULL JOIN (SELECT k, b FROM t WHERE c < 2) AS y ON x.k = y.k",
        "SELECT x.k, y.b FROM (SELECT k, a FROM t WHERE c > 1) AS x JOIN (SELECT k, b FROM t WHERE c < 2) AS y ON x.k = y.k",
        id="both-copies-filtered",
    ),
    pytest.param(
        "SELECT x.k, y.k FROM t AS x LEFT JOIN t AS y ON x.k = y.a",
        "SELECT x.k, y.k FROM t AS x JOIN t AS y ON x.k = y.a",
        id="different-columns-are-not-the-twin",
    ),
    pytest.param(
        "SELECT x.id, y.x FROM w AS x LEFT JOIN w AS y ON x.id = y.id",
        "SELECT x.id, y.x FROM w AS x JOIN w AS y ON x.id = y.id",
        id="a-nullable-unique-column-does-not-match-itself",
    ),
    pytest.param(
        "SELECT x.k1, p.v, q.v FROM u AS x LEFT JOIN u AS p ON x.k1 = p.k1 LEFT JOIN u AS q ON p.k1 = q.k1",
        "SELECT x.k1, p.v, p.v FROM u AS x LEFT JOIN u AS p ON x.k1 = p.k1",
        id="lookup-by-part-of-a-key-repeats-rows",
    ),
    pytest.param(
        "SELECT x.id, p.x, q.x FROM w AS x LEFT JOIN w AS p ON x.id IS NOT DISTINCT FROM p.id LEFT JOIN w AS q ON p.id IS NOT DISTINCT FROM q.id",
        "SELECT x.id, p.x, p.x FROM w AS x LEFT JOIN w AS p ON x.id IS NOT DISTINCT FROM p.id",
        id="lookup-by-a-nullable-unique-column",
    ),
    pytest.param(
        "SELECT x.k, p.b, q.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k LEFT JOIN t AS q ON p.k = q.k AND q.c > 1",
        "SELECT x.k, p.b, p.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k",
        id="lookup-with-an-extra-condition",
    ),
    pytest.param(
        "SELECT x.k, p.v FROM t AS x LEFT JOIN u AS p ON x.k = p.k1 JOIN w ON p.v IS NOT DISTINCT FROM w.x",
        "SELECT x.k, p.v FROM t AS x JOIN u AS p ON x.k = p.k1 JOIN w ON p.v IS NOT DISTINCT FROM w.x",
        id="null-safe-on-does-not-reject-padding",
    ),
    pytest.param(
        "SELECT x.k, p.v FROM t AS x LEFT JOIN u AS p ON x.k = p.k1 JOIN w ON w.x > 1 AND (p.v > 0 OR w.id > 0)",
        "SELECT x.k, p.v FROM t AS x JOIN u AS p ON x.k = p.k1 JOIN w ON w.x > 1 AND (p.v > 0 OR w.id > 0)",
        id="or-with-an-other-table-does-not-reject",
    ),
    pytest.param(
        "SELECT l.c, r.k FROM (SELECT 1 AS c) AS l LEFT JOIN t AS r ON TRUE",
        "SELECT l.c, r.k FROM (SELECT 1 AS c) AS l JOIN t AS r ON TRUE",
        id="on-true-against-a-table-that-may-be-empty",
    ),
    pytest.param(
        "SELECT l.c, r.c FROM (SELECT 1 AS c) AS l LEFT JOIN (SELECT 2 AS c) AS r ON l.c = r.c - 1",
        "SELECT 1 AS c, NULL AS d",
        id="constant-condition-that-is-true",
    ),
    pytest.param(
        "SELECT k FROM t WHERE NOT ((b = c) IS NOT DISTINCT FROM FALSE)",
        "SELECT k FROM t WHERE b = c",
        id="is-not-false-keeps-the-null-rows-of-nullable-columns",
    ),
    pytest.param(
        "SELECT k, (b > 1) IS TRUE AS flag FROM t",
        "SELECT k, b > 1 AS flag FROM t",
        id="is-true-in-a-select-list-is-not-a-filter",
    ),
    pytest.param(
        "SELECT t.k, u.v FROM t LEFT JOIN u ON t.k = u.k1 AND NOT ((t.a = u.v) IS NOT DISTINCT FROM FALSE) WHERE t.a > 0",
        "SELECT t.k, u.v FROM t LEFT JOIN u ON t.k = u.k1 AND t.a = u.v WHERE t.a > 0",
        id="is-not-false-under-an-outer-join-where-u-is-nullable",
    ),
    pytest.param(
        "SELECT SUM(DISTINCT a) FROM t GROUP BY b",
        "SELECT SUM(a) FROM t GROUP BY b",
        id="distinct-sum-of-a-column-that-is-not-a-key",
    ),
    pytest.param(
        "SELECT SUM(DISTINCT v) FROM u GROUP BY k1",
        "SELECT SUM(v) FROM u GROUP BY k1",
        id="distinct-sum-of-a-column-outside-the-key",
    ),
    pytest.param(
        "SELECT SUM(DISTINCT k2) FROM u",
        "SELECT SUM(k2) FROM u",
        id="distinct-sum-of-part-of-a-key-without-the-rest-grouped",
    ),
    pytest.param(
        "SELECT COUNT(DISTINCT id) FROM w",
        "SELECT COUNT(id) FROM w",
        id="a-nullable-unique-column-still-counts-distinct-values-but-is-not-claimed-here",
        marks=pytest.mark.xfail(reason="true equivalence the rule declines (nullable keys are not read)", strict=True),
    ),
    pytest.param(
        "SELECT SUM(v) FROM (SELECT k1, v FROM u UNION ALL SELECT k1, v FROM u) AS d GROUP BY k1",
        "SELECT SUM(v) FROM u GROUP BY k1",
        id="sum-sees-repeats",
    ),
    pytest.param(
        "SELECT MIN(v) FROM (SELECT k1, v FROM u UNION ALL SELECT k2, v FROM u) AS d",
        "SELECT MIN(v) FROM u",
        id="union-operands-differ",
    ),
    pytest.param(
        "SELECT CAST(f AS DECIMAL(10, 0)) AS d FROM t",
        "SELECT f AS d FROM t",
        id="a-cast-of-a-non-integer-rounds",
    ),
]


@pytest.mark.parametrize("left, right", EQUIVALENT)
def test_equivalent_pairs_are_proved_and_hold_on_random_databases(left, right):
    assert _proof(left, right).proven
    assert not _witness(left, right)


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_are_not_proved(left, right):
    assert not _proof(left, right).proven


@pytest.mark.parametrize("left, right", [p.values[:2] for p in NEAR_MISSES if not p.marks])
def test_near_misses_have_a_witness(left, right):
    assert _witness(left, right)


def test_integer_cast_is_proved_under_a_stated_assumption():
    result = _proof("SELECT CAST(a AS DECIMAL(19, 2)) AS d FROM t", "SELECT a AS d FROM t")
    assert result.proven and DECIMAL_FIT_ASSUMPTION in result.assumptions


def test_integer_cast_fold_needs_no_assumption_when_the_digits_fit():
    result = _proof("SELECT CAST(a AS DECIMAL(40, 2)) AS d FROM t", "SELECT a AS d FROM t")
    assert result.proven and DECIMAL_FIT_ASSUMPTION not in result.assumptions


def _select(sql: str) -> sqlglot.exp.Select:
    return sqlglot.parse_one(sql, read="mysql")


def test_twin_rule_changes_only_the_join_kind_and_the_trivial_conjuncts():
    out = twin_outer_join(_select("SELECT x.k FROM t AS x LEFT JOIN t AS y ON x.k = y.k AND x.a = x.a AND x.k = y.k"), FACTS)
    assert out.sql(dialect="mysql") == "SELECT x.k FROM t AS x JOIN t AS y ON x.k = y.k"
    assert twin_outer_join(_select("SELECT x.k FROM t AS x LEFT JOIN t AS y ON x.b = y.b"), FACTS) is None
    assert twin_outer_join(_select("SELECT x.k FROM t AS x LEFT JOIN u AS y ON x.k = y.k1"), FACTS) is None
    assert twin_outer_join(_select("SELECT x.k FROM t AS x LEFT JOIN t AS y USING (k)"), FACTS) is None
    assert twin_outer_join(_select("SELECT x.k FROM t AS x LEFT JOIN t AS x ON x.k = x.k"), FACTS) is None


def test_lookup_rule_reads_only_a_left_join_of_the_same_table_on_a_whole_key():
    sql = "SELECT x.k, q.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k LEFT JOIN t AS q ON p.k = q.k"
    assert lookup_self_join(_select(sql), FACTS).sql(dialect="mysql") == "SELECT x.k, p.c FROM t AS x LEFT JOIN t AS p ON x.k = p.k"
    assert lookup_self_join(_select(sql.replace("LEFT JOIN t AS q", "JOIN t AS q")), FACTS) is None
    assert lookup_self_join(_select(sql.replace("p.k = q.k", "p.k = q.a")), FACTS) is None
    assert lookup_self_join(_select(sql.replace("t AS q ON p.k = q.k", "u AS q ON p.k = q.k1")), FACTS) is None
    assert lookup_self_join(_select("SELECT * FROM t AS x LEFT JOIN t AS p ON x.k = p.k LEFT JOIN t AS q ON p.k = q.k"), FACTS) is None
    inside_subquery = "SELECT x.k, (SELECT MAX(z.a) FROM t AS z WHERE z.k = q.k) FROM t AS x LEFT JOIN t AS p ON x.k = p.k LEFT JOIN t AS q ON p.k = q.k"
    assert lookup_self_join(_select(inside_subquery), FACTS) is None


def test_inner_on_rule_leaves_padding_that_the_condition_can_keep():
    sql = "SELECT 1 FROM t AS x LEFT JOIN u AS p ON x.k = p.k1 JOIN w ON p.v = w.x"
    assert reject_in_inner_on(_select(sql)).sql(dialect="mysql") == "SELECT 1 FROM t AS x JOIN u AS p ON x.k = p.k1 JOIN w ON p.v = w.x"
    assert reject_in_inner_on(_select(sql.replace("p.v = w.x", "p.v IS NULL"))) is None
    assert reject_in_inner_on(_select(sql.replace("JOIN w ON", "RIGHT JOIN w ON"))) is None
    assert reject_in_inner_on(_select("SELECT 1 FROM t AS x RIGHT JOIN u AS p ON x.k = p.k1 LEFT JOIN w ON p.v = w.x JOIN t AS y ON w.x = y.k")) is None


def test_constant_rule_needs_a_side_that_always_holds_a_row():
    assert constant_outer_join(_select("SELECT 1 FROM (SELECT 1 AS c) AS l LEFT JOIN (SELECT 2 AS c) AS r ON TRUE")) is not None
    assert constant_outer_join(_select("SELECT 1 FROM (SELECT 1 AS c) AS l LEFT JOIN t AS r ON TRUE")) is None
    assert constant_outer_join(_select("SELECT 1 FROM (SELECT 1 AS c) AS l LEFT JOIN (SELECT 2 AS c WHERE FALSE) AS r ON TRUE")) is None
    assert constant_outer_join(_select("SELECT 1 FROM (SELECT 1 AS c) AS l LEFT JOIN (SELECT COUNT(*) AS c FROM t HAVING COUNT(*) > 99) AS r ON TRUE")) is None


def test_identity_rules_ignore_what_they_do_not_read():
    assert identity_rules(_select("SELECT SUM(DISTINCT t.k) FROM t JOIN u ON t.k = u.k1"), KEYS, NOT_NULL, TYPES, "mysql") is None
    assert identity_rules(_select("SELECT SUM(DISTINCT k) FROM t GROUP BY ROLLUP (b)"), KEYS, NOT_NULL, TYPES, "mysql") is None
    assert union_all_repeats(_select("SELECT SUM(v) FROM (SELECT v FROM u UNION ALL SELECT v FROM u) AS d")) is None
    assert union_all_repeats(_select("SELECT MIN(v) FROM (SELECT v FROM u UNION SELECT v FROM u) AS d")) is None
    assert identity_rules(_select("SELECT CAST(a AS DECIMAL(19, 2)) / 3 FROM t"), KEYS, NOT_NULL, TYPES, "mysql") is None
    assert identity_rules(_select("SELECT CAST(a AS DECIMAL(19, 2)) AS d FROM t"), KEYS, NOT_NULL, TYPES, "postgres") is None
