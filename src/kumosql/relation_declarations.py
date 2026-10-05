"""Versioned local records for query-to-query relation declarations.

This is a persistence and schema-resolution layer. A declaration is an explicit premise;
creating one does not prove that its two queries return the same bag, and the preferred side
does not rewrite queries by itself. Consumers must report results as conditional on the
declaration and its scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Mapping
import uuid

import sqlglot
from sqlglot import exp

from . import state

FORMAT_VERSION = 1
MAX_DECLARATIONS = 2000
_LOCK = threading.RLock()


class DeclarationError(ValueError):
    """A rejected declaration with a stable diagnostic code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OutputColumn:
    name: str
    type: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}

    @classmethod
    def from_json(cls, value: object) -> OutputColumn:
        if not isinstance(value, dict):
            raise DeclarationError("invalid_store", "an output column must be an object")
        name, type_name = value.get("name"), value.get("type")
        if not isinstance(name, str) or not name.strip():
            raise DeclarationError("invalid_store", "an output column needs a name")
        if not isinstance(type_name, str) or not type_name.strip():
            raise DeclarationError("invalid_store", f"output column {name!r} needs a known type")
        return cls(name.strip(), _type_key(type_name))


@dataclass(frozen=True)
class Evidence:
    """How a declaration's equivalence premise was obtained."""

    kind: str = "assertion"
    source: str = "user"
    reference: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in {"assertion", "snapshot_validation", "independent_proof"}:
            raise DeclarationError("invalid_evidence", f"unknown evidence kind {self.kind!r}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise DeclarationError("invalid_evidence", "evidence source must be a non-empty string")
        if self.reference is not None and not isinstance(self.reference, str):
            raise DeclarationError("invalid_evidence", "evidence reference must be a string")
        if self.kind != "assertion" and not (isinstance(self.reference, str) and self.reference.strip()):
            raise DeclarationError(
                "invalid_evidence", f"{self.kind} evidence needs a reference to its validation or proof"
            )

    def to_json(self) -> dict[str, str]:
        result = {"kind": self.kind, "source": self.source}
        if self.reference is not None:
            result["reference"] = self.reference
        return result

    @classmethod
    def from_json(cls, value: object) -> Evidence:
        if not isinstance(value, dict):
            raise DeclarationError("invalid_store", "evidence must be an object")
        try:
            return cls(value.get("kind"), value.get("source"), value.get("reference"))
        except DeclarationError as error:
            raise DeclarationError("invalid_store", str(error)) from error


@dataclass(frozen=True)
class Scope:
    """Data-freshness boundary under which the relation premise applies."""

    kind: str = "unspecified"
    reference: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in {
            "unspecified", "all_snapshots", "snapshot", "refresh", "incremental_state"
        }:
            raise DeclarationError("invalid_scope", f"unknown applicability scope {self.kind!r}")
        if self.kind in {"snapshot", "refresh", "incremental_state"} and not (
            isinstance(self.reference, str) and self.reference.strip()
        ):
            raise DeclarationError("invalid_scope", f"{self.kind} scope needs a reference")
        if self.kind in {"unspecified", "all_snapshots"} and self.reference is not None:
            raise DeclarationError("invalid_scope", f"{self.kind} scope does not take a reference")

    def to_json(self) -> dict[str, str]:
        result = {"kind": self.kind}
        if self.reference is not None:
            result["reference"] = self.reference
        return result

    @classmethod
    def from_json(cls, value: object) -> Scope:
        if not isinstance(value, dict):
            raise DeclarationError("invalid_store", "scope must be an object")
        try:
            return cls(value.get("kind"), value.get("reference"))
        except DeclarationError as error:
            raise DeclarationError("invalid_store", str(error)) from error


@dataclass(frozen=True)
class RelationDeclaration:
    id: str
    left_sql: str
    right_sql: str
    preferred_side: str | None
    left_schema: tuple[OutputColumn, ...]
    right_schema: tuple[OutputColumn, ...]
    output_mapping: tuple[tuple[str, str], ...]
    evidence: Evidence
    scope: Scope
    provenance: tuple[tuple[str, str], ...]
    created_at: str

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "left_sql": self.left_sql,
            "right_sql": self.right_sql,
            "preferred_side": self.preferred_side,
            "left_schema": [column.to_json() for column in self.left_schema],
            "right_schema": [column.to_json() for column in self.right_schema],
            "output_mapping": [list(pair) for pair in self.output_mapping],
            "evidence": self.evidence.to_json(),
            "scope": self.scope.to_json(),
            "provenance": dict(self.provenance),
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, value: object) -> RelationDeclaration:
        if not isinstance(value, dict):
            raise DeclarationError("invalid_store", "a declaration must be an object")
        try:
            identifier = str(uuid.UUID(value.get("id")))
        except (ValueError, TypeError, AttributeError) as error:
            raise DeclarationError("invalid_store", "a declaration id must be a UUID") from error
        left_sql, right_sql = value.get("left_sql"), value.get("right_sql")
        if not isinstance(left_sql, str) or not left_sql.strip():
            raise DeclarationError("invalid_store", "left_sql must be a non-empty string")
        if not isinstance(right_sql, str) or not right_sql.strip():
            raise DeclarationError("invalid_store", "right_sql must be a non-empty string")
        preferred = value.get("preferred_side")
        if preferred not in (None, "left", "right"):
            raise DeclarationError("invalid_store", "preferred_side must be left, right or null")
        left = _schema_from_json(value.get("left_schema"))
        right = _schema_from_json(value.get("right_schema"))
        mapping = _mapping_from_json(value.get("output_mapping"))
        _validate_mapping(left, right, mapping)
        raw_provenance = value.get("provenance")
        if not isinstance(raw_provenance, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in raw_provenance.items()
        ):
            raise DeclarationError("invalid_store", "provenance must be a string-to-string object")
        created_at = value.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise DeclarationError("invalid_store", "created_at must be an ISO timestamp")
        try:
            datetime.fromisoformat(created_at)
        except ValueError as error:
            raise DeclarationError("invalid_store", "created_at must be an ISO timestamp") from error
        return cls(
            identifier,
            left_sql.strip(),
            right_sql.strip(),
            preferred,
            left,
            right,
            mapping,
            Evidence.from_json(value.get("evidence")),
            Scope.from_json(value.get("scope")),
            tuple(sorted(raw_provenance.items())),
            created_at,
        )


def _schema_from_json(value: object) -> tuple[OutputColumn, ...]:
    if not isinstance(value, list):
        raise DeclarationError("invalid_store", "resolved schema must be a list")
    columns = tuple(OutputColumn.from_json(item) for item in value)
    _unique_names(columns, "invalid_store")
    return columns


def _mapping_from_json(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise DeclarationError("invalid_store", "output_mapping must be a list")
    pairs = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or any(not isinstance(name, str) for name in item):
            raise DeclarationError("invalid_store", "each output mapping must be a pair of names")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _type_key(value: str) -> str:
    return " ".join(value.upper().split())


def _unique_names(columns: tuple[OutputColumn, ...], code: str) -> None:
    seen = set()
    for column in columns:
        name = column.name.casefold()
        if name in seen:
            raise DeclarationError(code, f"duplicate output name {column.name!r}")
        seen.add(name)


def _validate_mapping(
    left: tuple[OutputColumn, ...],
    right: tuple[OutputColumn, ...],
    mapping: tuple[tuple[str, str], ...],
) -> None:
    left_by_name = {column.name.casefold(): column for column in left}
    right_by_name = {column.name.casefold(): column for column in right}
    if set(left_by_name) != set(right_by_name):
        raise DeclarationError("invalid_store", "resolved schemas and output mapping do not contain the same names")
    if len(mapping) != len(left) or len({a.casefold() for a, _ in mapping}) != len(mapping):
        raise DeclarationError("invalid_store", "output_mapping must map every output exactly once")
    for left_name, right_name in mapping:
        lkey, rkey = left_name.casefold(), right_name.casefold()
        if lkey not in left_by_name or rkey not in right_by_name or lkey != rkey:
            raise DeclarationError("invalid_store", "output_mapping must align columns by their names")
        if left_by_name[lkey].type != right_by_name[rkey].type:
            raise DeclarationError("invalid_store", f"mapped output {left_name!r} has incompatible types")


@dataclass(frozen=True)
class _Source:
    table_key: str
    alias: str
    explicit_alias: bool
    columns: tuple[OutputColumn, ...]

    @property
    def qualifiers(self) -> set[str]:
        if self.explicit_alias:
            return {self.alias.casefold()}
        return {self.alias.casefold(), self.table_key.casefold(), self.table_key.rsplit(".", 1)[-1].casefold()}


def _table_key(table: exp.Table) -> str:
    return ".".join(part.name.casefold() for part in table.parts)


def _table_schema(table: exp.Table, schemas: Mapping[str, Mapping[str, str]]) -> tuple[OutputColumn, ...]:
    key = _table_key(table)
    normalized = {
        ".".join(part.strip().strip("`").strip('"').casefold() for part in str(name).split(".")): value
        for name, value in schemas.items()
    }
    matches = [
        (candidate, value)
        for candidate, value in normalized.items()
        if candidate == key or candidate.endswith("." + key) or key.endswith("." + candidate)
    ]
    if len(matches) != 1:
        if not matches:
            raise DeclarationError("unknown_table_schema", f"no known schema for relation {key!r}")
        raise DeclarationError("ambiguous_table_schema", f"more than one known schema matches relation {key!r}")
    _, columns = matches[0]
    if not isinstance(columns, Mapping):
        raise DeclarationError("invalid_table_schema", f"schema for relation {key!r} must map columns to types")
    result = []
    for name, type_name in columns.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(type_name, str) or not type_name.strip():
            raise DeclarationError("invalid_table_schema", f"schema for relation {key!r} has an invalid column")
        result.append(OutputColumn(name.strip(), _type_key(type_name)))
    resolved = tuple(result)
    _unique_names(resolved, "duplicate_table_column")
    return resolved


def _source_alias(table: exp.Table) -> str:
    return (table.alias or table.name).casefold()


def _sources(select: exp.Select, schemas: Mapping[str, Mapping[str, str]]) -> list[_Source]:
    from_clause = select.args.get("from_")
    if from_clause is None:
        raise DeclarationError("missing_from", "query output schema needs a FROM relation")
    relations = [from_clause.this, *(from_clause.expressions or [])]
    relations.extend(join.this for join in select.args.get("joins") or [])
    result = []
    aliases = set()
    for relation in relations:
        if not isinstance(relation, exp.Table) or not isinstance(relation.this, exp.Identifier):
            raise DeclarationError("unsupported_source", "schema resolution currently requires direct table relations")
        alias = _source_alias(relation)
        if alias in aliases:
            raise DeclarationError("duplicate_relation_alias", f"relation alias {alias!r} is used twice")
        aliases.add(alias)
        result.append(_Source(_table_key(relation), alias, bool(relation.alias), _table_schema(relation, schemas)))
    return result


def _find_source(qualifier: str, sources: list[_Source]) -> _Source:
    hits = [source for source in sources if qualifier.casefold() in source.qualifiers]
    if len(hits) != 1:
        raise DeclarationError("unknown_relation_alias", f"cannot resolve relation qualifier {qualifier!r}")
    return hits[0]


def _column_type(column: exp.Column, sources: list[_Source]) -> str:
    if column.table:
        source = _find_source(column.table, sources)
        matches = [item for item in source.columns if item.name.casefold() == column.name.casefold()]
    else:
        matches = [
            item
            for source in sources
            for item in source.columns
            if item.name.casefold() == column.name.casefold()
        ]
    if not matches:
        raise DeclarationError("unknown_column", f"cannot resolve output column {column.name!r}")
    if len(matches) != 1:
        raise DeclarationError("ambiguous_column", f"output column {column.name!r} appears in multiple relations")
    return matches[0].type


def _star_sources(column: exp.Expression, sources: list[_Source]) -> list[_Source]:
    star = column.this if isinstance(column, exp.Column) else column
    if not isinstance(star, exp.Star):
        raise DeclarationError("unsupported_projection", "only a direct column or a star can be resolved without type inference")
    qualifier = column.table if isinstance(column, exp.Column) else star.args.get("table")
    return [_find_source(qualifier, sources)] if qualifier else sources


def _expand_star(column: exp.Expression, sources: list[_Source]) -> list[OutputColumn]:
    star = column.this if isinstance(column, exp.Column) else column
    assert isinstance(star, exp.Star)
    if star.args.get("replace"):
        raise DeclarationError("unsupported_star_replace", "SELECT * REPLACE needs expression type inference")
    except_items = star.args.get("except_") or []
    excluded = set()
    for item in except_items:
        if not isinstance(item, exp.Column):
            raise DeclarationError("invalid_star_except", "SELECT * EXCEPT entries must be column names")
        name = item.name.casefold()
        if name in excluded:
            raise DeclarationError("duplicate_star_except", f"column {item.name!r} is excluded twice")
        excluded.add(name)
    targets = _star_sources(column, sources)
    available = [item for source in targets for item in source.columns]
    names = {item.name.casefold() for item in available}
    missing = excluded - names
    if missing:
        raise DeclarationError("unknown_star_except", f"SELECT * EXCEPT names unknown column {sorted(missing)[0]!r}")
    return [item for item in available if item.name.casefold() not in excluded]


def resolve_output_schema(
    sql: str,
    schemas: Mapping[str, Mapping[str, str]],
    *,
    dialect: str = "bigquery",
) -> tuple[OutputColumn, ...]:
    """Resolve direct projections and stars using caller-supplied table schemas.

    Computed outputs are deliberately rejected until the project type resolver can provide
    their types. This prevents declarations from silently treating unknown or coerced types
    as compatible.
    """

    try:
        query = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as error:
        raise DeclarationError("parse_error", f"query could not be parsed: {error}") from error
    if not isinstance(query, exp.Select) or query.args.get("with_"):
        raise DeclarationError("unsupported_query", "schema resolution currently requires a SELECT without CTEs")
    sources = _sources(query, schemas)
    output = []
    for expression in query.expressions:
        alias = expression.alias if isinstance(expression, exp.Alias) else ""
        value = expression.this if isinstance(expression, exp.Alias) else expression
        if isinstance(value, exp.Star) or (isinstance(value, exp.Column) and isinstance(value.this, exp.Star)):
            output.extend(_expand_star(value, sources))
        elif isinstance(value, exp.Column):
            output.append(OutputColumn(alias or value.name, _column_type(value, sources)))
        else:
            raise DeclarationError(
                "unknown_expression_type",
                f"cannot resolve the type of output expression {value.sql(dialect=dialect)!r}",
            )
    resolved = tuple(output)
    if not resolved:
        raise DeclarationError("empty_output_schema", "query has no resolved output columns")
    _unique_names(resolved, "duplicate_output_name")
    return resolved


def _mapping(
    left: tuple[OutputColumn, ...], right: tuple[OutputColumn, ...]
) -> tuple[tuple[str, str], ...]:
    left_by_name = {column.name.casefold(): column for column in left}
    right_by_name = {column.name.casefold(): column for column in right}
    if set(left_by_name) != set(right_by_name):
        left_only = sorted(left_by_name.keys() - right_by_name.keys())
        right_only = sorted(right_by_name.keys() - left_by_name.keys())
        raise DeclarationError(
            "output_names_mismatch",
            f"query outputs differ by name (left only: {left_only}; right only: {right_only})",
        )
    pairs = []
    for column in left:
        other = right_by_name[column.name.casefold()]
        if column.type != other.type:
            raise DeclarationError(
                "incompatible_output_types",
                f"output {column.name!r} has incompatible types {column.type!r} and {other.type!r}",
            )
        pairs.append((column.name, other.name))
    return tuple(pairs)


def _side_key(sql: str) -> str:
    try:
        return sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery", pretty=False)
    except sqlglot.errors.ParseError as error:
        raise DeclarationError("parse_error", f"query could not be parsed: {error}") from error


def prepare(
    left_sql: str,
    right_sql: str,
    schemas: Mapping[str, Mapping[str, str]],
    *,
    preferred_side: str | None = None,
    evidence: Evidence | None = None,
    scope: Scope | None = None,
    provenance: Mapping[str, str] | None = None,
    declaration_id: str | None = None,
) -> RelationDeclaration:
    """Resolve, align and package an explicit query-to-query relation premise."""

    if not isinstance(left_sql, str) or not left_sql.strip() or not isinstance(right_sql, str) or not right_sql.strip():
        raise DeclarationError("empty_query", "both query sides must be non-empty strings")
    if preferred_side not in (None, "left", "right"):
        raise DeclarationError("invalid_preference", "preferred_side must be left, right or null")
    left_sql, right_sql = left_sql.strip(), right_sql.strip()
    if _side_key(left_sql) == _side_key(right_sql):
        raise DeclarationError("same_query", "the two sides resolve to the same query")
    left = resolve_output_schema(left_sql, schemas)
    right = resolve_output_schema(right_sql, schemas)
    mapping = _mapping(left, right)
    try:
        identifier = str(uuid.UUID(declaration_id)) if declaration_id else str(uuid.uuid4())
    except (ValueError, TypeError, AttributeError) as error:
        raise DeclarationError("invalid_id", "declaration_id must be a UUID") from error
    provenance = provenance or {"created_by": "user"}
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in provenance.items()):
        raise DeclarationError("invalid_provenance", "provenance must map strings to strings")
    evidence = evidence or Evidence()
    scope = scope or Scope()
    if not isinstance(evidence, Evidence) or not isinstance(scope, Scope):
        raise DeclarationError("invalid_metadata", "evidence and scope must use their typed records")
    return RelationDeclaration(
        identifier,
        left_sql,
        right_sql,
        preferred_side,
        left,
        right,
        mapping,
        evidence,
        scope,
        tuple(sorted(provenance.items())),
        datetime.now(timezone.utc).isoformat(),
    )


def _path() -> Path:
    return state.data_path("relation_declarations.json")


def _read_unlocked() -> list[RelationDeclaration]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as error:
        raise DeclarationError("invalid_store", f"could not read relation declarations: {error}") from error
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw.get("version") != FORMAT_VERSION
        or not isinstance(raw.get("declarations"), list)
    ):
        raise DeclarationError("unsupported_store_version", "relation declaration store has an unsupported format")
    items = [RelationDeclaration.from_json(item) for item in raw["declarations"]]
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise DeclarationError("invalid_store", "relation declaration IDs must be unique")
    _validate_preference_graph(items)
    return items


def load() -> list[RelationDeclaration]:
    """Load the versioned local declaration records without rewriting them."""

    with _LOCK, state._file_lock():
        return _read_unlocked()


def _reaches(edges: Mapping[str, set[str]], start: str, goal: str) -> bool:
    pending, seen = [start], set()
    while pending:
        current = pending.pop()
        if current == goal:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(edges.get(current, ()))
    return False


def _validate_preference_graph(items: list[RelationDeclaration]) -> None:
    edges: dict[str, set[str]] = {}
    for item in items:
        if item.preferred_side is None:
            continue
        lhs, rhs = _side_key(item.left_sql), _side_key(item.right_sql)
        if lhs == rhs:
            raise DeclarationError("preference_cycle", "a query cannot prefer itself")
        if item.preferred_side == "left":
            edges.setdefault(rhs, set()).add(lhs)
        else:
            edges.setdefault(lhs, set()).add(rhs)
    for source, targets in edges.items():
        if any(_reaches(edges, target, source) for target in targets):
            raise DeclarationError("preference_cycle", "preferred representations would create a rewrite cycle")


def _check_preference(items: list[RelationDeclaration], new: RelationDeclaration) -> None:
    _validate_preference_graph([*items, new])


def _write_unlocked(items: list[RelationDeclaration]) -> None:
    path = _path()
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".relation-declarations-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(
                {"version": FORMAT_VERSION, "declarations": [item.to_json() for item in items]},
                output,
                indent=2,
                sort_keys=True,
            )
            output.write("\n")
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def add(declaration: RelationDeclaration) -> RelationDeclaration:
    """Persist a prepared declaration, rejecting duplicate IDs and preference cycles."""

    if not isinstance(declaration, RelationDeclaration):
        raise DeclarationError("invalid_declaration", "add expects a prepared RelationDeclaration")
    try:
        declaration = RelationDeclaration.from_json(declaration.to_json())
    except (AttributeError, TypeError) as error:
        raise DeclarationError("invalid_declaration", "declaration is not a valid prepared record") from error
    except DeclarationError as error:
        raise DeclarationError("invalid_declaration", str(error)) from error
    with _LOCK, state._file_lock():
        items = _read_unlocked()
        if any(item.id == declaration.id for item in items):
            raise DeclarationError("duplicate_id", f"declaration {declaration.id} already exists")
        if len(items) >= MAX_DECLARATIONS:
            raise DeclarationError("too_many_declarations", "too many relation declarations")
        _check_preference(items, declaration)
        items.append(declaration)
        _write_unlocked(items)
    return declaration


def declare(*args, **kwargs) -> RelationDeclaration:
    """Prepare and persist a declaration with output schemas resolved immediately."""

    return add(prepare(*args, **kwargs))


def get(declaration_id: str) -> RelationDeclaration | None:
    try:
        identifier = str(uuid.UUID(declaration_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise DeclarationError("invalid_id", "declaration_id must be a UUID") from error
    return next((item for item in load() if item.id == identifier), None)


def remove(declaration_id: str) -> bool:
    """Revoke a declaration by stable ID; consumers can detect the missing premise."""

    try:
        identifier = str(uuid.UUID(declaration_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise DeclarationError("invalid_id", "declaration_id must be a UUID") from error
    with _LOCK, state._file_lock():
        items = _read_unlocked()
        kept = [item for item in items if item.id != identifier]
        if len(kept) == len(items):
            return False
        _write_unlocked(kept)
    return True
