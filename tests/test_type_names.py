"""A proof of a query that names a type BigQuery does not have is refused (each name was rejected by a dry run)."""

from __future__ import annotations

import pytest

from kumosql import apply_rule, prove_equivalent
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.rewrite import VerificationStatus
from kumosql.smt_equivalence import prove_equivalent_smt
from kumosql.type_names import BIGQUERY_TYPE_NAMES, invalid_type_name

# Each of these returned "Type not found: <name>" from a BigQuery dry run.
REJECTED = ["FLOAT", "INT32", "UUID", "DOUBLE", "VARCHAR", "TEXT", "REAL", "INT8", "BINARY", "TIMESTAMP_NTZ", "CHAR"]
# Each of these was accepted.
ACCEPTED = ["INT64", "INT", "SMALLINT", "INTEGER", "BIGINT", "TINYINT", "BYTEINT", "NUMERIC", "DECIMAL", "BIGNUMERIC",
            "BIGDECIMAL", "FLOAT64", "BOOL", "BOOLEAN", "STRING", "BYTES", "DATE", "DATETIME", "TIME", "TIMESTAMP",
            "INTERVAL", "GEOGRAPHY", "JSON"]


@pytest.mark.parametrize("name", REJECTED)
def test_a_type_name_bigquery_rejects_is_found(name):
    assert name in (invalid_type_name(f"SELECT CAST(x AS {name}) FROM t") or "")
    assert name in (invalid_type_name(f"SELECT SAFE_CAST(x AS {name}) FROM t") or "")


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_type_name_bigquery_accepts_passes(name):
    assert invalid_type_name(f"SELECT CAST(x AS {name}) FROM t") is None
    assert name in BIGQUERY_TYPE_NAMES


@pytest.mark.parametrize("sql,name", [
    ("SELECT CAST(x AS MAP<STRING, INT64>) FROM t", "MAP"),  # BigQuery: "MAP datatype is not supported"
    ("SELECT CAST(x AS ARRAY<FLOAT>) FROM t", "FLOAT"),  # BigQuery: "Type not found: FLOAT"
    ("SELECT CAST(x AS STRUCT<a UUID, b ARRAY<INT64>>) FROM t", "UUID"),
    ("SELECT CAST(CAST(x AS INT64) AS DOUBLE PRECISION) FROM t", "DOUBLE PRECISION"),
    ("SELECT CAST((SELECT a AS b FROM u) AS VARCHAR) FROM t", "VARCHAR"),
    ("SELECT cast(x as text) FROM t", "TEXT"),
])
def test_nested_and_lowercase_names_are_found(sql, name):
    assert name in invalid_type_name(sql)


@pytest.mark.parametrize("sql", [
    "SELECT CAST(x AS ARRAY<FLOAT64>) FROM t",
    "SELECT CAST(x AS STRUCT<a FLOAT64, b ARRAY<INT>>) FROM t",
    "SELECT CAST(x AS STRING FORMAT 'YYYY') FROM t",
    "SELECT CAST(x AS RANGE<DATE>) FROM t",
    "SELECT float, uuid FROM t",  # a column named like a type
    "SELECT x AS float FROM t",
    "SELECT SAFE_CAST(x AS NUMERIC) FROM t",
    "not sql at all '",
])
def test_valid_or_unrelated_text_passes(sql):
    assert invalid_type_name(sql) is None


@pytest.mark.parametrize("bad,good", [
    ("FLOAT", "FLOAT64"), ("INT32", "INT64"), ("UUID", "STRING"), ("VARCHAR", "STRING"), ("DOUBLE", "FLOAT64"),
])
def test_every_prover_refuses_a_pair_with_a_name_bigquery_rejects(bad, good):
    left = f"SELECT CAST(x AS {bad}) AS y FROM t"
    right = f"SELECT CAST(x AS {good}) AS y FROM t"
    assert not prove_equivalent(left, right).proven
    assert not prove_equivalent_smt(left, right).proven
    assert not prove_equivalent_algebraic(left, right).proven
    # the same pair is refused even when both sides name the invalid type
    assert not prove_equivalent(left, left).proven
    assert prove_equivalent(right, right).proven


def test_the_refusal_says_why():
    result = prove_equivalent("SELECT CAST(x AS FLOAT) FROM t", "SELECT CAST(x AS FLOAT64) FROM t")
    assert "FLOAT" in result.reason and "BigQuery" in result.reason


def test_a_rewrite_of_a_statement_with_a_rejected_type_is_unproven():
    result = apply_rule("remove_redundant_parentheses", "SELECT CAST((x) AS INT32) AS y FROM t WHERE ((a))")
    assert result.verification.status is VerificationStatus.UNPROVEN
    # the same rewrite with a valid type is proven
    ok = apply_rule("remove_redundant_parentheses", "SELECT CAST((x) AS INT64) AS y FROM t WHERE ((a))")
    assert ok.verification.status is VerificationStatus.PROVEN


# --- types written outside a cast -------------------------------------------------------------

@pytest.mark.parametrize("sql,name", [
    ("SELECT ARRAY<FLOAT>[1] AS a FROM t", "FLOAT"),
    ("SELECT [STRUCT<x INT32>(1)] AS a FROM t", "INT32"),
    ("SELECT STRUCT<text FLOAT>('a') AS a FROM t", "FLOAT"),  # the field is named text; its type is FLOAT
    ("SELECT FLOAT '1' AS a", "FLOAT"),
    ("SELECT TEXT '1' AS a", "TEXT"),
    ("SELECT x::FLOAT AS a FROM t", "FLOAT"),
    ("SELECT TRY_CAST(x AS FLOAT) AS a FROM t", "FLOAT"),
    ("CREATE TABLE d.x (a FLOAT) AS SELECT 1 AS a", "FLOAT"),
    ("CREATE TABLE d.x (a INT64, b VARCHAR)", "VARCHAR"),
    ("CREATE TABLE d.x (a INT64, b ARRAY<FLOAT>)", "FLOAT"),
    ("CREATE TEMP FUNCTION f(a FLOAT64, IN b TEXT) AS (a); SELECT f(1.0, 'a')", "TEXT"),
    ("CREATE TEMP FUNCTION f(a INT64) RETURNS DOUBLE AS (1.0); SELECT f(1)", "DOUBLE"),
    ("CREATE PROCEDURE p(OUT x FLOAT) BEGIN SELECT 1; END", "FLOAT"),
    ("DECLARE v FLOAT; SELECT v", "FLOAT"),
    ("DECLARE a, b INT32 DEFAULT 1; SELECT a", "INT32"),
    ("SELECT 1; DECLARE x REAL", "REAL"),
    ("BEGIN DECLARE x UUID; SELECT x; END", "UUID"),
])
def test_a_type_named_outside_a_cast_is_found(sql, name):
    assert name in invalid_type_name(sql)


@pytest.mark.parametrize("sql", [
    "SELECT ARRAY<FLOAT64>[1] AS a FROM t",
    "SELECT STRUCT<text STRING, float FLOAT64>('a', 1.0) AS a FROM t",  # fields named like types
    "SELECT STRUCT<float STRUCT<a INT64>>(STRUCT(1)) AS a FROM t",
    "SELECT DATE '2020-01-01', TIMESTAMP '2020-01-01 00:00:00', NUMERIC '1' FROM t",
    "SELECT a float, b text FROM t",  # an alias without AS is valid
    "SELECT array < 3 FROM t",
    "SELECT range < text AND y > 1 FROM t",  # columns named range and text
    "SELECT x::INT64 FROM t",
    "CREATE TABLE d.x (a FLOAT64, float INT64, PRIMARY KEY (text) NOT ENFORCED)",
    "CREATE TABLE d.x (a INT64, CONSTRAINT text PRIMARY KEY (a) NOT ENFORCED)",
    "CREATE VIEW v (float, text) AS SELECT 1, 2",
    "CREATE TABLE d.x AS (SELECT CAST(a AS FLOAT64) FROM t)",
    "CREATE TABLE d.x (a NUMERIC(10, 2), b STRING(10)) AS SELECT 1, 'a'",
    "CREATE TEMP FUNCTION f(a FLOAT64, IN b STRING, c ANY TYPE) RETURNS INT64 AS (1); SELECT f(1.0, 'a', 2)",
    "DECLARE float FLOAT64; DECLARE a, b INT64 DEFAULT 1",
    "BEGIN DECLARE x INT64; SELECT x; END",
    "SELECT CAST(x AS STRUCT<text INT64>) FROM t",
    "ALTER TABLE t ADD COLUMN a FLOAT64",
])
def test_valid_types_and_words_spelled_like_types_pass(sql):
    assert invalid_type_name(sql) is None


def test_a_column_list_type_in_alter_table_is_not_read():
    # Known limit: a bare type keyword outside the places above could be a column or an alias, so it is not read.
    assert invalid_type_name("ALTER TABLE t ADD COLUMN a FLOAT") is None


@pytest.mark.parametrize("bad,good", [
    ("SELECT ARRAY<FLOAT>[1] AS a FROM t", "SELECT ARRAY<FLOAT64>[1] AS a FROM t"),
    ("SELECT STRUCT<x INT32>(1) AS a FROM t", "SELECT STRUCT<x INT64>(1) AS a FROM t"),
    ("SELECT FLOAT '1' AS a", "SELECT FLOAT64 '1' AS a"),
    ("SELECT TEXT '1' AS a", "SELECT STRING '1' AS a"),
    ("SELECT TRY_CAST(x AS FLOAT) AS a FROM t", "SELECT TRY_CAST(x AS FLOAT64) AS a FROM t"),
])
def test_every_prover_refuses_a_type_written_outside_a_cast(bad, good):
    assert not prove_equivalent(bad, good).proven
    assert not prove_equivalent_smt(bad, good).proven
    assert not prove_equivalent_algebraic(bad, good).proven
    assert not prove_equivalent(bad, bad).proven
    assert prove_equivalent(good, good).proven


# --- the entry points that certify, bound or search without the structural, SMT or algebraic prover ----------
# Each has the three parts the wiring needs: a name BigQuery rejects is refused, the valid spelling still gets its
# verdict, and with the check switched off the invalid query is NOT refused (so the refusal is the wiring, not
# something else the entry point happens to do).

BAD_CAST = "SELECT CAST(x AS FLOAT) AS y FROM t"
GOOD_CAST = "SELECT CAST(x AS FLOAT64) AS y FROM t"


def _off(monkeypatch, module):
    monkeypatch.setattr(module, "invalid_type_name", lambda sql: None)


def _sqlsolver():
    from kumosql import sqlsolver_backend as backend

    schema = {"proj.ds.orders": [("id", "INT64"), ("amount", "FLOAT64")]}
    runtime = backend.SqlSolverRuntime(java="java", jar="sqlsolver.jar", lib_dir="lib")
    calls = []

    def runner(pairs, ddl, runtime, *, timeout_s):
        calls.append(pairs)
        return ["EQ", "NEQ", "NEQ"]  # a solver that would certify anything it is asked

    def run(left, right):
        return backend.prove_equivalent_sqlsolver(left, right, schema=schema, runtime=runtime, runner=runner)

    return run, calls


SOLVER_BAD = "SELECT CAST(id AS FLOAT) AS y FROM `proj.ds.orders`"
SOLVER_GOOD = "SELECT CAST(id AS FLOAT64) AS y FROM `proj.ds.orders`"


def test_sqlsolver_refuses_a_name_bigquery_rejects_and_never_asks_the_solver():
    from kumosql.smt_equivalence import SmtStatus

    run, calls = _sqlsolver()
    result = run(SOLVER_BAD, SOLVER_GOOD)
    assert result.status is SmtStatus.NOT_PROVEN and "FLOAT" in result.reason and "BigQuery" in result.reason
    assert run(SOLVER_BAD, SOLVER_BAD).status is SmtStatus.NOT_PROVEN
    assert not calls


def test_sqlsolver_still_proves_valid_types():
    from kumosql.smt_equivalence import SmtStatus

    run, calls = _sqlsolver()
    assert run(SOLVER_GOOD, SOLVER_GOOD).status is SmtStatus.PROVEN_EQUIVALENT
    assert calls


def test_sqlsolver_without_the_check_would_certify_the_invalid_pair(monkeypatch):
    from kumosql import sqlsolver_backend
    from kumosql.smt_equivalence import SmtStatus

    run, _ = _sqlsolver()
    _off(monkeypatch, sqlsolver_backend)
    assert run(SOLVER_BAD, SOLVER_GOOD).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("backend", ["auto", "sqlsolver", "algebraic", "z3"])
def test_the_combined_backend_refuses_every_route(backend):
    from kumosql import sqlsolver_backend

    result = sqlsolver_backend.prove_equivalent(SOLVER_BAD, SOLVER_GOOD, schema={"proj.ds.orders": ["id"]}, backend=backend)
    assert not result.proven
    assert "BigQuery would reject" in result.reason


def test_bounded_check_refuses_a_name_bigquery_rejects():
    pytest.importorskip("z3")
    from kumosql import bounded_equivalence as be

    schema = be.schema_from_prover({"t": ["x"]}, types={"t": {"x": "INT64"}})
    for left, right in [(BAD_CAST, GOOD_CAST), (BAD_CAST, BAD_CAST), (BAD_CAST, BAD_CAST + " WHERE x > 0")]:
        result = be.check_bounded(left, right, schema, rows=2, dialect="bigquery", timeout_ms=3000)
        assert result.status is be.BoundedStatus.UNKNOWN and "BigQuery would reject" in result.reason
        assert result.counterexample is None
    ok = be.check_bounded(GOOD_CAST, GOOD_CAST, schema, rows=2, dialect="bigquery", timeout_ms=3000)
    assert ok.status is be.BoundedStatus.BOUNDED_EQUIVALENT


def test_bounded_check_without_the_check_would_report_a_bound_for_the_invalid_query(monkeypatch):
    pytest.importorskip("z3")
    from kumosql import bounded_equivalence as be

    schema = be.schema_from_prover({"t": ["x"]}, types={"t": {"x": "INT64"}})
    _off(monkeypatch, be)
    result = be.check_bounded(BAD_CAST, GOOD_CAST, schema, rows=2, dialect="bigquery", timeout_ms=3000)
    assert result.status is be.BoundedStatus.BOUNDED_EQUIVALENT


def test_the_pasted_query_bounded_check_goes_through_the_same_refusal(monkeypatch):
    pytest.importorskip("z3")
    pytest.importorskip("duckdb")
    from kumosql import bounded_equivalence as be, prover_context

    schema = be.schema_from_prover({"t": ["x"]}, types={"t": {"x": "INT64"}})
    monkeypatch.setattr(prover_context, "settings", lambda: {"enabled": True, "timeout_ms": 3000, "bounded_rows": 2})
    result = prover_context.bounded(BAD_CAST, GOOD_CAST, schema=schema)
    assert result["status"] == "unknown" and "BigQuery would reject" in result["reason"]
    assert prover_context.bounded(GOOD_CAST, GOOD_CAST, schema=schema)["status"] == "bounded_equivalent"


def test_counterexample_search_refuses_a_name_bigquery_rejects(monkeypatch):
    pytest.importorskip("duckdb")
    from kumosql import counterexample as cx

    spec = cx.Spec({"t": cx.Table("t", [cx.Column("x", "INT")])})
    narrowed = " WHERE x > 0"
    assert cx.find_counterexample(spec, BAD_CAST, BAD_CAST + narrowed, dialect="bigquery", trials=20, seed=0) is False
    assert isinstance(cx.find_counterexample(spec, GOOD_CAST, GOOD_CAST + narrowed, dialect="bigquery", trials=20, seed=0), cx.Counterexample)
    # another dialect's types are that dialect's own
    assert isinstance(cx.find_counterexample(spec, BAD_CAST, BAD_CAST + narrowed, dialect="mysql", trials=20, seed=0), cx.Counterexample)
    _off(monkeypatch, cx)
    assert isinstance(cx.find_counterexample(spec, BAD_CAST, BAD_CAST + narrowed, dialect="bigquery", trials=20, seed=0), cx.Counterexample)


def test_executed_search_refuses_a_name_bigquery_rejects(monkeypatch):
    pytest.importorskip("duckdb")
    from kumosql import executed_refutation as er

    schema, types = {"t": ["x"]}, {"t": {"x": "INT64"}}
    narrowed = " WHERE x > 0"
    assert er.search_counterexample(BAD_CAST, BAD_CAST + narrowed, schema=schema, types=types) is None
    assert er.search_counterexample(GOOD_CAST, GOOD_CAST + narrowed, schema=schema, types=types) is not None
    _off(monkeypatch, er)
    assert er.search_counterexample(BAD_CAST, BAD_CAST + narrowed, schema=schema, types=types) is not None


STATEMENT_BAD = "CREATE TABLE d.x (a FLOAT) AS SELECT 1 AS a"
STATEMENT_GOOD = "CREATE TABLE d.x (a FLOAT64) AS SELECT 1 AS a"


def test_statement_proofs_read_the_column_list_of_a_create_table():
    from kumosql.statement_proof import prove_statements, prove_statements_smt

    for prover in (prove_statements, prove_statements_smt):
        assert not prover(STATEMENT_BAD, STATEMENT_GOOD).proven
        assert not prover(STATEMENT_BAD, STATEMENT_BAD).proven
        assert "BigQuery would reject" in prover(STATEMENT_BAD, STATEMENT_GOOD).reason
        assert prover(STATEMENT_GOOD, STATEMENT_GOOD).proven


def test_statement_proofs_without_the_check_would_prove_the_invalid_column_type(monkeypatch):
    from kumosql import statement_proof

    # The query inside is valid and the proofs unwrap it, so the column list was never read before this check.
    _off(monkeypatch, statement_proof)
    assert statement_proof.prove_statements(STATEMENT_BAD, STATEMENT_GOOD).proven
    assert statement_proof.prove_statements_smt(STATEMENT_BAD, STATEMENT_GOOD).proven
