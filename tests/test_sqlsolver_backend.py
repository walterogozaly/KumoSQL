import os
import stat
import sys

import pytest

from kumosql import sqlsolver_backend as backend
from kumosql.smt_equivalence import SmtStatus

SCHEMA = {"proj.ds.orders": [("id", "INT64"), ("status", "STRING"), ("amount", "FLOAT64")]}
LEFT = "SELECT id FROM `proj.ds.orders` WHERE amount > 5"
RIGHT = "SELECT o.id FROM proj.ds.orders AS o WHERE o.amount > 5"
RUNTIME = backend.SqlSolverRuntime(java="java", jar="sqlsolver.jar", lib_dir="lib")


def fake_runner(*verdicts):
    calls = []

    def runner(pairs, ddl, runtime, *, timeout_s):
        calls.append((pairs, ddl))
        return list(verdicts)

    runner.calls = calls
    return runner


def test_translate_flattens_names_and_stays_on_one_line():
    sql = backend.translate_query(LEFT, SCHEMA)
    assert "\n" not in sql
    assert "proj__ds__orders" in sql and "`" not in sql


def test_schema_ddl_uses_declared_types():
    ddl = backend.schema_to_ddl(SCHEMA)
    assert ddl == "CREATE TABLE proj__ds__orders (id INT, status VARCHAR(1024), amount DOUBLE);\n"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM proj.ds.orders, UNNEST([1,2]) AS x",
        "SELECT RAND() FROM proj.ds.orders",
        "SELECT id FROM proj.ds.missing",
        "SELECT * EXCEPT (id) FROM proj.ds.orders",
        "SELECT 1; SELECT 2",
    ],
)
def test_untranslatable_queries_are_refused(sql):
    with pytest.raises(backend.TranslationError):
        backend.translate_query(sql, SCHEMA)


def test_cte_names_do_not_need_a_schema_entry():
    sql = backend.translate_query("WITH s AS (SELECT id FROM proj.ds.orders) SELECT id FROM s", SCHEMA)
    assert "FROM s" in sql


def test_eq_with_clean_controls_is_a_proof():
    runner = fake_runner("EQ", "NEQ", "NEQ")
    result = backend.prove_equivalent_sqlsolver(LEFT, RIGHT, schema=SCHEMA, runtime=RUNTIME, runner=runner)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert len(runner.calls[0][0]) == 3


def test_eq_is_rejected_when_a_control_says_eq():
    result = backend.prove_equivalent_sqlsolver(
        LEFT, RIGHT, schema=SCHEMA, runtime=RUNTIME, runner=fake_runner("EQ", "EQ", "NEQ")
    )
    assert result.status is SmtStatus.NOT_PROVEN


@pytest.mark.parametrize("verdict", ["NEQ", "UNKNOWN", "TIMEOUT"])
def test_other_verdicts_are_not_proofs(verdict):
    result = backend.prove_equivalent_sqlsolver(
        LEFT, RIGHT, schema=SCHEMA, runtime=RUNTIME, runner=fake_runner(verdict, "NEQ", "NEQ")
    )
    assert result.status is SmtStatus.NOT_PROVEN
    assert verdict in result.reason


def test_runner_failure_is_not_proven():
    def boom(*args, **kwargs):
        raise RuntimeError("no Java")

    result = backend.prove_equivalent_sqlsolver(LEFT, RIGHT, schema=SCHEMA, runtime=RUNTIME, runner=boom)
    assert result.status is SmtStatus.NOT_PROVEN and "no Java" in result.reason


def test_missing_runtime_falls_back_to_z3(tmp_path, monkeypatch):
    pytest.importorskip("z3")
    monkeypatch.setenv("KUMOSQL_SQLSOLVER_HOME", str(tmp_path))
    result = backend.prove_equivalent(
        "SELECT a FROM t WHERE a > 5 AND a > 3", "SELECT a FROM t WHERE a > 5", schema={"t": ["a"]}
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT


def test_sqlsolver_backend_alone_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_SQLSOLVER_HOME", str(tmp_path))
    result = backend.prove_equivalent(LEFT, RIGHT, schema=SCHEMA, backend="sqlsolver")
    assert result.status is SmtStatus.NOT_PROVEN and "unavailable" in result.reason


def test_locate_runtime_reports_what_is_missing(tmp_path):
    runtime, why = backend.locate_runtime(tmp_path)
    assert runtime is None and "sqlsolver.jar" in why
    (tmp_path / "sqlsolver.jar").write_bytes(b"")
    runtime, why = backend.locate_runtime(tmp_path)
    assert runtime is None and "lib" in why


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as a stand-in for Java")
def test_run_sqlsolver_invokes_jar_and_reads_results(tmp_path):
    home = tmp_path / "home"
    (home / "lib").mkdir(parents=True)
    (home / "sqlsolver.jar").write_bytes(b"")
    java = tmp_path / "java"
    java.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = dict(a.lstrip('-').split('=', 1) for a in sys.argv[1:] if '=' in a)\n"
        "n = len(open(args['sql1']).read().splitlines())\n"
        "open(args['output'], 'w').write('EQ\\n' * n)\n"
    )
    java.chmod(java.stat().st_mode | stat.S_IEXEC)
    runtime = backend.SqlSolverRuntime(java=java, jar=home / "sqlsolver.jar", lib_dir=home / "lib")
    assert backend.run_sqlsolver([("SELECT 1", "SELECT 1"), ("SELECT 2", "SELECT 2")], "", runtime) == ["EQ", "EQ"]
