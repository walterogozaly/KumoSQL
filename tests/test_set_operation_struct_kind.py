"""False proofs from set-operation rules that read ``SELECT AS STRUCT`` operands as separate columns (issue #518).

``SELECT AS STRUCT x, y FROM t INTERSECT DISTINCT SELECT AS STRUCT k, w FROM u`` returns one STRUCT column. The rule
turned a set operation into ``SELECT DISTINCT s.x, s.y FROM t AS s WHERE EXISTS (...)`` and, reading the struct's
fields as columns, returned two plain columns, so the prover called both shapes equal. The pairs below return
different rows (DuckDB, optimizer off); the same pair without ``AS STRUCT`` is the rule's intended case and stays proven.
``output_names`` read the same operands as the columns ``x`` and ``y`` too, so ``X EXCEPT DISTINCT <empty>`` became
``SELECT DISTINCT`` of two columns where the query returns one struct.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.setop_rules import set_operation_to_exists

SCHEMA = {"t": {"x": "INT64", "y": "INT64"}, "u": {"k": "INT64", "w": "INT64"}}
ROWS = {"t": [(1, 1), (2, 2), (3, 1)], "u": [(1, 1), (3, 1), (9, 9)]}


def _bag(db, sql: str) -> Counter:
    return Counter(map(repr, run_unoptimized(db, sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])[0]))


def _differ(left: str, right: str) -> bool:
    db = duckdb.connect()
    db.execute("CREATE TABLE t(x BIGINT, y BIGINT)")
    db.execute("CREATE TABLE u(k BIGINT, w BIGINT)")
    for table, rows in ROWS.items():
        db.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    return _bag(db, left) != _bag(db, right)


def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=SCHEMA, dialect="bigquery", timeout_ms=3000).proven


OPERATIONS = ["INTERSECT DISTINCT", "EXCEPT DISTINCT"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_struct_result_is_not_the_same_as_its_fields_as_columns(operation):
    struct = f"SELECT AS STRUCT x, y FROM t {operation} SELECT AS STRUCT k, w FROM u"
    plain = f"SELECT x, y FROM t {operation} SELECT k, w FROM u"
    assert _differ(struct, plain), "the database must separate the pair"
    assert not _proven(struct, plain)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_the_same_pair_without_a_struct_still_proves(operation):
    plain = f"SELECT x, y FROM t {operation} SELECT k, w FROM u"
    test = "EXISTS" if operation.startswith("INTERSECT") else "NOT EXISTS"
    spelled = f"SELECT DISTINCT x, y FROM t WHERE {test} (SELECT 1 FROM u WHERE u.k IS NOT DISTINCT FROM t.x AND u.w IS NOT DISTINCT FROM t.y)"
    assert not _differ(plain, spelled)
    assert _proven(plain, spelled)


def test_the_rule_declines_struct_operands_only():
    parse = lambda sql: sqlglot.parse_one(sql, read="bigquery")
    assert set_operation_to_exists(parse("SELECT AS STRUCT x, y FROM t INTERSECT DISTINCT SELECT AS STRUCT k, w FROM u")) is None
    assert set_operation_to_exists(parse("SELECT x, y FROM t INTERSECT DISTINCT SELECT k, w FROM u")) is not None


def test_a_struct_minus_nothing_is_still_a_struct():
    struct = "SELECT AS STRUCT x, y FROM t EXCEPT DISTINCT SELECT AS STRUCT k, w FROM u WHERE FALSE"
    plain = "SELECT DISTINCT x, y FROM t"
    assert _differ(struct, plain), "the database must separate the pair"
    assert not _proven(struct, plain)


def test_a_plain_select_minus_nothing_is_still_its_distinct():
    assert _proven("SELECT x, y FROM t EXCEPT DISTINCT SELECT k, w FROM u WHERE FALSE", "SELECT DISTINCT x, y FROM t")
