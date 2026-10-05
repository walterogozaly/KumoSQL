"""A conservative GoogleSQL type checker: the output schema of a BigQuery query, STRUCT and ARRAY included.

``infer(sql, catalog)`` resolves every name in a query against a catalog of table schemas and gives:

* ``columns``: the query's output columns, each a name (``None`` when anonymous) and a :class:`GType` (``None`` when
  not known); ``columns`` is ``None`` when even the width is unknown (``SELECT *`` over a table with no schema);
* ``type_of(node)``: the type of any expression node of the typed tree, kept outside the AST (keyed by node identity);
* ``relation(node)``: the columns of a ``FROM`` item (table, subquery, ``UNNEST``, CTE reference);
* ``findings``: errors the query certainly has (an unknown or ambiguous column, a set operation whose branches differ
  in width or have no common type, a field access on a value with no such field, operands no signature accepts).

The rule is that anything uncertain is unknown. A type is given only when GoogleSQL's rules fix it: the supertype
and literal-coercion rules of ``docs/conversion_rules.md`` in google/googlesql, and the function signatures in
:mod:`kumosql.googlesql_signatures`. A query that is invalid has no type to be wrong about, so expression types are
given as if the query were valid; findings report invalidity only where it is certain (a column missing from a table
whose full schema is known, never from a table the catalog does not know).

Names follow GoogleSQL: a column reference or path takes the last identifier as its implicit alias, a range variable
on its own is a STRUCT of its table's columns (or the row value of a value table such as ``UNNEST``), and range
variables are looked up before columns. ``UNNEST`` of an array of structs makes the struct's fields columns.

The catalog takes BigQuery schemas (``dryrun.Field``, ``schema_fetch`` column maps) and Dataform declarations; upstream
models can be added with their inferred columns, in dependency order. The tree passed in is never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

# --- types --------------------------------------------------------------------------------------------------------

SCALAR_KINDS = (
    "INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC", "BOOL", "STRING", "BYTES", "DATE", "DATETIME", "TIME", "TIMESTAMP",
    "INTERVAL", "JSON", "GEOGRAPHY",
)
# GoogleSQL types BigQuery does not have; they pass through (a column of the type keeps it) but are rarely computed with.
GOOGLESQL_ONLY_KINDS = ("INT32", "UINT32", "UINT64", "FLOAT32", "UUID")
_PARAMETERIZED = ("ARRAY", "STRUCT", "RANGE")
_NUMERIC_ORDER = ("INT32", "UINT32", "INT64", "UINT64", "NUMERIC", "BIGNUMERIC", "FLOAT32", "FLOAT64")
_EXACT = {"INT32", "UINT32", "INT64", "UINT64", "NUMERIC", "BIGNUMERIC"}
# The supertypes of each numeric type (docs/conversion_rules.md, "Supertypes"), FLOAT (FLOAT32) left out where the
# documented examples disagree with the table (INT64 and FLOAT have the supertype DOUBLE).
_NUMERIC_SUPERTYPES = {
    "INT32": {"INT32", "INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"},
    "INT64": {"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"},
    "UINT32": {"UINT32", "INT64", "UINT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"},
    "UINT64": {"UINT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"},
    "FLOAT32": {"FLOAT32", "FLOAT64"},
    "FLOAT64": {"FLOAT64"},
    "NUMERIC": {"NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "BIGNUMERIC": {"BIGNUMERIC", "FLOAT64"},
}
# Implicit coercion of any expression of a type (docs/conversion_rules.md, "Coerce to").
_COERCE_TO = {
    "INT32": {"INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "INT64": {"NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "UINT32": {"INT64", "UINT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "UINT64": {"NUMERIC", "BIGNUMERIC", "FLOAT64"},
    "NUMERIC": {"BIGNUMERIC", "FLOAT64"},
    "BIGNUMERIC": {"FLOAT64"},
    "FLOAT32": {"FLOAT64"},
    "DATE": {"DATETIME"},
}
# Literal coercion, beyond what the literal's own type coerces to ("Literal coercion").
_LITERAL_COERCE_TO = {
    "int": {"INT32", "UINT32", "UINT64"},
    "float": {"NUMERIC", "BIGNUMERIC", "FLOAT32"},
    "string": {"DATE", "DATETIME", "TIME", "TIMESTAMP"},
    "bytes": set(),
}
NUMERIC_KINDS = frozenset(_NUMERIC_ORDER)


@dataclass(frozen=True)
class StructField:
    name: str | None
    type: "GType | None"


@dataclass(frozen=True)
class GType:
    """A GoogleSQL type. ``element`` is the ARRAY or RANGE element, ``fields`` the STRUCT fields; ``None`` parts are
    unknown. STRUCT field names are part of the type, as in GoogleSQL, and keep the case they were written in."""

    kind: str
    element: "GType | None" = None
    fields: tuple[StructField, ...] = ()

    @staticmethod
    def array(element: "GType | None") -> "GType":
        return GType("ARRAY", element)

    @staticmethod
    def range(element: "GType | None") -> "GType":
        return GType("RANGE", element)

    @staticmethod
    def struct(fields: Iterable) -> "GType":
        return GType("STRUCT", fields=tuple(f if isinstance(f, StructField) else StructField(*f) for f in fields))

    @property
    def complete(self) -> bool:
        if self.kind in ("ARRAY", "RANGE"):
            return self.element is not None and self.element.complete
        if self.kind == "STRUCT":
            return all(f.type is not None and f.type.complete for f in self.fields)
        return True

    def field(self, name: str) -> "GType | None":
        """The type of the field ``name`` (case-insensitive); None when absent, unknown or ambiguous."""

        found = [f for f in self.fields if f.name is not None and f.name.lower() == name.lower()]
        return found[0].type if self.kind == "STRUCT" and len(found) == 1 else None

    def sql(self) -> str:
        """The type as BigQuery prints it; an unknown part prints as ``?``."""

        if self.kind in ("ARRAY", "RANGE"):
            return f"{self.kind}<{self.element.sql() if self.element is not None else '?'}>"
        if self.kind == "STRUCT":
            parts = []
            for f in self.fields:
                inner = f.type.sql() if f.type is not None else "?"
                parts.append(f"{_quote(f.name)} {inner}" if f.name else inner)
            return f"STRUCT<{', '.join(parts)}>"
        return self.kind

    def __str__(self) -> str:
        return self.sql()


def _quote(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) else f"`{name}`"


INT64, FLOAT64, NUMERIC, BIGNUMERIC = GType("INT64"), GType("FLOAT64"), GType("NUMERIC"), GType("BIGNUMERIC")
BOOL, STRING, BYTES, JSON = GType("BOOL"), GType("STRING"), GType("BYTES"), GType("JSON")
DATE, DATETIME, TIME, TIMESTAMP = GType("DATE"), GType("DATETIME"), GType("TIME"), GType("TIMESTAMP")
INTERVAL, GEOGRAPHY = GType("INTERVAL"), GType("GEOGRAPHY")

# Spellings a BigQuery schema or GoogleSQL type text may use for each kind.
_TYPE_NAMES = {
    "INT64": "INT64", "INTEGER": "INT64", "INT": "INT64", "SMALLINT": "INT64", "BIGINT": "INT64",
    "TINYINT": "INT64", "BYTEINT": "INT64",
    "FLOAT64": "FLOAT64", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64",
    "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC", "BIGDECIMAL": "BIGNUMERIC",
    "BOOL": "BOOL", "BOOLEAN": "BOOL", "STRING": "STRING", "BYTES": "BYTES",
    "DATE": "DATE", "DATETIME": "DATETIME", "TIME": "TIME", "TIMESTAMP": "TIMESTAMP",
    "INTERVAL": "INTERVAL", "JSON": "JSON", "GEOGRAPHY": "GEOGRAPHY",
    "INT32": "INT32", "UINT32": "UINT32", "UINT64": "UINT64", "FLOAT32": "FLOAT32", "UUID": "UUID",
    "ARRAY": "ARRAY", "STRUCT": "STRUCT", "RECORD": "STRUCT", "RANGE": "RANGE",
}
_TYPE_TOKEN = re.compile(r"\s*(`(?:[^`\\]|\\.)*`|[A-Za-z_][\w]*|\d+|<|>|,|\(|\))")


def parse_type(text: str | None) -> GType | None:
    """Parse type text (``ARRAY<STRUCT<a INT64>>``, BigQuery schema spellings such as ``INTEGER``, ``FLOAT``,
    ``RECORD``, ``STRING(10)``, ``NUMERIC(10, 2)``); None when it is not a type this module knows.

    ``FLOAT`` means FLOAT64 here, as in BigQuery schemas; GoogleSQL's 32-bit float is ``FLOAT32``.
    """

    if not text or not isinstance(text, str):
        return None
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        m = _TYPE_TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip():
                return None
            break
        tokens.append(m.group(1))
        pos = m.end()
    reader = _TypeReader(tokens)
    try:
        result = reader.type()
    except (ValueError, IndexError):
        return None
    return result if reader.i == len(tokens) else None


class _TypeReader:
    def __init__(self, tokens: list[str]):
        self.tokens, self.i = tokens, 0

    def peek(self, offset: int = 0) -> str | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError(token)
        self.i += 1
        return token

    def type(self) -> GType:
        kind = _TYPE_NAMES.get(self.take().upper())
        if kind is None:
            raise ValueError("unknown type")
        if kind in ("ARRAY", "RANGE"):
            self.take("<")
            element = self.type()
            self.take(">")
            return GType(kind, element)
        if kind == "STRUCT":
            if self.peek() != "<":
                raise ValueError("STRUCT without fields")
            self.take("<")
            fields = []
            while self.peek() != ">":
                name = None
                if self.peek(1) not in ("<", ",", ">", "(", None):
                    name = self.take().strip("`")
                fields.append(StructField(name, self.type()))
                if self.peek() == ",":
                    self.take(",")
            self.take(">")
            return GType.struct(fields)
        if self.peek() == "(":  # STRING(10), NUMERIC(10, 2): parameters do not change the type
            while self.take() != ")":
                pass
        return GType(kind)


def from_datatype(datatype: exp.DataType, ambiguous: frozenset[str] = frozenset()) -> GType | None:
    """A sqlglot DataType (as parsed from BigQuery SQL) as a GType; None when sqlglot's type does not fix one.

    ``ambiguous`` names sqlglot types that stand for more than one GoogleSQL type in the query being typed (sqlglot
    reads both ``INT`` and GoogleSQL's ``INT32`` as INT).
    """

    if not isinstance(datatype, exp.DataType):
        return None
    t = datatype.this
    name = t.name if hasattr(t, "name") else str(t)
    if name in ambiguous:
        return None
    if name == "ARRAY":
        inner = datatype.expressions[0] if datatype.expressions else None
        element = from_datatype(inner, ambiguous) if isinstance(inner, exp.DataType) else None
        return GType.array(element) if element is not None else None
    if name == "RANGE":
        inner = datatype.expressions[0] if datatype.expressions else None
        element = from_datatype(inner, ambiguous) if isinstance(inner, exp.DataType) else None
        return GType.range(element) if element is not None else None
    if name == "STRUCT":
        fields = []
        for item in datatype.expressions:
            if isinstance(item, exp.ColumnDef) and isinstance(item.args.get("kind"), exp.DataType):
                inner = from_datatype(item.args["kind"], ambiguous)
                fields.append(StructField(item.name, inner))
            elif isinstance(item, exp.DataType):
                fields.append(StructField(None, from_datatype(item, ambiguous)))
            else:
                return None
            if fields[-1].type is None:
                return None
        return GType.struct(fields)
    kind = _SQLGLOT_KINDS.get(name)
    return GType(kind) if kind else None


# sqlglot DataType.Type names, as parsed from BigQuery SQL, to GoogleSQL kinds. FLOAT is GoogleSQL's 32-bit float and
# UUID is not a BigQuery type, so they map to nothing; INT (INT, INTEGER, BYTEINT, and GoogleSQL's INT32) is INT64
# unless the query names INT32.
_SQLGLOT_KINDS = {
    "BIGINT": "INT64", "INT": "INT64", "SMALLINT": "INT64", "TINYINT": "INT64",
    "DOUBLE": "FLOAT64", "DECIMAL": "NUMERIC", "BIGDECIMAL": "BIGNUMERIC",
    "BOOLEAN": "BOOL", "TEXT": "STRING", "VARCHAR": "STRING", "BINARY": "BYTES", "VARBINARY": "BYTES",
    "DATE": "DATE", "TIMESTAMP": "DATETIME", "DATETIME": "DATETIME", "TIME": "TIME", "TIMESTAMPTZ": "TIMESTAMP",
    "INTERVAL": "INTERVAL", "JSON": "JSON", "GEOGRAPHY": "GEOGRAPHY",
}


# --- catalog ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    """One output column: ``name`` is None for an anonymous column, ``type`` None when not known, ``required`` True
    only when the column can never be NULL."""

    name: str | None
    type: GType | None
    required: bool | None = None


class Catalog:
    """Table schemas, found under each spelling of their name (``project.dataset.table``, ``dataset.table``,
    ``table``); a spelling two tables share finds neither. ``functions`` are user-defined functions: a call to one has
    an unknown type even if its name matches a built-in function."""

    def __init__(self, functions: Iterable[str] = ()):
        self._tables: dict[str, tuple[Column, ...]] = {}
        self.functions = {f.lower() for f in functions}

    @classmethod
    def from_types(cls, tables: Mapping[str, object], functions: Iterable[str] = ()) -> "Catalog":
        """``{table: {column: type text or GType}}``; a list of ``(column, type)`` pairs keeps the column order."""

        catalog = cls(functions)
        for name, columns in tables.items():
            items = columns.items() if isinstance(columns, Mapping) else columns
            catalog.add(name, [Column(c, t if isinstance(t, GType) else parse_type(t)) for c, t in items])
        return catalog

    @classmethod
    def from_fields(cls, tables: Mapping[str, Iterable], functions: Iterable[str] = ()) -> "Catalog":
        """``{table: [dryrun.Field, ...]}``: REPEATED fields are arrays, RECORD fields structs, REQUIRED kept."""

        catalog = cls(functions)
        for name, fields in tables.items():
            catalog.add(name, [Column(f.name, field_type(f), (f.mode or "").upper() == "REQUIRED") for f in fields])
        return catalog

    def add(self, name: str, columns: Iterable[Column]) -> None:
        self._tables[name.strip("`").lower()] = tuple(columns)

    def tables(self) -> dict[str, tuple[Column, ...]]:
        return dict(self._tables)

    def lookup(self, name: str) -> tuple[Column, ...] | None:
        """Columns of the table spelled ``name``: an exact full name first, else the one table whose name ends with it."""

        key = name.strip("`").lower()
        if key in self._tables:
            return self._tables[key]
        owners = [full for full in self._tables if full.endswith("." + key)]
        return self._tables[owners[0]] if len(owners) == 1 else None


def field_type(f) -> GType | None:
    """The GType of a BigQuery schema field (``dryrun.Field`` or anything with name/type/mode/fields)."""

    kind = (getattr(f, "type", "") or "").upper()
    if kind in ("RECORD", "STRUCT"):
        inner = [StructField(x.name, field_type(x)) for x in getattr(f, "fields", ())]
        base = GType.struct(inner) if inner else None
    else:
        base = parse_type(kind)
    if (getattr(f, "mode", "") or "").upper() == "REPEATED":
        return GType.array(base) if base is not None else None
    return base


# --- results ------------------------------------------------------------------------------------------------------

FINDING_CODES = (
    "unknown_table", "unknown_column", "ambiguous_column", "incompatible_operands", "set_operation_width",
    "set_operation_type", "invalid_field_access", "no_matching_signature", "star_except_missing",
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    node: exp.Expression | None = field(default=None, compare=False, repr=False)


class TypedQuery:
    """The result of :func:`infer`; see the module docstring."""

    def __init__(self, tree, columns, findings, types, relations, error=None):
        self.tree = tree
        self.columns: tuple[Column, ...] | None = columns
        self.findings: tuple[Finding, ...] = tuple(findings)
        self._types = types
        self._relations = relations
        self.error: str | None = error

    def type_of(self, node: exp.Expression) -> GType | None:
        entry = self._types.get(id(node))
        return entry[1] if entry is not None and entry[0] is node else None

    def relation(self, node: exp.Expression) -> tuple[Column, ...] | None:
        entry = self._relations.get(id(node))
        return entry[1] if entry is not None and entry[0] is node else None

    @property
    def complete(self) -> bool:
        """Whether every output column has a complete type."""

        return self.columns is not None and all(c.type is not None and c.type.complete for c in self.columns)


def infer(sql_or_tree, catalog: Catalog | None = None, dialect: str = "bigquery") -> TypedQuery:
    """Type a query (SQL text or a parsed sqlglot tree) against ``catalog``; see the module docstring."""

    catalog = catalog or Catalog()
    text = sql_or_tree if isinstance(sql_or_tree, str) else None
    if isinstance(sql_or_tree, str):
        if "|>" in _without_strings(sql_or_tree):
            return TypedQuery(None, None, (), {}, {}, "pipe syntax is not typed")
        try:
            tree = sqlglot.parse_one(sql_or_tree, read=dialect)
        except Exception as exc:  # noqa: BLE001 - an unparsed query has no types
            return TypedQuery(None, None, (), {}, {}, f"parse error: {exc}"[:200])
    else:
        tree = sql_or_tree
    if not isinstance(tree, exp.Query):
        return TypedQuery(tree, None, (), {}, {}, "not a query")
    typer = _Typer(catalog, text if text is not None else tree.sql(dialect))
    try:
        rel = typer.query(tree, None, {})
    except _Unsupported as exc:
        return TypedQuery(tree, None, typer.findings, typer.types, typer.relations, str(exc))
    except RecursionError:
        return TypedQuery(tree, None, typer.findings, typer.types, typer.relations, "query too deep")
    columns = None
    if rel is not None and rel.columns is not None:
        columns = tuple(Column(c.name, _materialize(c.t), c.required) for c in rel.columns)
    return TypedQuery(tree, columns, typer.findings, typer.types, typer.relations)


def _without_strings(sql: str) -> str:
    return re.sub(r"'''.*?'''|\"\"\".*?\"\"\"|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`", "''", sql, flags=re.S)


# --- the typer ----------------------------------------------------------------------------------------------------

class _Unsupported(Exception):
    """A construct the typer does not model; the query's columns are unknown."""


@dataclass(frozen=True)
class T:
    """An expression's static type: ``type`` (None when unknown) and, for literals, the literal kind that GoogleSQL's
    literal coercion depends on: ``null`` (an untyped NULL, INT64 when nothing coerces it), ``int``, ``float``,
    ``string``, ``bytes`` or ``empty_array`` (``[]``, ARRAY<INT64> when nothing coerces it)."""

    type: GType | None
    lit: str | None = None


UNKNOWN = T(None)
NULL_LITERAL = T(INT64, "null")


def known(t: GType | None) -> T:
    return T(t) if t is not None else UNKNOWN


def _materialize(t: T) -> GType | None:
    return t.type


@dataclass
class _Col:
    name: str | None
    t: T
    required: bool | None = None


@dataclass
class _Rel:
    """A query's result: ``columns`` (None when unknown); ``value`` for a value table (SELECT AS VALUE) is its row
    type; ``as_struct`` marks SELECT AS STRUCT."""

    columns: list[_Col] | None
    value: T | None = None
    as_struct: bool = False


@dataclass
class _Range:
    """A FROM item: ``name`` (lower case, None when it has no alias), its columns (None when unknown), and for a value
    table (UNNEST, SELECT AS VALUE) the row ``value`` the range variable stands for."""

    name: str | None
    columns: list[_Col] | None
    value: T | None = None
    node: exp.Expression | None = None
    display: str | None = None  # the alias as written, the column name SELECT * gives a value table

    def struct_type(self) -> GType | None:
        if self.value is not None:
            return self.value.type
        if self.columns is None or any(c.t.type is None for c in self.columns):
            return None
        return GType.struct([StructField(c.name, c.t.type) for c in self.columns])

    def addressable(self) -> list[_Col] | None:
        """Columns a bare name can reach: the columns, or the fields of a STRUCT value table."""

        if self.value is not None:
            t = self.value.type
            if t is None:
                return None
            if t.kind == "STRUCT":
                return [_Col(f.name, known(f.type)) for f in t.fields if f.name]
            return []
        return self.columns


class _Scope:
    """Names visible in one query block's clauses: its FROM ranges, the USING-merged columns, and the outer scope."""

    def __init__(self, parent: "_Scope | None"):
        self.parent = parent
        self.ranges: list[_Range] = []
        self.merged: dict[str, _Col] = {}  # USING columns, by lower name
        self.star: list[_Col] | None = []  # SELECT * columns in order; None when unknown

    def lookup(self, name: str):
        """``("range", _Range)``, ``("column", _Col)``, ``("ambiguous", None)``, ``("unknown", None)`` or ``("none", None)``."""

        key = name.lower()
        ranges = [r for r in self.ranges if r.name == key]
        if len(ranges) > 1:
            return ("ambiguous", None)
        if ranges:
            return ("range", ranges[0])
        if key in self.merged:
            return ("column", self.merged[key])
        found: list[_Col] = []
        unknown = False
        for r in self.ranges:
            cols = r.addressable()
            if cols is None:
                unknown = True
                continue
            found.extend(c for c in cols if c.name is not None and c.name.lower() == key)
        if len(found) > 1:
            return ("ambiguous", None)
        if unknown:
            return ("unknown", None)
        if found:
            return ("column", found[0])
        return ("none", None)


class _Typer:
    def __init__(self, catalog: Catalog, text: str):
        self.catalog = catalog
        self.text = text
        self.types: dict[int, tuple[exp.Expression, GType | None]] = {}
        self.relations: dict[int, tuple[exp.Expression, tuple[Column, ...] | None]] = {}
        self.findings: list[Finding] = []
        clean = _without_strings(text).upper()
        ambiguous = set()
        if re.search(r"\bINT32\b", clean):
            ambiguous.add("INT")
        self.ambiguous = frozenset(ambiguous)
        from . import googlesql_signatures

        self.signatures = googlesql_signatures

    # ---- bookkeeping

    def note(self, node: exp.Expression, t: T) -> T:
        self.types[id(node)] = (node, t.type)
        return t

    def note_relation(self, node: exp.Expression, columns: list[_Col] | None) -> None:
        self.relations[id(node)] = (node, None if columns is None else tuple(Column(c.name, c.t.type) for c in columns))

    def finding(self, code: str, message: str, node: exp.Expression | None = None) -> None:
        self.findings.append(Finding(code, message, node))

    def datatype(self, node) -> GType | None:
        return from_datatype(node, self.ambiguous)

    # ---- queries

    def query(self, node: exp.Expression, outer: _Scope | None, ctes: dict) -> _Rel | None:
        if isinstance(node, exp.Subquery):
            inner = self.query(node.this, outer, ctes)
            return inner
        if isinstance(node, exp.Paren):
            return self.query(node.this, outer, ctes)
        ctes = self.with_clause(node, outer, ctes)
        if isinstance(node, exp.SetOperation):
            return self.set_operation(node, outer, ctes)
        if isinstance(node, exp.Select):
            return self.select(node, outer, ctes)
        raise _Unsupported(f"query shape {type(node).__name__}")

    def with_clause(self, node: exp.Expression, outer: _Scope | None, ctes: dict) -> dict:
        with_ = node.args.get("with") or node.args.get("with_")
        if not isinstance(with_, exp.With):
            return ctes
        ctes = dict(ctes)
        recursive = bool(with_.args.get("recursive"))
        for cte in with_.expressions:
            name = cte.alias_or_name.lower()
            body = cte.this
            if recursive and self._references(body, name):
                rel = self.recursive_cte(body, name, outer, ctes)
            else:
                rel = self.query(body, outer, ctes)
            rel = self._rename(rel, cte.args.get("alias"))
            ctes[name] = rel
        return ctes

    def _rename(self, rel: _Rel | None, alias) -> _Rel | None:
        """Apply a CTE or derived-table column list (``WITH c (a, b) AS ...``)."""

        names = [c.name for c in alias.args.get("columns") or []] if isinstance(alias, exp.TableAlias) else []
        if not names or rel is None:
            return rel
        if rel.columns is None or len(rel.columns) != len(names):
            return _Rel(None)
        return _Rel([_Col(n, c.t, c.required) for n, c in zip(names, rel.columns)])

    @staticmethod
    def _references(node: exp.Expression, name: str) -> bool:
        return any(not t.args.get("db") and t.name.lower() == name for t in node.find_all(exp.Table))

    def recursive_cte(self, body: exp.Expression, name: str, outer, ctes) -> _Rel | None:
        """A recursive CTE is typed by its non-recursive term; the recursive term must give the same types."""

        if not isinstance(body, exp.Union):
            return _Rel(None)
        base = self.query(body.this, outer, ctes)
        if base is None or base.columns is None:
            return _Rel(None)
        base = _Rel([_Col(c.name, T(c.t.type if c.t.lit != "null" else INT64)) for c in base.columns])
        step = self.query(body.expression, outer, {**ctes, name: base})
        if step is None or step.columns is None or len(step.columns) != len(base.columns):
            return _Rel(None)
        for b, s in zip(base.columns, step.columns):
            if s.t.type != b.t.type and not coercible(s.t, b.t.type):
                return _Rel([_Col(c.name, UNKNOWN) for c in base.columns])
        return base

    def set_operation(self, node: exp.SetOperation, outer, ctes) -> _Rel | None:
        mode = _set_mode(node)
        leaves = self._set_leaves(node)
        rels = [self.query(leaf, outer, ctes) for leaf in leaves]
        if mode == "unknown" or any(r is None or r.columns is None for r in rels):
            return _Rel(None)
        if mode is not None:
            return self.by_name(node, rels, mode)
        widths = {len(r.columns) for r in rels}
        if len(widths) > 1:
            self.finding("set_operation_width", f"set operation branches have {sorted(widths)} columns", node)
            return _Rel(None)
        columns = []
        for position, first in enumerate(rels[0].columns):
            label = first.name or f"#{position + 1}"
            columns.append(_Col(first.name, self._set_output([r.columns[position].t for r in rels], label, node)))
        return _Rel(columns)

    def _set_output(self, branch: list[T], label: str, node) -> T:
        """One output column of a set operation: the supertype of its branches, no longer a literal."""

        t = supertype(branch)
        if t is None:
            if all(b.type is not None and b.type.complete for b in branch) and incompatible(branch):
                self.finding("set_operation_type", f"set operation output {label} has no common type: "
                             + ", ".join(b.type.sql() for b in branch), node)
            return UNKNOWN
        return T(t.type) if t.lit != "null" else T(INT64)

    def _set_leaves(self, node: exp.Expression) -> list[exp.Expression]:
        """The inputs of one n-ary set operation: directly nested operations of the same kind and mode (GoogleSQL
        needs parentheses to mix them, and sqlglot nests ``a UNION ALL b UNION ALL c`` as binary nodes)."""

        def same(child) -> bool:
            return (type(child) is type(node) and bool(child.args.get("distinct")) == bool(node.args.get("distinct"))
                    and _set_mode(child) == _set_mode(node)
                    and [n.lower() for n in _set_on(child) or ()] == [n.lower() for n in _set_on(node) or ()]
                    and not any(child.args.get(k) for k in ("with", "with_", "order", "limit", "offset")))

        out = []
        for child in (node.this, node.expression):
            if same(child):
                out.extend(self._set_leaves(child))
            else:
                out.append(child)
        return out

    def by_name(self, node: exp.SetOperation, rels: list[_Rel], mode: str) -> _Rel | None:
        """BY NAME and CORRESPONDING: inputs matched by column name. ``mode`` is ``strict`` (BY NAME, STRICT
        CORRESPONDING: the same names everywhere), ``inner`` (CORRESPONDING: the names every input has), ``left`` (the
        first input's names) or ``full`` (every name, the first input's first); BY (list) / ON (list) fixes the output
        names and their order. A name an input lacks is padded with NULL, which does not affect the type."""

        written = _set_on(node)
        on = [name.lower() for name in written] if written is not None else None
        names = []
        for r in rels:
            if r.value is not None or r.as_struct:
                return _Rel(None)
            lower = [c.name.lower() if c.name else None for c in r.columns]
            # Anonymous or duplicate columns are an error, except outside a BY / ON list (they are not matched).
            matched = [key for key in lower if on is None or key in on]
            if None in matched or len(set(matched)) != len(matched):
                return _Rel(None)
            names.append(lower)
        if on is not None:
            if len(set(on)) != len(on):
                return _Rel(None)
            if mode in ("strict", "inner") and any(set(on) - set(n) for n in names):
                return _Rel(None)
            if mode == "left" and set(on) - set(names[0]):
                return _Rel(None)
            if mode == "full" and any(not any(key in n for n in names) for key in on):
                return _Rel(None)
            output = list(on)
        elif mode == "strict":
            if any(set(n) != set(names[0]) for n in names):
                return _Rel(None)
            output = names[0]
        elif mode == "inner":
            output = [key for key in names[0] if all(key in n for n in names[1:])]
        elif mode == "left":
            output = names[0]
            if any(not set(n) & set(output) for n in names[1:]):
                return _Rel(None)
        else:
            output = []
            for n in names:
                output.extend(key for key in n if key not in output)
        if not output:
            return _Rel(None)
        columns = []
        for position, key in enumerate(output):
            having = [(r, n.index(key)) for r, n in zip(rels, names) if key in n]
            # The output names are spelled as in the BY / ON list, else as in the first input that has them.
            display = written[position] if written is not None else having[0][0].columns[having[0][1]].name
            columns.append(_Col(display, self._set_output([r.columns[i].t for r, i in having], display, node)))
        return _Rel(columns)

    def select(self, node: exp.Select, outer: _Scope | None, ctes: dict) -> _Rel | None:
        scope = self.from_clause(node, outer, ctes)
        columns: list[_Col] | None = []
        for item in node.expressions:
            expanded = self.select_item(item, scope, ctes)
            if expanded is None or columns is None:
                columns = None
                continue
            columns.extend(expanded)
        self.check_clauses(node, scope, ctes)
        kind = node.args.get("kind")
        kind = str(kind).upper() if kind else None
        if kind == "VALUE":
            if columns is None or len(columns) != 1:
                return _Rel(None)
            return _Rel([_Col(None, columns[0].t)], value=columns[0].t)
        if kind == "STRUCT":
            return _Rel(columns, as_struct=True)
        if kind:
            return _Rel(None)  # SELECT AS <proto>
        return _Rel(columns)

    def check_clauses(self, node: exp.Select, scope: _Scope, ctes: dict) -> None:
        """Type the other clauses for their findings and node types (they do not change the output types)."""

        for key in ("where", "having", "qualify"):
            clause = node.args.get(key)
            if isinstance(clause, exp.Expression) and clause.this is not None:
                self.expr(clause.this, scope, ctes)
        for join in node.args.get("joins") or []:
            on = join.args.get("on")
            if isinstance(on, exp.Expression):
                self.expr(on, scope, ctes)

    # ---- FROM

    def from_clause(self, node: exp.Select, outer: _Scope | None, ctes: dict) -> _Scope:
        scope = _Scope(outer)
        from_ = node.args.get("from") or node.args.get("from_")
        if from_ is None:
            return scope
        self.add_item(from_.this, scope, outer, ctes, None)
        for join in node.args.get("joins") or []:
            self.add_item(join.this, scope, outer, ctes, join)
        if node.args.get("laterals"):
            scope.ranges.append(_Range(None, None))
            scope.star = None
        return scope

    def add_item(self, item: exp.Expression, scope: _Scope, outer, ctes, join: exp.Join | None) -> None:
        before = list(scope.star) if scope.star is not None else None
        new = self.range_of(item, scope, outer, ctes)
        if isinstance(item, (exp.Table, exp.Subquery)) and item.args.get("joins"):
            new = [_Range(None, None, node=item)]  # a parenthesized join
        if item.args.get("pivots"):
            new = [self.pivoted(new, item.args["pivots"], outer, ctes, item)]
        for r in new:
            if r.node is not None:
                self.note_relation(r.node, r.columns if r.value is None else r.addressable())
        using = join.args.get("using") if join is not None else None
        if using:
            self.merge_using(scope, new, using, join, before)
        else:
            scope.ranges.extend(new)
            if scope.star is not None:
                for r in new:
                    cols = self.star_columns(r)
                    if cols is None:
                        scope.star = None
                        break
                    scope.star.extend(cols)

    def pivoted(self, base: list[_Range], pivots: list, outer, ctes, item) -> _Range:
        """The range a FROM item gives after its PIVOT / UNPIVOT operators (unknown columns when not modelled)."""

        alias = pivots[-1].args.get("alias")
        name = alias.name.lower() if isinstance(alias, exp.TableAlias) and alias.name else None
        current = base[0] if len(base) == 1 and base[0].value is None else None
        for pivot in pivots:
            if current is None or current.columns is None:
                return _Range(name, None, node=item)
            columns = self.unpivot(pivot, current) if pivot.args.get("unpivot") else self.pivot(pivot, current, outer, ctes)
            if columns is None:
                return _Range(name, None, node=item)
            pivot_alias = pivot.args.get("alias")
            current = _Range(pivot_alias.name.lower() if isinstance(pivot_alias, exp.TableAlias) and pivot_alias.name
                             else None, columns, node=item)
        return _Range(name, current.columns, node=item)

    def pivot(self, pivot: exp.Pivot, base: _Range, outer, ctes) -> list[_Col] | None:
        """PIVOT: the input columns no aggregate and no FOR expression reads, then one column per IN value and
        aggregate (value-major), typed as the aggregate and named ``prefix_value``."""

        fields = pivot.args.get("fields") or []
        if len(fields) != 1 or not isinstance(fields[0], exp.In):
            return None
        scope = _Scope(outer)
        scope.ranges.append(base)
        aggregates = []
        for agg in pivot.expressions:
            prefix = agg.alias if isinstance(agg, exp.Alias) else None
            call = agg.this if isinstance(agg, exp.Alias) else agg
            aggregates.append((prefix or None, self.expr(call, scope, ctes)))
        if len(aggregates) > 1 and any(prefix is None for prefix, _ in aggregates):
            return None
        source = self.expr(fields[0].this, scope, ctes)
        lower = [c.name.lower() if c.name else None for c in base.columns]
        if None in lower or len(set(lower)) != len(lower):
            return None
        read = set()
        for node in [*pivot.expressions, fields[0].this]:
            if node.find(exp.Query):
                return None  # which input columns a subquery reads decides the grouping columns
            for column in node.find_all(exp.Column):
                parts = [p.name.lower() for p in column.parts]
                if parts[0] == base.name and len(parts) > 1 and parts[1] in lower:
                    read.add(parts[1])
                elif parts[0] in lower:
                    read.add(parts[0])
        columns = [_Col(c.name, _plain(c.t), c.required) for c in base.columns if c.name.lower() not in read]
        for value in fields[0].expressions:
            label = _pivot_value_name(value, source.type)
            if label is None:
                return None
            for prefix, t in aggregates:
                columns.append(_Col(f"{prefix}_{label}" if prefix else label, _plain(t)))
        return columns

    def unpivot(self, pivot: exp.Pivot, base: _Range) -> list[_Col] | None:
        """UNPIVOT: the input columns not unpivoted, then the value columns (typed as the first column set; the
        sets must have equal types) and the name column (STRING, or INT64 for integer labels)."""

        fields = pivot.args.get("fields") or []
        if len(fields) != 1 or not isinstance(fields[0], exp.In) or not isinstance(fields[0].this, exp.Identifier):
            return None
        targets = pivot.expressions[0].expressions if len(pivot.expressions) == 1 and isinstance(
            pivot.expressions[0], exp.Tuple) else pivot.expressions
        if not targets or not all(isinstance(t, (exp.Identifier, exp.Column)) for t in targets):
            return None
        by_name: dict[str, _Col] = {}
        for c in base.columns:
            if c.name is None:
                continue
            by_name[c.name.lower()] = None if c.name.lower() in by_name else c  # a duplicate name can't be unpivoted
        sets, labels = [], set()
        for entry in fields[0].expressions:
            label = entry.args.get("alias") if isinstance(entry, exp.PivotAlias) else None
            entry = entry.this if isinstance(entry, exp.PivotAlias) else entry
            members = entry.expressions if isinstance(entry, exp.Tuple) else [entry]
            if not all(isinstance(m, exp.Column) and not m.table for m in members) or len(members) != len(targets):
                return None
            cols = [by_name.get(m.name.lower()) for m in members]
            if any(c is None for c in cols):
                return None
            sets.append(cols)
            if label is None:
                labels.add("string")
            elif isinstance(label, exp.Literal):
                labels.add("string" if label.is_string else "int" if re.fullmatch(r"\d+", label.this) else "other")
            else:
                labels.add("other")
        if not sets or len(labels) != 1 or labels == {"other"}:
            return None
        used = {c.name.lower() for cols in sets for c in cols}
        columns = [_Col(c.name, _plain(c.t), c.required) for c in base.columns if not (c.name and c.name.lower() in used)]
        for position, target in enumerate(targets):
            types = [cols[position].t.type for cols in sets]
            first = types[0]
            same = first is not None and all(t is not None and _equivalent(t, first) for t in types)
            columns.append(_Col(target.name, T(first) if same else UNKNOWN))
        columns.append(_Col(fields[0].this.name, T(STRING if labels == {"string"} else INT64), True))
        return columns

    @staticmethod
    def star_columns(r: _Range) -> list[_Col] | None:
        if r.value is not None:
            t = r.value.type
            if t is None:
                return None
            if t.kind == "STRUCT":
                return [_Col(f.name, known(f.type)) for f in t.fields]
            return [_Col(r.display, r.value)]
        return None if r.columns is None else list(r.columns)

    def merge_using(self, scope: _Scope, new: list[_Range], using, join: exp.Join, before) -> None:
        names = [u.name if isinstance(u, exp.Expression) else str(u) for u in using]
        left_ranges = list(scope.ranges)
        scope.ranges.extend(new)
        side = str(join.args.get("side") or "").upper()
        star_left = before
        right_cols: list[_Col] | None = []
        for r in new:
            cols = self.star_columns(r)
            if cols is None or right_cols is None:
                right_cols = None
            else:
                right_cols.extend(cols)
        merged_star: list[_Col] = []
        for name in names:
            key = name.lower()
            left = self._unique(left_ranges, scope.merged, key)
            right = self._unique(new, {}, key)
            if left is None or right is None:
                t = UNKNOWN
                col_name = name
            else:
                col_name = left.name if side != "RIGHT" else right.name
                if left.t.type is not None and left.t.type == right.t.type:
                    t = T(left.t.type)
                else:
                    t = UNKNOWN
            scope.merged[key] = _Col(col_name, t)
            merged_star.append(scope.merged[key])
        if star_left is None or right_cols is None:
            scope.star = None
            return
        keys = {n.lower() for n in names}
        if sum(1 for c in star_left if c.name and c.name.lower() in keys) != len(keys) or sum(
            1 for c in right_cols if c.name and c.name.lower() in keys
        ) != len(keys):
            scope.star = None
            return
        scope.star = merged_star + [c for c in star_left if not (c.name and c.name.lower() in keys)] + [
            c for c in right_cols if not (c.name and c.name.lower() in keys)
        ]

    @staticmethod
    def _unique(ranges: list[_Range], merged: dict, key: str) -> _Col | None:
        if key in merged:
            return merged[key]
        found = []
        for r in ranges:
            cols = r.addressable()
            if cols is None:
                return None
            found.extend(c for c in cols if c.name and c.name.lower() == key)
        return found[0] if len(found) == 1 else None

    def range_of(self, item: exp.Expression, scope: _Scope, outer, ctes) -> list[_Range]:
        """The range variables one FROM item adds (an UNNEST WITH OFFSET adds two)."""

        alias = item.args.get("alias")
        alias_name = alias.name if isinstance(alias, exp.TableAlias) and alias.name else None
        if isinstance(item, exp.Table):
            return [self.table_range(item, scope, ctes, alias_name)]
        if isinstance(item, exp.Subquery):
            rel = self.query(item.this, outer, ctes)
            rel = self._rename(rel, alias)
            name = alias_name.lower() if alias_name else None
            if rel is None:
                return [_Range(name, None, node=item)]
            if rel.value is not None:
                return [_Range(name, None, value=_plain(rel.value), node=item, display=alias_name)]
            columns = None if rel.columns is None else [_Col(c.name, _plain(c.t), c.required) for c in rel.columns]
            return [_Range(name, columns, node=item)]
        if isinstance(item, exp.Unnest):
            return self.unnest_range(item, scope, ctes)
        return [_Range(alias_name.lower() if alias_name else None, None, node=item)]

    def table_range(self, item: exp.Table, scope: _Scope, ctes, alias_name: str | None) -> _Range:
        parts = [p.name for p in (item.args.get("catalog"), item.args.get("db"), item.this) if isinstance(p, exp.Expression)]
        if not isinstance(item.this, exp.Identifier):
            return _Range(alias_name.lower() if alias_name else None, None, node=item)  # a table function
        name = (alias_name or parts[-1]).lower()
        # A path whose first part is a range variable to its left is an array to unnest (FROM t, t.arr).
        if len(parts) > 1:
            kind, target = scope.lookup(parts[0])
            if kind in ("range", "column"):
                return _Range(name, None, node=item)
        if len(parts) == 1 and parts[0].lower() in ctes:
            rel = ctes[parts[0].lower()]
            if rel is None or rel.columns is None:
                return _Range(name, None, node=item)
            if rel.value is not None:
                return _Range(name, None, value=_plain(rel.value), node=item, display=alias_name or parts[-1])
            return _Range(name, [_Col(c.name, _plain(c.t), c.required) for c in rel.columns], node=item)
        columns = self.catalog.lookup(".".join(parts))
        if columns is None:
            self.finding("unknown_table", f"table {'.'.join(parts)} is not in the catalog", item)
            return _Range(name, None, node=item)
        return _Range(name, [_Col(c.name, known(c.type), c.required) for c in columns], node=item)

    def unnest_range(self, item: exp.Unnest, scope: _Scope, ctes) -> list[_Range]:
        alias = item.args.get("alias")
        names = [c.name for c in alias.args.get("columns") or []] if isinstance(alias, exp.TableAlias) else []
        if isinstance(alias, exp.TableAlias) and alias.name and not names:
            names = [alias.name]
        exprs = item.expressions
        if len(exprs) != 1 or len(names) > 1:
            return [_Range(names[0].lower() if names else None, None, node=item)]
        t = self.expr(exprs[0], scope, ctes)
        element: T
        if t.lit == "empty_array":
            element = UNKNOWN
        elif t.type is not None and t.type.kind == "ARRAY":
            element = known(t.type.element)
        elif t.lit == "null":
            element = UNKNOWN
        else:
            element = UNKNOWN
        name = names[0] if names else None
        ranges = [_Range(name.lower() if name else None, None, value=element, node=item, display=name)]
        offset = item.args.get("offset")
        if offset:
            off_name = offset.name if isinstance(offset, exp.Expression) else "offset"
            ranges.append(_Range(off_name.lower(), None, value=T(INT64), display=off_name))
        return ranges

    # ---- SELECT list

    def select_item(self, item: exp.Expression, scope: _Scope, ctes) -> list[_Col] | None:
        if isinstance(item, exp.Star):
            return self.star(item, scope, ctes)
        if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            return self.qualified_star(item, scope, ctes)
        if isinstance(item, exp.Dot) and isinstance(item.expression, exp.Star):
            return self.qualified_star(item, scope, ctes)
        if isinstance(item, exp.Alias):
            t = self.expr(item.this, scope, ctes)
            self.note(item, t)
            return [_Col(item.alias, t, _required(item.this, t))]
        t = self.expr(item, scope, ctes)
        return [_Col(implicit_alias(item), t, _required(item, t))]

    def star(self, item: exp.Star, scope: _Scope, ctes) -> list[_Col] | None:
        if scope.star is None:
            return None
        return self.except_replace(list(scope.star), item, scope, ctes)

    def except_replace(self, columns: list[_Col], item: exp.Expression, scope: _Scope, ctes) -> list[_Col] | None:
        star = item.this if isinstance(item, exp.Column) and isinstance(item.this, exp.Star) else item
        if isinstance(item, exp.Dot):
            star = item.expression
        excepts = star.args.get("except_") or star.args.get("except") or []
        replaces = star.args.get("replace") or []
        if star.args.get("rename"):
            return None
        for e in excepts:
            key = e.name.lower()
            matches = [c for c in columns if c.name and c.name.lower() == key]
            if not matches:
                self.finding("star_except_missing", f"column {e.name} in SELECT * EXCEPT is not in the star", e)
                return None
            columns = [c for c in columns if not (c.name and c.name.lower() == key)]
        for r in replaces:
            if not isinstance(r, exp.Alias):
                return None
            key = r.alias.lower()
            t = self.expr(r.this, scope, ctes)
            positions = [i for i, c in enumerate(columns) if c.name and c.name.lower() == key]
            if len(positions) != 1:
                if not positions:
                    self.finding("star_except_missing", f"column {r.alias} in SELECT * REPLACE is not in the star", r)
                return None
            columns[positions[0]] = _Col(columns[positions[0]].name, t)
        return columns

    def qualified_star(self, item: exp.Expression, scope: _Scope, ctes) -> list[_Col] | None:
        if isinstance(item, exp.Column):
            parts = [p.name for p in (item.args.get("catalog"), item.args.get("db"), item.args.get("table")) if p is not None]
            target = self.path(parts, scope, item, ctes)
        else:
            target = self.expr(item.this, scope, ctes)
        if isinstance(target, _Range):
            if target.value is not None:
                t = target.value.type
                if t is None or t.kind != "STRUCT":
                    return None
                return self.except_replace([_Col(f.name, known(f.type)) for f in t.fields], item, scope, ctes)
            if target.columns is None:
                return None
            return self.except_replace(list(target.columns), item, scope, ctes)
        if isinstance(target, T) and target.type is not None and target.type.kind == "STRUCT":
            return self.except_replace([_Col(f.name, known(f.type)) for f in target.type.fields], item, scope, ctes)
        return None

    # ---- names

    def path(self, parts: list[str], scope: _Scope | None, node, ctes):
        """Resolve a dotted path: a ``_Range`` when it names a whole range variable, else a ``T``."""

        s = scope
        while s is not None:
            kind, target = s.lookup(parts[0])
            if kind == "none":
                s = s.parent
                continue
            if kind == "ambiguous":
                self.finding("ambiguous_column", f"{parts[0]} is ambiguous", node)
                return UNKNOWN
            if kind == "unknown":
                return UNKNOWN
            if kind == "range":
                rest = parts[1:]
                if target.value is not None:
                    if not rest:
                        return target
                    return self.fields(target.value, rest, node)
                if not rest:
                    return target
                if target.columns is None:
                    return UNKNOWN
                matches = [c for c in target.columns if c.name and c.name.lower() == rest[0].lower()]
                if len(matches) != 1:
                    if not matches and target.node is not None and isinstance(target.node, exp.Table) and \
                            not self._table_is_cte(target.node, ctes):
                        self.finding("unknown_column", f"{target.name} has no column {rest[0]}", node)
                    elif not matches:
                        self.finding("unknown_column", f"{target.name} has no column {rest[0]}", node)
                    return UNKNOWN
                return self.fields(_plain(matches[0].t), rest[1:], node)
            return self.fields(_plain(target.t), parts[1:], node)
        if scope is not None and self._complete_chain(scope):
            self.finding("unknown_column", f"unrecognized name {parts[0]}", node)
        return UNKNOWN

    @staticmethod
    def _table_is_cte(node: exp.Table, ctes) -> bool:
        return not node.args.get("db") and node.name.lower() in ctes

    @staticmethod
    def _complete_chain(scope: _Scope) -> bool:
        s = scope
        while s is not None:
            if any(r.addressable() is None for r in s.ranges):
                return False
            s = s.parent
        return True

    def fields(self, t: T, names: list[str], node) -> T:
        for name in names:
            if t.type is None:
                return UNKNOWN
            if t.type.kind == "JSON":
                t = T(JSON)
                continue
            if t.type.kind != "STRUCT":
                if t.type.kind in SCALAR_KINDS and t.type.kind != "JSON":
                    self.finding("invalid_field_access", f"cannot access field {name} on a value of type {t.type.sql()}", node)
                return UNKNOWN
            matches = [f for f in t.type.fields if f.name and f.name.lower() == name.lower()]
            if len(matches) != 1:
                if not matches and t.type.complete:
                    self.finding("invalid_field_access", f"field {name} is not in {t.type.sql()}", node)
                return UNKNOWN
            t = known(matches[0].type)
        return t

    # ---- expressions

    def expr(self, node: exp.Expression, scope: _Scope | None, ctes) -> T:
        try:
            t = self._expr(node, scope, ctes)
        except _Unsupported:
            raise
        except (RecursionError,):
            raise
        except Exception:  # noqa: BLE001 - a shape this typer does not read is an unknown type
            t = UNKNOWN
        if isinstance(t, _Range):
            st = t.struct_type()
            t = known(st)
        return self.note(node, t)

    def _expr(self, node: exp.Expression, scope: _Scope | None, ctes) -> T:
        if isinstance(node, exp.Paren):
            inner = self.expr(node.this, scope, ctes)
            return T(inner.type) if inner.lit in ("empty_array",) else inner
        if isinstance(node, exp.Null):
            return NULL_LITERAL
        if isinstance(node, exp.Boolean):
            return T(BOOL)
        if isinstance(node, exp.Literal):
            return literal_type(node)
        if isinstance(node, exp.Column):
            if isinstance(node.this, exp.Star):
                return UNKNOWN
            parts = [p.name for p in (node.args.get("catalog"), node.args.get("db"), node.args.get("table"), node.this)
                     if p is not None]
            if not parts or any(not isinstance(p, str) or p == "" for p in parts):
                return UNKNOWN
            result = self.path(parts, scope, node, ctes)
            if isinstance(result, _Range):
                return known(result.struct_type())
            return result
        if isinstance(node, exp.Dot):
            return self.dot(node, scope, ctes)
        if isinstance(node, exp.Alias):
            return self.expr(node.this, scope, ctes)
        if isinstance(node, (exp.Subquery,)) and not isinstance(node.parent, (exp.From, exp.Join)):
            return self.scalar_subquery(node, scope, ctes)
        if isinstance(node, exp.Exists):
            self.subquery_rel(node.this, scope, ctes)
            return T(BOOL)
        if isinstance(node, exp.Struct):
            return self.struct(node, scope, ctes)
        if isinstance(node, exp.Tuple):
            items = [self.expr(e, scope, ctes) for e in node.expressions]
            if len(items) < 2 or any(i.type is None for i in items):
                return UNKNOWN
            return T(GType.struct([StructField(None, _materialize_lit(i)) for i in items]))
        if isinstance(node, exp.Array):
            return self.array(node, scope, ctes)
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return self.cast(node, scope, ctes)
        if isinstance(node, exp.Bracket):
            return self.bracket(node, scope, ctes)
        if isinstance(node, exp.Window):
            self._window_parts(node, scope, ctes)
            return self.expr(node.this, scope, ctes)
        if isinstance(node, (exp.IgnoreNulls, exp.RespectNulls)):
            return self.expr(node.this, scope, ctes)
        if isinstance(node, exp.Interval):
            return T(INTERVAL)
        return self.signatures.type_call(self, node, scope, ctes)

    def _window_parts(self, node: exp.Window, scope, ctes) -> None:
        for p in node.args.get("partition_by") or []:
            self.expr(p, scope, ctes)

    def dot(self, node: exp.Dot, scope, ctes) -> T:
        # sqlglot puts up to four path parts in a Column; longer paths and field access on other values are Dots.
        parts: list[str] = []
        base = node
        while isinstance(base, exp.Dot) and isinstance(base.expression, exp.Identifier):
            parts.insert(0, base.expression.name)
            base = base.this
        if isinstance(base, exp.Column) and not isinstance(base.this, exp.Star):
            prefix = [p.name for p in (base.args.get("catalog"), base.args.get("db"), base.args.get("table"), base.this)
                      if p is not None]
            result = self.path(prefix + parts, scope, node, ctes)
            if isinstance(result, _Range):
                return known(result.struct_type())
            return result
        if isinstance(node.expression, exp.Identifier):
            inner = self.expr(node.this, scope, ctes)
            return self.fields(_plain(inner), [node.expression.name], node)
        return self.signatures.type_call(self, node, scope, ctes)

    def subquery_rel(self, node: exp.Expression, scope, ctes) -> _Rel | None:
        query = node.this if isinstance(node, exp.Subquery) else node
        try:
            return self.query(query, scope, ctes)
        except _Unsupported:
            return None

    def scalar_subquery(self, node: exp.Subquery, scope, ctes) -> T:
        rel = self.subquery_rel(node, scope, ctes)
        return self.single_value(rel)

    @staticmethod
    def single_value(rel: _Rel | None) -> T:
        if rel is None or rel.columns is None:
            return UNKNOWN
        if rel.value is not None:
            return _plain(rel.value)
        if rel.as_struct:
            if any(c.t.type is None for c in rel.columns):
                return UNKNOWN
            return T(GType.struct([StructField(c.name, _materialize_lit(c.t)) for c in rel.columns]))
        if len(rel.columns) != 1:
            return UNKNOWN
        return _plain(rel.columns[0].t)

    def struct(self, node: exp.Struct, scope, ctes) -> T:
        fields = []
        for item in node.expressions:
            if isinstance(item, (exp.Alias, exp.PropertyEQ)):
                value = item.this if isinstance(item, exp.Alias) else item.expression
                name = item.alias if isinstance(item, exp.Alias) else item.this.name
            else:
                value, name = item, implicit_alias(item)
            t = self.expr(value, scope, ctes)
            if t.type is None:
                return UNKNOWN
            fields.append(StructField(name, _materialize_lit(t)))
        return T(GType.struct(fields))

    def array(self, node: exp.Array, scope, ctes) -> T:
        items = node.expressions
        if len(items) == 1 and isinstance(items[0], exp.Query):
            rel = self.subquery_rel(items[0], scope, ctes)
            element = self.single_value(rel)
            if element.type is None:
                return UNKNOWN
            return T(GType.array(_materialize_lit(element)))
        if not items:
            return T(GType.array(INT64), "empty_array")
        ts = [self.expr(e, scope, ctes) for e in items]
        st = supertype(ts)
        if st is None or st.type is None:
            return UNKNOWN
        return T(GType.array(_materialize_lit(st)))

    def cast(self, node: exp.Expression, scope, ctes) -> T:
        to = node.args.get("to")
        inner = node.this
        target = self.datatype(to)
        if isinstance(inner, (exp.Array, exp.Struct)) and not isinstance(node, exp.TryCast) and \
                not node.args.get("safe") and target is not None and target.kind in ("ARRAY", "STRUCT"):
            # ARRAY<T>[...] and STRUCT<...>(...) are typed constructors: sqlglot reads them as casts.
            self.expr(inner, scope, ctes)
            return T(target)
        self.expr(inner, scope, ctes)
        return known(target)

    def bracket(self, node: exp.Bracket, scope, ctes) -> T:
        base = self.expr(node.this, scope, ctes)
        for e in node.expressions:
            self.expr(e, scope, ctes)
        if base.type is None:
            return UNKNOWN
        if base.type.kind == "ARRAY":
            return known(base.type.element)
        if base.type.kind == "JSON":
            return T(JSON)
        if base.type.kind == "STRUCT" and len(node.expressions) == 1:
            key = node.expressions[0]
            if isinstance(key, exp.Literal) and key.is_string:
                return T(base.type.field(key.this)) if base.type.field(key.this) is not None else UNKNOWN
            if isinstance(key, exp.Literal) and node.args.get("offset") in (0, 1):
                position = int(key.this) - node.args["offset"]  # OFFSET(k) is offset 0, ORDINAL(k) offset 1
                fields = base.type.fields
                return known(fields[position].type) if 0 <= position < len(fields) else UNKNOWN
        return UNKNOWN


def _set_mode(node: exp.SetOperation) -> str | None:
    """None for a positional set operation, else the BY NAME / CORRESPONDING mode (see ``_Typer.by_name``)."""

    side = str(node.args.get("side") or "").upper()
    kind = str(node.args.get("kind") or "").upper()
    if not node.args.get("by_name"):
        return "unknown" if side or kind or node.args.get("on") else None
    if side in ("FULL", "LEFT"):
        return side.lower()
    if side:
        return "unknown"
    if kind == "OUTER":
        return "full"
    if kind == "INNER":
        return "inner"
    return "unknown" if kind else "strict"


def _set_on(node: exp.SetOperation) -> tuple[str, ...] | None:
    on = node.args.get("on")
    if not on:
        return None
    return tuple(c.name if isinstance(c, exp.Expression) else str(c) for c in on)


def _equivalent(a: GType, b: GType) -> bool:
    """Equal types, ignoring STRUCT field names."""

    if a.kind != b.kind or (a.element is None) != (b.element is None) or len(a.fields) != len(b.fields):
        return False
    if a.element is not None and not _equivalent(a.element, b.element):
        return False
    return all(_equivalent(x.type, y.type) for x, y in zip(a.fields, b.fields))


def _pivot_value_name(value: exp.Expression, source: GType | None) -> str | None:
    """The column name PIVOT gives one IN value: its alias, else a name built from the constant (None when GoogleSQL
    builds none or the typer can't be sure of it)."""

    if isinstance(value, exp.PivotAlias):
        alias = value.args.get("alias")
        return alias.name if isinstance(alias, exp.Identifier) else None
    if isinstance(value, exp.Null):
        return "NULL"
    if source is None:
        return None
    integer = source.kind in ("INT64", "NUMERIC", "BIGNUMERIC")
    if isinstance(value, exp.Literal) and not value.is_string and integer and re.fullmatch(r"\d+", value.this):
        return "_" + value.this
    if (isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal) and not value.this.is_string and integer
            and re.fullmatch(r"\d+", value.this.this)):
        return "minus_" + value.this.this
    if isinstance(value, exp.Literal) and value.is_string and source.kind == "STRING":
        return value.this
    if isinstance(value, exp.Boolean) and source.kind == "BOOL":
        return "TRUE" if value.this else "FALSE"
    return None


def _plain(t: T) -> T:
    """A value read back from a column: no longer a literal (an untyped NULL column is INT64)."""

    if t.lit is None:
        return t
    return T(_materialize_lit(t))


def _materialize_lit(t: T) -> GType | None:
    """The type a literal has when nothing coerces it: an untyped NULL is INT64, ``[]`` is ARRAY<INT64>."""

    return t.type


def literal_type(node: exp.Literal) -> T:
    if node.is_string:
        # sqlglot reads b'..' as a HexString/ByteString, not a string Literal
        return T(STRING, "string")
    text = node.this
    if re.fullmatch(r"\d+", text) or re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        value = int(text, 16) if text[:2].lower() == "0x" else int(text)
        return T(INT64, "int") if value <= 2**63 - 1 else UNKNOWN
    if re.fullmatch(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
        return T(FLOAT64, "float")
    return UNKNOWN


def implicit_alias(node: exp.Expression) -> str | None:
    """GoogleSQL's implicit alias: the last identifier of a column path or field access."""

    if isinstance(node, exp.Column) and isinstance(node.this, exp.Identifier):
        return node.this.name
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Identifier):
        return node.expression.name
    return None


def _required(node: exp.Expression, t: T) -> bool | None:
    if isinstance(node, (exp.Literal, exp.Boolean)) and t.lit != "null":
        return True
    if isinstance(node, (exp.Count, exp.CountIf)):
        return True
    return None


# --- coercion and supertypes ----------------------------------------------------------------------------------------

def coercible(t: T, target: GType | None) -> bool:
    """Whether a value of static type ``t`` coerces to ``target`` implicitly (GoogleSQL's coercion rules)."""

    if target is None or t.type is None:
        return False
    if t.lit == "null":
        return True
    if t.lit == "empty_array":
        return target.kind == "ARRAY"
    if t.type == target:
        return True
    if t.lit in _LITERAL_COERCE_TO and target.kind in _LITERAL_COERCE_TO[t.lit]:
        return True
    return target.kind in _COERCE_TO.get(t.type.kind, ()) and not target.fields and target.element is None


def supertype(ts: list[T]) -> T | None:
    """The common supertype of expression types, with GoogleSQL's rules for literals; None when there is none or it is
    not certain. The result is a literal only when every input is an untyped NULL or ``[]``."""

    if any(t.type is None for t in ts):
        return None
    if not ts:
        return None
    if all(t.lit == "null" for t in ts):
        return NULL_LITERAL
    typed = [t for t in ts if t.lit != "null"]
    if all(t.lit == "empty_array" for t in typed):
        return T(GType.array(INT64), "empty_array")
    typed = [t for t in typed if t.lit != "empty_array"]
    non_literals = [t for t in typed if t.lit is None]
    literals = [t for t in typed if t.lit is not None]
    if any(t.lit == "empty_array" for t in ts) and any(t.type.kind != "ARRAY" for t in typed):
        return None
    if non_literals:
        candidates = _common_supertypes([t.type for t in non_literals])
        if candidates is None:
            return None
        for candidate in candidates:  # most specific first
            if _allowed_supertype(candidate, typed) and all(coercible(lit, candidate) for lit in literals):
                return T(candidate)
        return None
    candidates = _common_supertypes([t.type for t in literals])
    candidates = [c for c in candidates or [] if _allowed_supertype(c, typed)]
    if not candidates:
        return None
    return T(candidates[0])


def _allowed_supertype(candidate: GType, inputs: list[T]) -> bool:
    """GoogleSQL (``GetCommonSuperTypeImpl``) accepts FLOAT64 as a supertype only when an input is floating point
    (a float literal counts), NUMERIC only when an input is NUMERIC, BIGNUMERIC only when one is BIGNUMERIC."""

    required = {"FLOAT64": {"FLOAT32", "FLOAT64"}, "NUMERIC": {"NUMERIC"}, "BIGNUMERIC": {"BIGNUMERIC"}}.get(candidate.kind)
    return required is None or any(t.type.kind in required for t in inputs)


def _common_supertypes(types: list[GType]) -> list[GType] | None:
    """The common supertypes of non-literal types, most specific first; None when uncertain or there are none."""

    first = types[0]
    if all(t == first for t in types):
        if first.kind in NUMERIC_KINDS:
            exact = first.kind in _EXACT
            return [GType(k) for k in _NUMERIC_ORDER if k in _NUMERIC_SUPERTYPES[first.kind]
                    and (not exact or k in _EXACT or k == "FLOAT64")]
        return [first]
    kinds = {t.kind for t in types}
    if kinds <= NUMERIC_KINDS:
        if "UINT64" in kinds and kinds & {"INT32", "INT64"}:
            return None  # documented as having no supertype; leave it unknown
        if "FLOAT32" in kinds and kinds - {"FLOAT32", "FLOAT64"}:
            return None
        common = set.intersection(*(_NUMERIC_SUPERTYPES[k] for k in kinds))
        if all(k in _EXACT for k in kinds):
            exact = common & _EXACT
            ordered = [GType(k) for k in _NUMERIC_ORDER if k in exact]
            return ordered + [GType(k) for k in _NUMERIC_ORDER if k in common - exact] if ordered else None
        return [GType(k) for k in _NUMERIC_ORDER if k in common] or None
    if kinds == {"STRUCT"}:
        widths = {len(t.fields) for t in types}
        if len(widths) != 1:
            return None
        names = {tuple((f.name or "").lower() for f in t.fields) for t in types}
        if len(names) != 1:
            return None
        fields = []
        for position in range(len(first.fields)):
            column = [t.fields[position].type for t in types]
            if any(c is None for c in column):
                return None
            inner = _common_supertypes(column)
            if not inner:
                return None
            fields.append(StructField(first.fields[position].name, inner[0]))
        return [GType.struct(fields)]
    return None


def incompatible(ts: list[T]) -> bool:
    """Whether expression types certainly have no common supertype (scalar types of different families)."""

    scalar = [t for t in ts if t.lit != "null"]
    if not scalar or any(t.type is None for t in scalar):
        return False
    families = set()
    for t in scalar:
        kind = t.type.kind
        if kind not in SCALAR_KINDS:
            return False
        if t.lit == "string":
            return False  # a string literal coerces to several types
        families.add("number" if kind in NUMERIC_KINDS else kind)
    return len(families) > 1 and not families <= {"DATE", "DATETIME"}
