"""Unicode case maps are not inverses of each other: the inner one of ``LOWER(UPPER(x))`` can matter.

``LOWER(UPPER('ς'))`` is ``'σ'`` while ``LOWER('ς')`` stays ``'ς'``, and ``UPPER(LOWER('İ'))`` is ``'I'``
while ``UPPER('İ')`` stays ``'İ'``. Each pair below returns different rows in DuckDB on the table next to
it, so no prover may call it equivalent. Repeating the same map changes nothing (checked on every code
point in DuckDB and with Python's full Unicode mappings), so those folds stay proven.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

# Final sigma, dotted capital I, sharp s, a titlecase digraph, the Kelvin sign and plain ASCII.
TRICKY = ["ς", "σ", "İ", "ı", "ß", "ǅ", "K", "Ab"]
DEPT = {"dept": ["deptno", "name"]}


def _bags(left: str, right: str, dialect: str, ddl: str, table: str, rows: list[tuple]) -> tuple[Counter, Counter]:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute(ddl)
    for row in rows:
        db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in row)})", row)
    run = lambda sql: Counter(db.execute(sqlglot.transpile(sql, read=dialect, write="duckdb")[0]).fetchall())  # noqa: E731
    return run(left), run(right)


def _proven(left: str, right: str, dialect: str, schema) -> list[str]:
    proofs = []
    if prove_equivalent_algebraic(left, right, schema=schema, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT:
        proofs.append("algebraic")
    if prove_equivalent_smt(left, right, schema=schema, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT:
        proofs.append("smt")
    return proofs


# (left, right, dialect, prover schema, DDL, table, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT LOWER(UPPER(x)) AS z FROM t", "SELECT LOWER(x) AS z FROM t", "bigquery", None,
        "CREATE TABLE t (x VARCHAR)", "t", [("ς",), ("σ",)],
        id="S009-006-lower-of-upper-final-sigma",
    ),
    pytest.param(
        "SELECT UPPER(LOWER(x)) AS z FROM t", "SELECT UPPER(x) AS z FROM t", "bigquery", None,
        "CREATE TABLE t (x VARCHAR)", "t", [("İ",)],
        id="upper-of-lower-dotted-capital-i",
    ),
    pytest.param(
        # SQLSolver's Spark pair 44 (SimplifyCaseConversionExpressions), labelled equivalent there.
        "SELECT UPPER(LOWER(name)) FROM dept", "SELECT UPPER(name) FROM dept", "mysql", DEPT,
        "CREATE TABLE dept (deptno BIGINT, name VARCHAR)", "dept", [(1, "İ")],
        id="spark-upper-of-lower-with-schema",
    ),
    pytest.param(
        "SELECT x FROM t WHERE LOWER(UPPER(x)) = 'σ'", "SELECT x FROM t WHERE LOWER(x) = 'σ'", "bigquery", None,
        "CREATE TABLE t (x VARCHAR)", "t", [("ς",)],
        id="lower-of-upper-in-a-filter",
    ),
    pytest.param(
        # LOWER(TRIM(..)) is moved to TRIM(LOWER(..)) first, which then met the composed maps.
        "SELECT LOWER(TRIM(UPPER(x))) AS z FROM t", "SELECT TRIM(LOWER(x)) AS z FROM t", "bigquery", None,
        "CREATE TABLE t (x VARCHAR)", "t", [("ς",)],
        id="lower-of-trimmed-upper",
    ),
]


@pytest.mark.parametrize("left, right, dialect, schema, ddl, table, rows", WRONG_PROOFS)
def test_composed_case_maps_are_not_the_outer_map(left, right, dialect, schema, ddl, table, rows):
    left_bag, right_bag = _bags(left, right, dialect, ddl, table, rows)
    assert left_bag != right_bag
    assert _proven(left, right, dialect, schema) == []


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT LOWER(LOWER(x)) AS z FROM t", "SELECT LOWER(x) AS z FROM t"),
        ("SELECT UPPER(UPPER(x)) AS z FROM t", "SELECT UPPER(x) AS z FROM t"),
        ("SELECT LOWER(UPPER(x)) AS z FROM t", "SELECT LOWER(UPPER(x)) AS z FROM t"),
        # Not proven on master, whose cross fold stopped after rewriting the outer pair.
        ("SELECT UPPER(LOWER(LOWER(x))) AS z FROM t", "SELECT UPPER(LOWER(x)) AS z FROM t"),
        ("SELECT x FROM t WHERE LOWER(LOWER(x)) = 'a'", "SELECT x FROM t WHERE LOWER(x) = 'a'"),
        ("SELECT LOWER(TRIM(x)) AS z FROM t", "SELECT TRIM(LOWER(x)) AS z FROM t"),
    ],
)
def test_repeated_case_maps_still_prove(left, right):
    left_bag, right_bag = _bags(left, right, "bigquery", "CREATE TABLE t (x VARCHAR)", "t", [(c,) for c in TRICKY + [f" {c} " for c in TRICKY]])
    assert left_bag == right_bag
    result = prove_equivalent_algebraic(left, right, dialect="bigquery")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
