"""Whole-pipeline analysis for Dataform projects and folders of BigQuery SQL.

A pipeline is loaded into a model dependency graph, then every model is
qualified in dependency order so that each downstream model sees the output
columns of the models it reads. From that, the module derives:

* column lineage: each output column mapped to the upstream columns it is
  computed from, with transitive closure across the whole pipeline;
* column consumption: every upstream column a model reads anywhere (SELECT,
  WHERE, JOIN, GROUP BY, ...), used to find dead columns;
* duplicate logic: identical normalized SELECT subtrees that occur in more
  than one place, which are candidates for a shared model.

Everything here is offline and read-only. Anything the analysis cannot see
(an unparseable model, a ``SELECT *`` over a source with no known schema) is
reported as a diagnostic, and dead-column results are withheld for tables
whose consumers could not be fully analysed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from .ast_utils import quiet_parser as _quiet_parser
from .sqlx import (
    mask_sqlx_interpolations as _mask_sqlx_interpolations,
    split_sqlx_sections as _split_sqlx_sections,
)

if TYPE_CHECKING:
    from .near_duplicates import NearDuplicateCluster


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

    @property
    def key(self) -> str:
        return self.target.key

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


@dataclass
class Pipeline:
    models: dict[str, Model]
    sources: dict[str, Target] = field(default_factory=dict)
    source_schema: dict[str, dict[str, str]] = field(default_factory=dict)
    diagnostics: list[PipelineDiagnostic] = field(default_factory=list)

    # ------------------------------------------------------------------ graph

    def __post_init__(self) -> None:
        self._resolver = _TargetResolver([*self.models, *self.sources])
        self._analysis: _Analysis | None = None

    def resolve(self, table: exp.Table | str) -> str | None:
        """Map a table reference to a model or declared source key."""

        return self._resolver.resolve(table)

    @property
    def upstream(self) -> dict[str, set[str]]:
        return self._analyse().upstream

    @property
    def downstream(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {key: set() for key in self.models}
        for key, parents in self.upstream.items():
            for parent in parents:
                result.setdefault(parent, set()).add(key)
        return result

    def topological_order(self) -> list[str]:
        return self._analyse().order

    # ---------------------------------------------------------------- columns

    def output_columns(self, model: str) -> tuple[str, ...]:
        return self._analyse().outputs.get(model, ())

    def column_lineage(self) -> dict[ColumnRef, frozenset[ColumnRef]]:
        """Direct lineage: each model output column to the columns it is built from."""

        return dict(self._analyse().lineage)

    def upstream_columns(self, column: ColumnRef) -> frozenset[ColumnRef]:
        """Every column, across all models and sources, that feeds ``column``."""

        return _closure(column, self._analyse().lineage)

    def downstream_columns(self, column: ColumnRef) -> frozenset[ColumnRef]:
        """Every model column, across the pipeline, computed from ``column``."""

        return _closure(column, self._analyse().reverse_lineage)

    def consumed_columns(self) -> dict[str, frozenset[ColumnRef]]:
        """For each model, every upstream column it references anywhere."""

        return dict(self._analyse().consumed)

    def dead_columns(self) -> dict[str, tuple[str, ...]]:
        """Output columns of intermediate models that no downstream model reads.

        Terminal models (nothing downstream) are treated as pipeline outputs
        and never reported. A model is skipped when any consumer could not be
        analysed, because an unseen reader might use any column.
        """

        analysis = self._analyse()
        downstream = self.downstream
        result: dict[str, tuple[str, ...]] = {}
        for key in analysis.order:
            readers = downstream.get(key, set())
            if analysis.blind or not readers or key in analysis.opaque_readers_of:
                continue
            outputs = analysis.outputs.get(key)
            if not outputs:
                continue
            used = {
                ref.column.lower()
                for reader in readers
                for ref in analysis.consumed.get(reader, ())
                if ref.table == key
            }
            dead = tuple(column for column in outputs if column.lower() not in used)
            if dead:
                result[key] = dead
        return result

    # ------------------------------------------------------------- duplicates

    def duplicate_selects(self, *, min_nodes: int = 12) -> list[DuplicateGroup]:
        """Identical normalized SELECT subtrees occurring in two or more places.

        Only maximal duplicates are returned: a group is dropped when every
        occurrence sits inside an occurrence of a larger reported group.
        """

        return _find_duplicates(self._analyse().parsed, min_nodes=min_nodes)

    def near_duplicate_selects(
        self, *, min_nodes: int = 12, threshold: float = 0.7
    ) -> list["NearDuplicateCluster"]:
        """Clusters of similar, not identical, SELECTs, with their differences.

        See :mod:`kumosql.near_duplicates`.
        """

        from .near_duplicates import find_near_duplicates

        return find_near_duplicates(self._analyse().parsed, min_nodes=min_nodes, threshold=threshold)

    def all_diagnostics(self) -> list[PipelineDiagnostic]:
        return [*self.diagnostics, *self._analyse().diagnostics]

    def report(self, *, min_nodes: int = 12, similarity: float = 0.7) -> dict:
        """A JSON-serialisable summary of the whole-pipeline analysis."""

        return {
            "models": len(self.models),
            "sources": sorted(self.sources),
            "order": self.topological_order(),
            "upstream": {key: sorted(value) for key, value in sorted(self.upstream.items())},
            "dead_columns": {key: list(value) for key, value in self.dead_columns().items()},
            "duplicates": [
                {
                    "fingerprint": group.fingerprint,
                    "node_count": group.node_count,
                    "occurrences": [f"{o.model} ({o.location})" for o in group.occurrences],
                    "sql": group.sql,
                }
                for group in self.duplicate_selects(min_nodes=min_nodes)
            ],
            "near_duplicates": [
                cluster.to_json()
                for cluster in self.near_duplicate_selects(min_nodes=min_nodes, threshold=similarity)
            ],
            "diagnostics": [
                {"model": d.model, "code": d.code, "message": d.message}
                for d in self.all_diagnostics()
            ],
        }

    def _analyse(self) -> "_Analysis":
        if self._analysis is None:
            self._analysis = _Analysis.run(self)
        return self._analysis


# --------------------------------------------------------------------- loading


_REF_RE = re.compile(r"\$\{\s*ref\(\s*(?P<args>[^()]*?)\s*\)\s*\}")
_SELF_RE = re.compile(r"\$\{\s*self\(\s*\)\s*\}")
_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")


def _config_value(config: str, key: str) -> str | None:
    match = re.search(rf"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", config)
    return match.group(2) if match else None


def _read_project_defaults(root: Path) -> tuple[str, str]:
    """Return (default project, default dataset) from Dataform settings, if any."""

    settings = root / "workflow_settings.yaml"
    if settings.is_file():
        text = settings.read_text(encoding="utf-8")

        def value(key: str) -> str:
            match = re.search(rf"(?m)^\s*{key}\s*:\s*['\"]?([^'\"\s#]+)", text)
            return match.group(1) if match else ""

        return value("defaultProject"), value("defaultDataset")
    legacy = root / "dataform.json"
    if legacy.is_file():
        data = json.loads(legacy.read_text(encoding="utf-8"))
        return data.get("defaultDatabase", ""), data.get("defaultSchema", "")
    return "", ""


def _parse_ref_args(args: str, default: Target) -> Target:
    if args.lstrip().startswith("{"):
        name = _config_value(args, "name") or ""
        schema = _config_value(args, "schema") or default.schema
        database = _config_value(args, "database") or default.database
        return Target(database, schema, name)
    parts = [match.group(2) for match in _STRING_RE.finditer(args)]
    if len(parts) == 1:
        return Target(default.database, default.schema, parts[0])
    if len(parts) == 2:
        return Target(default.database, parts[0], parts[1])
    if len(parts) >= 3:
        return Target(parts[0], parts[1], parts[2])
    raise ValueError(f"unsupported ref() arguments: {args!r}")


def load_sqlx_project(
    root: str | Path,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
) -> Pipeline:
    """Load a Dataform project (``definitions/**.sqlx``) or a folder of ``.sql`` files.

    ``${ref(...)}`` and ``${self()}`` are resolved to table names using the
    project defaults; other interpolations are masked so the SQL still
    parses. For exact compiled SQL, prefer :func:`load_compiled_graph` with
    the output of ``dataform compile --json``.
    """

    root = Path(root)
    database, dataset = _read_project_defaults(root)
    search_root = root / "definitions" if (root / "definitions").is_dir() else root
    models: dict[str, Model] = {}
    sources: dict[str, Target] = {}
    diagnostics: list[PipelineDiagnostic] = []

    for path in sorted([*search_root.rglob("*.sqlx"), *search_root.rglob("*.sql")]):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(root))
        if path.suffix == ".sql":
            target = Target(name=path.stem)
            models[target.key] = Model(target, "sql", text, relative)
            continue
        try:
            sections = _split_sqlx_sections(text)
        except ValueError as exc:
            diagnostics.append(PipelineDiagnostic(relative, "sqlx_parse_error", str(exc)))
            continue
        config = next(
            (body for kind, body in sections if kind == "block" and body.lstrip().startswith("config")),
            "",
        )
        kind = _config_value(config, "type") or "table"
        target = Target(
            _config_value(config, "database") or database,
            _config_value(config, "schema") or dataset,
            _config_value(config, "name") or path.stem,
        )
        if kind == "declaration":
            sources[target.key] = target
            continue
        default = Target(database, dataset, "")
        body = "".join(section for kind_, section in sections if kind_ == "sql")
        dependencies: list[Target] = []

        def substitute(match: re.Match[str]) -> str:
            ref = _parse_ref_args(match.group("args"), default)
            dependencies.append(ref)
            return ref.sql()

        try:
            body = _REF_RE.sub(substitute, body)
        except ValueError as exc:
            diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
        body = _SELF_RE.sub(target.sql(), body)
        masked: tuple[str, ...] = ()
        if "${" in body:
            body, restorations = _mask_sqlx_interpolations(body)
            masked = tuple(item.original for item in restorations)
        models[target.key] = Model(target, kind, body, relative, tuple(dependencies), masked)

    return Pipeline(models, sources, dict(source_schema or {}), diagnostics)


def load_compiled_graph(
    graph: str | Path | dict,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
) -> Pipeline:
    """Load the JSON printed by ``dataform compile --json``."""

    if isinstance(graph, (str, Path)):
        graph = json.loads(Path(graph).read_text(encoding="utf-8"))

    def target_of(raw: dict) -> Target:
        return Target(raw.get("database", ""), raw.get("schema", ""), raw.get("name", ""))

    models: dict[str, Model] = {}
    for kind_key, default_kind in (("tables", "table"), ("assertions", "assertion"), ("operations", "operations")):
        for item in graph.get(kind_key, []):
            target = target_of(item.get("target", {}))
            sql = item.get("query")
            if sql is None:
                sql = ";\n".join(item.get("queries", []))
            kind = item.get("type", default_kind) if kind_key == "tables" else default_kind
            models[target.key] = Model(
                target,
                kind,
                sql,
                item.get("fileName"),
                tuple(target_of(dep) for dep in item.get("dependencyTargets", [])),
            )
    sources = {
        target.key: target
        for target in (target_of(item.get("target", {})) for item in graph.get("declarations", []))
    }
    return Pipeline(models, sources, dict(source_schema or {}))


# -------------------------------------------------------------------- analysis


class _TargetResolver:
    """Resolve full, dataset-qualified or bare table names to known keys."""

    def __init__(self, keys: list[str]):
        self._by_suffix: dict[str, set[str]] = defaultdict(set)
        for key in keys:
            parts = key.lower().split(".")
            for start in range(len(parts)):
                self._by_suffix[".".join(parts[start:])].add(key)

    def resolve(self, table: exp.Table | str) -> str | None:
        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.lower().split(".")
        # Try the most specific spelling first, then drop leading qualifiers.
        for start in range(len(parts)):
            matches = self._by_suffix.get(".".join(parts[start:]))
            if matches and len(matches) == 1:
                return next(iter(matches))
            if matches:
                return None
        return None


def _parse_model(sql: str) -> exp.Expression | None:
    with _quiet_parser():
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    queries = [s for s in statements if isinstance(s, exp.Query)]
    return queries[-1] if queries else None


def _nested_schema(flat: dict[str, dict[str, str]]) -> dict:
    """Turn ``{"p.d.t": {...}}`` into sqlglot's nested catalog/db/table mapping."""

    nested: dict = {}
    for key, columns in flat.items():
        parts = key.split(".")
        parts = [""] * (3 - len(parts)) + parts if len(parts) < 3 else parts[-3:]
        nested.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = columns
    return nested


def _source_table(scope: Scope, column: exp.Column) -> exp.Table | None:
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(column.table)
        if isinstance(source, exp.Table):
            return source
        if source is not None:
            return None
        current = current.parent
    return None


def _has_unexpanded_star(query: exp.Expression) -> bool:
    return any(
        isinstance(node, exp.Star) and isinstance(node.parent, (exp.Select, exp.Column))
        for node in query.walk()
    )


def _table_name_for_schema(table: exp.Table) -> str:
    return ".".join(part.name for part in table.parts)


@dataclass
class _Analysis:
    upstream: dict[str, set[str]]
    order: list[str]
    parsed: dict[str, exp.Expression]
    outputs: dict[str, tuple[str, ...]]
    lineage: dict[ColumnRef, frozenset[ColumnRef]]
    reverse_lineage: dict[ColumnRef, frozenset[ColumnRef]]
    consumed: dict[str, frozenset[ColumnRef]]
    opaque_readers_of: set[str]
    # True when some model's reads are unknown entirely (unparseable, with no
    # declared dependencies), so no column can be called dead.
    blind: bool
    diagnostics: list[PipelineDiagnostic]

    @classmethod
    def run(cls, pipeline: Pipeline) -> "_Analysis":
        diagnostics: list[PipelineDiagnostic] = []
        parsed: dict[str, exp.Expression] = {}
        upstream: dict[str, set[str]] = {}
        unresolved_tables: dict[str, set[str]] = {}
        blind_models: list[str] = []

        for key, model in pipeline.models.items():
            parents = {
                resolved
                for dep in model.declared_dependencies
                if (resolved := pipeline.resolve(dep.key)) and resolved != key
            }
            if model.is_query:
                try:
                    query = _parse_model(model.sql)
                except Exception as exc:  # sqlglot raises several error types
                    query = None
                    diagnostics.append(PipelineDiagnostic(key, "parse_error", str(exc).splitlines()[0]))
                if query is not None:
                    parsed[key] = query
                    cte_names = {
                        cte.alias_or_name.lower() for cte in query.find_all(exp.CTE)
                    }
                    for table in query.find_all(exp.Table):
                        if not table.db and table.name.lower() in cte_names:
                            continue
                        resolved = pipeline.resolve(table)
                        if resolved and resolved != key:
                            parents.add(resolved)
                        elif resolved is None and table.name:
                            unresolved_tables.setdefault(key, set()).add(
                                _table_name_for_schema(table)
                            )
                elif model.sql.strip():
                    diagnostics.append(PipelineDiagnostic(key, "no_query", "model has no parseable query"))
            if key not in parsed and model.sql.strip() and not model.declared_dependencies:
                blind_models.append(key)
            upstream[key] = parents

        order = _topological_order(upstream, diagnostics)

        outputs: dict[str, tuple[str, ...]] = {}
        direct: dict[ColumnRef, frozenset[ColumnRef]] = {}
        consumed: dict[str, frozenset[ColumnRef]] = {}
        opaque_readers_of: set[str] = set()
        schema: dict[str, dict[str, str]] = dict(pipeline.source_schema)

        for key in order:
            query = parsed.get(key)
            if query is None:
                opaque_readers_of.update(upstream.get(key, ()))
                continue
            try:
                qualified = qualify(
                    query.copy(),
                    schema=_nested_schema(schema),
                    dialect="bigquery",
                    validate_qualify_columns=False,
                    quote_identifiers=False,
                )
            except Exception as exc:
                diagnostics.append(PipelineDiagnostic(key, "qualify_error", str(exc).splitlines()[0]))
                opaque_readers_of.update(upstream.get(key, ()))
                continue

            if _has_unexpanded_star(qualified):
                diagnostics.append(
                    PipelineDiagnostic(
                        key,
                        "unexpanded_star",
                        "SELECT * over a table with unknown columns; readers of this model treat it as opaque",
                    )
                )
                opaque_readers_of.update(upstream.get(key, ()))

            # Drop CTE and subquery columns nothing reads (``SELECT *`` in a
            # CTE otherwise counts every column as used), then collect reads.
            try:
                pruned = pushdown_projections(qualified.copy())
            except Exception:
                pruned = qualified
            used: set[ColumnRef] = set()
            words = {
                word.lower()
                for text in pipeline.models[key].masked_expressions
                for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
            }
            if words:
                for parent in upstream.get(key, ()):
                    for column in outputs.get(parent, ()):
                        if column.lower() in words:
                            used.add(ColumnRef(parent, column))
            for scope in traverse_scope(pruned):
                for column in scope.columns:
                    table = _source_table(scope, column)
                    if table is None:
                        continue
                    owner = pipeline.resolve(table) or _table_name_for_schema(table)
                    used.add(ColumnRef(owner, column.name))
            consumed[key] = frozenset(used)

            names = tuple(qualified.named_selects)
            outputs[key] = names
            if names and "*" not in names:
                schema[key] = {name: "UNKNOWN" for name in names}
            for name in names:
                try:
                    node = lineage(name, qualified, dialect="bigquery")
                except Exception as exc:
                    diagnostics.append(
                        PipelineDiagnostic(key, "lineage_error", f"{name}: {str(exc).splitlines()[0]}")
                    )
                    continue
                leaves: set[ColumnRef] = set()
                for item in node.walk():
                    if item.downstream or not isinstance(item.source, exp.Table):
                        continue
                    owner = pipeline.resolve(item.source) or _table_name_for_schema(item.source)
                    leaves.add(ColumnRef(owner, item.name.split(".")[-1].strip('"`')))
                direct[ColumnRef(key, name)] = frozenset(leaves)

        reverse: dict[ColumnRef, set[ColumnRef]] = defaultdict(set)
        for column, parents in direct.items():
            for parent in parents:
                reverse[parent].add(column)

        for key, tables in sorted(unresolved_tables.items()):
            unknown = sorted(t for t in tables if t not in schema)
            if unknown:
                diagnostics.append(
                    PipelineDiagnostic(key, "external_tables", "reads tables outside the pipeline: " + ", ".join(unknown))
                )

        if blind_models:
            diagnostics.append(
                PipelineDiagnostic(
                    ",".join(sorted(blind_models)),
                    "unknown_reads",
                    "these models could not be analysed and declare no dependencies; dead-column detection is disabled",
                )
            )

        return cls(
            upstream=upstream,
            order=order,
            parsed=parsed,
            outputs=outputs,
            lineage=direct,
            reverse_lineage={k: frozenset(v) for k, v in reverse.items()},
            consumed=consumed,
            opaque_readers_of=opaque_readers_of,
            blind=bool(blind_models),
            diagnostics=diagnostics,
        )


def _topological_order(
    upstream: dict[str, set[str]], diagnostics: list[PipelineDiagnostic]
) -> list[str]:
    indegree = {key: len(parents & upstream.keys()) for key, parents in upstream.items()}
    children: dict[str, list[str]] = defaultdict(list)
    for key, parents in upstream.items():
        for parent in parents:
            if parent in upstream:
                children[parent].append(key)
    ready = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while ready:
        key = ready.popleft()
        order.append(key)
        for child in sorted(children[key]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    cyclic = sorted(key for key in upstream if key not in set(order))
    if cyclic:
        diagnostics.append(PipelineDiagnostic(",".join(cyclic), "cycle", "dependency cycle between models"))
        order.extend(cyclic)
    return order


def _closure(
    start: ColumnRef, edges: dict[ColumnRef, frozenset[ColumnRef]]
) -> frozenset[ColumnRef]:
    seen: set[ColumnRef] = set()
    pending = deque(edges.get(start, ()))
    while pending:
        item = pending.popleft()
        if item in seen:
            continue
        seen.add(item)
        pending.extend(edges.get(item, ()))
    return frozenset(seen)


# ------------------------------------------------------------------ duplicates


def _select_location(select: exp.Expression) -> str:
    parent = select.parent
    if isinstance(parent, exp.CTE):
        return f"cte:{parent.alias_or_name}"
    if isinstance(parent, exp.Subquery):
        return f"subquery:{parent.alias_or_name or '<anonymous>'}"
    if parent is None:
        return "query"
    return f"nested:{type(parent).__name__.lower()}"


def _fingerprint(select: exp.Expression) -> tuple[str, str]:
    canonical = select.copy()
    for node in canonical.walk():
        node.comments = None
    sql = canonical.sql(
        dialect="bigquery", normalize=True, normalize_functions="upper", comments=False
    )
    return hashlib.sha1(sql.encode("utf-8")).hexdigest()[:16], sql


def _find_duplicates(parsed: dict[str, exp.Expression], *, min_nodes: int) -> list[DuplicateGroup]:
    groups: dict[str, list[tuple[str, exp.Expression]]] = defaultdict(list)
    sql_of: dict[str, str] = {}
    size_of: dict[str, int] = {}
    fingerprint_of: dict[int, str] = {}

    for key, query in parsed.items():
        for select in query.find_all(exp.Select):
            size = sum(1 for _ in select.walk())
            if size < min_nodes:
                continue
            fingerprint, sql = _fingerprint(select)
            groups[fingerprint].append((key, select))
            sql_of[fingerprint] = sql
            size_of[fingerprint] = size
            fingerprint_of[id(select)] = fingerprint

    duplicated = {fp for fp, members in groups.items() if len(members) > 1}

    def nested_in_duplicate(select: exp.Expression) -> bool:
        parent = select.parent
        while parent is not None:
            if isinstance(parent, exp.Select) and fingerprint_of.get(id(parent)) in duplicated:
                return True
            parent = parent.parent
        return False

    result = []
    for fingerprint in duplicated:
        members = groups[fingerprint]
        if all(nested_in_duplicate(select) for _, select in members):
            continue
        result.append(
            DuplicateGroup(
                fingerprint=fingerprint,
                node_count=size_of[fingerprint],
                sql=sql_of[fingerprint],
                occurrences=tuple(
                    DuplicateOccurrence(key, _select_location(select)) for key, select in members
                ),
            )
        )
    return sorted(result, key=lambda group: (-group.node_count, group.fingerprint))
