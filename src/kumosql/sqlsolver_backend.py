"""Equivalence proofs with the algebraic prover plus an optional SQLSolver backend.

SQLSolver (https://github.com/SJTU-IPADS/SQLSolver, Apache-2.0) is a Java prover
that decides bag-equivalence of SQL queries with linear integer arithmetic. It is
stronger than the Z3 prover in ``smt_equivalence`` on some shapes (aggregates over
joins, nested subqueries) and weaker on others, so this module only ever *adds*
proofs. ``prove_equivalent`` tries the pure-Python algebraic prover first
(``algebraic_equivalence``, which also gives counterexamples) and asks
SQLSolver only when that finds no proof.

Nothing here needs administrator rights. The runtime is looked up in user space:

* ``KUMOSQL_SQLSOLVER_HOME`` (or ``%LOCALAPPDATA%\\kumosql\\sqlsolver`` on Windows,
  ``~/.local/share/kumosql/sqlsolver`` elsewhere) holding ``sqlsolver.jar`` and a
  ``lib`` folder with the Z3 native libraries (``libz3`` and ``libz3java``).
* Java from ``KUMOSQL_JAVA``, a ``jre`` folder inside that home, ``JAVA_HOME`` or
  ``PATH``, in that order.

SQLSolver reads Calcite SQL, one statement per line, plus ``CREATE TABLE``
statements. BigQuery SQL is parsed with sqlglot and re-emitted in that form with
fully qualified table names flattened to a single identifier.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .algebraic_equivalence import prove_equivalent_algebraic
from .parse_check import refuse_misread_proofs
from .sqlx_fragments import masked_template_problem
from .type_names import invalid_type_name
from .ast_utils import star_modified
from .smt_equivalence import (
    SmtEquivalenceResult,
    SmtStatus,
    _NONDETERMINISTIC_NAMES,
    _NONDETERMINISTIC_TYPES,
    prove_equivalent_smt,
)

JAR_NAME = "sqlsolver.jar"
SQLSOLVER_LICENSE = "Apache-2.0"
SQLSOLVER_ASSUMPTIONS = (
    "proved by SQLSolver under bag semantics with the declared schema",
    "column types come from the schema; columns without a type are treated as INT",
    "runtime errors (division by zero, overflow, failed casts) are not modeled",
)

_BIGQUERY_TYPES = {
    "INT64": "INT",
    "INT": "INT",
    "INTEGER": "INT",
    "FLOAT64": "DOUBLE",
    "FLOAT": "DOUBLE",
    "NUMERIC": "DECIMAL(38, 9)",
    "BIGNUMERIC": "DECIMAL(38, 9)",
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
    "STRING": "VARCHAR(1024)",
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
    "TIME": "TIME",
}
_UNSUPPORTED_NODES = (
    exp.Unnest,
    exp.Qualify,
    exp.Struct,
    exp.Array,
    exp.Window,
    exp.TableSample,
    exp.Pivot,
    exp.Lateral,
)
_LIMITED_WRAPPER = "SELECT * FROM ({sql}) AS kumosql_control WHERE 1 = 0"

# A schema maps a table name as written in the query to its columns, either bare
# names or (name, BigQuery type) pairs.
Schema = Mapping[str, Sequence["str | tuple[str, str]"]]


class TranslationError(Exception):
    """The query cannot be expressed in the form SQLSolver accepts."""


@dataclass(frozen=True)
class SqlSolverRuntime:
    """Where Java and SQLSolver live on this computer."""

    java: Path
    jar: Path
    lib_dir: Path

    def describe(self) -> str:
        return f"java={self.java} jar={self.jar} lib={self.lib_dir}"


def default_home() -> Path:
    override = os.environ.get("KUMOSQL_SQLSOLVER_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "kumosql" / "sqlsolver"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "kumosql" / "sqlsolver"


def _find_java(home: Path) -> Path | None:
    exe = "java.exe" if os.name == "nt" else "java"
    candidates: list[Path] = []
    if os.environ.get("KUMOSQL_JAVA"):
        candidates.append(Path(os.environ["KUMOSQL_JAVA"]))
    candidates.append(home / "jre" / "bin" / exe)
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / exe)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("java")
    return Path(found) if found else None


def locate_runtime(home: Path | None = None) -> tuple[SqlSolverRuntime | None, str]:
    """Return the runtime, or ``None`` and a one-line reason it is unavailable."""

    home = home or default_home()
    jar = home / JAR_NAME
    if not jar.is_file():
        return None, f"{JAR_NAME} not found in {home} (set KUMOSQL_SQLSOLVER_HOME)"
    lib_dir = home / "lib"
    if not lib_dir.is_dir():
        return None, f"Z3 native libraries folder not found: {lib_dir}"
    java = _find_java(home)
    if java is None:
        return None, "no Java found (set KUMOSQL_JAVA or put a JRE in the SQLSolver home's jre folder)"
    return SqlSolverRuntime(java=java, jar=jar, lib_dir=lib_dir), "ok"


# ----------------------------------------------------------------- translation


def flatten_table_name(name: str) -> str:
    """``proj.ds.tbl`` becomes ``proj__ds__tbl`` so Calcite sees one identifier."""

    return re.sub(r"[^0-9A-Za-z_]+", "__", name.strip("`")).lower()


def _schema_columns(columns: Sequence["str | tuple[str, str]"]) -> list[tuple[str, str]]:
    result = []
    for column in columns:
        name, type_name = (column, "INT") if isinstance(column, str) else column
        base = re.split(r"[<(]", str(type_name).upper(), maxsplit=1)[0].strip()
        sql_type = _BIGQUERY_TYPES.get(base)
        if sql_type is None:
            raise TranslationError(f"column {name} has unsupported type {type_name}")
        result.append((str(name).lower(), sql_type))
    return result


def schema_to_ddl(schema: Schema) -> str:
    """Render ``CREATE TABLE`` statements for SQLSolver's schema file."""

    statements = []
    for table, columns in schema.items():
        cols = ", ".join(f"{name} {sql_type}" for name, sql_type in _schema_columns(columns))
        statements.append(f"CREATE TABLE {flatten_table_name(table)} ({cols});")
    return "\n".join(statements) + "\n"


def _table_key(table: exp.Table) -> str:
    parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    return ".".join(part.name for part in parts if part is not None)


def translate_query(sql: str, schema: Schema) -> str:
    """Re-emit a BigQuery query as one line of Calcite-compatible SQL."""

    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        raise TranslationError(f"parse error: {error}") from error
    statements = [s for s in statements if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise TranslationError("expected exactly one query statement")
    tree = statements[0]

    known = {flatten_table_name(name) for name in schema}
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for node in tree.walk():
        if isinstance(node, _UNSUPPORTED_NODES):
            raise TranslationError(f"{type(node).__name__} is not supported by SQLSolver")
        if type(node).__name__ in _NONDETERMINISTIC_TYPES:
            raise TranslationError(f"nondeterministic function {type(node).__name__}")
        if isinstance(node, exp.Anonymous) and node.name.upper() in _NONDETERMINISTIC_NAMES:
            raise TranslationError(f"nondeterministic function {node.name.upper()}")
        if isinstance(node, exp.Star) and star_modified(node):
            raise TranslationError("SELECT * EXCEPT/REPLACE/RENAME/ILIKE is not supported")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            if not node.args.get("db") and node.name.lower() in cte_names:
                continue
            if flatten_table_name(_table_key(node)) not in known:
                raise TranslationError(f"table {_table_key(node)} is missing from the schema")

    def flatten(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            if node.args.get("db") or node.name.lower() not in cte_names:
                flat = flatten_table_name(_table_key(node))
                node.set("catalog", None)
                node.set("db", None)
                node.set("this", exp.to_identifier(flat))
        return node

    tree = tree.transform(flatten)
    for identifier in tree.find_all(exp.Identifier):
        identifier.set("quoted", False)
        identifier.set("this", identifier.name.lower())
    return " ".join(tree.sql(dialect="postgres", pretty=False).split())


# --------------------------------------------------------------------- running


def _environment(runtime: SqlSolverRuntime) -> dict[str, str]:
    env = dict(os.environ)
    lib = str(runtime.lib_dir)
    key = "PATH" if os.name == "nt" else ("DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH")
    env[key] = lib + os.pathsep + env.get(key, "")
    return env


def run_sqlsolver(
    pairs: Sequence[tuple[str, str]],
    schema_ddl: str,
    runtime: SqlSolverRuntime,
    *,
    timeout_s: float = 60.0,
) -> list[str]:
    """Run translated pairs through one JVM; return one EQ/NEQ/UNKNOWN/TIMEOUT per pair."""

    with tempfile.TemporaryDirectory(prefix="kumosql-sqlsolver-") as tmp:
        folder = Path(tmp)
        (folder / "left.sql").write_text("\n".join(l for l, _ in pairs) + "\n", encoding="utf-8")
        (folder / "right.sql").write_text("\n".join(r for _, r in pairs) + "\n", encoding="utf-8")
        (folder / "schema.sql").write_text(schema_ddl, encoding="utf-8")
        command = [
            str(runtime.java),
            f"-Djava.library.path={runtime.lib_dir}",
            "-jar",
            str(runtime.jar),
            f"-sql1={folder / 'left.sql'}",
            f"-sql2={folder / 'right.sql'}",
            f"-schema={folder / 'schema.sql'}",
            f"-output={folder / 'result.txt'}",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=folder,
                env=_environment(runtime),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ["TIMEOUT"] * len(pairs)
        except OSError as error:
            raise RuntimeError(f"could not start Java: {error}") from error
        result_file = folder / "result.txt"
        if not result_file.is_file():
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"SQLSolver produced no result file ({tail[0]})")
        lines = [line.strip() for line in result_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != len(pairs):
        raise RuntimeError(f"SQLSolver returned {len(lines)} results for {len(pairs)} pairs")
    return lines


@refuse_misread_proofs
def prove_equivalent_sqlsolver(
    left_sql: str,
    right_sql: str,
    *,
    schema: Schema,
    runtime: SqlSolverRuntime | None = None,
    timeout_s: float = 60.0,
    runner: Callable[..., list[str]] = run_sqlsolver,
) -> SmtEquivalenceResult:
    """Ask SQLSolver whether two BigQuery queries return the same result bag.

    Only ``PROVEN_EQUIVALENT`` is a proof. SQLSolver's ``NEQ`` carries no
    counterexample, so it is reported as ``NOT_PROVEN``.
    """

    unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
    if unknown_type:
        # SQLSolver reads the schema's column types, not the queries' casts, so it would not notice a type name
        # BigQuery rejects, and the translation prints FLOAT, INT32 and VARCHAR as FLOAT64, INT64 and STRING.
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BigQuery would reject the query: {unknown_type}")
    masked = masked_template_problem(left_sql, right_sql)
    if masked:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, masked)
    if runtime is None:
        runtime, why = locate_runtime()
        if runtime is None:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"SQLSolver unavailable: {why}")
    try:
        left = translate_query(left_sql, schema)
        right = translate_query(right_sql, schema)
        ddl = schema_to_ddl(schema)
    except TranslationError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"cannot translate for SQLSolver: {error}")

    # SQLSolver answers EQ when both queries fail semantic checks, so each query
    # is also compared with an always-empty wrapper of itself. A control that
    # comes back EQ means the query was not understood and the proof is void.
    pairs = [
        (left, right),
        (left, _LIMITED_WRAPPER.format(sql=left)),
        (right, _LIMITED_WRAPPER.format(sql=right)),
    ]
    try:
        verdicts = runner(pairs, ddl, runtime, timeout_s=timeout_s)
    except RuntimeError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"SQLSolver failed: {error}")
    main, left_control, right_control = verdicts
    if left_control == "EQ" or right_control == "EQ":
        return SmtEquivalenceResult(
            SmtStatus.NOT_PROVEN,
            "SQLSolver did not accept one of the queries (check the schema column types)",
        )
    if main == "EQ":
        return SmtEquivalenceResult(
            SmtStatus.PROVEN_EQUIVALENT, "SQLSolver proved the queries equivalent", assumptions=SQLSOLVER_ASSUMPTIONS
        )
    return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"SQLSolver result: {main}")


@refuse_misread_proofs
def prove_equivalent(
    left_sql: str,
    right_sql: str,
    *,
    schema: Schema | None = None,
    backend: str = "auto",
    timeout_ms: int = 5000,
    sqlsolver_timeout_s: float = 60.0,
    exact_arithmetic: bool = False,
) -> SmtEquivalenceResult:
    """Prove equivalence with the algebraic prover, then SQLSolver when installed.

    ``backend`` is ``"auto"`` (algebraic normalization plus Z3 first, SQLSolver
    only when that finds no proof), ``"algebraic"``, ``"sqlsolver"`` or ``"z3"``
    (plain, no normalization). The algebraic stage is pure Python on top of
    ``z3-solver`` and also supplies counterexamples, which SQLSolver cannot.
    """

    if backend not in {"auto", "algebraic", "sqlsolver", "z3"}:
        raise ValueError(f"unknown backend {backend!r}")
    plain_schema = (
        {table: [c if isinstance(c, str) else c[0] for c in cols] for table, cols in schema.items()}
        if schema
        else None
    )
    first: SmtEquivalenceResult | None = None
    if backend in {"auto", "algebraic", "z3"}:
        prover = prove_equivalent_smt if backend == "z3" else prove_equivalent_algebraic
        first = prover(
            left_sql, right_sql, schema=plain_schema, exact_arithmetic=exact_arithmetic, timeout_ms=timeout_ms
        )
        if first.proven or backend != "auto":
            return first
    if schema is None:
        second = SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "SQLSolver needs a schema")
    else:
        second = prove_equivalent_sqlsolver(left_sql, right_sql, schema=schema, timeout_s=sqlsolver_timeout_s)
    if first is None or second.proven:
        return second
    return SmtEquivalenceResult(
        first.status,
        f"{first.reason}; SQLSolver: {second.reason}",
        counterexample=first.counterexample,
        assumptions=first.assumptions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove two BigQuery queries equivalent with SQLSolver (Z3 fallback).")
    parser.add_argument("left", nargs="?", help="path to the left SQL file")
    parser.add_argument("right", nargs="?", help="path to the right SQL file")
    parser.add_argument("--schema", help="JSON file mapping table names to column lists or [name, type] pairs")
    parser.add_argument("--backend", choices=["auto", "algebraic", "sqlsolver", "z3"], default="auto")
    parser.add_argument("--check", action="store_true", help="report whether Java and SQLSolver are usable, then exit")
    args = parser.parse_args(argv)
    if args.check:
        runtime, why = locate_runtime()
        print(f"SQLSolver ready: {runtime.describe()}" if runtime else f"SQLSolver not available: {why}")
        return 0 if runtime else 1
    if not (args.left and args.right):
        parser.error("left and right SQL files are required")
    left = Path(args.left).read_text(encoding="utf-8")
    right = Path(args.right).read_text(encoding="utf-8")
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8")) if args.schema else None
    result = prove_equivalent(left, right, schema=schema, backend=args.backend)
    json.dump(
        {"status": result.status.value, "reason": result.reason, "assumptions": list(result.assumptions)},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0 if result.proven else 1


if __name__ == "__main__":
    raise SystemExit(main())
