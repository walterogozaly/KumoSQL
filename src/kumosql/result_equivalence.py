"""Execution-based equivalence checks for BigQuery SQL rewrites.

This module complements the static prover in :mod:`kumosql.equivalence`.
Instead of comparing ASTs, it runs the original and the rewritten SQL against
the same deterministic synthetic tables in a local DuckDB engine and compares
the result sets. Agreement on synthetic data is evidence, not a proof; a
disagreement is a concrete counterexample.

Each run is isolated: every side of every seed gets its own in-memory DuckDB
connection, physical source tables are loaded fresh from the synthetic
dataset, and tables written by a script (``CREATE TABLE``/``INSERT``) are
renamed to run-unique local names so two runs can never observe each other's
output.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import math
import random
import re
from typing import Any, Iterable, Mapping

import sqlglot
from sqlglot import exp

from .sqlx import looks_like_sqlx, split_sqlx_sections

Schema = Mapping[str, Mapping[str, str]]
"""Table name (as written in SQL, e.g. ``p.d.customers``) -> column -> BigQuery type."""

Row = tuple[Any, ...]


class ResultEquivalenceStatus(str, Enum):
    """Possible outcomes of an execution-based comparison."""

    EQUIVALENT = "equivalent"
    DIFFERENT = "different"
    ERROR = "error"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class QueryOutput:
    """Column names and rows produced by one execution."""

    columns: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class ResultEquivalence:
    """The outcome of comparing two queries over one or more synthetic datasets."""

    status: ResultEquivalenceStatus
    reason: str
    seeds_checked: tuple[int, ...] = ()
    failing_seed: int | None = None
    left_output: QueryOutput | None = None
    right_output: QueryOutput | None = None
    only_left: tuple[Row, ...] = ()
    only_right: tuple[Row, ...] = ()
    left_duckdb_sql: tuple[str, ...] = ()
    right_duckdb_sql: tuple[str, ...] = ()

    @property
    def equivalent(self) -> bool:
        return self.status is ResultEquivalenceStatus.EQUIVALENT

    def describe(self, max_rows: int = 10) -> str:
        """Render a human-readable report, including any counterexample."""

        lines = [f"{self.status.value}: {self.reason}"]
        if self.failing_seed is not None:
            lines.append(f"failing seed: {self.failing_seed}")
        if self.left_output is not None and self.right_output is not None:
            lines.append(f"left columns:  {self.left_output.columns}")
            lines.append(f"right columns: {self.right_output.columns}")
        for label, rows in (("only in left", self.only_left), ("only in right", self.only_right)):
            if rows:
                lines.append(f"{label} ({len(rows)} rows):")
                lines.extend(f"  {row!r}" for row in rows[:max_rows])
        for label, statements in (("left DuckDB SQL", self.left_duckdb_sql), ("right DuckDB SQL", self.right_duckdb_sql)):
            if statements and self.status is not ResultEquivalenceStatus.EQUIVALENT:
                lines.append(f"{label}:")
                lines.extend(f"  {statement}" for statement in statements)
        return "\n".join(lines)


class ExecutionError(RuntimeError):
    """Raised when SQL cannot be translated or executed locally."""


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

# Deliberately small domains: joins find matches, GROUP BY produces multi-row
# groups, and duplicate rows appear, which is where rewrites usually break.
_DOMAINS: dict[str, tuple[Any, ...]] = {
    "INT64": (-1, 0, 1, 2, 3, 4, 5, 7),
    "FLOAT64": (-2.5, 0.0, 0.5, 1.0, 1.25, 3.75, 10.0),
    "NUMERIC": tuple(Decimal(v) for v in ("-1.50", "0", "0.25", "1", "2.75", "100")),
    "STRING": ("", "a", "A", "b", "b ", "café", "x_y", "long value"),
    "BOOL": (True, False),
    "DATE": tuple(date(2024, 1, 1) + timedelta(days=d) for d in (0, 1, 30, 31, 59, 365)),
    "TIMESTAMP": tuple(
        datetime(2024, 1, 1) + timedelta(hours=h) for h in (0, 1, 23, 24, 24 * 31, 24 * 366)
    ),
}

_TYPE_ALIASES = {
    "INT": "INT64",
    "INTEGER": "INT64",
    "BIGINT": "INT64",
    "FLOAT": "FLOAT64",
    "DOUBLE": "FLOAT64",
    "DECIMAL": "NUMERIC",
    "BOOLEAN": "BOOL",
    "DATETIME": "TIMESTAMP",
}

_DUCKDB_TYPES = {
    "INT64": "BIGINT",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL(38, 9)",
    "STRING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
}


def _normalize_type(bq_type: str) -> str:
    upper = bq_type.strip().upper()
    upper = _TYPE_ALIASES.get(upper, upper)
    if upper not in _DOMAINS:
        raise ValueError(f"unsupported synthetic column type: {bq_type!r}")
    return upper


@dataclass(frozen=True)
class SyntheticTable:
    """Column (name, BigQuery type) pairs and the rows generated for them."""

    columns: tuple[tuple[str, str], ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class SyntheticDataset:
    """Deterministic tables keyed by their SQL name (e.g. ``p.d.customers``)."""

    seed: int
    tables: Mapping[str, SyntheticTable] = field(default_factory=dict)


_MAX_CONSTANTS_PER_TYPE = 24


def query_constants(*sqls: str) -> dict[str, tuple[Any, ...]]:
    """Values worth generating for each column type, read from the queries' own constants.

    A filter such as ``it.info = 'top 250 rank'`` or ``d_year = 1998`` never matches values
    drawn from a small fixed domain, so both sides of a comparison return nothing and agree
    trivially. Adding the constants (and a string matching each ``LIKE`` pattern) makes such
    filters select rows.
    """

    found: dict[str, list[Any]] = {t: [] for t in ("INT64", "FLOAT64", "NUMERIC", "STRING", "DATE")}
    for sql in sqls:
        try:
            statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
        except sqlglot.errors.SqlglotError:
            continue
        for statement in statements:
            for node in statement.walk():
                if isinstance(node, (exp.Like, exp.ILike)) and isinstance(node.expression, exp.Literal):
                    pattern = node.expression.name
                    found["STRING"].append(pattern.replace("%", "").replace("_", "x"))
                    continue
                if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
                    if node.to.this == exp.DataType.Type.DATE:
                        try:
                            found["DATE"].append(date.fromisoformat(node.this.name[:10]))
                        except ValueError:
                            pass
                    continue
                if not isinstance(node, exp.Literal) or isinstance(node.parent, (exp.Like, exp.ILike, exp.Cast)):
                    continue
                if node.is_string:
                    found["STRING"].append(node.name)
                    try:
                        found["DATE"].append(date.fromisoformat(node.name))
                    except ValueError:
                        pass
                    continue
                text = node.name
                try:
                    if re.fullmatch(r"-?\d+", text):
                        found["INT64"].append(int(text))
                    else:
                        found["FLOAT64"].append(float(text))
                        found["NUMERIC"].append(Decimal(text))
                except (ValueError, ArithmeticError):
                    pass
    out = {}
    for type_name, values in found.items():
        unique = [v for v in dict.fromkeys(values) if v not in _DOMAINS[type_name]]
        if unique:
            out[type_name] = tuple(unique[:_MAX_CONSTANTS_PER_TYPE])
    return out


def _draw(rng: random.Random, col_type: str, extras: Mapping[str, tuple[Any, ...]]) -> Any:
    # Half the values come from the queries' constants when there are any, so that rows
    # matching several filters at once are common, not a rare coincidence.
    if extras.get(col_type) and rng.random() < 0.5:
        return rng.choice(extras[col_type])
    return rng.choice(_DOMAINS[col_type])


def generate_synthetic_dataset(
    schema: Schema,
    *,
    seed: int,
    rows_per_table: int = 25,
    null_rate: float = 0.15,
    extra_values: Mapping[str, Iterable[Any]] | None = None,
) -> SyntheticDataset:
    """Generate reproducible synthetic rows for every table in ``schema``.

    Seed 0 is always an empty dataset, which catches rewrites that differ only
    when an input is empty (for example aggregates without ``GROUP BY``).
    Every other seed includes NULLs and at least one exact duplicate row per
    non-empty table, so bag semantics are exercised.
    """

    rng = random.Random(seed)
    extras = {
        name: tuple(v for v in (extra_values or {}).get(name, ()) if v not in values)
        for name, values in _DOMAINS.items()
    }
    tables: dict[str, SyntheticTable] = {}
    for table_name in sorted(schema):
        columns = tuple((name, _normalize_type(t)) for name, t in schema[table_name].items())
        if not columns:
            raise ValueError(f"table {table_name!r} has no columns")
        count = 0 if seed == 0 else rng.randint(1, rows_per_table)
        rows: list[Row] = []
        for _ in range(count):
            rows.append(
                tuple(
                    None if rng.random() < null_rate else _draw(rng, col_type, extras)
                    for _, col_type in columns
                )
            )
        if rows:
            rows.append(rng.choice(rows))
            rng.shuffle(rows)
        tables[table_name] = SyntheticTable(columns=columns, rows=tuple(rows))
    return SyntheticDataset(seed=seed, tables=tables)


# ---------------------------------------------------------------------------
# SQL preparation
# ---------------------------------------------------------------------------

_REF_RE = re.compile(
    r"""\$\{\s*ref\(\s*(?P<args>(?:"[^"]*"|'[^']*')(?:\s*,\s*(?:"[^"]*"|'[^']*'))*)\s*\)\s*\}"""
)
_INTERPOLATION_RE = re.compile(r"\$\{")


def sqlx_to_sql(sqlx: str) -> str:
    """Reduce Dataform SQLX to executable SQL.

    ``config``/``js``/``pre_operations``/``post_operations`` blocks are
    dropped and ``${ref("name")}`` / ``${ref("schema", "name")}`` become
    backtick-quoted table names. Any other interpolation fails closed, because
    guessing its value could hide a real difference.
    """

    sql = "".join(text for kind, text in split_sqlx_sections(sqlx) if kind == "sql")

    def replace(match: re.Match[str]) -> str:
        parts = [part.strip()[1:-1] for part in re.findall(r'"[^"]*"|\'[^\']*\'', match.group("args"))]
        return "`" + ".".join(parts) + "`"

    sql = _REF_RE.sub(replace, sql)
    if _INTERPOLATION_RE.search(sql):
        raise ExecutionError("SQLX contains interpolations other than ref(); cannot execute locally")
    return sql


def _table_key(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _local_name(key: str) -> str:
    return "src__" + re.sub(r"[^0-9A-Za-z_]", "_", key.replace(".", "__"))


def _cte_names(statement: exp.Expression) -> set[str]:
    names: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias)
    return names


def _write_target(statement: exp.Expression) -> exp.Table | None:
    if isinstance(statement, exp.Create):
        target = statement.this
    elif isinstance(statement, exp.Insert):
        target = statement.this
    else:
        return None
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _schema_lookup(schema: Schema) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for key in schema:
        lookup[key.lower()] = key
    return lookup


def prepare_statements(
    sql: str,
    schema: Schema,
    *,
    run_tag: str,
) -> tuple[list[str], str | None]:
    """Translate BigQuery SQL (or SQLX) to DuckDB statements for one isolated run.

    Returns the DuckDB statements and the local name of the table the script
    wrote last (``None`` when the final statement is a query). Physical tables
    must appear in ``schema``; unknown tables fail closed instead of being
    silently treated as empty.
    """

    if looks_like_sqlx(sql):
        sql = sqlx_to_sql(sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise ExecutionError(f"BigQuery parse failed: {exc}") from exc
    if not statements:
        raise ExecutionError("no SQL statements to execute")

    lookup = _schema_lookup(schema)
    targets: dict[str, str] = {}
    duckdb_sql: list[str] = []
    last_target: str | None = None
    for index, statement in enumerate(statements):
        ctes = _cte_names(statement)
        target = _write_target(statement)
        target_local: str | None = None
        if target is not None:
            target_key = _table_key(target).lower()
            target_local = targets.get(target_key)
            if target_local is None:
                target_local = f"__eqv_{run_tag}_target_{len(targets) + 1:03d}"
                if isinstance(statement, exp.Insert):
                    # INSERT appends to existing rows, so the renamed target
                    # starts as a copy of the source table it stands in for.
                    if target_key not in lookup:
                        raise ExecutionError(
                            f"statement {index + 1} inserts into {_table_key(target)!r}, "
                            "which is neither created by the script nor in the synthetic schema"
                        )
                    duckdb_sql.append(
                        f'CREATE TABLE "{target_local}" AS SELECT * FROM "{_local_name(lookup[target_key])}"'
                    )
        elif isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            last_target = None
        for table in list(statement.find_all(exp.Table)):
            if table is target:
                continue
            key = _table_key(table)
            if not key:
                continue
            if not table.db and not table.catalog and key in ctes:
                continue
            lowered = key.lower()
            if lowered in targets:
                local = targets[lowered]
            elif lowered in lookup:
                local = _local_name(lookup[lowered])
            else:
                raise ExecutionError(
                    f"statement {index + 1} references table {key!r}, which is not in the synthetic schema"
                )
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(local))
        if target is not None:
            # Register the target only after resolving reads, so
            # ``CREATE TABLE t AS SELECT ... FROM t`` reads the source table.
            targets[target_key] = target_local
            last_target = target_local
            target.set("catalog", None)
            target.set("db", None)
            target.set("this", exp.to_identifier(target_local))
        try:
            duckdb_sql.append(statement.sql(dialect="duckdb"))
        except sqlglot.errors.SqlglotError as exc:
            raise ExecutionError(f"cannot translate statement {index + 1} to DuckDB: {exc}") from exc
    return duckdb_sql, last_target


# ---------------------------------------------------------------------------
# Execution and comparison
# ---------------------------------------------------------------------------


def _connect():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ExecutionError(
            "duckdb is required for result equivalence; install kumosql[execution]"
        ) from exc
    return duckdb.connect(database=":memory:")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return repr(value) if not isinstance(value, Decimal) else f"'{value}'"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"cannot render synthetic value {value!r}")


def _load_dataset(connection, dataset: SyntheticDataset) -> None:
    for key, table in dataset.tables.items():
        local = _local_name(key)
        column_sql = ", ".join(f'"{name}" {_DUCKDB_TYPES[t]}' for name, t in table.columns)
        connection.execute(f'CREATE TABLE "{local}" ({column_sql})')
        if table.rows:
            # One multi-row INSERT of literals: executemany costs a round trip
            # per row, and bound parameters make DuckDB probe for optional
            # Python modules on every call.
            values_sql = ", ".join(
                "(" + ", ".join(_sql_literal(value) for value in row) + ")" for row in table.rows
            )
            connection.execute(f'INSERT INTO "{local}" VALUES {values_sql}')


def execute_on_dataset(
    sql: str,
    schema: Schema,
    dataset: SyntheticDataset,
    *,
    run_tag: str = "run",
) -> tuple[QueryOutput, list[str]]:
    """Run ``sql`` in a fresh DuckDB connection loaded with ``dataset``."""

    statements, last_target = prepare_statements(sql, schema, run_tag=run_tag)
    connection = _connect()
    try:
        _load_dataset(connection, dataset)
        cursor = None
        for statement in statements:
            try:
                cursor = connection.execute(statement)
            except Exception as exc:
                raise ExecutionError(f"DuckDB failed on {statement!r}: {exc}") from exc
        if last_target is not None:
            cursor = connection.execute(f'SELECT * FROM "{last_target}"')
        if cursor is None or cursor.description is None:
            raise ExecutionError("script produced neither a query result nor a written table")
        try:
            columns = tuple(column[0] for column in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except Exception as exc:
            raise ExecutionError(f"DuckDB failed while fetching results: {exc}") from exc
    finally:
        connection.close()
    return QueryOutput(columns=columns, rows=rows), statements


def _normalize_value(value: Any, float_digits: int) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return ("NaN",)
        if value == 0:
            return 0.0
        return float(f"{value:.{float_digits}g}")
    if isinstance(value, Decimal):
        return value.normalize() if value == value else ("NaN",)
    if isinstance(value, list):
        return tuple(_normalize_value(v, float_digits) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _normalize_value(v, float_digits)) for k, v in value.items()))
    return value


def _sort_key(row: Row) -> tuple:
    return tuple((value is None, type(value).__name__, repr(value)) for value in row)


def compare_outputs(
    left: QueryOutput,
    right: QueryOutput,
    *,
    ignore_row_order: bool = True,
    check_column_names: bool = True,
    float_digits: int = 12,
) -> tuple[bool, str, tuple[Row, ...], tuple[Row, ...]]:
    """Compare two outputs; returns (equal, reason, only_left, only_right)."""

    if len(left.columns) != len(right.columns):
        return False, f"column counts differ ({len(left.columns)} vs {len(right.columns)})", (), ()
    if check_column_names and [c.lower() for c in left.columns] != [c.lower() for c in right.columns]:
        return False, "column names differ", (), ()

    left_rows = [tuple(_normalize_value(v, float_digits) for v in row) for row in left.rows]
    right_rows = [tuple(_normalize_value(v, float_digits) for v in row) for row in right.rows]

    left_counts = Counter(left_rows)
    right_counts = Counter(right_rows)
    only_left = tuple(sorted((left_counts - right_counts).elements(), key=_sort_key))
    only_right = tuple(sorted((right_counts - left_counts).elements(), key=_sort_key))
    if only_left or only_right:
        return False, "result multisets differ", only_left, only_right
    if not ignore_row_order and left_rows != right_rows:
        return False, "same rows but in a different order", (), ()
    return True, "results match", (), ()


def check_result_equivalence(
    left_sql: str,
    right_sql: str,
    schema: Schema,
    *,
    seeds: Iterable[int] = range(8),
    rows_per_table: int = 25,
    null_rate: float = 0.15,
    ignore_row_order: bool = True,
    check_column_names: bool = True,
    float_digits: int = 12,
    use_query_constants: bool = True,
) -> ResultEquivalence:
    """Run both queries over several synthetic datasets and compare results.

    With ``use_query_constants`` the generated values include the queries' own constants
    (see :func:`query_constants`), so their filters match some rows.

    Stops at the first seed that yields a counterexample or an execution
    error. An error on either side is never reported as equivalence. Each side
    is executed twice per seed; a side that differs from itself makes the
    result ``INCONCLUSIVE`` instead of equivalent or different.
    """

    extras = query_constants(left_sql, right_sql) if use_query_constants else None
    checked: list[int] = []
    left_sql_out: list[str] = []
    right_sql_out: list[str] = []
    for seed in seeds:
        dataset = generate_synthetic_dataset(
            schema, seed=seed, rows_per_table=rows_per_table, null_rate=null_rate, extra_values=extras
        )
        try:
            left_output, left_sql_out = execute_on_dataset(
                left_sql, schema, dataset, run_tag=f"left_{seed}"
            )
        except ExecutionError as exc:
            return ResultEquivalence(
                ResultEquivalenceStatus.ERROR, f"left side failed: {exc}", tuple(checked), seed
            )
        try:
            right_output, right_sql_out = execute_on_dataset(
                right_sql, schema, dataset, run_tag=f"right_{seed}"
            )
        except ExecutionError as exc:
            return ResultEquivalence(
                ResultEquivalenceStatus.ERROR,
                f"right side failed: {exc}",
                tuple(checked),
                seed,
                left_output=left_output,
                left_duckdb_sql=tuple(left_sql_out),
            )
        # A side that disagrees with itself on identical input cannot support
        # either an equivalence or a counterexample claim.
        for side, side_sql, first_output in (
            ("left", left_sql, left_output),
            ("right", right_sql, right_output),
        ):
            try:
                repeat_output, _ = execute_on_dataset(
                    side_sql, schema, dataset, run_tag=f"{side}_{seed}_repeat"
                )
            except ExecutionError as exc:
                return ResultEquivalence(
                    ResultEquivalenceStatus.ERROR,
                    f"{side} side failed on repeat run: {exc}",
                    tuple(checked),
                    seed,
                )
            stable, _, _, _ = compare_outputs(
                first_output,
                repeat_output,
                ignore_row_order=ignore_row_order,
                check_column_names=check_column_names,
                float_digits=float_digits,
            )
            if not stable:
                return ResultEquivalence(
                    ResultEquivalenceStatus.INCONCLUSIVE,
                    f"{side} side returned different results on two runs over the same "
                    f"synthetic data (seed {seed}); it is nondeterministic, so the "
                    "comparison proves nothing either way",
                    tuple(checked),
                    seed,
                    left_duckdb_sql=tuple(left_sql_out),
                    right_duckdb_sql=tuple(right_sql_out),
                )
        checked.append(seed)
        equal, reason, only_left, only_right = compare_outputs(
            left_output,
            right_output,
            ignore_row_order=ignore_row_order,
            check_column_names=check_column_names,
            float_digits=float_digits,
        )
        if not equal:
            return ResultEquivalence(
                ResultEquivalenceStatus.DIFFERENT,
                reason,
                tuple(checked),
                seed,
                left_output,
                right_output,
                only_left,
                only_right,
                tuple(left_sql_out),
                tuple(right_sql_out),
            )
    if not checked:
        raise ValueError("at least one seed is required")
    return ResultEquivalence(
        ResultEquivalenceStatus.EQUIVALENT,
        f"results match on {len(checked)} synthetic datasets",
        tuple(checked),
        left_duckdb_sql=tuple(left_sql_out),
        right_duckdb_sql=tuple(right_sql_out),
    )


def assert_result_equivalent(left_sql: str, right_sql: str, schema: Schema, **kwargs: Any) -> ResultEquivalence:
    """Pytest-friendly wrapper that raises ``AssertionError`` with a counterexample."""

    result = check_result_equivalence(left_sql, right_sql, schema, **kwargs)
    if not result.equivalent:
        raise AssertionError(result.describe())
    return result
