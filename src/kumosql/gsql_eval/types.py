"""GoogleSQL types as the evaluator sees them, and the coercion rules between them.

Every expression gets one static type when the query is analysed, as in BigQuery; values then flow as
plain Python payloads whose meaning comes from that type (an ``INT64`` is an ``int``, a ``NUMERIC`` a
``Decimal``, a ``TIMESTAMP`` an ``int`` count of microseconds since the epoch, an ``ARRAY`` a tuple, a
``STRUCT`` a tuple of field values, ``NULL`` is ``None`` for every type). Because the type travels with
the column, never with the value, ``TRUE`` and ``1`` can never be confused: they cannot meet in one
column, and results compare their types too (:mod:`kumosql.gsql_eval.result`).

``INT32``, ``UINT32``, ``UINT64`` and ``FLOAT`` (32-bit) are GoogleSQL types BigQuery does not have;
tables may hold them (the compliance tests' tables do), but any expression reading one is unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sqlglot import exp

from .errors import AnalysisError, Unsupported

SCALARS = (
    "INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64", "BOOL", "STRING", "BYTES",
    "DATE", "DATETIME", "TIME", "TIMESTAMP", "INTERVAL", "JSON",
)
FOREIGN = ("INT32", "UINT32", "UINT64", "FLOAT32", "OTHER")
NUMERIC_ORDER = ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64")


@dataclass(frozen=True)
class Type:
    kind: str
    elem: Optional["Type"] = None  # ARRAY
    fields: tuple = ()  # STRUCT: ((name or None, Type), ...)
    label: str = ""  # OTHER: the GoogleSQL name, for messages

    def __str__(self) -> str:
        if self.kind == "ARRAY":
            return f"ARRAY<{self.elem}>"
        if self.kind == "STRUCT":
            return "STRUCT<" + ", ".join(f"{n} {t}" if n else str(t) for n, t in self.fields) + ">"
        if self.kind == "OTHER":
            return self.label or "OTHER"
        return self.kind

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_ORDER

    @property
    def foreign(self) -> bool:
        """A type BigQuery does not have, anywhere inside."""

        if self.kind in FOREIGN:
            return True
        if self.kind == "ARRAY":
            return self.elem.foreign
        if self.kind == "STRUCT":
            return any(t.foreign for _, t in self.fields)
        return False

    def field_index(self, name: str) -> int | None:
        """The position of field ``name`` (case-insensitive); raises on an ambiguous name."""

        lowered = name.lower()
        hits = [i for i, (n, _) in enumerate(self.fields) if n is not None and n.lower() == lowered]
        if len(hits) > 1:
            raise AnalysisError(f"Struct field name {name} is ambiguous")
        return hits[0] if hits else None


INT64 = Type("INT64")
NUMERIC = Type("NUMERIC")
BIGNUMERIC = Type("BIGNUMERIC")
FLOAT64 = Type("FLOAT64")
BOOL = Type("BOOL")
STRING = Type("STRING")
BYTES = Type("BYTES")
DATE = Type("DATE")
DATETIME = Type("DATETIME")
TIME = Type("TIME")
TIMESTAMP = Type("TIMESTAMP")
INTERVAL = Type("INTERVAL")
JSON = Type("JSON")
INT32 = Type("INT32")
UINT32 = Type("UINT32")
UINT64 = Type("UINT64")
FLOAT32 = Type("FLOAT32")

BY_NAME = {t.kind: t for t in (INT64, NUMERIC, BIGNUMERIC, FLOAT64, BOOL, STRING, BYTES, DATE, DATETIME, TIME, TIMESTAMP,
                               INTERVAL, JSON, INT32, UINT32, UINT64, FLOAT32)}
# GoogleSQL spellings used by the compliance tests' printed results
BY_NAME.update({"DOUBLE": FLOAT64, "FLOAT": FLOAT32, "BOOLEAN": BOOL, "INT": INT64, "BIGDECIMAL": BIGNUMERIC, "DECIMAL": NUMERIC})


def array(elem: Type) -> Type:
    if elem.kind == "ARRAY":
        raise AnalysisError("Arrays of arrays are not supported")
    return Type("ARRAY", elem=elem)


def struct(fields: Iterable[tuple[str | None, Type]]) -> Type:
    return Type("STRUCT", fields=tuple(fields))


def other(label: str) -> Type:
    return Type("OTHER", label=label)


def comparable(t: Type) -> bool:
    """Whether ``<``, ``ORDER BY``, ``MIN``/``MAX`` accept the type."""

    if t.kind in ("ARRAY", "STRUCT", "JSON", "OTHER"):
        return False
    return not t.foreign


def equatable(t: Type) -> bool:
    """Whether ``=`` accepts the type (BigQuery compares structs field by field but never arrays or JSON)."""

    if t.kind == "STRUCT":
        return all(equatable(f) for _, f in t.fields)
    return comparable(t)


def groupable(t: Type) -> bool:
    """``GROUP BY``, ``DISTINCT`` and set operations (arrays and structs of groupable types group in BigQuery)."""

    if t.kind == "ARRAY":
        return groupable(t.elem)
    if t.kind == "STRUCT":
        return all(groupable(f) for _, f in t.fields)
    return t.kind not in ("JSON", "OTHER") and not t.foreign


# --- sqlglot data types ------------------------------------------------------------------------

_DTYPES = {
    "BIGINT": INT64, "INT": INT64, "SMALLINT": INT64, "TINYINT": INT64, "MEDIUMINT": INT64,
    "DOUBLE": FLOAT64, "FLOAT": FLOAT32,
    "DECIMAL": NUMERIC, "BIGDECIMAL": BIGNUMERIC,
    "TEXT": STRING, "VARCHAR": STRING, "CHAR": STRING, "NVARCHAR": STRING,
    "BINARY": BYTES, "VARBINARY": BYTES, "BLOB": BYTES,
    "BOOLEAN": BOOL,
    "DATE": DATE,
    "DATETIME": DATETIME,  # sqlglot 26
    "TIMESTAMP": DATETIME,  # sqlglot 30 reads BigQuery's DATETIME as TIMESTAMP
    "TIMESTAMPTZ": TIMESTAMP,  # ... and BigQuery's TIMESTAMP as TIMESTAMPTZ
    "TIME": TIME,
    "INTERVAL": INTERVAL,
    "JSON": JSON,
}


def from_sqlglot(node: exp.Expression) -> Type:
    """The GoogleSQL type a sqlglot ``DataType`` (BigQuery dialect) names."""

    if not isinstance(node, exp.DataType):
        raise Unsupported(f"type {node.sql('bigquery')}")
    name = node.this.name if hasattr(node.this, "name") else str(node.this)
    if name == "ARRAY":
        if len(node.expressions) != 1:
            raise Unsupported("ARRAY type")
        return array(from_sqlglot(node.expressions[0]))
    if name == "STRUCT":
        fields = []
        for item in node.expressions:
            if isinstance(item, exp.ColumnDef):
                fields.append((item.name, from_sqlglot(item.args["kind"])))
            elif isinstance(item, exp.DataType):
                fields.append((None, from_sqlglot(item)))
            else:
                raise Unsupported("STRUCT field")
        return struct(fields)
    if node.expressions:
        raise Unsupported(f"parameterized type {node.sql('bigquery')}")  # NUMERIC(10, 2) rounds; STRING(3) checks length
    if name in _DTYPES:
        return _DTYPES[name]
    raise Unsupported(f"type {node.sql('bigquery')}")


def parse_type_name(text: str) -> Type:
    """A type written in GoogleSQL (``INT64``, ``ARRAY<STRUCT<a INT64, STRING>>``), as the compliance tests print them."""

    text = text.strip()
    pos = 0

    def skip():
        nonlocal pos
        while pos < len(text) and text[pos].isspace():
            pos += 1

    def word() -> str:
        nonlocal pos
        skip()
        start = pos
        while pos < len(text) and (text[pos].isalnum() or text[pos] in "_."):
            pos += 1
        return text[start:pos]

    def one() -> Type:
        nonlocal pos
        name = word().upper()
        skip()
        if name in ("ARRAY", "STRUCT", "RANGE", "MAP") and pos < len(text) and text[pos] == "<":
            pos += 1
            skip()
            if name == "ARRAY":
                if text[pos] == ">":  # the compliance printer writes ARRAY<> for nested arrays
                    pos += 1
                    return Type("ARRAY", elem=other("?"))
                elem = one()
                skip()
                assert text[pos] == ">", text
                pos += 1
                return Type("ARRAY", elem=elem)
            if name in ("RANGE", "MAP"):
                depth = 1
                start = pos
                while depth:
                    depth += {"<": 1, ">": -1}.get(text[pos], 0)
                    pos += 1
                return other(f"{name}<{text[start:pos - 1]}>")
            fields = []
            while True:
                skip()
                if text[pos] == ">":
                    pos += 1
                    break
                save = pos
                first = word()
                skip()
                if first and pos < len(text) and text[pos] not in ",><" and first.upper() not in (set(BY_NAME) | {"ARRAY", "STRUCT"}):
                    fields.append((first, one()))
                elif first and pos < len(text) and text[pos] not in ",><" and first.upper() in (set(BY_NAME) | {"ARRAY", "STRUCT"}) and \
                        text[pos].isalpha():
                    fields.append((first, one()))  # a field named like a type: "int64 INT64"
                else:
                    pos = save
                    fields.append((None, one()))
                skip()
                if text[pos] == ",":
                    pos += 1
            return struct(fields)
        if name in BY_NAME:
            return BY_NAME[name]
        return other(name)

    result = one()
    return result


# --- coercion ----------------------------------------------------------------------------------

def numeric_rank(t: Type) -> int:
    return NUMERIC_ORDER.index(t.kind) if t.kind in NUMERIC_ORDER else -1


def implicitly_coercible(source: Type, target: Type) -> bool:
    """Coercion of a non-literal value (BigQuery's conversion rules): widening numbers and DATE to DATETIME."""

    if source == target:
        return True
    if source.is_numeric and target.is_numeric:
        return numeric_rank(source) <= numeric_rank(target)
    if source == DATE and target == DATETIME:
        return True
    if source.kind == "STRUCT" and target.kind == "STRUCT" and len(source.fields) == len(target.fields):
        return all(implicitly_coercible(s, t) for (_, s), (_, t) in zip(source.fields, target.fields))
    return False


def supertype(types: list[tuple[Type, str | None]]) -> Type:
    """The common supertype of ``(type, literal kind)`` pairs; literal kind is ``"null"``, ``"literal"`` or ``None``.

    NULL literals take any type; string literals may become a date or time type; numbers widen along
    INT64, NUMERIC, BIGNUMERIC, FLOAT64.
    """

    typed = [(t, k) for t, k in types if k != "null"]
    if not typed:
        return INT64
    candidates = [t for t, _ in typed]
    first = candidates[0]
    if all(t == first for t in candidates):
        return first
    non_literal = [t for t, k in typed if k != "literal"]
    if all(t.is_numeric for t in candidates):
        return max(candidates, key=numeric_rank)
    temporal = {"DATE", "DATETIME", "TIME", "TIMESTAMP"}
    kinds = {t.kind for t in candidates}
    if kinds <= temporal | {"STRING"}:
        strings_literal = all(k == "literal" for t, k in typed if t.kind == "STRING")
        others = {t.kind for t in candidates if t.kind != "STRING"}
        if strings_literal and len(others) == 1:
            return next(t for t in candidates if t.kind != "STRING")
        if strings_literal and others == {"DATE", "DATETIME"}:
            return DATETIME
        if not ("STRING" in kinds) and kinds == {"DATE", "DATETIME"}:
            return DATETIME
    if all(t.kind == "STRUCT" for t in candidates) and len({len(t.fields) for t in candidates}) == 1:
        fields = []
        for i in range(len(first.fields)):
            fields.append((first.fields[i][0], supertype([(t.fields[i][1], None) for t in candidates])))
        result = struct(fields)
        if all(implicitly_coercible(t, result) for t in candidates):
            return result
    if non_literal and all(implicitly_coercible(t, non_literal[0]) for t in candidates):
        return non_literal[0]
    raise AnalysisError("No common supertype for " + ", ".join(str(t) for t in candidates))


__all__ = [name for name in dir() if name.isupper()] + [
    "Type", "array", "struct", "other", "comparable", "equatable", "groupable", "from_sqlglot", "parse_type_name",
    "implicitly_coercible", "supertype", "numeric_rank",
]
