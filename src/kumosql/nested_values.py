"""ARRAY and STRUCT values and types for generated databases.

The data generators (:mod:`kumosql.result_equivalence`, :mod:`kumosql.targeted_data`) and the
DuckDB runs that refute queries share one model of nested values:

* NULL of any type is ``None``;
* an ARRAY is :class:`Array`, a tagged tuple of elements in order;
* a STRUCT is :class:`Struct`, its ``(name, value)`` fields in order. Two structs are equal when
  their values are equal position by position, as in GoogleSQL (field names are part of the type,
  not of the value).

Tagged values never equal a plain tuple, a row or each other's kind, so an ARRAY cannot pass for
a STRUCT with the same contents.

BigQuery storage rules that generated tables follow (so a counterexample is a database BigQuery
can hold): an ARRAY column is never NULL (BigQuery stores a NULL array as an empty one) and never
holds a NULL element; arrays never hold arrays directly; a STRUCT column and every field may be
NULL unless declared REQUIRED.

Types are read with sqlglot's BigQuery type parser into :class:`NestedType`, a small stand-in
for :class:`kumosql.googlesql_types.GType` kept behind :func:`parse_type`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
from typing import Any, Callable, Iterator

import sqlglot
from sqlglot import exp

_SCALARS = {
    exp.DataType.Type.BIGINT: "INT64",
    exp.DataType.Type.INT: "INT64",
    exp.DataType.Type.SMALLINT: "INT64",
    exp.DataType.Type.TINYINT: "INT64",
    exp.DataType.Type.DOUBLE: "FLOAT64",
    exp.DataType.Type.FLOAT: "FLOAT64",
    exp.DataType.Type.DECIMAL: "NUMERIC",
    exp.DataType.Type.TEXT: "STRING",
    exp.DataType.Type.VARCHAR: "STRING",
    exp.DataType.Type.BOOLEAN: "BOOL",
    exp.DataType.Type.DATE: "DATE",
    exp.DataType.Type.DATETIME: "DATETIME",
    exp.DataType.Type.TIMESTAMP: "TIMESTAMP",
    exp.DataType.Type.TIMESTAMPTZ: "TIMESTAMP",
}
_DUCKDB_SCALARS = {
    "INT64": "BIGINT",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL(38, 9)",
    "STRING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
}


@dataclass(frozen=True)
class NestedType:
    """A BigQuery type: a scalar name (``INT64``, ``STRING``...), ``ARRAY`` or ``STRUCT``."""

    kind: str
    element: "NestedType | None" = None
    fields: tuple[tuple[str | None, "NestedType"], ...] = ()

    @property
    def nested(self) -> bool:
        return self.kind in ("ARRAY", "STRUCT")

    def sql(self) -> str:
        """GoogleSQL type text, e.g. ``ARRAY<STRUCT<key STRING, value INT64>>``."""

        if self.kind == "ARRAY":
            return f"ARRAY<{self.element.sql()}>"
        if self.kind == "STRUCT":
            return "STRUCT<" + ", ".join(f"{name} {t.sql()}" if name else t.sql() for name, t in self.fields) + ">"
        return self.kind

    def duckdb(self) -> str:
        """The DuckDB type: ``LIST`` and ``STRUCT`` (an unnamed field is named by position)."""

        if self.kind == "ARRAY":
            return f"{self.element.duckdb()}[]"
        if self.kind == "STRUCT":
            parts = [f'"{name or f"_field_{i + 1}"}" {t.duckdb()}' for i, (name, t) in enumerate(self.fields)]
            return "STRUCT(" + ", ".join(parts) + ")"
        return _DUCKDB_SCALARS[self.kind]

    def field(self, name: str) -> "NestedType | None":
        """The type of field ``name`` (case-insensitive); ``None`` when absent or ambiguous."""

        hits = [t for n, t in self.fields if n is not None and n.lower() == name.lower()]
        return hits[0] if len(hits) == 1 else None

    def walk(self, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], "NestedType"]]:
        """Every named field below this type as ``(path, type)``; array elements keep the array's path."""

        if self.kind == "ARRAY":
            yield from self.element.walk(path)
        elif self.kind == "STRUCT":
            for name, t in self.fields:
                if name:
                    yield path + (name,), t
                    yield from t.walk(path + (name,))


def _from_datatype(node: exp.DataType) -> NestedType | None:
    if node.this == exp.DataType.Type.ARRAY:
        if len(node.expressions) != 1:
            return None
        element = _from_datatype(node.expressions[0])
        if element is None or element.kind == "ARRAY":  # BigQuery has no array of arrays
            return None
        return NestedType("ARRAY", element=element)
    if node.this == exp.DataType.Type.STRUCT:
        fields = []
        for item in node.expressions:
            if isinstance(item, exp.ColumnDef):
                kind = item.args.get("kind")
                inner = _from_datatype(kind) if isinstance(kind, exp.DataType) else None
                fields.append((item.name, inner))
            elif isinstance(item, exp.DataType):
                fields.append((None, _from_datatype(item)))
            else:
                return None
        if not fields or any(t is None for _, t in fields):
            return None
        named = [n.lower() for n, _ in fields if n]
        if len(named) != len(set(named)):
            return None
        return NestedType("STRUCT", fields=tuple(fields))
    scalar = _SCALARS.get(node.this)
    if scalar is None:
        return None
    if scalar in ("NUMERIC", "STRING") and node.expressions:
        return None  # NUMERIC(p, s) and STRING(n) are parameterized: not generated
    return NestedType(scalar)


_PARSED: dict[str, NestedType | None] = {}


def parse_type(text: str) -> NestedType | None:
    """A BigQuery type from its text (``INTEGER``, ``ARRAY<STRUCT<a INT64>>``...); ``None`` when unreadable."""

    key = text.strip()
    if key not in _PARSED:
        try:
            node = exp.DataType.build(key, dialect="bigquery")
        except (sqlglot.errors.SqlglotError, ValueError):
            node = None
        _PARSED[key] = _from_datatype(node) if isinstance(node, exp.DataType) else None
    return _PARSED[key]


def is_nested(text: str) -> bool:
    """Whether ``text`` names an ARRAY or STRUCT type."""

    parsed = parse_type(text) if text else None
    return parsed is not None and parsed.nested


# --- values -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Array:
    """An ARRAY value: its elements in order."""

    elements: tuple = ()

    def __iter__(self):
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)

    def __repr__(self) -> str:
        return "[" + ", ".join(repr(v) for v in self.elements) + "]"


@dataclass(frozen=True, eq=False)
class Struct:
    """A STRUCT value: ``(name, value)`` fields in order; equal by position, as in GoogleSQL."""

    fields: tuple[tuple[str | None, Any], ...] = ()

    def values(self) -> tuple:
        return tuple(v for _, v in self.fields)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Struct) and self.values() == other.values()

    def __hash__(self) -> int:
        return hash(("STRUCT", self.values()))

    def __repr__(self) -> str:
        return "STRUCT(" + ", ".join(f"{v!r} AS {n}" if n else repr(v) for n, v in self.fields) + ")"


def build_value(
    t: NestedType,
    scalar: Callable[[NestedType, tuple[str, ...]], Any],
    lengths: Callable[[tuple[str, ...]], int],
    null_field: Callable[[tuple[str, ...]], bool],
    path: tuple[str, ...] = (),
) -> Any:
    """A value of type ``t`` that BigQuery can store: ``scalar(type, path)`` draws a scalar for a field path,
    ``lengths(path)`` sizes an array and ``null_field(path)`` decides whether a struct field is NULL.
    Array elements are never NULL; a struct inside an array is never NULL itself."""

    if t.kind == "ARRAY":
        out = []
        for _ in range(lengths(path)):
            value = build_value(t.element, scalar, lengths, null_field, path)
            if value is not None:
                out.append(value)
        return Array(tuple(out))
    if t.kind == "STRUCT":
        fields = []
        for index, (name, inner) in enumerate(t.fields):
            sub = path + ((name or f"_field_{index + 1}"),)
            fields.append((name, None if null_field(sub) else build_value(inner, scalar, lengths, null_field, sub)))
        return Struct(tuple(fields))
    return scalar(t, path)


def storable(value: Any, t: NestedType) -> bool:
    """Whether BigQuery can store ``value`` as type ``t`` (no NULL array, no NULL array element)."""

    if t.kind == "ARRAY":
        return isinstance(value, Array) and all(v is not None and storable(v, t.element) for v in value)
    if value is None:
        return True
    if t.kind == "STRUCT":
        return isinstance(value, Struct) and len(value.fields) == len(t.fields) and all(
            storable(v, inner) for v, (_, inner) in zip(value.values(), t.fields)
        )
    return True


# --- literals -----------------------------------------------------------------------------


def _scalar_literal(value: Any, kind: str, dialect: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot write {value!r} as a literal")
        return f"CAST('{value!r}' AS {'FLOAT64' if dialect == 'bigquery' else 'DOUBLE'})"
    if isinstance(value, Decimal):
        return f"NUMERIC '{value}'" if dialect == "bigquery" else f"CAST('{value}' AS DECIMAL(38, 9))"
    if isinstance(value, datetime):
        name = "DATETIME" if dialect == "bigquery" and kind == "DATETIME" else "TIMESTAMP"
        return f"{name} '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        if dialect == "bigquery":
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"cannot write {value!r} as a literal")


def literal(value: Any, t: NestedType, dialect: str = "duckdb") -> str:
    """``value`` as a typed SQL literal of type ``t`` in ``dialect`` (``duckdb`` or ``bigquery``)."""

    if value is None:
        return f"CAST(NULL AS {t.sql() if dialect == 'bigquery' else t.duckdb()})"
    return f"CAST({_untyped(value, t, dialect)} AS {t.sql() if dialect == 'bigquery' else t.duckdb()})"


def _untyped(value: Any, t: NestedType, dialect: str) -> str:
    # BigQuery types a bare NULL or [] inside a constructor as INT64, which no cast then turns into the column's type
    if value is None:
        return "NULL" if dialect == "duckdb" else f"CAST(NULL AS {t.sql()})"
    if t.kind == "ARRAY":
        if not isinstance(value, Array):
            raise ValueError(f"not an ARRAY value: {value!r}")
        if not value and dialect == "bigquery":
            return f"{t.sql()}[]"
        return "[" + ", ".join(_untyped(v, t.element, dialect) for v in value) + "]"
    if t.kind == "STRUCT":
        if not isinstance(value, Struct) or len(value.fields) != len(t.fields):
            raise ValueError(f"not a STRUCT value of {t.sql()}: {value!r}")
        parts = []
        for index, (v, (name, inner)) in enumerate(zip(value.values(), t.fields)):
            text = _untyped(v, inner, dialect)
            if dialect == "bigquery":
                parts.append(f"{text} AS {name}" if name else text)
            else:
                parts.append(f"'{name or f'_field_{index + 1}'}': {text}")
        return ("STRUCT(" + ", ".join(parts) + ")") if dialect == "bigquery" else ("{" + ", ".join(parts) + "}")
    return _scalar_literal(value, t.kind, dialect)


def from_python(value: Any, t: NestedType) -> Any:
    """A DuckDB result value or a JSON value (list, dict) of type ``t`` as a tagged value.

    A dict that names every field is read by name, any other dict or sequence by position.
    """

    if value is None:
        return None
    if t.kind == "ARRAY":
        return Array(tuple(from_python(v, t.element) for v in value))
    if t.kind == "STRUCT":
        if isinstance(value, dict):
            by_name = {str(k).lower(): v for k, v in value.items()}
            named = all(name and name.lower() in by_name for name, _ in t.fields) and len(by_name) == len(t.fields)
            items = [by_name[name.lower()] for name, _ in t.fields] if named else list(value.values())
        else:
            items = list(value)
        return Struct(tuple((name, from_python(v, inner)) for (name, inner), v in zip(t.fields, items)))
    return value


def to_json(value: Any) -> Any:
    """A JSON-ready form of a generated value: arrays as lists, structs as ``{name: value}``."""

    if isinstance(value, Array):
        return [to_json(v) for v in value]
    if isinstance(value, Struct):
        return {(name or f"_field_{i + 1}"): to_json(v) for i, (name, v) in enumerate(value.fields)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


__all__ = [
    "Array",
    "NestedType",
    "Struct",
    "build_value",
    "from_python",
    "is_nested",
    "literal",
    "parse_type",
    "storable",
    "to_json",
]
