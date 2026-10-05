"""Integer and Boolean column domains for the SMT prover outside BigQuery (kumosql.smt_column_domains).

Every pair the prover proves here is also run on DuckDB over rows with NULLs, and every near miss must stay unproved.
"""

import json
from pathlib import Path

import pytest

z3 = pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import smt_column_domains, smt_equivalence
from kumosql.algebraic_equivalence import prove_equivalent_algebraic

INT_ROWS = [(None, 1), (0, 2), (1, 3), (2, 4), (3, 5), (10, 6), (11, 7), (19, 8), (20, 9), (-1, 10), (5, None)]
BOOL_ROWS = [(None, None, 1), (True, None, 2), (False, None, 3), (None, True, 4), (None, False, 5), (True, True, 6), (True, False, 7), (False, True, 8), (False, False, 9)]
TYPES = {"t": {"a": "INT", "d": "DECIMAL(10, 2)", "k": "BIGINT", "u": "VARCHAR(10)"}, "q": {"b": "BOOLEAN", "c": "BOOLEAN", "k": "INT"}}
SCHEMA = {"t": ["a", "d", "k", "u"], "q": ["b", "c", "k"]}


def proves(left, right, types=None, **options):
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types or TYPES, dialect="mysql", compare_names=False, exact_arithmetic=True, **options)
    return result.proven


def same_rows(left, right, table, columns, rows):
    con = duckdb.connect()
    con.execute(f"CREATE TABLE {table} ({columns})")
    con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    run = lambda sql: sorted(con.execute(sql).fetchall(), key=repr)  # noqa: E731
    return run(left) == run(right)


def duck_int(sql):
    # the DuckDB table holds one INT column a, a DECIMAL d is checked separately
    return sql.replace("t.", "")


INT_PAIRS = [
    ("SELECT k FROM t WHERE a > 10", "SELECT k FROM t WHERE a >= 11"),
    ("SELECT k FROM t WHERE 10 < a", "SELECT k FROM t WHERE 11 <= a"),
    ("SELECT k FROM t WHERE a < 20", "SELECT k FROM t WHERE a <= 19"),
    ("SELECT k FROM t WHERE a > 10 AND a < 20", "SELECT k FROM t WHERE a BETWEEN 11 AND 19"),
    ("SELECT k FROM t WHERE a > 1 AND a < 2", "SELECT k FROM t WHERE 1 = 0"),
    ("SELECT k FROM t WHERE a > 1 AND a < 3", "SELECT k FROM t WHERE a = 2"),
    ("SELECT k FROM t WHERE a - 10 > 100", "SELECT k FROM t WHERE a >= 111"),
    ("SELECT k FROM t WHERE a > 1.0", "SELECT k FROM t WHERE a >= 2"),
    ("SELECT k FROM t WHERE a > 10.5", "SELECT k FROM t WHERE a >= 11"),
    ("SELECT k FROM t WHERE a > 0 AND a < 3 OR a > 4 AND a < 7", "SELECT k FROM t WHERE a IN (1, 2, 5, 6)"),
]
INT_NEAR_MISSES = [
    ("SELECT k FROM t WHERE a > 10", "SELECT k FROM t WHERE a >= 10"),
    ("SELECT k FROM t WHERE a > 10 AND a < 20", "SELECT k FROM t WHERE a BETWEEN 10 AND 19"),
    ("SELECT k FROM t WHERE a > 1 AND a < 3", "SELECT k FROM t WHERE a = 1"),
    ("SELECT k FROM t WHERE a > 1 AND a < 2.5", "SELECT k FROM t WHERE 1 = 0"),
    ("SELECT k FROM t WHERE a - 10 > 100", "SELECT k FROM t WHERE a >= 110"),
]
# the same shapes over columns that are not integers: DECIMAL and a string are not read as whole numbers
NOT_INTEGER_PAIRS = [
    ("SELECT k FROM t WHERE d > 10", "SELECT k FROM t WHERE d >= 11"),
    ("SELECT k FROM t WHERE d > 1 AND d < 2", "SELECT k FROM t WHERE 1 = 0"),
    ("SELECT k FROM t WHERE d > 10 AND d < 20", "SELECT k FROM t WHERE d BETWEEN 11 AND 19"),
]


@pytest.mark.parametrize("left, right", INT_PAIRS)
def test_integer_column_bounds(left, right):
    assert proves(left, right)
    assert same_rows(left.replace("t.", ""), right.replace("t.", ""), "t", "a INT, k INT", [(a, k) for a, k in INT_ROWS])


@pytest.mark.parametrize("left, right", INT_NEAR_MISSES)
def test_integer_near_misses_stay_unproved(left, right):
    assert not proves(left, right)


@pytest.mark.parametrize("left, right", NOT_INTEGER_PAIRS)
def test_decimal_columns_get_no_integer_fact(left, right):
    assert not proves(left, right)


def test_integer_fact_needs_a_declared_integer_type_and_a_non_sqlite_dialect():
    left, right = INT_PAIRS[0]
    assert not proves(left, right, types={"t": {"a": "TEXT", "k": "INT"}})
    assert not proves(left, right, types={"t": {"k": "INT"}})
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="sqlite", compare_names=False, exact_arithmetic=True)
    assert not result.proven  # SQLite type affinity: an INTEGER column may hold 10.5 or text


BOOL_PAIRS = [
    ("SELECT k FROM q WHERE b = TRUE", "SELECT k FROM q WHERE b"),
    ("SELECT k FROM q WHERE b <> TRUE", "SELECT k FROM q WHERE NOT b"),
    ("SELECT k FROM q WHERE b <> FALSE", "SELECT k FROM q WHERE b"),
    ("SELECT b = TRUE FROM q", "SELECT b FROM q"),
    ("SELECT b = FALSE FROM q", "SELECT NOT b FROM q"),
    ("SELECT b <> FALSE FROM q", "SELECT b FROM q"),
    ("SELECT b OR b FROM q", "SELECT b FROM q"),
    ("SELECT b OR (b AND c) FROM q", "SELECT b FROM q"),
    ("SELECT (b AND c) OR b FROM q", "SELECT b FROM q"),
    ("SELECT b AND (b OR c) FROM q", "SELECT b FROM q"),
]
BOOL_NEAR_MISSES = [
    ("SELECT k FROM q WHERE b = TRUE", "SELECT k FROM q WHERE NOT b"),
    ("SELECT k FROM q WHERE b = FALSE", "SELECT k FROM q WHERE b"),
    ("SELECT b <> TRUE FROM q", "SELECT b = FALSE OR b IS NULL FROM q"),
    ("SELECT b OR c FROM q", "SELECT b FROM q"),
    ("SELECT b OR (b AND c) FROM q", "SELECT c FROM q"),
    ("SELECT b AND (b OR c) FROM q", "SELECT b AND c FROM q"),
    ("SELECT k FROM q WHERE b <> TRUE", "SELECT k FROM q WHERE b = FALSE OR b IS NULL"),
]


@pytest.mark.parametrize("left, right", BOOL_PAIRS)
def test_boolean_column_domain(left, right):
    assert proves(left, right, boolean_columns=True)
    assert same_rows(left, right, "q", "b BOOLEAN, c BOOLEAN, k INT", BOOL_ROWS)


@pytest.mark.parametrize("left, right", BOOL_PAIRS[1:])  # the first, a WHERE filter, was already proved without the domain
def test_boolean_domain_is_opt_in(left, right):
    # in MySQL a BOOLEAN column is a TINYINT(1) that can hold 2: `b = TRUE` and `b` differ there
    assert not proves(left, right)


@pytest.mark.parametrize("left, right", BOOL_NEAR_MISSES)
def test_boolean_near_misses_stay_unproved(left, right):
    assert not proves(left, right, boolean_columns=True)


def test_boolean_domain_ignores_non_boolean_columns():
    assert not proves("SELECT k OR k FROM q", "SELECT k FROM q", boolean_columns=True)


def test_domain_facts_name_only_the_declared_types():
    assert smt_column_domains.is_integer_type("bigint") and smt_column_domains.is_integer_type("INT(11)")
    assert not smt_column_domains.is_integer_type("DECIMAL(10, 2)") and not smt_column_domains.is_integer_type(None)
    assert smt_column_domains.is_boolean_type("BOOLEAN") and not smt_column_domains.is_boolean_type("TINYINT(1)")


CASES = Path(__file__).resolve().parent / "fixtures" / "qed_cockroach" / "qed_cockroach_pairs.jsonl"


def _case(name):
    for line in CASES.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["name"] == name:
            return row
    raise KeyError(name)


def _prove_case(name, **options):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import qed_cockroach_bench as bench
    import sqlsolver_bench as sb

    case = _case(name)
    return sb.prove_result(case["sql_a"], case["sql_b"], bench._tables(case["ddl"]), **options)


@pytest.mark.parametrize("name", ["memo/357", "memo/372", "xform/1010", "memo/171", "norm/1279"])
def test_cockroach_index_scan_pairs_are_proved(name):
    assert _prove_case(name, boolean_columns=True).proven


def test_a_solver_without_a_model_degrades_to_unknown(monkeypatch):
    """The Z3 model was once read after the last satisfiable check had been popped: the exception escaped the prover."""

    monkeypatch.setattr(smt_column_domains, "facts", lambda *args, **kwargs: [])
    result = _prove_case("memo/372", boolean_columns=True)  # the pair needs the integer fact, so it is not proved; it must not raise
    assert not result.proven


def test_a_z3_exception_anywhere_in_a_proof_is_not_proven(monkeypatch):
    def boom(*args, **kwargs):
        raise z3.Z3Exception("model is not available")

    monkeypatch.setattr(smt_equivalence, "_prove_smt", boom)
    result = smt_equivalence._prove_with_limit("SELECT 1", "SELECT 1")
    assert not result.proven and "solver" in result.reason
