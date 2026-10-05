"""Window relations in the SMT prover: which windows ignore ties, and when two spellings are one relation.

Every claim is also run on DuckDB (one thread, ties and NULLs in the data): a window called tie-free must
give the same rows for every storage order, and a pair the prover identifies must agree on random data.
"""

from __future__ import annotations

from collections import Counter
import itertools
import random

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("z3")
sqlglot = pytest.importorskip("sqlglot")

from kumosql import smt_windows  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, TableConstraints, prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["id", "a", "b", "c"], "u": ["id", "a"]}


def _window(sql: str):
    return sqlglot.parse_one(sql, read="bigquery").find(sqlglot.exp.Window)


TIE_FREE = [
    "SUM(c) OVER (PARTITION BY b)",
    "COUNT(*) OVER ()",
    "COUNTIF(c > 1) OVER (PARTITION BY b)",
    "MIN(c) OVER (PARTITION BY b ORDER BY a)",
    "MAX(c) OVER (PARTITION BY b ORDER BY a RANGE BETWEEN 1 PRECEDING AND CURRENT ROW)",
    "AVG(c) OVER (PARTITION BY b ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)",
    "SUM(c) OVER (ORDER BY a ROWS BETWEEN CURRENT ROW AND CURRENT ROW)",
    "RANK() OVER (PARTITION BY b ORDER BY a)",
    "DENSE_RANK() OVER (ORDER BY a DESC)",
    "PERCENT_RANK() OVER (PARTITION BY b ORDER BY a)",
    "CUME_DIST() OVER (PARTITION BY b ORDER BY a)",
]
TIE_DEPENDENT = [
    "ROW_NUMBER() OVER (PARTITION BY b ORDER BY a)",
    "NTILE(2) OVER (ORDER BY a)",
    "LAG(c) OVER (PARTITION BY b ORDER BY a)",
    "LEAD(c) OVER (ORDER BY a)",
    "FIRST_VALUE(c) OVER (PARTITION BY b ORDER BY a)",
    "LAST_VALUE(c) OVER (PARTITION BY b ORDER BY a)",
    "NTH_VALUE(c, 2) OVER (ORDER BY a)",
    "SUM(c) OVER (PARTITION BY b ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(c) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING)",
    "ARRAY_AGG(c) OVER (PARTITION BY b)",
    "ANY_VALUE(c) OVER (PARTITION BY b)",
    "FIRST_VALUE(c IGNORE NULLS) OVER (ORDER BY a)",
    "STDDEV(c) OVER (PARTITION BY b)",
]


@pytest.mark.parametrize("call", TIE_FREE)
def test_tie_free_windows_are_recognised(call):
    assert smt_windows.is_tie_free(_window(f"SELECT {call} AS w FROM t"))


@pytest.mark.parametrize("call", TIE_DEPENDENT)
def test_other_windows_are_not_called_tie_free(call):
    assert not smt_windows.is_tie_free(_window(f"SELECT {call} AS w FROM t"))


@pytest.fixture
def db():
    connection = duckdb.connect(":memory:")
    configure(connection)
    connection.execute("SET threads=1")
    yield connection
    connection.close()


def _rows(db, sql: str):
    return Counter(bigquery_rows(db.execute(faithful(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="duckdb")).fetchall()))


def _load(db, table: str, columns: list[str], rows):
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table} ({', '.join(c + ' INT' for c in columns)})")
    for row in rows:
        db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", list(row))


def _data(rng: random.Random, width: int, rows: int):
    # small domains and NULLs, so partitions have tied order keys
    return [[rng.choice([None, 0, 1, 1, 2]) for _ in range(width)] for _ in range(rows)]


@pytest.mark.parametrize("call", TIE_FREE)
def test_tie_free_windows_give_the_same_rows_in_every_storage_order(db, call):
    sql = f"SELECT id, {call} AS w FROM t"
    rng = random.Random(call)
    for _ in range(3):
        rows = [[i] + r for i, r in enumerate(_data(rng, 3, 4))]
        seen = set()
        for order in itertools.permutations(rows):
            _load(db, "t", ["id", "a", "b", "c"], order)
            seen.add(tuple(sorted(_rows(db, sql).items(), key=repr)))
        assert len(seen) == 1, (call, rows)


def test_a_tie_dependent_window_does_change_with_the_storage_order(db):
    # the reason ROW_NUMBER stays opaque: two tied rows swap their numbers
    sql = "SELECT id, ROW_NUMBER() OVER (ORDER BY a) AS w FROM t"
    seen = set()
    for order in itertools.permutations([[1, 5, 0, 0], [2, 5, 0, 0]]):
        _load(db, "t", ["id", "a", "b", "c"], order)
        seen.add(tuple(sorted(_rows(db, sql).items(), key=repr)))
    assert len(seen) == 2


def _prove(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="bigquery")


def _agree(db, left: str, right: str, seeds=range(25)):
    for seed in seeds:
        rng = random.Random(seed)
        _load(db, "t", ["id", "a", "b", "c"], [[i] + r for i, r in enumerate(_data(rng, 3, rng.choice([0, 1, 4, 7])))])
        _load(db, "u", ["id", "a"], [[i] + r for i, r in enumerate(_data(rng, 1, rng.choice([0, 2, 5])))])
        assert _rows(db, left) == _rows(db, right), (left, right, seed)


def _joined(call: str, order: str) -> str:
    """``call`` is written with ``{x}`` where the window names a column of t."""

    items = f"x.a, {call.format(x='x.')} AS w"
    return f"SELECT {items} FROM " + ("t AS x JOIN u AS y ON x.a = y.a" if order == "ty" else "u AS y JOIN t AS x ON y.a = x.a")


WINDOWS_FOR_JOIN = [
    "SUM({x}c) OVER (PARTITION BY {x}b)",
    "COUNT(*) OVER (PARTITION BY {x}b)",
    "RANK() OVER (PARTITION BY {x}b ORDER BY {x}a)",
    "MAX({x}c) OVER (PARTITION BY {x}b ORDER BY {x}a)",
]


@pytest.mark.parametrize("call", WINDOWS_FOR_JOIN)
def test_tie_free_windows_over_a_reordered_join_are_proven_equal_without_a_tie_assumption(db, call):
    left, right = _joined(call, "ty"), _joined(call, "yt")
    result = _prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert not any("window functions" in note for note in result.assumptions)
    _agree(db, left, right)


def test_the_same_text_over_the_same_input_needs_no_assumption_for_a_tie_free_window():
    sql = "SELECT a, SUM(c) OVER (PARTITION BY b) AS s FROM t WHERE a > 1"
    result = _prove(sql, sql.replace("a > 1", "a > 1 AND a > 1"))
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert not any("window functions" in note for note in result.assumptions)


def test_a_tie_dependent_window_keeps_its_assumption():
    sql = "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c) AS n FROM t WHERE a > 1"
    result = _prove(sql, sql)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert any("window functions" in note for note in result.assumptions)


def test_a_tie_dependent_window_over_a_reordered_join_is_not_identified():
    # equal input bags, but which tied row gets which number may differ between the two plans
    call = "ROW_NUMBER() OVER (PARTITION BY {x}b ORDER BY {x}a)"
    left, right = _joined(call, "ty"), _joined(call, "yt")
    assert _prove(left, right).status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_window_over_a_ties_row_frame_is_not_identified():
    call = "SUM({x}c) OVER (PARTITION BY {x}b ORDER BY {x}a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    assert _prove(_joined(call, "ty"), _joined(call, "yt")).status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "right",
    [
        # another input: the second join keeps fewer rows
        "SELECT x.a, SUM(x.c) OVER (PARTITION BY x.b) AS w FROM u AS y JOIN t AS x ON y.a = x.a AND x.c > 0",
        # another window over the same input
        "SELECT x.a, SUM(x.c) OVER (PARTITION BY x.a) AS w FROM u AS y JOIN t AS x ON y.a = x.a",
        "SELECT x.a, MAX(x.c) OVER (PARTITION BY x.b) AS w FROM u AS y JOIN t AS x ON y.a = x.a",
        # a window over another column
        "SELECT x.a, SUM(x.b) OVER (PARTITION BY x.b) AS w FROM u AS y JOIN t AS x ON y.a = x.a",
    ],
)
def test_windows_over_different_inputs_or_specs_are_not_identified(db, right):
    left = "SELECT x.a, SUM(x.c) OVER (PARTITION BY x.b) AS w FROM t AS x JOIN u AS y ON x.a = y.a"
    assert _prove(left, right).status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_window_over_a_join_on_a_key_is_the_window_over_the_semijoin(db):
    # u.a is a unique key, so the join repeats no row of t: the two inputs are one bag only under the declared key
    constraints = {"u": TableConstraints(not_null=frozenset({"a"}), keys=(("a",),))}
    left = "SELECT x.a, SUM(x.c) OVER (PARTITION BY x.b) AS w FROM t AS x JOIN u AS y ON x.a = y.a"
    right = "SELECT x.a, SUM(x.c) OVER (PARTITION BY x.b) AS w FROM t AS x WHERE x.a IN (SELECT a FROM u)"
    keyed = prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=constraints, compare_names=False, dialect="bigquery")
    assert keyed.status is SmtStatus.PROVEN_EQUIVALENT, keyed.reason
    assert not any("window functions" in note for note in keyed.assumptions)
    assert _prove(left, right).status is not SmtStatus.PROVEN_EQUIVALENT  # a repeated u.a repeats rows of t
    for seed in range(25):  # databases that satisfy the key, ties everywhere else
        rng = random.Random(seed)
        _load(db, "t", ["id", "a", "b", "c"], [[i] + r for i, r in enumerate(_data(rng, 3, rng.choice([0, 3, 6])))])
        _load(db, "u", ["id", "a"], [[i, v] for i, v in enumerate(rng.sample([0, 1, 2, 3], rng.choice([0, 2, 3])))])
        assert _rows(db, left) == _rows(db, right), seed


def test_the_smt_prover_alone_is_unchanged_for_plain_queries():
    assert prove_equivalent_smt("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a > 1 AND a > 1").status is SmtStatus.PROVEN_EQUIVALENT
