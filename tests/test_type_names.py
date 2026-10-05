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


# --- positions that are not a cast or a CREATE column list ---------------------------------------------------
# Dry-run facts (2026-10-05): each statement below returned "Type not found: FLOAT" from BigQuery, except where a
# comment says the position is only validated when the statement runs.

POSITIONS = [
    ("ALTER TABLE d.t ADD COLUMN a FLOAT", "FLOAT"),  # dry run: Type not found (the table must exist first)
    ("ALTER TABLE d.t ADD COLUMN b INT64, ADD COLUMN IF NOT EXISTS a FLOAT", "FLOAT"),
    ("ALTER TABLE d.t ALTER COLUMN a SET DATA TYPE FLOAT", "FLOAT"),
    ("ALTER TABLE IF EXISTS d.t ALTER COLUMN IF EXISTS a SET DATA TYPE UUID", "UUID"),
    ("ALTER TABLE d.t ADD COLUMN a NUMERIC(10, 2), ADD COLUMN b VARCHAR(10)", "VARCHAR"),
    # sqlglot reads these spellings and prints the full BigQuery form (BigQuery rejects the syntax or the type)
    ("ALTER TABLE d.t ADD COLUMNS (a INT64, b FLOAT)", "FLOAT"),
    ("ALTER TABLE d.t ADD a FLOAT", "FLOAT"),
    ("ALTER TABLE d.t ALTER COLUMN a TYPE FLOAT", "FLOAT"),
    ("ALTER TABLE d.t ALTER COLUMN a FLOAT", "FLOAT"),
    ("LOAD DATA INTO d.t (a FLOAT) FROM FILES (format='CSV', uris=['gs://b/x'])", "FLOAT"),  # dry run: Type not found
    ("LOAD DATA OVERWRITE d.t (a INT64, b TEXT) FROM FILES (format='CSV', uris=['gs://b/x'])", "TEXT"),
    ("LOAD DATA INTO d.t FROM FILES (format='CSV', uris=['gs://b/x']) WITH PARTITION COLUMNS (p FLOAT)", "FLOAT"),  # inferred
    ("ALTER TABLE d.t ADD COLUMN s.c FLOAT", "FLOAT"),  # a field of a struct column is a dotted name; inferred
    ("CREATE EXTERNAL TABLE d.t (a STRING) WITH PARTITION COLUMNS (p FLOAT) OPTIONS (format='CSV')", "FLOAT"),
    ("CREATE EXTERNAL TABLE d.t WITH PARTITION COLUMNS (p FLOAT) OPTIONS (format='CSV')", "FLOAT"),
    ("CREATE MODEL d.m INPUT (f1 FLOAT) OUTPUT (o STRING) REMOTE WITH CONNECTION DEFAULT", "FLOAT"),  # dry run: Type not found
    ("CREATE MODEL d.m INPUT (f1 INT64) OUTPUT (o VARCHAR) REMOTE WITH CONNECTION DEFAULT", "VARCHAR"),
    ("CREATE TABLE FUNCTION d.f(x INT64) RETURNS TABLE<a FLOAT> AS (SELECT 1 AS a)", "FLOAT"),  # dry run: Type not found
    ("CREATE TABLE FUNCTION d.f(t TABLE<a FLOAT>) AS (SELECT 1 AS a)", "FLOAT"),
    ("CREATE TABLE FUNCTION d.f(x FLOAT) AS (SELECT x)", "FLOAT"),
    ("CREATE PROCEDURE d.p() BEGIN DECLARE x FLOAT; SELECT 1; END", "FLOAT"),  # dry run: Type not found
    ("SELECT RANGE<FLOAT> '[1, 2)' AS r", "FLOAT"),  # dry run: Type not found
    ("CREATE TABLE d.t (a RANGE<FLOAT>)", "FLOAT"),
    ("DECLARE x RANGE<FLOAT>; SELECT 1", "FLOAT"),  # dry run: Type not found
    ("SELECT FLOAT r'1' AS a", "FLOAT"),
    ("SELECT FLOAT b'1' AS a", "FLOAT"),
    # script bodies: the tokenizer keeps the rest of these statements as one string, and a body is only validated
    # when it runs (a dry run accepts `IF TRUE THEN SELECT CAST(1 AS FLOAT); END IF`), so these rest on the type system
    ("WHILE TRUE DO SELECT CAST(1 AS FLOAT); END WHILE", "FLOAT"),
    ("LOOP SELECT CAST(1 AS FLOAT); END LOOP", "FLOAT"),
    ("REPEAT SELECT CAST(1 AS FLOAT); UNTIL TRUE END REPEAT", "FLOAT"),
    ("BEGIN SELECT 1; EXCEPTION WHEN ERROR THEN SELECT CAST(1 AS FLOAT); END", "FLOAT"),
    ("BEGIN SELECT 1; EXCEPTION WHEN ERROR THEN DECLARE y FLOAT; END", "FLOAT"),
    ("IF TRUE THEN CREATE TEMP TABLE z (a FLOAT); END IF", "FLOAT"),
    ("IF x THEN SELECT 1; ELSE CREATE TEMP TABLE z (a FLOAT); END IF", "FLOAT"),
    ("WHILE TRUE DO CREATE TEMP TABLE z (a FLOAT); END WHILE", "FLOAT"),
    ("LOOP CREATE TEMP TABLE z (a FLOAT); END LOOP", "FLOAT"),
    ("FOR r IN (SELECT 1) DO CREATE TEMP TABLE z (a FLOAT); END FOR", "FLOAT"),
    ("CASE WHEN TRUE THEN CREATE TEMP TABLE z (a FLOAT); END CASE", "FLOAT"),
    ("IF TRUE THEN BEGIN DECLARE x FLOAT; END; END IF", "FLOAT"),
    ("SELECT 1; ALTER TABLE d.t ADD COLUMN a FLOAT; SELECT 2", "FLOAT"),
]


@pytest.mark.parametrize("sql,name", POSITIONS)
def test_a_type_in_an_unusual_position_is_found(sql, name):
    assert name in (invalid_type_name(sql) or "")


# The same positions with valid types (and with names spelled like types), each of which BigQuery accepts or
# which is not a type at all.
VALID_POSITIONS = [
    "ALTER TABLE d.t ADD COLUMN a FLOAT64",
    "ALTER TABLE d.t ADD COLUMN IF NOT EXISTS a STRUCT<x FLOAT64>, ADD COLUMN b NUMERIC(10, 2) OPTIONS(description='float')",
    "ALTER TABLE d.t ADD COLUMN float FLOAT64",  # a column named like a type
    "ALTER TABLE d.t ADD COLUMNS (a INT64, text STRING)",
    "ALTER TABLE d.t ALTER COLUMN a SET DATA TYPE NUMERIC",
    "ALTER TABLE d.t ALTER COLUMN a SET DEFAULT 1",
    "ALTER TABLE d.t ALTER COLUMN a DROP NOT NULL",
    "ALTER TABLE d.t ALTER COLUMN IF EXISTS float SET OPTIONS (description='float')",
    "ALTER TABLE d.t RENAME COLUMN float TO text",
    "ALTER TABLE d.t DROP COLUMN float",
    "ALTER TABLE d.t ADD PRIMARY KEY (a) NOT ENFORCED",
    "ALTER TABLE d.t ADD CONSTRAINT c PRIMARY KEY (a) NOT ENFORCED",
    "ALTER TABLE d.t SET OPTIONS (description='add column a float')",
    "LOAD DATA INTO d.t (a INT64, b STRING) FROM FILES (format='CSV', uris=['gs://b/x'])",
    "LOAD DATA INTO d.t FROM FILES (format='CSV', uris=['gs://b/x'])",
    "LOAD DATA INTO d.t FROM FILES (format='CSV', uris=['gs://b/x']) WITH PARTITION COLUMNS (p STRING, d DATE)",
    "ALTER TABLE d.t ADD COLUMN s.c INT64, ADD COLUMN s.float STRING",
    "LOAD DATA OVERWRITE d.t FROM FILES (format='CSV', uris=['gs://b/float'])",
    "CREATE EXTERNAL TABLE d.t WITH PARTITION COLUMNS (p STRING, d DATE) OPTIONS (format='CSV')",
    "CREATE EXTERNAL TABLE d.t (a FLOAT64) WITH PARTITION COLUMNS OPTIONS (format='CSV')",
    "CREATE MODEL d.m INPUT (f1 INT64, text STRING) OUTPUT (o FLOAT64) REMOTE WITH CONNECTION DEFAULT",
    "CREATE MODEL d.m OPTIONS (model_type='linear_reg') AS SELECT 1 AS label, 2 AS input",
    "CREATE TABLE FUNCTION d.f(x FLOAT64, t TABLE<a INT64, float STRING>) RETURNS TABLE<b INT64, text STRING> AS (SELECT 1 AS b, 'a' AS text)",
    "CREATE PROCEDURE d.p(x FLOAT64) BEGIN DECLARE y INT64; SELECT y; END",
    "SELECT RANGE<DATE> '[2020-01-01, 2020-02-01)' AS r",
    "CREATE TABLE d.t (a RANGE<DATETIME>)",
    "DECLARE x RANGE<DATE>; SELECT 1",
    "SELECT DATE r'2020-01-01' AS a",
    "SELECT t.range < text, y > 1 FROM t",  # range is a field here, not RANGE<...>
    "SELECT x.struct < text, y > 1 FROM t",
    "WHILE TRUE DO SELECT CAST(1 AS FLOAT64); END WHILE",
    "LOOP DECLARE x FLOAT64; END LOOP",
    "BEGIN SELECT 1; EXCEPTION WHEN ERROR THEN SELECT CAST(1 AS NUMERIC); END",
    "IF x > 1 THEN SELECT 1; ELSE CREATE TEMP TABLE z (a FLOAT64, b STRING); END IF",
    "SELECT CASE WHEN a THEN float ELSE text END FROM t",
    "EXECUTE IMMEDIATE 'SELECT CAST(1 AS FLOAT)'",  # a string, out of scope
]


@pytest.mark.parametrize("sql", VALID_POSITIONS)
def test_valid_types_in_an_unusual_position_pass(sql):
    assert invalid_type_name(sql) is None


@pytest.mark.parametrize("bad,good", [
    ("SELECT ARRAY<FLOAT>[1] AS a FROM t", "SELECT ARRAY<FLOAT64>[1] AS a FROM t"),
    ("SELECT STRUCT<x INT32>(1) AS a FROM t", "SELECT STRUCT<x INT64>(1) AS a FROM t"),
    ("SELECT FLOAT '1' AS a", "SELECT FLOAT64 '1' AS a"),
    ("SELECT TEXT '1' AS a", "SELECT STRING '1' AS a"),
    ("SELECT TRY_CAST(x AS FLOAT) AS a FROM t", "SELECT TRY_CAST(x AS FLOAT64) AS a FROM t"),
    ("SELECT RANGE<FLOAT> '[1, 2)' AS a", "SELECT RANGE<FLOAT64> '[1, 2)' AS a"),
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


# A rewrite step that only reprints a statement sqlglot reads (an ALTER, a LOAD, a model or table function, a
# script body) compared the two trees, and the trees of FLOAT and FLOAT64 are the same. Each pair below was
# accepted before the positions were read.
REWRITTEN = [
    ("ALTER TABLE d.t ADD COLUMN a FLOAT", "ALTER TABLE d.t ADD COLUMN a FLOAT64"),
    ("ALTER TABLE d.t ALTER COLUMN a SET DATA TYPE INT32", "ALTER TABLE d.t ALTER COLUMN a SET DATA TYPE INT64"),
    ("ALTER TABLE d.t ADD COLUMNS (a UUID)", "ALTER TABLE d.t ADD COLUMNS (a STRING)"),
    ("LOAD DATA INTO d.t (a FLOAT) FROM FILES (format='CSV', uris=['gs://b/x'])",
     "LOAD DATA INTO d.t (a FLOAT64) FROM FILES (format='CSV', uris=['gs://b/x'])"),
    ("CREATE TABLE FUNCTION d.f(x INT64) RETURNS TABLE<a FLOAT> AS (SELECT 1 AS a)",
     "CREATE TABLE FUNCTION d.f(x INT64) RETURNS TABLE<a FLOAT64> AS (SELECT 1 AS a)"),
    ("CREATE MODEL d.m INPUT (f1 FLOAT) OUTPUT (o STRING) REMOTE WITH CONNECTION DEFAULT",
     "CREATE MODEL d.m INPUT (f1 FLOAT64) OUTPUT (o STRING) REMOTE WITH CONNECTION DEFAULT"),
    ("CREATE TABLE d.t (a RANGE<FLOAT>)", "CREATE TABLE d.t (a RANGE<FLOAT64>)"),
    ("IF TRUE THEN CREATE TEMP TABLE z (a FLOAT); END IF", "IF TRUE THEN CREATE TEMP TABLE z (a FLOAT64); END IF"),
]


@pytest.mark.parametrize("bad,good", REWRITTEN)
def test_a_rewrite_acceptance_refuses_a_type_in_an_unusual_position(bad, good, monkeypatch):
    from kumosql import rewrite

    ok, problems = rewrite._verify_sql(bad, good)
    assert not ok and "BigQuery" in problems[0]
    assert not rewrite._verify_sql(good, bad)[0]
    # with the check switched off the pair is accepted: the refusal is this check, not something else
    _off(monkeypatch, rewrite)
    assert rewrite._verify_sql(bad, good) == (True, [])


@pytest.mark.parametrize("bad,good", REWRITTEN)
def test_a_rewrite_acceptance_still_accepts_valid_types_in_an_unusual_position(bad, good):
    from kumosql import rewrite

    assert rewrite._verify_sql(good, good) == (True, [])
    assert rewrite._verify_sql(good, good.replace(" (", "  (", 1)) == (True, [])  # a layout change
    assert rewrite._verify_sql(bad, bad.replace(" (", "  (", 1)) == (True, [])  # layout only: the same tokens
