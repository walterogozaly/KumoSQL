"""Value types shared by the pipeline modules."""

from __future__ import annotations

from dataclasses import dataclass

from .identity import NodeIdentity


@dataclass(frozen=True, order=True)
class Target:
    """A BigQuery table identity; empty parts are unknown."""

    database: str = ""
    schema: str = ""
    name: str = ""

    @property
    def key(self) -> str:
        return ".".join(part for part in (self.database, self.schema, self.name) if part)

    def sql(self) -> str:
        return f"`{self.key}`"


@dataclass(frozen=True, order=True)
class ColumnRef:
    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class ColumnLineage:
    """How one model output column is built, one hop back.

    ``status`` is ``traced`` (built from ``sources``), ``constant`` (checked
    to read no column at all, such as a literal or ``COUNT(*)``) or
    ``unknown`` (could not be traced; ``reason`` says why and ``sources``
    holds only what was resolved before the trace gave up). ``transform`` is
    ``passthrough``, ``renamed``, ``expression``, ``aggregate``, ``window``,
    ``union``, ``constant`` or ``unknown``.
    """

    column: "ColumnRef"
    sources: frozenset["ColumnRef"]
    status: str
    transform: str
    reason: str | None = None


@dataclass(frozen=True)
class ColumnTrace:
    """Everything upstream of one column, with untraceable parts kept apart."""

    column: "ColumnRef"
    upstream: frozenset["ColumnRef"]
    # Columns the trace ended on that are not produced by any pipeline model.
    sources: frozenset["ColumnRef"]
    # Columns on the way whose own inputs could not be traced, with the reason.
    unknown: tuple[tuple["ColumnRef", str], ...]

    @property
    def complete(self) -> bool:
        return not self.unknown


@dataclass
class Model:
    """One pipeline node: a table, view, incremental table, assertion or operation."""

    target: Target
    kind: str
    sql: str
    path: str | None = None
    declared_dependencies: tuple[Target, ...] = ()
    # Dataform expressions masked out of ``sql``; identifiers in them count as reads.
    masked_expressions: tuple[str, ...] = ()
    # Dataform tags from the config block; workflow configurations select actions by them.
    tags: tuple[str, ...] = ()
    # Dataform ``assertions`` of the config block: columns that are never NULL and
    # sets of columns that are unique. Declared by the project, checked when its assertions run.
    non_null: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    # Dataform ``pre_operations`` and ``post_operations`` statements, with refs resolved: scripts that run around the query.
    operations_sql: tuple[str, ...] = ()
    # How many leading entries of ``operations_sql`` run before the query; the rest run after it.
    pre_operations: int = 0

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def identity(self) -> NodeIdentity | None:
        """Identity of the output table, falling back to the asset path."""

        if self.target.key:
            return NodeIdentity.for_target(self.target.database, self.target.schema, self.target.name)
        return self.asset_identity

    @property
    def asset_identity(self) -> NodeIdentity | None:
        return NodeIdentity.for_asset(self.path) if self.path else None

    @property
    def is_query(self) -> bool:
        return self.kind in {"table", "view", "incremental", "assertion", "sql"}


@dataclass(frozen=True)
class PipelineDiagnostic:
    model: str
    code: str
    message: str


@dataclass(frozen=True)
class DuplicateOccurrence:
    model: str
    location: str  # "query", "cte:<name>" or "subquery:<alias>"


@dataclass(frozen=True)
class DuplicateGroup:
    fingerprint: str
    node_count: int
    sql: str
    occurrences: tuple[DuplicateOccurrence, ...]
