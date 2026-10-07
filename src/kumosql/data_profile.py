"""Data profiling: what a table's values look like, as generated SQL that BigQuery or DuckDB runs.

BigQuery's own data profile scan is a managed Dataplex service, so there is no engine to reuse.
This module reproduces what it reports (row count; per column the null and distinct counts,
min, max, mean, standard deviation, quartiles, string lengths and the most common values) by
writing ordinary read-only SQL for an *executor* to run:

* :class:`DuckDBExecutor` runs it on a local DuckDB connection (tests, files, small copies);
* :class:`BigQueryExecutor` runs it in the billing project under the same dry run and
  ``maximumBytesBilled`` guards as every other BigQuery query KumoSQL sends.

Columns are profiled a chunk at a time, so a wide table never becomes one enormous query, and a
chunk BigQuery rejects is retried column by column: one unsupported column is reported under
``skipped``, never the whole profile. A byte-cap refusal is the exception: it stops the run.

Values are real data. ``include_values=False`` (``--no-values``) leaves out the most common
values and the min and max of string columns.

``TableProfile`` in :mod:`kumosql.table_profile` is a different thing: what a *query's* result
means (grain and scope). A :class:`DataProfile` describes the values stored in a table.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Sequence

import sqlglot
from sqlglot import exp

from .sql_validation import quote_table_path, validate_readonly_query

__all__ = [
    "BigQueryExecutor", "ByteCapExceeded", "ColumnProfile", "DataProfile", "DuckDBExecutor", "Executor",
    "ProfileError", "TopValue", "classify", "profile_table", "profile_queries",
]

VERSION = 1
DEFAULT_TOP_VALUES = 10
MAX_TOP_VALUES = 50
DEFAULT_MAX_COLUMNS = 500
DEFAULT_CHUNK = 20
MAX_TEXT = 200
KINDS = ("numeric", "string", "boolean", "temporal", "other")


class ProfileError(ValueError):
    """The profile could not be computed; the message says what to change."""


class ByteCapExceeded(ProfileError):
    """BigQuery's dry run estimated more bytes than the cap allows; nothing was run."""


# ------------------------------------------------------------------- results


@dataclass
class TopValue:
    value: str
    count: int
    fraction: float | None  # of the column's non-null values

    def to_json(self) -> dict:
        return {"value": self.value, "count": self.count, "fraction": self.fraction}


@dataclass
class ColumnProfile:
    name: str
    type: str
    kind: str
    non_null: int
    null_count: int
    null_fraction: float | None
    distinct: int | None = None
    unique_fraction: float | None = None  # distinct values among the non-null ones
    min: object = None
    max: object = None
    mean: float | None = None
    stddev: float | None = None
    p25: float | int | None = None
    median: float | int | None = None
    p75: float | int | None = None
    min_length: int | None = None
    max_length: int | None = None
    mean_length: float | None = None
    top_values: list[TopValue] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # all_null, constant, unique

    def to_json(self) -> dict:
        data = {name: getattr(self, name) for name in (
            "name", "type", "kind", "non_null", "null_count", "null_fraction", "distinct", "unique_fraction",
            "min", "max", "mean", "stddev", "p25", "median", "p75", "min_length", "max_length", "mean_length")}
        data["top_values"] = [item.to_json() for item in self.top_values]
        data["flags"] = list(self.flags)
        return data


@dataclass
class DataProfile:
    table: str
    dialect: str
    generated_at: str
    row_count: int
    columns: list[ColumnProfile]
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sample_percent: float | None = None
    row_filter: str | None = None
    approximate_distinct: bool = False
    estimated_bytes: int | None = None
    bytes_billed: int | None = None

    def to_json(self) -> dict:
        return {
            "version": VERSION, "table": self.table, "dialect": self.dialect, "generated_at": self.generated_at,
            "row_count": self.row_count, "sample_percent": self.sample_percent, "row_filter": self.row_filter,
            "approximate_distinct": self.approximate_distinct, "estimated_bytes": self.estimated_bytes,
            "bytes_billed": self.bytes_billed, "notes": list(self.notes), "skipped": list(self.skipped),
            "columns": [column.to_json() for column in self.columns],
        }

    @classmethod
    def from_json(cls, data: object) -> "DataProfile":
        """Read a profile written by :meth:`to_json`; raises ``ProfileError`` when it is not one."""

        try:
            if not isinstance(data, dict) or data.get("version") != VERSION:
                raise ValueError("not a data profile of this version")
            columns = []
            for item in data["columns"]:
                tops = [TopValue(str(t["value"]), int(t["count"]), t.get("fraction")) for t in item.get("top_values", [])]
                known = {k: v for k, v in item.items() if k not in ("top_values", "flags")}
                columns.append(ColumnProfile(**known, top_values=tops, flags=list(item.get("flags", []))))
            return cls(
                table=str(data["table"]), dialect=str(data["dialect"]), generated_at=str(data["generated_at"]),
                row_count=int(data["row_count"]), columns=columns, skipped=list(data.get("skipped", [])),
                notes=list(data.get("notes", [])), sample_percent=data.get("sample_percent"),
                row_filter=data.get("row_filter"), approximate_distinct=bool(data.get("approximate_distinct")),
                estimated_bytes=data.get("estimated_bytes"), bytes_billed=data.get("bytes_billed"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError(f"not a readable data profile: {exc}") from None


# ------------------------------------------------------------------ typing


_NUMERIC = re.compile(r"^(U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?\d*|FLOAT\d*|DOUBLE( PRECISION)?|REAL|NUMERIC|BIGNUMERIC|DECIMAL|BIGDECIMAL)$")
_STRING = re.compile(r"^(STRING|VARCHAR|TEXT|CHAR|BPCHAR|NVARCHAR)$")
_TEMPORAL = re.compile(r"^(DATE|DATETIME|TIME|TIMETZ|TIMESTAMP|TIMESTAMPTZ|TIMESTAMP_[A-Z]+|TIMESTAMP WITH(OUT)? TIME ZONE|TIME WITH(OUT)? TIME ZONE)$")


def classify(type_name: str) -> str:
    """``numeric``, ``string``, ``boolean``, ``temporal`` or ``other`` (arrays, structs, JSON, bytes, geography...)."""

    text = re.sub(r"\(.*\)", "", str(type_name or "")).strip().upper()
    if not text or "<" in text or text.endswith("]") or text.startswith(("ARRAY", "STRUCT", "LIST", "MAP", "RECORD")):
        return "other"
    if _NUMERIC.match(text):
        return "numeric"
    if _STRING.match(text):
        return "string"
    if text in ("BOOL", "BOOLEAN"):
        return "boolean"
    if _TEMPORAL.match(text):
        return "temporal"
    return "other"


# ------------------------------------------------------------------ SQL text


_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str, dialect: str) -> str:
    if dialect == "bigquery":
        if any(ch in name for ch in "`\\\n\r\0"):
            raise ProfileError("a column name with a backtick, backslash or line break cannot be profiled")
        return f"`{name}`"
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _text_type(dialect: str) -> str:
    return "STRING" if dialect == "bigquery" else "VARCHAR"


def _checked_filter(row_filter: str | None, dialect: str) -> str | None:
    """The filter re-rendered from its parse tree: one boolean expression, no subquery, no comment."""

    if row_filter is None or not row_filter.strip():
        return None
    if len(row_filter) > 2000:
        raise ProfileError("the row filter is longer than 2,000 characters")
    try:
        parsed = sqlglot.parse(row_filter, read=dialect)
    except Exception:  # noqa: BLE001 - sqlglot's parse errors carry SQL text; say only that it did not parse
        raise ProfileError("the row filter is not a SQL condition") from None
    nodes = [node for node in parsed if node is not None]
    if len(nodes) != 1 or isinstance(nodes[0], (exp.Query, exp.DML, exp.DDL, exp.Command, exp.Semicolon)):
        raise ProfileError("the row filter must be one SQL condition such as `status = 'open'`")
    for node in nodes[0].walk():
        if isinstance(node, (exp.Query, exp.DML, exp.DDL, exp.Command, exp.Subquery)):
            raise ProfileError("the row filter cannot contain a subquery")
    return nodes[0].sql(dialect=dialect, comments=False)


def _source(table_sql: str, dialect: str, sample_percent: float | None, row_filter: str | None, columns: str) -> str:
    sample = ""
    if sample_percent is not None:
        sample = (f" TABLESAMPLE SYSTEM ({sample_percent:g} PERCENT)" if dialect == "bigquery"
                  else f" TABLESAMPLE bernoulli ({sample_percent:g} PERCENT)")
    where = f" WHERE ({row_filter})" if row_filter else ""
    return f"SELECT {columns} FROM {table_sql}{sample}{where}"


def _stats_select(i: int, name: str, kind: str, dialect: str, approximate: bool, values: bool) -> list[str]:
    col, p, text = _ident(name, dialect), f"c{i}_", _text_type(dialect)
    parts = [f"COUNT({col}) AS {p}n"]
    if kind == "other":
        return parts
    if dialect == "bigquery" and approximate:
        parts.append(f"APPROX_COUNT_DISTINCT({col}) AS {p}d")
    else:
        parts.append(f"COUNT(DISTINCT {col}) AS {p}d")
    if kind == "numeric":
        # DuckDB refuses STDDEV and quantiles over NaN or infinity; BigQuery returns NaN, which is reported as null.
        finite = col if dialect == "bigquery" else f"CASE WHEN isfinite(CAST({col} AS DOUBLE)) THEN {col} END"
        parts += [f"MIN({col}) AS {p}min", f"MAX({col}) AS {p}max", f"AVG({finite}) AS {p}mean", f"STDDEV({finite}) AS {p}sd"]
        for suffix, step in (("p25", 1), ("med", 2), ("p75", 3)):
            if dialect == "bigquery":
                parts.append(f"APPROX_QUANTILES({col}, 4)[OFFSET({step})] AS {p}{suffix}")
            else:
                parts.append(f"quantile_cont(CAST({finite} AS DOUBLE), {step / 4}) AS {p}{suffix}")
    elif kind == "temporal":
        parts += [f"CAST(MIN({col}) AS {text}) AS {p}min", f"CAST(MAX({col}) AS {text}) AS {p}max"]
    elif kind == "string":
        if values:
            parts += [f"MIN({col}) AS {p}min", f"MAX({col}) AS {p}max"]
        parts += [f"MIN(LENGTH({col})) AS {p}minlen", f"MAX(LENGTH({col})) AS {p}maxlen", f"AVG(LENGTH({col})) AS {p}meanlen"]
    return parts


def _top_sql(chunk: Sequence[tuple[int, str, str]], base: str, dialect: str, limit: int) -> str:
    text = _text_type(dialect)
    branches = []
    for i, name, _kind in chunk:
        col = _ident(name, dialect)
        branches.append(
            f"SELECT {_literal(str(i))} AS kumo_col, kumo_v, kumo_n FROM (SELECT CAST({col} AS {text}) AS kumo_v, "
            f"COUNT(*) AS kumo_n FROM src WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY kumo_n DESC, kumo_v LIMIT {limit})"
        )
    return f"WITH src AS ({base}) " + " UNION ALL ".join(branches)


# --------------------------------------------------------------- executors


class Executor:
    """Runs profile SQL. ``rows`` are dicts keyed by column alias."""

    dialect = "duckdb"

    def table_sql(self, table: str) -> str:
        raise NotImplementedError

    def describe(self, table: str) -> dict[str, str]:
        """``{column: type}`` in table order."""
        raise NotImplementedError

    def run(self, sql: str) -> list[dict]:
        raise NotImplementedError

    @property
    def estimated_bytes(self) -> int | None:
        return None

    @property
    def bytes_billed(self) -> int | None:
        return None


class DuckDBExecutor(Executor):
    dialect = "duckdb"

    def __init__(self, connection) -> None:
        self.connection = connection

    def table_sql(self, table: str) -> str:
        parts = str(table).split(".")
        if not 1 <= len(parts) <= 3 or not all(_PLAIN.match(part) for part in parts):
            raise ProfileError("a DuckDB table is named like `orders` or `main.orders`")
        return ".".join(_ident(part, "duckdb") for part in parts)

    def describe(self, table: str) -> dict[str, str]:
        try:
            rows = self.connection.execute(f"DESCRIBE {self.table_sql(table)}").fetchall()
        except Exception as exc:  # noqa: BLE001 - surface DuckDB's reason
            raise ProfileError(f"could not read the table: {str(exc).splitlines()[0]}") from None
        return {str(row[0]): str(row[1]) for row in rows}

    def run(self, sql: str) -> list[dict]:
        try:
            cursor = self.connection.execute(sql)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise ProfileError(f"DuckDB could not run the profile query: {str(exc).splitlines()[0]}") from None


class BigQueryExecutor(Executor):
    """Profile queries in the billing project, each dry-run first and capped in bytes billed.

    ``project`` is the billing project (default: the one chosen in Settings). Nothing is guessed
    from the table name. ``max_bytes`` defaults to the cap in Settings → Scopes.
    """

    dialect = "bigquery"

    def __init__(self, project: str | None = None, max_bytes: int | None = None) -> None:
        from . import scope_queries

        try:
            self.project = scope_queries._need_project(project)
        except scope_queries.QueryError as exc:
            raise ProfileError(str(exc)) from None
        self.max_bytes = max_bytes if max_bytes is not None else scope_queries.get_settings().max_bytes_billed
        self._estimated = 0
        self._billed = 0
        self._saw_estimate = self._saw_billed = False

    def table_sql(self, table: str) -> str:
        try:
            return quote_table_path(table)
        except ValueError as exc:
            raise ProfileError(str(exc)) from None

    def describe(self, table: str) -> dict[str, str]:
        from . import bigquery_catalog, schema_fetch

        self.table_sql(table)
        project, dataset, name = table.split(".")
        try:
            metadata = bigquery_catalog.get_table(project, dataset, name)
        except Exception as exc:  # noqa: BLE001 - credentials, permissions, missing table
            raise ProfileError(f"could not read the table's schema from BigQuery: {exc}") from None
        columns = schema_fetch.columns_of(metadata)
        if not columns:
            raise ProfileError("BigQuery returned no schema for this table")
        return columns

    def _guard(self, sql: str) -> None:
        try:
            validate_readonly_query(sql)
        except ValueError as exc:
            raise ProfileError(str(exc)) from None

    def estimate(self, sql: str) -> int | None:
        """Dry-run ``sql`` (free) and return its estimated bytes, refusing one over the cap."""

        from . import scope_queries

        self._guard(sql)
        try:
            plan = scope_queries.dry_run(sql, None, project=self.project, max_bytes=self.max_bytes)
        except scope_queries.QueryError as exc:
            if "cap" in str(exc):
                raise ByteCapExceeded(str(exc)) from None
            raise ProfileError(str(exc)) from None
        if plan["estimated_bytes"] is not None:
            self._estimated += plan["estimated_bytes"]
            self._saw_estimate = True
        return plan["estimated_bytes"]

    def run(self, sql: str) -> list[dict]:
        from . import data_sources, scope_queries

        self._guard(sql)
        source = data_sources.Source("profile", "profile", "bigquery_sql", sql)
        try:
            result = data_sources.RUNNERS["bigquery_sql"](source, self.project, self.max_bytes)
        except (scope_queries.QueryError, data_sources.DataSourceError) as exc:
            if "cap" in str(exc):
                raise ByteCapExceeded(str(exc)) from None
            raise ProfileError(str(exc)) from None
        if result.estimated_bytes is not None:
            self._estimated += result.estimated_bytes
            self._saw_estimate = True
        if result.bytes_billed is not None:
            self._billed += result.bytes_billed
            self._saw_billed = True
        return [dict(zip(result.columns, row)) for row in result.rows]

    @property
    def estimated_bytes(self) -> int | None:
        return self._estimated if self._saw_estimate else None

    @property
    def bytes_billed(self) -> int | None:
        return self._billed if self._saw_billed else None


# ----------------------------------------------------------------- values


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _count(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> int | float | None:
    """A number from a driver value or from BigQuery's text; NaN and infinity become ``None``."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else _finite(float(value))
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return _finite(float(value))
            except ValueError:
                return None
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _fraction(part: int, whole: int) -> float | None:
    return round(min(part / whole, 1.0), 6) if whole else None


def _column_profile(name: str, type_name: str, kind: str, i: int, row: dict, rows: int, values: bool) -> ColumnProfile:
    p = f"c{i}_"
    non_null = _count(row.get(p + "n"))
    nulls = max(rows - non_null, 0)
    profile = ColumnProfile(name, type_name, kind, non_null, nulls, _fraction(nulls, rows))
    if kind == "other":
        pass
    else:
        profile.distinct = min(_count(row.get(p + "d")), non_null)
        profile.unique_fraction = _fraction(profile.distinct, non_null)
    if kind == "numeric":
        profile.min, profile.max = _number(row.get(p + "min")), _number(row.get(p + "max"))
        mean, sd = _number(row.get(p + "mean")), _number(row.get(p + "sd"))
        profile.mean = float(mean) if mean is not None else None
        profile.stddev = float(sd) if sd is not None else None
        profile.p25, profile.median, profile.p75 = (_number(row.get(p + s)) for s in ("p25", "med", "p75"))
    elif kind == "temporal":
        profile.min, profile.max = _text(row.get(p + "min")), _text(row.get(p + "max"))
    elif kind == "string":
        if values:
            profile.min, profile.max = _text(row.get(p + "min")), _text(row.get(p + "max"))
        low, high, mean = _number(row.get(p + "minlen")), _number(row.get(p + "maxlen")), _number(row.get(p + "meanlen"))
        profile.min_length = int(low) if low is not None else None
        profile.max_length = int(high) if high is not None else None
        profile.mean_length = float(mean) if mean is not None else None
    if non_null == 0:
        profile.flags.append("all_null")
    elif kind != "other" and profile.distinct == 1:
        profile.flags.append("constant")
    elif kind != "other" and profile.distinct == non_null and non_null > 1:
        profile.flags.append("unique")
    return profile


# ------------------------------------------------------------------ driver


def _plan(table: str, executor: Executor, include: Iterable[str] | None, exclude: Iterable[str],
          max_columns: int) -> tuple[list[tuple[int, str, str, str]], list[dict]]:
    schema = executor.describe(table)
    by_fold = {name.casefold(): name for name in schema}
    names = list(schema)
    if include:
        wanted = []
        for item in include:
            if item.casefold() not in by_fold:
                raise ProfileError(f"the table has no column {item!r}; it has {', '.join(schema)}")
            wanted.append(by_fold[item.casefold()])
        names = list(dict.fromkeys(wanted))
    dropped = {item.casefold() for item in exclude}
    names = [name for name in names if name.casefold() not in dropped]
    skipped = [{"name": name, "reason": "over_column_limit"} for name in names[max_columns:]]
    columns = [(i, name, schema[name], classify(schema[name])) for i, name in enumerate(names[:max_columns])]
    return columns, skipped


def _clean_options(sample_percent, top_values, dialect, row_filter):
    if sample_percent is not None:
        if isinstance(sample_percent, bool) or not isinstance(sample_percent, (int, float)) or not 0 < sample_percent <= 100:
            raise ProfileError("the sample percent must be above 0 and at most 100")
        if sample_percent == 100:
            sample_percent = None
    if isinstance(top_values, bool) or not isinstance(top_values, int) or not 0 <= top_values <= MAX_TOP_VALUES:
        raise ProfileError(f"top values must be a whole number from 0 to {MAX_TOP_VALUES}")
    return sample_percent, _checked_filter(row_filter, dialect)


def profile_queries(table: str, executor: Executor, *, include: Iterable[str] | None = None,
                    exclude: Iterable[str] = (), sample_percent: float | None = None, row_filter: str | None = None,
                    top_values: int = DEFAULT_TOP_VALUES, include_values: bool = True, approximate: bool = True,
                    max_columns: int = DEFAULT_MAX_COLUMNS, chunk_size: int = DEFAULT_CHUNK) -> list[str]:
    """The SQL :func:`profile_table` would send for each chunk (nothing is run), statistics then top values."""

    dialect = executor.dialect
    sample_percent, flt = _clean_options(sample_percent, top_values, dialect, row_filter)
    table_sql = executor.table_sql(table)
    columns, _ = _plan(table, executor, include, exclude, max_columns)
    queries = []
    for chunk in _chunks(columns, chunk_size) or [[]]:
        parts = ["COUNT(*) AS rows_total"]
        for i, name, _type, kind in chunk:
            parts += _stats_select(i, name, kind, dialect, approximate, include_values)
        names = ", ".join(_ident(name, dialect) for _i, name, _t, _k in chunk) or "1 AS x"
        queries.append(f"SELECT {', '.join(parts)} FROM ({_source(table_sql, dialect, sample_percent, flt, names)})")
        if top_values and include_values and chunk:
            wide = [(i, name, kind) for i, name, _t, kind in chunk if kind != "other"]
            if wide:
                base = _source(table_sql, dialect, sample_percent, flt, names)
                queries.append(_top_sql(wide, base, dialect, top_values))
    return queries


def _chunks(columns: list, size: int) -> list[list]:
    size = max(1, size)
    return [columns[start:start + size] for start in range(0, len(columns), size)]


def profile_table(table: str, executor: Executor, *, include: Iterable[str] | None = None,
                  exclude: Iterable[str] = (), sample_percent: float | None = None, row_filter: str | None = None,
                  top_values: int = DEFAULT_TOP_VALUES, include_values: bool = True, approximate: bool = True,
                  max_columns: int = DEFAULT_MAX_COLUMNS, chunk_size: int = DEFAULT_CHUNK,
                  now: Callable[[], datetime] | None = None) -> DataProfile:
    """Profile ``table`` with ``executor``.

    ``sample_percent`` profiles a random share of the rows (BigQuery ``TABLESAMPLE SYSTEM``, which
    reads whole blocks); ``row_filter`` is one SQL condition applied to the rows first. ``approximate``
    lets BigQuery estimate distinct counts (DuckDB always counts exactly). ``top_values`` is how many
    of the most common values each column keeps (0 for none). Raises :class:`ProfileError`, and
    :class:`ByteCapExceeded` when a BigQuery dry run is over the cap.
    """

    dialect = executor.dialect
    sample_percent, flt = _clean_options(sample_percent, top_values, dialect, row_filter)
    table_sql = executor.table_sql(table)
    columns, skipped = _plan(table, executor, include, exclude, max_columns)
    notes: list[str] = []
    profiles: dict[int, ColumnProfile] = {}
    rows_total: int | None = None

    def stats(chunk: list, approx: bool = approximate) -> tuple[dict, int]:
        parts = ["COUNT(*) AS rows_total"]
        for i, name, _type, kind in chunk:
            parts += _stats_select(i, name, kind, dialect, approx, include_values)
        names = ", ".join(_ident(name, dialect) for _i, name, _t, _k in chunk) or "1 AS x"
        result = executor.run(f"SELECT {', '.join(parts)} FROM ({_source(table_sql, dialect, sample_percent, flt, names)})")
        row = result[0] if result else {}
        return row, _count(row.get("rows_total"))

    def tops(chunk: list, _count_unused: int) -> dict[int, list[TopValue]]:
        wide = [(i, name, kind) for i, name, _t, kind in chunk if kind != "other"]
        found: dict[int, list[TopValue]] = {i: [] for i, _n, _k in wide}
        if not wide:
            return found
        names = ", ".join(_ident(name, dialect) for _i, name, _t, _k in chunk)
        for item in executor.run(_top_sql(wide, _source(table_sql, dialect, sample_percent, flt, names), dialect, top_values)):
            i = _count(item.get("kumo_col"))
            if i in found and item.get("kumo_v") is not None:
                found[i].append(TopValue(_text(item["kumo_v"]) or "", _count(item.get("kumo_n")), None))
        return found

    if not columns:
        _row, rows_total = stats([])
    for chunk in _chunks(columns, chunk_size):
        done = []
        try:
            row, count = stats(chunk)
            done.append((chunk, row, count))
        except ByteCapExceeded:
            raise
        except ProfileError:
            # One column BigQuery cannot aggregate this way must not cost the others; try each alone, and a
            # column whose approximate distinct count is refused once more with an exact one.
            for one in ([c] for c in chunk):
                try:
                    try:
                        row, count = stats(one)
                    except ProfileError:
                        if not (dialect == "bigquery" and approximate):
                            raise
                        row, count = stats(one, False)
                    done.append((one, row, count))
                except ByteCapExceeded:
                    raise
                except ProfileError as inner:
                    skipped.append({"name": one[0][1], "reason": "error", "error": str(inner)[:300]})
        for part, row, count in done:
            if rows_total is None:
                rows_total = count
            for i, name, type_name, kind in part:
                profiles[i] = _column_profile(name, type_name, kind, i, row, count, include_values)
            if top_values and include_values:
                try:
                    found = tops(part, count)
                except ByteCapExceeded:
                    raise
                except ProfileError as exc:
                    notes.append(f"most common values were skipped for {len(part)} column(s): {str(exc)[:200]}")
                    continue
                for i, items in found.items():
                    profile = profiles[i]
                    for item in items:
                        item.fraction = _fraction(item.count, profile.non_null)
                    # A column whose values are all different has no "most common" one; listing ten is noise.
                    profile.top_values = [] if "unique" in profile.flags else items
    if rows_total is None:  # every column failed: the row count is still worth having
        _row, rows_total = stats([])
    if sample_percent is not None:
        notes.append("sampled: counts describe the sample, not the whole table; most common value fractions are approximate")
    if dialect == "bigquery" and approximate:
        notes.append("distinct counts are approximate (APPROX_COUNT_DISTINCT) and quartiles use APPROX_QUANTILES")
    stamp = (now or (lambda: datetime.now(timezone.utc)))()
    return DataProfile(
        table=table, dialect=dialect, generated_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), row_count=rows_total,
        columns=[profiles[i] for i in sorted(profiles)], skipped=skipped, notes=notes,
        sample_percent=sample_percent, row_filter=flt, approximate_distinct=dialect == "bigquery" and approximate,
        estimated_bytes=executor.estimated_bytes, bytes_billed=executor.bytes_billed,
    )
