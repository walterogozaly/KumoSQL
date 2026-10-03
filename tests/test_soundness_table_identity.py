"""Two references are one relation only when every qualifier matches.

Rules that let a table stand in for another (a joined row witnessing an EXISTS, an EXCEPT of two filters of
one table) compared the bare table name, so ``s1.u`` stood in for ``s2.u``. Each wrong proof below returns
different rows in DuckDB on the database next to it; the controls spell one relation and stay proven.
"""

from collections import Counter

import pytest
import sqlglot
from sqlglot import exp

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import same_table
from kumosql.duckdb_load import run_unoptimized

DIALECTS = ("bigquery", "duckdb")

WITNESS_DDL = ["CREATE SCHEMA s1", "CREATE SCHEMA s2", "CREATE TABLE t (a INT)", "CREATE TABLE u (a INT)", "CREATE TABLE s1.u (a INT)", "CREATE TABLE s2.u (a INT)"]
WITNESS_ROWS = {"t": [(1,)], "s1.u": [(1,)]}

# (left, right, schema, DDL, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM s2.u AS w WHERE w.a = p.a)",
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a",
        None,
        WITNESS_DDL,
        WITNESS_ROWS,
        id="join-to-one-schema-witnesses-exists-over-another",
    ),
    pytest.param(
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM u AS w WHERE w.a = p.a)",
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a",
        None,
        WITNESS_DDL,
        WITNESS_ROWS,
        id="qualified-join-witnesses-exists-over-unqualified-name",
    ),
    pytest.param(
        "SELECT a FROM s1.u WHERE a > 0 EXCEPT DISTINCT SELECT a FROM s2.u WHERE a > 5",
        "SELECT DISTINCT a FROM s1.u WHERE a > 0 AND NOT COALESCE(a > 5, FALSE)",
        {"u": ["a"]},
        WITNESS_DDL,
        {"s1.u": [(10,)]},
        id="except-of-filters-over-tables-in-two-schemas",
    ),
]

CONTROLS = [
    pytest.param(
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM s1.u AS w WHERE w.a = p.a)",
        "SELECT p.a FROM t AS p JOIN s1.u AS q ON p.a = q.a",
        None,
        id="join-witnesses-exists-over-the-same-qualified-table",
    ),
    pytest.param(
        "SELECT p.a FROM t AS p JOIN u AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM u AS w WHERE w.a = p.a)",
        "SELECT p.a FROM t AS p JOIN u AS q ON p.a = q.a",
        None,
        id="join-witnesses-exists-over-the-same-unqualified-table",
    ),
    pytest.param(
        "SELECT p.a FROM t AS p JOIN prj.s1.u AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM prj.s1.u AS w WHERE w.a = p.a)",
        "SELECT p.a FROM t AS p JOIN prj.s1.u AS q ON p.a = q.a",
        None,
        id="join-witnesses-exists-over-the-same-three-part-name",
    ),
    pytest.param(
        "SELECT a FROM s1.u WHERE a > 0 EXCEPT DISTINCT SELECT a FROM s1.u WHERE a > 5",
        "SELECT DISTINCT a FROM s1.u WHERE a > 0 AND NOT COALESCE(a > 5, FALSE)",
        {"u": ["a"]},
        id="except-of-filters-over-one-qualified-table",
    ),
]


def _bags(left: str, right: str, ddl: list[str], rows: dict[str, list[tuple]]) -> tuple[Counter, Counter]:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for statement in ddl:
        db.execute(statement)
    for name, values in rows.items():
        for row in values:
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    return tuple(Counter(result) for result in run_unoptimized(db, left, right))


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("left, right, schema, ddl, rows", WRONG_PROOFS)
def test_different_qualifiers_are_different_tables(left, right, schema, ddl, rows, dialect):
    first, second = _bags(left, right, ddl, rows)
    assert first != second
    assert not prove_equivalent_algebraic(left, right, schema=schema, dialect=dialect).proven


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("left, right, schema", CONTROLS)
def test_the_same_qualified_table_stays_proven(left, right, schema, dialect):
    assert prove_equivalent_algebraic(left, right, schema=schema, dialect=dialect).proven


def test_duckdb_folds_unquoted_qualifiers():
    left = "SELECT p.a FROM t AS p JOIN S1.U AS q ON p.a = q.a WHERE EXISTS (SELECT 1 FROM s1.u AS w WHERE w.a = p.a)"
    right = "SELECT p.a FROM t AS p JOIN S1.U AS q ON p.a = q.a"
    assert prove_equivalent_algebraic(left, right, dialect="duckdb").proven


@pytest.mark.parametrize(
    "dialect, first, second, same",
    [
        ("duckdb", "s1.u", "s2.u", False),
        ("duckdb", "s1.u", "u", False),
        ("duckdb", "s1.u", "S1.U", True),
        ("duckdb", '"S1".u', "s1.u", True),
        ("postgres", '"U"', "U", False),
        ("bigquery", "ds.T", "ds.t", False),
        ("bigquery", "p.ds.t", "ds.t", False),
        ("bigquery", "p1.ds.t", "p2.ds.t", False),
        ("bigquery", "`p.ds.t`", "p.ds.t AS x", True),
    ],
)
def test_same_table_compares_every_qualifier(dialect, first, second, same):
    table = lambda name: sqlglot.parse_one(f"SELECT 1 FROM {name}", read=dialect).find(exp.Table)  # noqa: E731
    assert same_table(table(first), table(second), dialect) is same
