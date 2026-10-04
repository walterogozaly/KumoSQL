"""The parse check: sqlglot's reading of a query against an independent one (``kumosql.parse_check``).

The misreads below were found by running both readings on an engine (``tools/parser_oracle.py``, MySQL 8.0 and
DuckDB) or on BigQuery (``docs/parser-checks.md``). Every case is a text, so the tests are parallel-safe.
"""

from __future__ import annotations

import dataclasses

import duckdb
import pytest

from kumosql import parse_check as pc
from kumosql.equivalence import EquivalenceResult, EquivalenceStatus
from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus


# ---------------------------------------------------------------------------------------------------------
# What sqlglot gets wrong, per dialect

MISREADS = [
    # GoogleSQL: |, ^, & and << on separate levels, || beside *, comparisons not associative
    ("bigquery", "SELECT 2 | 1 & 0", "(2) | (1 & 0)"),
    ("bigquery", "SELECT 1 ^ 1 << 1", "BITXOR"),
    ("bigquery", "SELECT 4 | 1 << 1", "(4) | (1 << 1)"),
    ("bigquery", "SELECT a FROM t WHERE a | b & c > 0", "(a) | (b & c)"),
    ("bigquery", "SELECT a FROM t WHERE a > 10 IS TRUE", "not associative"),
    ("bigquery", "SELECT a FROM t WHERE a IS NULL = FALSE", "not associative"),
    ("bigquery", "SELECT a FROM t WHERE a = b IS NOT DISTINCT FROM c", "not associative"),
    # MySQL 8.0 sql_yacc.yy
    ("mysql", "SELECT a FROM t WHERE a = b < c", "(a = b) < (c)"),
    ("mysql", "SELECT 1--1", "skips '--1'"),
    ("mysql", "SELECT a FROM t WHERE a XOR b AND c", "(a) XOR (b AND c)"),
    ("mysql", "SELECT a FROM t WHERE !a = b", "NOT (a)"),
    ("mysql", "SELECT 1 AND !2 IS NULL", "NOT (2)"),
    ("mysql", "SELECT NULL > 2 % 3 IS NULL", "IS (NULL)"),
    # PostgreSQL gram.y, as DuckDB builds on it
    ("postgres", "SELECT 1 UNION SELECT 2 INTERSECT SELECT 3", "UNION (SELECT 2 INTERSECT SELECT 3)"),
    ("duckdb", "SELECT ~1 + 1", "~ (1 + 1)"),
    ("postgres", "SELECT a FROM t WHERE a = b IS NULL", "(a = b) IS (NULL)"),
    ("postgres", "SELECT 'a' 'b'", "adjacent strings"),
    ("duckdb", "SELECT 1 | NOT 1", "rejects NOT"),
]


@pytest.mark.parametrize("dialect, sql, fragment", MISREADS, ids=[f"{d}:{s}" for d, s, _ in MISREADS])
def test_known_misreads_are_disagreements(dialect, sql, fragment):
    check = pc.check_query(sql, dialect)
    assert check.status == "disagree", check
    assert fragment in " ".join(check.reasons)
    assert pc.disagreement(sql, dialect)


READ_RIGHT = [
    ("bigquery", "SELECT a + b * c - d / e AS x FROM t WHERE a = 1 AND NOT b OR c <> 2 AND d NOT IN (1, 2) ORDER BY a DESC, b"),
    ("bigquery", "SELECT a FROM t WHERE (a | b) & c > 0 AND x BETWEEN 1 AND 5 OR y LIKE 'x%' OR z IS NOT NULL"),
    ("bigquery", "SELECT a FROM t -- a comment\nWHERE x = 1 AND y IS NULL"),
    ("bigquery", "SELECT a FROM t GROUP BY a HAVING SUM(b) > 1 AND NOT MIN(c) < 2 QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b DESC) = 1"),
    ("bigquery", "SELECT a FROM t UNION ALL SELECT b FROM u UNION ALL SELECT c FROM v"),
    ("bigquery", "SELECT 'a' 'b', NULL | 1, TRUE AND NOT FALSE"),
    ("mysql", "SELECT a FROM t WHERE (a XOR (b AND c)) AND (a = b) < c AND NOT d = e"),
    ("mysql", "SELECT 1 - -1, 1 -- c\n, 2"),
    ("mysql", "SELECT a DIV b, c FROM t WHERE x <=> y ORDER BY a DESC"),
    ("postgres", "SELECT 1 UNION (SELECT 2 INTERSECT SELECT 3)"),
    ("duckdb", "SELECT ~(1 + 1), a = b AND c IS NULL FROM t"),
]


@pytest.mark.parametrize("dialect, sql", READ_RIGHT, ids=[f"{d}:{s[:50]}" for d, s in READ_RIGHT])
def test_correct_readings_agree(dialect, sql):
    check = pc.check_query(sql, dialect)
    assert check.status == "agree", check
    assert pc.disagreement(sql, dialect) is None
    assert check.compared > 0


def test_a_dialect_or_construct_without_a_table_is_unchecked_never_a_disagreement():
    assert pc.check_query("SELECT a | b & c FROM t", "sqlite").status == "unchecked"
    assert pc.check_query("SELECT a FROM t, LATERAL (SELECT 1)", "bigquery").status == "unchecked"
    assert pc.check_query("CREATE TABLE t (a INT)", "bigquery").status == "unchecked"
    assert pc.check_query("SELECT a FROM t |> WHERE a > 1", "bigquery").status == "unchecked"
    assert pc.guarded("SELECT a | b & c FROM t", "SELECT 1", "sqlite") is None


def test_text_sqlglot_cannot_parse_is_unchecked():
    assert pc.check_query("SELECT FROM WHERE", "bigquery").status == "unchecked"


# ---------------------------------------------------------------------------------------------------------
# The independent reading is the engine's: DuckDB is the engine in process


def _value(connection, sql):
    return connection.execute(sql).fetchall()


DUCKDB_SAMPLES = [
    "SELECT ~1 + 1",
    "SELECT NOT 1 = 2 IS NULL",
    "SELECT 2 << 1 + 1",
    "SELECT 7 | 2 & 3",
    "SELECT 1 + 2 * 3 - 4 / 2",
    "SELECT 3 = 3 AND 4 > 5 OR 1 < 2",
    "SELECT -2 * 3 - 1",
    "SELECT 6 & 3 << 1",
    "SELECT 1 = 1 OR 1 = 0 AND 1 = 0",
    "SELECT 5 > 3 IS NOT NULL",
]


@pytest.mark.parametrize("sql", DUCKDB_SAMPLES)
def test_the_independent_reading_gives_the_engines_answer(sql):
    spelled = pc.reading(sql, "duckdb")
    assert spelled is not None
    connection = duckdb.connect()
    assert _value(connection, spelled) == _value(connection, sql)


def test_reading_spells_out_the_grouping():
    assert pc.reading("SELECT 2 | 1 & 0", "bigquery") == "SELECT (2 | (1 & 0))"
    assert pc.reading("SELECT !2 = 1", "mysql") == "SELECT ((!2) = 1)"
    assert pc.reading("SELECT ~1 + 1", "duckdb") == "SELECT (~(1 + 1))"
    assert pc.reading("SELECT 1", "sqlite") is None


# ---------------------------------------------------------------------------------------------------------
# NULL, TRUE and FALSE carry no position in sqlglot's tree; the check places them by order


def test_null_operands_are_anchored():
    # sqlglot reads ``NOT (2 IS NULL)``; MySQL reads ``(NOT 2) IS NULL``. Only the NULL tells them apart.
    assert pc.check_query("SELECT NOT 2 IS NULL", "mysql").status == "agree"
    assert pc.check_query("SELECT !2 IS NULL", "mysql").status == "disagree"
    assert pc.check_query("SELECT a FROM t WHERE x IS NOT NULL AND y = TRUE OR z IS FALSE", "bigquery").status == "agree"


# ---------------------------------------------------------------------------------------------------------
# The round trip compares operator grouping, not an engine's printing idioms


def test_round_trip_ignores_printing_idioms_that_leave_the_grouping_alone():
    # MySQL has no FULL JOIN and no DESC NULLS FIRST; sqlglot prints emulations. DIV is printed as a CAST.
    assert pc.round_trip("SELECT a FROM t FULL JOIN u ON t.x = u.x", "mysql") is None
    # nor an ANTI or SEMI JOIN (the SQLSolver TPC-H rewrites use them): printed as NOT EXISTS / EXISTS
    assert pc.round_trip("SELECT a FROM t LEFT ANTI JOIN u ON t.x = u.x WHERE NOT a IS NULL", "mysql") is None
    assert pc.round_trip("SELECT a FROM t ORDER BY a DESC NULLS FIRST", "mysql") is None
    assert pc.round_trip("SELECT a DIV b + c DIV d FROM t", "mysql") is None
    assert pc.round_trip("SELECT a - (b - c), NOT (a AND b), (a OR b) AND c FROM t", "bigquery") is None


def test_round_trip_flags_a_printing_that_regroups(monkeypatch):
    from sqlglot import exp

    original = exp.Expression.sql

    def lossy(self, *args, **kwargs):  # a generator that drops the parentheses around a nested subtraction
        return original(self, *args, **kwargs).replace("(b - c)", "b - c")

    monkeypatch.setattr(exp.Expression, "sql", lossy)
    pc.round_trip.cache_clear()
    try:
        assert "operator grouping" in (pc.round_trip("SELECT a - (b - c) FROM t", "bigquery") or "")
    finally:
        pc.round_trip.cache_clear()


# ---------------------------------------------------------------------------------------------------------
# The hook where proofs are accepted

MISREAD_PAIR = ("SELECT a | b & c AS v FROM t", "SELECT (a | b) & c AS v FROM t")
SAME_PAIR = ("SELECT a + 1 AS v FROM t", "select a + 1 as v from t")


def _unguarded(prover):
    """``prover`` without the check (what every prover did before it), whatever it calls inside."""

    inner = getattr(prover, "__wrapped__", prover)

    def run(*args, **kwargs):
        with pc.outermost():  # the calls it makes itself count as nested, so they are not checked either
            return inner(*args, **kwargs)

    return run


def test_smt_prover_declines_a_proof_that_rests_on_a_misread():
    from kumosql.smt_equivalence import prove_equivalent_smt

    assert _unguarded(prove_equivalent_smt)(*MISREAD_PAIR).status is SmtStatus.PROVEN_EQUIVALENT  # the false proof
    result = prove_equivalent_smt(*MISREAD_PAIR)
    assert result.status is SmtStatus.NOT_PROVEN
    assert result.reason.startswith("parser disagreement:")
    assert prove_equivalent_smt(*SAME_PAIR).status is SmtStatus.PROVEN_EQUIVALENT


def test_basic_prover_declines_a_proof_that_rests_on_a_misread():
    from kumosql.equivalence import prove_equivalent

    assert _unguarded(prove_equivalent)(*MISREAD_PAIR).status is EquivalenceStatus.PROVEN_EQUIVALENT
    result = prove_equivalent(*MISREAD_PAIR)
    assert result.status is EquivalenceStatus.NOT_PROVEN
    assert result.reason.startswith("parser disagreement:")
    assert prove_equivalent(*SAME_PAIR).status is EquivalenceStatus.PROVEN_EQUIVALENT


def test_algebraic_prover_declines_under_the_dialect_of_the_call():
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    text = "SELECT x FROM t WHERE a = b < c"
    unguarded = _unguarded(prove_equivalent_algebraic)
    assert unguarded(text, text, dialect="mysql").status is SmtStatus.PROVEN_EQUIVALENT
    result = prove_equivalent_algebraic(text, text, dialect="mysql")
    assert result.status is SmtStatus.NOT_PROVEN
    assert "MySQL reads" in result.reason
    clean = "SELECT x FROM t WHERE (a = b) < c"
    assert prove_equivalent_algebraic(clean, clean, dialect="mysql").status is SmtStatus.PROVEN_EQUIVALENT
    # GoogleSQL rejects the text outright
    assert "not associative" in prove_equivalent_algebraic(text, text).reason


def test_sqlsolver_entry_points_decline_too():
    from kumosql import sqlsolver_backend

    def runner(pairs, ddl, runtime, timeout_s):  # SQLSolver says EQ to the pair and NEQ to both controls
        return ["EQ", "NEQ", "NEQ"]

    result = sqlsolver_backend.prove_equivalent_sqlsolver(
        *MISREAD_PAIR, schema={"t": ["a", "b", "c"]}, runtime=object(), runner=runner
    )
    assert result.status is SmtStatus.NOT_PROVEN
    assert result.reason.startswith("parser disagreement:")
    result = sqlsolver_backend.prove_equivalent(*MISREAD_PAIR, backend="z3")
    assert result.status is SmtStatus.NOT_PROVEN


def test_a_conditional_proof_is_declined_and_its_conditions_dropped():
    @pc.refuse_misread_proofs
    def prover(left, right, **kwargs):
        return SmtEquivalenceResult(SmtStatus.PROVEN_CONDITIONALLY, "if a is never NULL", conditions=("a NOT NULL",), assumptions=("x",))

    result = prover(*MISREAD_PAIR)
    assert result.status is SmtStatus.NOT_PROVEN and result.conditions == () and result.assumptions == ()
    assert prover(*SAME_PAIR).status is SmtStatus.PROVEN_CONDITIONALLY


def test_other_fields_of_the_result_survive():
    @pc.refuse_misread_proofs
    def prover(left_sql, right_sql):
        return EquivalenceResult(EquivalenceStatus.PROVEN_EQUIVALENT, "same", diagnostics=("kept",), proof_checks=())

    result = prover(left_sql=MISREAD_PAIR[0], right_sql=MISREAD_PAIR[1])
    assert result.status is EquivalenceStatus.NOT_PROVEN and result.diagnostics == ("kept",)
    assert dataclasses.is_dataclass(result)


def test_only_a_proof_is_changed():
    @pc.refuse_misread_proofs
    def prover(left, right, **kwargs):
        return SmtEquivalenceResult(SmtStatus.NOT_EQUIVALENT, "counterexample")

    assert prover(*MISREAD_PAIR).status is SmtStatus.NOT_EQUIVALENT


def test_only_the_outermost_call_checks():
    calls = []

    @pc.refuse_misread_proofs
    def inner(left, right, **kwargs):
        calls.append("inner")
        return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, "inner")

    @pc.refuse_misread_proofs
    def outer(left, right, **kwargs):
        return inner("SELECT a | b & c FROM t", right, **kwargs)  # a text the prover wrote itself

    result = outer(*SAME_PAIR)
    assert calls == ["inner"] and result.status is SmtStatus.PROVEN_EQUIVALENT
    with pc.outermost() as top:
        assert top is True
        with pc.outermost() as nested:
            assert nested is False
    with pc.outermost() as again:
        assert again is True


def test_a_failing_check_never_fails_the_prover(monkeypatch):
    def broken(sql, dialect="bigquery"):
        raise RuntimeError("checker bug")

    monkeypatch.setattr(pc, "disagreement", broken)

    @pc.refuse_misread_proofs
    def prover(left, right, **kwargs):
        return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, "ok")

    assert prover(*MISREAD_PAIR).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("dialect", ["mysql", "bigquery", "postgres", "duckdb"])
def test_a_number_with_a_leading_dot_is_one_token(dialect):
    # sqlglot's MySQL tokenizer reads ``.49`` as a dot and a number and the parser joins them; that is not a misread
    for sql in ("SELECT 1 - .49", "SELECT ROUND(SALARY * (1 - .49), 0) FROM s", "SELECT x * .5 FROM t"):
        assert pc.check_query(sql, dialect).status == "agree", (dialect, sql)
