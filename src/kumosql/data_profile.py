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
    "Node", "ProfileError", "TopValue", "classify", "profile_table", "profile_queries",
]

VERSION = 1
DEFAULT_TOP_VALUES = 10
MAX_TOP_VALUES = 50
DEFAULT_MAX_COLUMNS = 500
DEFAULT_CHUNK = 20
MAX_TEXT = 200
KINDS = ("numeric", "string", "boolean", "temporal", "array", "other")
MAX_DEPTH = 6


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
    unit: str = "rows"  # "elements" for a field inside an array: counts are over the array elements

    def to_json(self) -> dict:
        data = {name: getattr(self, name) for name in (
            "name", "type", "kind", "non_null", "null_count", "null_fraction", "distinct", "unique_fraction",
            "min", "max", "mean", "stddev", "p25", "median", "p75", "min_length", "max_length", "mean_length", "unit")}
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


@dataclass(frozen=True)
class Node:
    """A column or a field inside a STRUCT: its type, whether it is repeated (an array) and, for a STRUCT, its fields."""

    name: str
    type: str
    repeated: bool = False
    children: tuple["Node", ...] | None = None


@dataclass(frozen=True)
class Item:
    """One thing to profile: a column, a STRUCT field (``address.city``) or array elements (``tags[]``).

    ``expr`` is its SQL in terms of the table and of the ``e1``, ``e2`` names that ``unnests`` introduce, in order,
    one ``UNNEST`` per array between the table and the item."""

    index: int
    name: str
    type: str
    kind: str
    expr: str
    unnests: tuple[str, ...] = ()

    @property
    def alias(self) -> str:
        return f"k{self.index}"


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


def _field(expr: str, name: str, dialect: str) -> str:
    if dialect == "bigquery":
        return f"{expr}.{_ident(name, dialect)}"
    return f"struct_extract({expr}, {_literal(name)})"


def _source(table_sql: str, dialect: str, sample_percent: float | None, row_filter: str | None,
            items: Sequence[Item]) -> str:
    """``SELECT <each item as its alias> FROM table [sample] [CROSS JOIN UNNEST ...] [WHERE filter]``."""

    sample = ""
    if sample_percent is not None:
        sample = (f" TABLESAMPLE SYSTEM ({sample_percent:g} PERCENT)" if dialect == "bigquery"
                  else f" TABLESAMPLE bernoulli ({sample_percent:g} PERCENT)")
    unnests = items[0].unnests if items else ()
    joins = "".join(
        f" CROSS JOIN UNNEST({expr}) AS e{n}" if dialect == "bigquery" else f" CROSS JOIN UNNEST({expr}) AS u{n}(e{n})"
        for n, expr in enumerate(unnests, 1))
    where = f" WHERE ({row_filter})" if row_filter else ""
    columns = ", ".join(f"{item.expr} AS {item.alias}" for item in items) or "1 AS x"
    return f"SELECT {columns} FROM {table_sql}{sample}{joins}{where}"


def _length(col: str, dialect: str) -> str:
    return f"COALESCE(ARRAY_LENGTH({col}), 0)" if dialect == "bigquery" else f"COALESCE(len({col}), 0)"


def _stats_select(item: Item, dialect: str, approximate: bool, values: bool) -> list[str]:
    col, p, text, kind = item.alias, f"c{item.index}_", _text_type(dialect), item.kind
    if kind == "array":  # present when it has elements; an empty array and a NULL one both count as missing
        size = _length(col, dialect)
        return [f"COUNT(CASE WHEN {size} > 0 THEN 1 END) AS {p}n", f"MIN({size}) AS {p}minlen",
                f"MAX({size}) AS {p}maxlen", f"AVG({size}) AS {p}meanlen"]
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


def _top_sql(chunk: Sequence[Item], base: str, dialect: str, limit: int) -> str:
    text = _text_type(dialect)
    branches = []
    for item in chunk:
        col = item.alias
        branches.append(
            f"SELECT {_literal(str(item.index))} AS kumo_col, kumo_v, kumo_n FROM (SELECT CAST({col} AS {text}) AS kumo_v, "
            f"COUNT(*) AS kumo_n FROM src WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY kumo_n DESC, kumo_v LIMIT {limit})"
        )
    return f"WITH src AS ({base}) " + " UNION ALL ".join(branches)


_BQ_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}


def _bigquery_nodes(fields: Sequence[object]) -> list[Node]:
    nodes = []
    for item in fields:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        kind = str(item.get("type") or "").upper()
        kind = _BQ_TYPES.get(kind, kind)
        children = tuple(_bigquery_nodes(item.get("fields") or [])) if kind == "STRUCT" else None
        nodes.append(Node(str(item["name"]), kind, str(item.get("mode") or "").upper() == "REPEATED", children))
    return nodes


def _duckdb_node(name: str, type_text: str) -> Node:
    """A column from DuckDB's type text: ``STRUCT(a INTEGER, b VARCHAR[])[]`` becomes a repeated STRUCT node.

    Fixed-size arrays, arrays of arrays, maps and unions are left as one value of that type."""

    try:
        parsed = sqlglot.exp.DataType.build(type_text, dialect="duckdb")
    except Exception:  # noqa: BLE001 - a type sqlglot cannot read is still a column
        return Node(name, type_text)
    return _duckdb_shape(name, parsed, type_text)


def _duckdb_shape(name: str, parsed: exp.DataType, type_text: str) -> Node:
    if parsed.this == exp.DataType.Type.STRUCT:
        fields = [item for item in parsed.expressions if isinstance(item, exp.ColumnDef) and isinstance(item.args.get("kind"), exp.DataType)]
        if len(fields) == len(parsed.expressions):
            return Node(name, "STRUCT", False, tuple(_duckdb_shape(f.name, f.args["kind"], f.args["kind"].sql("duckdb")) for f in fields))
    if (parsed.this == exp.DataType.Type.ARRAY and len(parsed.expressions) == 1 and isinstance(parsed.expressions[0], exp.DataType)
            and not re.search(r"\[\d+\]$", type_text)):
        inner = _duckdb_shape(name, parsed.expressions[0], parsed.expressions[0].sql("duckdb"))
        if not inner.repeated:
            return Node(name, inner.type, True, inner.children)
    return Node(name, type_text if parsed.this != exp.DataType.Type.STRUCT else "STRUCT")


# --------------------------------------------------------------- executors


class Executor:
    """Runs profile SQL. ``rows`` are dicts keyed by column alias."""

    dialect = "duckdb"

    def table_sql(self, table: str) -> str:
        raise NotImplementedError

    def fields(self, table: str) -> list[Node]:
        """The table's columns in order, STRUCT fields nested."""
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

    def fields(self, table: str) -> list[Node]:
        try:
            rows = self.connection.execute(f"DESCRIBE {self.table_sql(table)}").fetchall()
        except Exception as exc:  # noqa: BLE001 - surface DuckDB's reason
            raise ProfileError(f"could not read the table: {str(exc).splitlines()[0]}") from None
        return [_duckdb_node(str(row[0]), str(row[1])) for row in rows]

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

    def fields(self, table: str) -> list[Node]:
        from . import bigquery_catalog

        self.table_sql(table)
        project, dataset, name = table.split(".")
        try:
            metadata = bigquery_catalog.get_table(project, dataset, name)
        except Exception as exc:  # noqa: BLE001 - credentials, permissions, missing table
            raise ProfileError(f"could not read the table's schema from BigQuery: {exc}") from None
        nodes = _bigquery_nodes(metadata.get("schema") or [])
        if not nodes:
            raise ProfileError("BigQuery returned no schema for this table")
        return nodes

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


def _column_profile(item: Item, row: dict, rows: int, values: bool) -> ColumnProfile:
    p, kind = f"c{item.index}_", item.kind
    non_null = _count(row.get(p + "n"))
    nulls = max(rows - non_null, 0)
    profile = ColumnProfile(item.name, item.type, kind, non_null, nulls, _fraction(nulls, rows),
                            unit="elements" if item.unnests else "rows")
    if kind not in ("other", "array"):
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
    elif kind in ("string", "array"):
        if kind == "string" and values:
            profile.min, profile.max = _text(row.get(p + "min")), _text(row.get(p + "max"))
        low, high, mean = _number(row.get(p + "minlen")), _number(row.get(p + "maxlen")), _number(row.get(p + "meanlen"))
        profile.min_length = int(low) if low is not None else None  # for an array: its number of elements
        profile.max_length = int(high) if high is not None else None
        profile.mean_length = float(mean) if mean is not None else None
    if non_null == 0:
        profile.flags.append("all_null")
    elif kind not in ("other", "array") and profile.distinct == 1:
        profile.flags.append("constant")
    elif kind not in ("other", "array") and profile.distinct == non_null and non_null > 1:
        profile.flags.append("unique")
    return profile


# ------------------------------------------------------------------ driver


def _expand(nodes: Sequence[Node], dialect: str, max_items: int) -> tuple[list[Item], list[dict]]:
    """Every column, STRUCT field and array's elements as an :class:`Item`, in table order."""

    items: list[Item] = []
    skipped: list[dict] = []

    def add(name, type_name, kind, expr, unnests):
        items.append(Item(len(items), name, type_name, kind, expr, unnests))

    def walk(node: Node, expr: str, display: str, unnests: tuple[str, ...], depth: int) -> None:
        if len(items) >= max_items:
            skipped.append({"name": display, "reason": "over_column_limit"})
            return
        if node.repeated:
            add(display, f"ARRAY<{node.type}>", "array", expr, unnests)
            if depth >= MAX_DEPTH:
                skipped.append({"name": display + "[]", "reason": "too_deeply_nested"})
                return
            element = Node(node.name, node.type, False, node.children)
            walk(element, f"e{len(unnests) + 1}", display + "[]", (*unnests, expr), depth + 1)
        elif node.children is not None:
            add(display, "STRUCT", "other", expr, unnests)
            if depth >= MAX_DEPTH and node.children:
                skipped.append({"name": display + ".*", "reason": "too_deeply_nested"})
                return
            for child in node.children:
                walk(child, _field(expr, child.name, dialect), f"{display}.{child.name}", unnests, depth + 1)
        else:
            add(display, node.type, classify(node.type), expr, unnests)

    for node in nodes:
        walk(node, _ident(node.name, dialect), node.name, (), 0)
    return items, skipped


def _plan(table: str, executor: Executor, include: Iterable[str] | None, exclude: Iterable[str],
          max_columns: int) -> tuple[list[Item], list[dict]]:
    """The items to profile. ``include`` and ``exclude`` name top-level columns; a STRUCT brings its fields."""

    nodes = executor.fields(table)
    by_fold = {node.name.casefold(): node for node in nodes}
    chosen = nodes
    if include:
        wanted = []
        for name in include:
            if name.casefold() not in by_fold:
                raise ProfileError(f"the table has no column {name!r}; it has {', '.join(node.name for node in nodes)}")
            wanted.append(by_fold[name.casefold()])
        chosen = list(dict.fromkeys(wanted))
    dropped = {name.casefold() for name in exclude}
    chosen = [node for node in chosen if node.name.casefold() not in dropped]
    return _expand(chosen, executor.dialect, max_columns)


def _clean_options(sample_percent, top_values, dialect, row_filter):
    if sample_percent is not None:
        if isinstance(sample_percent, bool) or not isinstance(sample_percent, (int, float)) or not 0 < sample_percent <= 100:
            raise ProfileError("the sample percent must be above 0 and at most 100")
        if sample_percent == 100:
            sample_percent = None
    if isinstance(top_values, bool) or not isinstance(top_values, int) or not 0 <= top_values <= MAX_TOP_VALUES:
        raise ProfileError(f"top values must be a whole number from 0 to {MAX_TOP_VALUES}")
    return sample_percent, _checked_filter(row_filter, dialect)


def _groups(items: Sequence[Item], size: int) -> list[list[Item]]:
    """Chunks of at most ``size`` items that share one set of ``UNNEST``s, so each chunk is a single query."""

    size = max(1, size)
    chunks: list[list[Item]] = []
    for key in dict.fromkeys(item.unnests for item in items):
        same = [item for item in items if item.unnests == key]
        chunks += [same[start:start + size] for start in range(0, len(same), size)]
    return chunks


def _stats_sql(chunk: Sequence[Item], table_sql: str, dialect: str, sample_percent, flt, approximate: bool, values: bool) -> str:
    parts = ["COUNT(*) AS rows_total"]
    for item in chunk:
        parts += _stats_select(item, dialect, approximate, values)
    return f"SELECT {', '.join(parts)} FROM ({_source(table_sql, dialect, sample_percent, flt, chunk)})"


def _top_items(chunk: Sequence[Item]) -> list[Item]:
    return [item for item in chunk if item.kind not in ("other", "array")]


def profile_queries(table: str, executor: Executor, *, include: Iterable[str] | None = None,
                    exclude: Iterable[str] = (), sample_percent: float | None = None, row_filter: str | None = None,
                    top_values: int = DEFAULT_TOP_VALUES, include_values: bool = True, approximate: bool = True,
                    max_columns: int = DEFAULT_MAX_COLUMNS, chunk_size: int = DEFAULT_CHUNK) -> list[str]:
    """The SQL :func:`profile_table` would send for each chunk (nothing is run), statistics then top values."""

    dialect = executor.dialect
    sample_percent, flt = _clean_options(sample_percent, top_values, dialect, row_filter)
    table_sql = executor.table_sql(table)
    items, _ = _plan(table, executor, include, exclude, max_columns)
    queries = []
    for chunk in _groups(items, chunk_size) or [[]]:
        queries.append(_stats_sql(chunk, table_sql, dialect, sample_percent, flt, approximate, include_values))
        wide = _top_items(chunk)
        if top_values and include_values and wide:
            queries.append(_top_sql(wide, _source(table_sql, dialect, sample_percent, flt, chunk), dialect, top_values))
    return queries


def profile_table(table: str, executor: Executor, *, include: Iterable[str] | None = None,
                  exclude: Iterable[str] = (), sample_percent: float | None = None, row_filter: str | None = None,
                  top_values: int = DEFAULT_TOP_VALUES, include_values: bool = True, approximate: bool = True,
                  max_columns: int = DEFAULT_MAX_COLUMNS, chunk_size: int = DEFAULT_CHUNK,
                  now: Callable[[], datetime] | None = None) -> DataProfile:
    """Profile ``table`` with ``executor``.

    A STRUCT column is followed by one entry per field (``address.city``). An array column is reported with
    its number of elements, followed by its elements (``tags[]``, ``items[].sku``), whose counts are over
    elements, not rows. ``include`` and ``exclude`` name top-level columns.

    ``sample_percent`` profiles a random share of the rows (BigQuery ``TABLESAMPLE SYSTEM``, which
    reads whole blocks); ``row_filter`` is one SQL condition applied to the rows first. ``approximate``
    lets BigQuery estimate distinct counts (DuckDB always counts exactly). ``top_values`` is how many
    of the most common values each column keeps (0 for none). Raises :class:`ProfileError`, and
    :class:`ByteCapExceeded` when a BigQuery dry run is over the cap.
    """

    dialect = executor.dialect
    sample_percent, flt = _clean_options(sample_percent, top_values, dialect, row_filter)
    table_sql = executor.table_sql(table)
    items, skipped = _plan(table, executor, include, exclude, max_columns)
    notes: list[str] = []
    profiles: dict[int, ColumnProfile] = {}
    rows_total: int | None = None

    def stats(chunk: list[Item], approx: bool = approximate) -> tuple[dict, int]:
        result = executor.run(_stats_sql(chunk, table_sql, dialect, sample_percent, flt, approx, include_values))
        row = result[0] if result else {}
        return row, _count(row.get("rows_total"))

    def tops(chunk: list[Item]) -> dict[int, list[TopValue]]:
        wide = _top_items(chunk)
        found: dict[int, list[TopValue]] = {item.index: [] for item in wide}
        if not wide:
            return found
        sql = _top_sql(wide, _source(table_sql, dialect, sample_percent, flt, chunk), dialect, top_values)
        for item in executor.run(sql):
            i = _count(item.get("kumo_col"))
            if i in found and item.get("kumo_v") is not None:
                found[i].append(TopValue(_text(item["kumo_v"]) or "", _count(item.get("kumo_n")), None))
        return found

    if not items:
        _row, rows_total = stats([])
    for chunk in _groups(items, chunk_size):
        done = []
        try:
            row, count = stats(chunk)
            done.append((chunk, row, count))
        except ByteCapExceeded:
            raise
        except ProfileError:
            # One column BigQuery cannot aggregate this way must not cost the others; try each alone, and a
            # column whose approximate distinct count is refused once more with an exact one.
            for one in ([item] for item in chunk):
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
                    skipped.append({"name": one[0].name, "reason": "error", "error": str(inner)[:300]})
        for part, row, count in done:
            if rows_total is None and not part[0].unnests:
                rows_total = count
            for item in part:
                profiles[item.index] = _column_profile(item, row, count, include_values)
            if top_values and include_values:
                try:
                    found = tops(part)
                except ByteCapExceeded:
                    raise
                except ProfileError as exc:
                    notes.append(f"most common values were skipped for {len(part)} column(s): {str(exc)[:200]}")
                    continue
                for i, values in found.items():
                    profile = profiles[i]
                    for value in values:
                        value.fraction = _fraction(value.count, profile.non_null)
                    # A column whose values are all different has no "most common" one; listing ten is noise.
                    profile.top_values = [] if "unique" in profile.flags else values
    if rows_total is None:  # no top-level column was profiled: the row count is still worth having
        _row, rows_total = stats([])
    if any(item.unnests for item in items):
        notes.append("fields inside arrays are profiled over the array elements: their counts are elements, not rows")
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
