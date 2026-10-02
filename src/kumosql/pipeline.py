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
from dataclasses import dataclass, field, replace
import gc
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Iterable, Mapping

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from .timing import Progress, stage
from .ast_utils import quiet_parser as _quiet_parser, set_with_clause, top_level_query, with_clause
from .resilience import (
    PipelineLoadError,  # noqa: F401
    build_completeness,
    diagnostic_entry,
    guarded,
    summarize,
)
from .identity import IdentityResolution, NodeIdentity, normalize_table_reference
from .pipeline_duplicates import _find_duplicates, _fingerprint, _select_location  # noqa: F401
from .pipeline_loading import (  # noqa: F401
    _TargetResolver,
    _is_asset_reference,
    _reference_label,
    load_compiled_graph,
    load_sqlx_project,
)
from .pipeline_types import (  # noqa: F401
    ColumnLineage,
    ColumnRef,
    ColumnTrace,
    DuplicateGroup,
    DuplicateOccurrence,
    Model,
    PipelineDiagnostic,
    Target,
)

if TYPE_CHECKING:
    from .coverage import Thresholds
    from .scopes import Scope as SavedScope
    from .near_duplicates import NearDuplicateCluster


@dataclass
class Pipeline:
    models: dict[str, Model]
    sources: dict[str, Target] = field(default_factory=dict)
    source_schema: dict[str, dict[str, str]] = field(default_factory=dict)
    diagnostics: list[PipelineDiagnostic] = field(default_factory=list)
    default_project: str = ""
    default_dataset: str = ""

    # ------------------------------------------------------------------ graph

    def __post_init__(self) -> None:
        target_keys = [model.target.key for model in self.models.values() if model.target.key]
        self._resolver = _TargetResolver([*target_keys, *self.sources])
        self._known_table_nodes: set[NodeIdentity] = set()
        self._node_kinds: dict[NodeIdentity, str] = {}
        self._asset_nodes: dict[NodeIdentity, set[NodeIdentity]] = defaultdict(set)
        for model in self.models.values():
            identity = model.identity
            if identity is not None:
                if identity.kind == "table":
                    self._known_table_nodes.add(identity)
                self._node_kinds.setdefault(identity, model.kind)
            if model.asset_identity is not None and identity is not None:
                self._asset_nodes[model.asset_identity].add(identity)
        for target in self.sources.values():
            identity = NodeIdentity.for_target(target.database, target.schema, target.name)
            self._known_table_nodes.add(identity)
            self._node_kinds.setdefault(identity, "source")
        # Partial names match on their trailing parts, so index known tables by last part.
        self._known_by_tail: dict[str, list[NodeIdentity]] = {}
        for known in self._known_table_nodes:
            if known.kind == "table" and known.parts:
                self._known_by_tail.setdefault(known.parts[-1], []).append(known)
        self._analysis: _Analysis | None = None
        self._memo: dict = {}
        self._memo_locks: dict = {}
        self._memo_guard = threading.Lock()
        self._analysis_lock = threading.Lock()

    def __getstate__(self) -> dict:
        """Everything except locks and per-run memos, so a parsed project can be saved and reopened."""

        state = self.__dict__.copy()
        for key in ("_memo", "_memo_locks", "_memo_guard", "_analysis_lock"):
            state.pop(key, None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._memo = {}
        self._memo_locks = {}
        self._memo_guard = threading.Lock()
        self._analysis_lock = threading.Lock()

    def resolve(self, table: exp.Table | str) -> str | None:
        """Map a table reference to a model or declared source key."""

        resolution = self.resolve_reference(table, use_defaults=True)
        if resolution.matched:
            return resolution.identity.key
        # Existing SQL analysis permits suffix resolution for query text.
        # Observed job references use resolve_observed_references, which is
        # exact and never uses this fallback.
        return self._resolver.resolve(table)

    def resolve_reference(self, reference: object, *, use_defaults: bool = False) -> IdentityResolution:
        """Resolve a reference to a known node or retain it as an external identity.

        Defaults are intended for SQL references. Observed job references
        should be passed as fully-qualified values without defaults.
        """

        label = _reference_label(reference)
        if _is_asset_reference(reference):
            asset = reference if isinstance(reference, NodeIdentity) else NodeIdentity.for_asset(label)
            candidates = tuple(sorted(self._asset_nodes.get(asset, ()), key=lambda node: node.stable_key))
            if len(candidates) == 1:
                identity = candidates[0]
                return IdentityResolution(label, identity, "exact", node_kind=self._node_kinds.get(identity, "model"))
            if len(candidates) > 1:
                return IdentityResolution(label, asset, "ambiguous", candidates, "external")
            return IdentityResolution(label, asset, "unmatched", node_kind="external")

        normalized = normalize_table_reference(
            reference,
            default_project=self.default_project if use_defaults else "",
            default_dataset=self.default_dataset if use_defaults else "",
        )
        if normalized is None:
            return IdentityResolution(
                label, NodeIdentity.unresolved(label), "unmatched", node_kind="external"
            )
        if normalized.kind == "wildcard":
            return IdentityResolution(label, normalized, "pattern", node_kind="wildcard")
        if normalized.kind == "system":
            return IdentityResolution(label, normalized, "system", node_kind="system")

        base_identity = replace(normalized, decorator="") if normalized.decorator else normalized
        if base_identity in self._known_table_nodes:
            return IdentityResolution(
                label,
                base_identity,
                "via_default" if normalized.defaulted else "exact",
                node_kind=self._node_kinds.get(base_identity, "source"),
                decorator=normalized.decorator,
            )

        candidates = self._identity_candidates(base_identity)
        status = "ambiguous" if len(candidates) > 1 else "unmatched"
        return IdentityResolution(
            label,
            normalized,
            status,
            candidates,
            "external",
            normalized.decorator,
        )

    def resolve_observed_references(self, references: Iterable[object]) -> tuple[IdentityResolution, ...]:
        """Resolve every observed job reference while retaining unmatched items."""

        return tuple(self.resolve_reference(reference) for reference in references)

    def _identity_candidates(self, reference: NodeIdentity) -> tuple[NodeIdentity, ...]:
        # Candidates help explain ambiguous partial names. A fully qualified
        # observation must never be suggested as another project's table.
        if reference.kind != "table" or not reference.parts or len(reference.parts) >= 3:
            return ()
        candidates = []
        for known in self._known_by_tail.get(reference.parts[-1], ()):
            short, long = sorted((reference.parts, known.parts), key=len)
            if short == long[-len(short) :]:
                candidates.append(known)
        return tuple(sorted(candidates, key=lambda node: node.stable_key))

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

    def table_reads(self) -> dict[str, frozenset[str]]:
        """Every table each query model reads: pipeline models and declared sources by key, others as spelled."""

        analysis = self._analyse()
        reads = {key: set(parents) for key, parents in analysis.upstream.items() if key in analysis.parsed}
        for key, tables in analysis.external_reads.items():
            reads.setdefault(key, set()).update(tables)
        return {key: frozenset(tables) for key, tables in reads.items()}

    def topological_order(self) -> list[str]:
        return self._analyse().order

    # ---------------------------------------------------------------- columns

    def output_columns(self, model: str) -> tuple[str, ...]:
        return self._analyse().outputs.get(model, ())

    def column_lineage(self) -> dict[ColumnRef, frozenset[ColumnRef]]:
        """Direct lineage: each model output column to the columns it is built from."""

        return dict(self._analyse().lineage)

    def explain_lineage(self) -> dict[ColumnRef, ColumnLineage]:
        """One-hop lineage for every output column of every analysed model.

        Unlike ``column_lineage()``, a column that could not be traced is
        present with ``status == "unknown"`` and a reason, and is never
        confused with a column that reads nothing (``status == "constant"``).
        """

        return dict(self._analyse().records)

    def trace_column(self, column: ColumnRef) -> ColumnTrace:
        """Trace ``column`` back to source columns, reporting what is unknown.

        ``unknown`` lists every column on the path whose inputs could not be
        determined (a failed or unparsed model, an unexpanded ``SELECT *``,
        an ambiguous reference), so a result with unknowns is a lower bound.
        """

        analysis = self._analyse()
        seen: set[ColumnRef] = set()
        sources: set[ColumnRef] = set()
        unknown: dict[ColumnRef, str] = {}
        pending = deque([column])
        while pending:
            item = pending.popleft()
            if item in seen:
                continue
            seen.add(item)
            record = analysis.records.get(item)
            if record is None:
                reason = analysis.untraced_reason(item, self.models)
                if reason:
                    unknown[item] = reason
                else:
                    sources.add(item)
                continue
            if record.status == "unknown":
                unknown[item] = record.reason or "unknown"
            pending.extend(sorted(record.sources))
        seen.discard(column)
        return ColumnTrace(
            column=column,
            upstream=frozenset(seen),
            sources=frozenset(sources - {column}),
            unknown=tuple(sorted(unknown.items())),
        )

    def lineage_report(self) -> list[dict]:
        """JSON-ready lineage rows for the graph view's Explain lineage tab."""

        rows = []
        for ref, record in sorted(self._analyse().records.items()):
            row = {
                "node": ref.table,
                "column": ref.column,
                "sources": [{"node": s.table, "column": s.column} for s in sorted(record.sources)],
                "transform": record.transform,
                "status": record.status,
                "complete": self.trace_column(ref).complete,
            }
            if record.reason:
                row["reason"] = record.reason
            rows.append(row)
        return rows

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
        cyclic = {d.model for d in analysis.diagnostics if d.code == "cycle"}
        result: dict[str, tuple[str, ...]] = {}
        for key in analysis.order:
            readers = downstream.get(key, set())
            if analysis.blind or not readers or key in analysis.opaque_readers_of:
                continue
            if any(reader not in analysis.consumed or reader in cyclic for reader in readers):
                continue  # a reader that was not analysed, or sits in a dependency cycle (so its input columns were unknown), might use any column
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

        def compute():
            parsed = self._analyse().parsed
            with stage("duplicates", models=len(parsed)):
                return _find_duplicates(parsed, min_nodes=min_nodes)

        return self._remembered(("duplicates", min_nodes), compute)

    def near_duplicate_selects(
        self, *, min_nodes: int = 12, threshold: float = 0.7
    ) -> list["NearDuplicateCluster"]:
        """Clusters of similar, not identical, SELECTs, with their differences.

        See :mod:`kumosql.near_duplicates`.
        """

        from .near_duplicates import find_near_duplicates

        def compute():
            parsed = self._analyse().parsed
            with stage("near_duplicates", models=len(parsed)):
                return find_near_duplicates(parsed, min_nodes=min_nodes, threshold=threshold)

        return self._remembered(("near_duplicates", min_nodes, threshold), compute)

    def has_remembered(self, name: str) -> bool:
        """Whether an expensive result (``duplicates`` or ``near_duplicates``) is already computed."""

        return any(key[0] == name for key in list(self._memo))

    def has_cached(self, prefix: str) -> bool:
        """Whether a saved-by-commit result whose name starts with ``prefix`` is ready."""

        return any(key[0] == "cached" and key[1].startswith(prefix) for key in list(self._memo))

    def _remembered(self, key: tuple, compute):
        """Compute once per pipeline: the project is immutable, so repeats (and the other pages) reuse it."""

        with self._memo_guard:
            lock = self._memo_locks.setdefault(key, threading.Lock())
        with lock:  # one lock per result, so a long search never blocks the graph or other results
            if key not in self._memo:
                self._memo[key] = compute()
            return self._memo[key]

    def all_diagnostics(self) -> list[PipelineDiagnostic]:
        return [*self.diagnostics, *self._analyse().diagnostics]

    def completeness(self) -> dict:
        """What the analysis could not see and which views that affects.

        ``complete`` is false when any asset failed to load or parse, a
        statement was skipped, or a table reference was ambiguous. ``views``
        gives a flag per view (``graph``, ``lineage``, ``impact``,
        ``dead_columns``); a false flag means results in that view may be
        missing readers or edges. ``gaps`` lists each asset with a ``kind``
        and ``message``. The same block is in ``report()``.
        """

        entries = [diagnostic_entry(d.model, d.code, d.message) for d in self.all_diagnostics()]
        return build_completeness(entries)

    def report(
        self,
        *,
        min_nodes: int = 12,
        similarity: float = 0.7,
        scope: "SavedScope | None" = None,
        observed_reads: Iterable[object] = (),
        observed_scope: "SavedScope | None" = None,
        verdicts: "Mapping[str, Mapping[str, int]] | None" = None,
        window: "Mapping[str, str | None] | None" = None,
        thresholds: "Thresholds | None" = None,
        include_duplicates: bool = True,
    ) -> dict:
        """A JSON-serialisable summary of the whole-pipeline analysis.

        ``include_duplicates=False`` skips the duplicate and near-duplicate
        searches (``duplicates`` and ``near_duplicates`` are then empty); they
        are the slowest part and the graph does not need them.

        With a ``scope`` the report is limited to models whose target matches
        it (fields ``project``, ``dataset``, ``name`` and ``table``; any other
        field raises ``ValueError``); project-level diagnostics are kept; a
        duplicate group is kept if any of its occurrences is in scope. Optional
        ``observed_reads`` are canonical job-history records; ``observed_scope``
        filters them before graph aggregation. ``coverage`` is computed within
        the scope (``coverage["scoped"]``); ``thresholds`` adds a pass/fail
        ``coverage["gate"]``.
        """

        report = self._full_report(min_nodes=min_nodes, similarity=similarity, include_duplicates=include_duplicates)
        from .graph import build_query_graph

        graph, error = guarded(
            None,
            lambda: build_query_graph(self, observed_reads, scope=observed_scope).to_json(),
        )
        report["graph"] = graph
        if error:
            report["diagnostics"].append(
                diagnostic_entry("graph", "section_failed", f"graph could not be built ({error})")
            )
            report["diagnostic_summary"] = summarize(report["diagnostics"])
        observed_gaps = self._observed_gaps(graph)
        report["completeness"] = build_completeness(report["diagnostics"], observed_gaps)
        if graph is not None:
            graph["completeness"] = report["completeness"]
        result = report if scope is None else self._scoped(report, scope)
        keep = None if scope is None else self.scope_keys(scope)
        result["coverage"] = guarded(None, lambda: self._coverage_of(result, verdicts, window, thresholds, keep))[0]
        return result

    def _coverage_of(self, report: dict, verdicts, window, thresholds=None, keep: set[str] | None = None) -> dict:
        from .coverage import build_coverage

        analysis = self._analyse()
        if keep is None:
            statements = (analysis.statements_total, analysis.statements_matched)
        else:
            per_model = [analysis.statements_by_model.get(key, (0, 0)) for key in keep]
            statements = (sum(t for t, _ in per_model), sum(m for _, m in per_model))
            report = {
                **report,
                "column_lineage": [row for row in report["column_lineage"] if row["node"] in keep],
            }
        return build_coverage(
            report,
            statements=statements,
            verdicts=verdicts,
            window=window,
            thresholds=thresholds,
            scoped=keep is not None,
        )

    def coverage(
        self,
        *,
        observed_reads: Iterable[object] = (),
        verdicts: "Mapping[str, Mapping[str, int]] | None" = None,
        window: "Mapping[str, str | None] | None" = None,
        scope: "SavedScope | None" = None,
        thresholds: "Thresholds | None" = None,
    ) -> dict:
        """Anonymized aggregate coverage, optionally within a saved ``scope``
        and gated by ``thresholds``; see :mod:`kumosql.coverage`."""

        return self.report(
            observed_reads=observed_reads, verdicts=verdicts, window=window, scope=scope, thresholds=thresholds
        )["coverage"]

    @staticmethod
    def _observed_gaps(graph: dict | None) -> list[dict]:
        if not graph:
            return []
        gaps = []
        unresolved = graph["unresolved_observations"]["count"]
        if unresolved:
            gaps.append(
                {
                    "asset": "job history",
                    "kind": "unmatched_reference",
                    "message": f"{unresolved} observed table reference(s) matched no asset",
                }
            )
        unattributed = graph["unattributed_observations"]["count"]
        if unattributed:
            gaps.append(
                {
                    "asset": "job history",
                    "kind": "unattributed_reads",
                    "message": f"{unattributed} observed statement(s) read graph tables without a destination asset",
                }
            )
        return gaps

    def model_record(
        self, key: str, profile: object | None = None, tags: "Mapping[str, list[str]] | None" = None,
        sources: "list | None" = None,
    ) -> dict[str, object]:
        """The fields a scope rule can use on model ``key``; a table ``profile`` adds its own.

        ``tags`` is :func:`kumosql.tags.tag_lookup` (computed once for many models); ``tag`` is empty without it.
        ``sources`` is :func:`kumosql.data_sources.join_index` (built once for many models): the columns of data
        sources with a ``full_name`` column join the model's own ``project.dataset.name``.
        """

        from . import data_sources
        from .scopes import profile_record

        model = self.models[key]
        target = model.target
        record: dict[str, object] = {
            "project": target.database,
            "dataset": target.schema,
            "table": target.name,
            "name": target.name,
            "model": key,
            "kind": model.kind,
            "path": model.path,
            "depends_on": [dep.key for dep in model.declared_dependencies],
            "tag": list((tags or {}).get(key.strip().casefold(), ())),
        }
        if profile is not None:
            record.update(profile_record(profile))
        index = data_sources.join_index() if sources is None else sources
        if index:
            full_name = ".".join(
                part for part in (target.database or getattr(self, "default_project", ""),
                                  target.schema or getattr(self, "default_dataset", ""), target.name) if part)
            record["full_name"] = full_name
            record.update(data_sources.extra_fields(index, full_name, record))
        return record

    def scope_keys(self, scope: "SavedScope", profiles: "Mapping[str, object] | None" = None) -> set[str]:
        """Keys of the models a saved scope matches.

        A rule field that models do not have (job-history fields such as a
        submitter, for instance) raises ``UnknownFieldError``, a ``ValueError``.
        Table-profile fields are computed on demand, or taken from ``profiles``.
        """

        from . import data_sources
        from .scopes import MODEL_FIELDS, PROFILE_FIELDS

        scope.require_fields((*MODEL_FIELDS, *PROFILE_FIELDS, *data_sources.joined_columns()), "pipeline models")
        if profiles is None and set(map(str.casefold, scope.fields_used())) & set(PROFILE_FIELDS):
            from .table_profile import profile_pipeline

            profiles = profile_pipeline(self)
        profiles = profiles or {}
        tags = None
        if {"tag"} & set(map(str.casefold, scope.fields_used())):
            from .tags import tag_lookup

            tags = tag_lookup(self)
        index = data_sources.join_index()
        return {key for key in self.models if scope.matches(self.model_record(key, profiles.get(key), tags, index))}

    def assess_schema_change(self, kind: str, table: str, column: str, **kwargs):
        """Which models break, and which change their output columns or types, if ``table`` gains, loses, renames or retypes ``column``."""

        from .schema_change import assess_schema_change

        return assess_schema_change(self, kind, table, column, **kwargs)

    def assess_change(
        self,
        kind: str,
        target: str,
        column: str | None = None,
        *,
        scope: "SavedScope | None" = None,
        observed_reads: Iterable[object] = (),
    ):
        """Blast radius of a drop, rename, changed expression or dropped table.

        ``kind`` is ``drop_column``, ``rename_column``, ``change_expression``
        or ``drop_table``. Readers that cannot be analysed are listed as
        unknown, never dropped, and ``safe_to_delete`` is always ``unknown``.
        ``observed_reads`` (job history) add readers no model declares, listed
        under ``observed`` with last seen and confidence. See ``kumosql.impact``.
        """

        from .impact import assess_change

        return assess_change(self, kind, target, column, scope=scope, observed_reads=observed_reads)

    def _scoped(self, report: dict, scope: "SavedScope") -> dict:
        keep = self.scope_keys(scope)
        keep_node_ids = {
            self.models[key].identity.stable_key
            for key in keep
            if self.models[key].identity is not None
        }

        def in_scope(occurrence: str) -> bool:
            return occurrence.rpartition(" (")[0] in keep

        near = []
        for cluster in report["near_duplicates"]:
            if any(in_scope(o) for v in cluster["variants"] for o in v["occurrences"]):
                near.append(cluster)
        graph = None if report["graph"] is None else self._scope_graph(report["graph"], keep_node_ids)
        kept_diagnostics = [d for d in report["diagnostics"] if not d["model"] or d["model"] in keep]
        completeness = build_completeness(
            kept_diagnostics,
            [
                {k: g[k] for k in ("asset", "kind", "message")}
                for g in report["completeness"]["gaps"]
                if g["origin"] == "observed"
            ],
        )
        if graph is not None:
            graph["completeness"] = completeness
        return {
            **report,
            "completeness": completeness,
            "scope": scope.name,
            "models": len(keep),
            "order": [key for key in report["order"] if key in keep],
            "node_identities": {
                key: value for key, value in report["node_identities"].items() if key in keep
            },
            "graph": graph,
            "upstream": {k: v for k, v in report["upstream"].items() if k in keep},
            "dead_columns": {k: v for k, v in report["dead_columns"].items() if k in keep},
            "duplicates": [
                group for group in report["duplicates"] if any(in_scope(o) for o in group["occurrences"])
            ],
            "near_duplicates": near,
            "diagnostics": kept_diagnostics,
            "diagnostic_summary": summarize(kept_diagnostics),
        }

    @staticmethod
    def _scope_graph(graph: dict, keep_node_ids: set) -> dict:
        graph = dict(graph)
        graph["edges"] = [
            edge for edge in graph["edges"] if edge["downstream_id"] in keep_node_ids
        ]
        graph_node_ids = set(keep_node_ids)
        graph_node_ids.update(
            endpoint_id
            for edge in graph["edges"]
            for endpoint_id in (edge["upstream_id"], edge["downstream_id"])
        )
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] in graph_node_ids]
        graph["unresolved_observations"] = {
            **graph["unresolved_observations"],
            "unscoped_count": graph["unresolved_observations"]["count"],
            "count": None,
            "model_scope_applied": False,
            "samples": [
                sample
                for sample in graph["unresolved_observations"]["samples"]
                if sample.get("downstream_id") in keep_node_ids
            ],
        }
        graph["unattributed_observations"] = {
            **graph["unattributed_observations"],
            "unscoped_count": graph["unattributed_observations"]["count"],
            "count": None,
            "model_scope_applied": False,
            "samples": [],
        }
        graph["scope_applied"] = {
            "observations": graph["scope_applied"]["observations"],
            "models": True,
        }
        graph["diagnostics"] = [
            {
                **diagnostic,
                "unscoped_count": diagnostic["count"],
                "count": None,
                "model_scope_applied": False,
            }
            for diagnostic in graph["diagnostics"]
        ]
        return graph

    def _full_report(self, *, min_nodes: int, similarity: float, include_duplicates: bool = True) -> dict:
        failed: list[PipelineDiagnostic] = []

        def section(name: str, default, action):
            value, error = guarded(default, action)
            if error:
                failed.append(
                    PipelineDiagnostic(name, "section_failed", f"{name} could not be computed ({error})")
                )
            return value

        order = section("order", [], self.topological_order)
        dead = section("dead_columns", {}, lambda: {k: list(v) for k, v in self.dead_columns().items()})
        lineage_rows = section("column_lineage", [], self.lineage_report)
        duplicates = section(
            "duplicates",
            [],
            lambda: [] if not include_duplicates else [
                {
                    "fingerprint": group.fingerprint,
                    "node_count": group.node_count,
                    "occurrences": [f"{o.model} ({o.location})" for o in group.occurrences],
                    "sql": group.sql,
                }
                for group in self.duplicate_selects(min_nodes=min_nodes)
            ],
        )
        near = section(
            "near_duplicates",
            [],
            lambda: [] if not include_duplicates else [
                cluster.to_json()
                for cluster in self.near_duplicate_selects(min_nodes=min_nodes, threshold=similarity)
            ],
        )
        diagnostics = [
            diagnostic_entry(d.model, d.code, d.message)
            for d in [*self.diagnostics, *guarded([], lambda: self._analyse().diagnostics)[0], *failed]
        ]
        return {
            "models": len(self.models),
            "sources": sorted(self.sources),
            "node_identities": {
                key: {
                    "identity": model.identity.to_json() if model.identity else None,
                    "model_kind": model.kind,
                    "asset": model.asset_identity.to_json() if model.asset_identity else None,
                }
                for key, model in sorted(self.models.items())
            },
            "order": order,
            "upstream": {key: sorted(value) for key, value in sorted(self.upstream.items())},
            "dead_columns": dead,
            "column_lineage": lineage_rows,
            "duplicates": duplicates,
            "near_duplicates": near,
            "diagnostics": diagnostics,
            "diagnostic_summary": summarize(diagnostics),
        }

    def _analyse(self) -> "_Analysis":
        with self._analysis_lock:
            if self._analysis is None:
                with stage("analyse", models=len(self.models)):
                    self._analysis = _Analysis.run(self)
            return self._analysis


def _seconds_from_env(name: str, default: float) -> float:
    """A time budget in seconds from the environment; ``0`` means no limit."""

    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return max(value, 0.0)


def _parse_script(sql: str) -> tuple[exp.Expression | None, int, list[exp.Expression]]:
    """The last query of a script, how many other queries it has, and those queries (read for their tables only)."""

    with _quiet_parser():
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    queries = []
    unread = 0
    for statement in statements:
        # ``CREATE [OR REPLACE] TABLE|VIEW ... AS SELECT`` and ``INSERT ... SELECT`` and ``EXPORT DATA ... AS SELECT`` read what their query reads.
        if isinstance(statement, exp.Query):
            query = statement
        elif isinstance(statement, (exp.Create, exp.Insert)):
            query = top_level_query(statement)
            if isinstance(query, exp.Query):
                _adopt_statement_ctes(statement, query)
                _apply_target_columns(statement, query)
        elif type(statement).__name__ == "Export":  # EXPORT DATA ... AS SELECT; sqlglot 26 cannot parse it
            query = statement.this
        else:
            query = None
        if isinstance(query, exp.Query):
            queries.append(query)
        elif _may_read_tables(statement):
            unread += 1
    query = queries[-1] if queries else None
    if query is not None:
        _name_unaliased_casts(query)
    return query, max(len(queries) - 1, 0) + unread, queries[:-1]


_TEMPLATE_TOKEN = re.compile(r"__sqlx_token_\d+__")
_COMMAND_READS = re.compile(r"\b(?:from|join|into|update|merge|using)\b", re.IGNORECASE)


def _may_read_tables(statement: exp.Expression) -> bool:
    """A statement KumoSQL does not trace that might still read or write tables: DML other than a query, or syntax sqlglot left as raw text."""

    if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
        return True
    if isinstance(statement, exp.Command):
        return bool(_COMMAND_READS.search(str(statement.expression or "")))
    return isinstance(statement, exp.Create) and isinstance(statement.expression, exp.Command)


def _adopt_statement_ctes(statement: exp.Expression, query: exp.Query) -> None:
    """``WITH ... INSERT INTO t SELECT ...`` keeps its CTEs on the statement; the query needs them to resolve its reads."""

    clause = with_clause(statement)
    if clause is None:
        return
    own = with_clause(query)
    if own is not None:
        clause = clause.copy()
        clause.set("expressions", [*clause.expressions, *own.expressions])
        set_with_clause(query, clause)
    else:
        set_with_clause(query, clause.copy())
    set_with_clause(statement, None)


def _apply_target_columns(statement: exp.Expression, query: exp.Query) -> None:
    """``INSERT INTO t (a, b) SELECT ...`` and ``CREATE VIEW v (a, b) AS ...`` name the outputs by position."""

    target = statement.this
    if not isinstance(target, exp.Schema):
        return
    names = []
    for item in target.expressions:
        inner = item.this if isinstance(item, exp.ColumnDef) else item
        name = getattr(inner, "name", "")
        if not name:
            return
        names.append(name)
    first = query
    while isinstance(first, exp.SetOperation):
        first = first.left
    first = first.unnest() if isinstance(first, exp.Subquery) else first
    if not isinstance(first, exp.Select) or len(first.expressions) != len(names):
        return
    if any(projection.is_star or isinstance(projection.unalias(), exp.Star) for projection in first.expressions):
        return
    first.set(
        "expressions",
        [exp.alias_(projection.unalias(), name, quoted=False) for projection, name in zip(first.expressions, names)],
    )


def _name_unaliased_casts(query: exp.Expression) -> None:
    """BigQuery names ``CAST(a AS T)`` with no alias ``f0_``, not ``a``; give it a name that cannot be mistaken for the column."""

    for select in query.find_all(exp.Select):
        for index, projection in enumerate(select.expressions):
            if isinstance(projection, (exp.Cast, exp.TryCast)) and not isinstance(projection, exp.Alias):
                select.expressions[index] = exp.alias_(projection, f"_col_{index}", quoted=False)


def _nested_schema(flat: dict[str, dict[str, str]]) -> dict:
    """Turn ``{"p.d.t": {...}}`` into sqlglot's nested catalog/db/table mapping."""

    nested: dict = {}
    for key, columns in flat.items():
        parts = key.split(".")
        parts = [""] * (3 - len(parts)) + parts if len(parts) < 3 else parts[-3:]
        nested.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = columns
    return nested


def _add_table(schema: MappingSchema, key: str, columns: dict[str, str]) -> None:
    parts = key.split(".")
    parts = [""] * (3 - len(parts)) + parts if len(parts) < 3 else parts[-3:]
    table = exp.Table(
        this=exp.to_identifier(parts[2]),
        db=exp.Identifier(this=parts[1]),
        catalog=exp.Identifier(this=parts[0]),
    )
    schema.add_table(table, columns, dialect="bigquery")


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


def _excepted_columns(pipeline: "Pipeline", query: exp.Expression) -> set[ColumnRef]:
    """Columns named in ``SELECT * EXCEPT (...)``: expanding the star drops them, but the query still names them."""

    stars = [
        star
        for star in query.find_all(exp.Star)
        if star.args.get("except_") and isinstance(star.parent, (exp.Select, exp.Column))
    ]
    if not stars:
        return set()
    found: set[ColumnRef] = set()
    try:
        scopes = traverse_scope(query)
    except Exception:
        return found
    for scope in scopes:
        for star in stars:
            owner = star.parent if isinstance(star.parent, exp.Column) else star
            if owner.parent is not scope.expression and owner.find_ancestor(exp.Select) is not scope.expression:
                continue
            qualifier = owner.table if isinstance(owner, exp.Column) else ""
            tables = [
                source
                for name, source in scope.sources.items()
                if isinstance(source, exp.Table) and (not qualifier or name == qualifier)
            ]
            for table in tables:
                resolved = pipeline.resolve(table) or _table_name_for_schema(table)
                for column in star.args["except_"]:
                    found.add(ColumnRef(resolved, column.name))
    return found


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
    records: dict[ColumnRef, ColumnLineage]
    reverse_lineage: dict[ColumnRef, frozenset[ColumnRef]]
    consumed: dict[str, frozenset[ColumnRef]]
    opaque_readers_of: set[str]
    # True when some model's reads are unknown entirely (unparseable, with no
    # declared dependencies), so no column can be called dead.
    blind: bool
    diagnostics: list[PipelineDiagnostic]
    # Query statements seen in query models, and how many were analysed.
    statements_total: int = 0
    statements_matched: int = 0
    # Per model: (statements seen, statements analysed), for scoped coverage.
    statements_by_model: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Per model: tables it reads that no model or declared source matches, as spelled.
    external_reads: dict[str, frozenset[str]] = field(default_factory=dict)

    def untraced_reason(self, column: ColumnRef, models: dict[str, Model]) -> str | None:
        """Why a column with no lineage record is unknown, or None for a source column."""

        if column.table in self.outputs:
            return "unexpanded_star" if "*" in self.outputs[column.table] else "unknown_column"
        if column.table in models:
            return "unparsed_model"
        return None

    @classmethod
    def run(cls, pipeline: Pipeline) -> "_Analysis":
        # Every parsed query stays alive for the whole run, so with the default thresholds the
        # collector keeps re-walking millions of long-lived syntax-tree nodes (seconds per pass
        # late in a large project). Collect less often, and park what exists in the permanent
        # generation once reading is done.
        thresholds = gc.get_threshold()
        gc.set_threshold(max(thresholds[0], 200_000), 50, 100)
        try:
            return cls._run(pipeline)
        finally:
            gc.unfreeze()
            gc.set_threshold(*thresholds)

    @classmethod
    def _run(cls, pipeline: Pipeline) -> "_Analysis":
        diagnostics: list[PipelineDiagnostic] = []
        parsed: dict[str, exp.Expression] = {}
        upstream: dict[str, set[str]] = {}
        unresolved_tables: dict[str, set[str]] = {}
        ambiguous_tables: dict[str, set[str]] = {}
        blind_models: list[str] = []
        statements_total = statements_matched = 0
        statements_by_model: dict[str, tuple[int, int]] = {}

        # Other spellings under which a model is read (a project-qualified name
        # for a model keyed by its bare name): its columns must be known there
        # too, or ``SELECT *`` over it cannot be expanded.
        spellings: dict[str, set[str]] = {}
        reading = Progress("read models", len(pipeline.models))
        for key, model in pipeline.models.items():
            reading.step(key)
            parents = {
                resolved
                for dep in model.declared_dependencies
                if (resolved := pipeline.resolve(dep.key)) and resolved != key
            }
            if model.is_query:
                try:
                    query, skipped, earlier = _parse_script(model.sql)
                    statements_total += skipped + 1
                    statements_matched += 1 if query is not None else 0
                    statements_by_model[key] = (skipped + 1, 1 if query is not None else 0)
                    if skipped:
                        diagnostics.append(
                            PipelineDiagnostic(
                                key,
                                "skipped_statements",
                                f"script has {skipped + 1} queries; only the last was analysed for columns, "
                                f"so column reads by the other {skipped} are missing (the tables they read are in the graph)",
                            )
                        )
                except Exception as exc:  # sqlglot raises several error types
                    query = None
                    statements_total += 1
                    statements_by_model[key] = (1, 0)
                    diagnostics.append(PipelineDiagnostic(key, "parse_error", str(exc).splitlines()[0]))
                if query is not None:
                    parsed[key] = query
                    # Tables read by the script's earlier queries are dependencies too, even though
                    # only the last query is traced column by column.
                    for each in (*earlier, query):
                        cte_names = {cte.alias_or_name.lower() for cte in each.find_all(exp.CTE)}
                        for table in each.find_all(exp.Table):
                            if not table.db and table.name.lower() in cte_names:
                                continue
                            resolved = pipeline.resolve(table)
                            if resolved and resolved != key:
                                parents.add(resolved)
                                spelled = _table_name_for_schema(table)
                                if spelled != resolved:
                                    spellings.setdefault(resolved, set()).add(spelled)
                            elif resolved is None and table.name:
                                bucket = (
                                    ambiguous_tables
                                    if pipeline._resolver.is_ambiguous(table)
                                    else unresolved_tables
                                )
                                bucket.setdefault(key, set()).add(_table_name_for_schema(table))
                elif model.sql.strip():
                    diagnostics.append(PipelineDiagnostic(key, "no_query", "model has no parseable query"))
            elif model.sql.strip():
                diagnostics.append(
                    PipelineDiagnostic(
                        key,
                        "unparsed_operation",
                        f"{model.kind} statements are not analysed; tables they read or write have no edges here",
                    )
                )
            if key not in parsed and model.sql.strip() and not model.declared_dependencies:
                blind_models.append(key)
            upstream[key] = parents
        reading.finish()
        gc.collect()
        gc.freeze()

        with stage("order models", models=len(upstream)):
            order = _topological_order(upstream, diagnostics)

        outputs: dict[str, tuple[str, ...]] = {}
        direct: dict[ColumnRef, frozenset[ColumnRef]] = {}
        records: dict[ColumnRef, ColumnLineage] = {}
        consumed: dict[str, frozenset[ColumnRef]] = {}
        opaque_readers_of: set[str] = set()
        schema: dict[str, dict[str, str]] = dict(pipeline.source_schema)
        # One MappingSchema grown model by model: qualify() would otherwise
        # rebuild and re-normalise the whole nested schema for every model.
        sqlglot_schema = MappingSchema(_nested_schema(schema), dialect="bigquery")

        tracing = Progress("trace columns", len(order))
        # Column tracing copies a model's whole query once per output column, so wide models with
        # many CTEs can take seconds each. A budget keeps a load bounded: past it, a model keeps
        # its columns and edges but its column-level lineage is skipped and reported as a gap.
        model_budget = _seconds_from_env("KUMOSQL_LINEAGE_MODEL_SECONDS", 8.0)
        total_deadline = _seconds_from_env("KUMOSQL_LINEAGE_SECONDS", 120.0)
        total_deadline = time.perf_counter() + total_deadline if total_deadline else None
        for key in order:
            tracing.step(key)
            query = parsed.get(key)
            if query is None:
                opaque_readers_of.update(upstream.get(key, ()))
                continue
            excepted: set[ColumnRef] = set()
            try:
                marked = query.copy()
                for node in marked.find_all(exp.Column):
                    node.meta["named"] = True  # written in the SQL, as opposed to made by expanding a star
                excepted = _excepted_columns(pipeline, marked)
                qualified = qualify(
                    marked,
                    schema=sqlglot_schema,
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
            if pruned is not qualified:
                # Pruning drops columns the outer query never uses, which is right for a ``*`` that was
                # expanded, but a column the SQL names in a CTE or subquery is still read: dropping it
                # from the table breaks the model whether or not anything uses the result.
                for scope in traverse_scope(qualified):
                    for column in scope.columns:
                        if not column.meta.get("named"):
                            continue
                        table = _source_table(scope, column)
                        if table is None:
                            continue
                        owner = pipeline.resolve(table) or _table_name_for_schema(table)
                        used.add(ColumnRef(owner, column.name))
            used.update(excepted)
            consumed[key] = frozenset(used)

            names = tuple(qualified.named_selects)
            outputs[key] = names
            if names and "*" not in names:
                schema[key] = {name: "UNKNOWN" for name in names}
                _add_table(sqlglot_schema, key, schema[key])
                for spelled in sorted(spellings.get(key, ())):
                    _add_table(sqlglot_schema, spelled, schema[key])
            # ``qualified`` is already qualified: hand lineage() its scope so it
            # neither copies nor re-qualifies the query once per output column.
            try:
                lineage_scope = build_scope(qualified)
            except Exception:
                lineage_scope = None
            is_union = isinstance(qualified, getattr(exp, "SetOperation", exp.Union))
            model_deadline = time.perf_counter() + model_budget if model_budget else None
            skipped_columns = 0
            for name in names:
                ref = ColumnRef(key, name)
                now = time.perf_counter()
                if name != "*" and (
                    (model_deadline is not None and now > model_deadline)
                    or (total_deadline is not None and now > total_deadline)
                ):
                    records[ref] = ColumnLineage(ref, frozenset(), "unknown", "unknown", "lineage_skipped")
                    skipped_columns += 1
                    continue
                if name == "*":
                    records[ref] = ColumnLineage(ref, frozenset(), "unknown", "unknown", "unexpanded_star")
                    continue
                try:
                    node = lineage(
                        name,
                        qualified,
                        dialect="bigquery",
                        scope=lineage_scope,
                        copy=lineage_scope is None,
                        # Trimming copies the whole query once per column to print a tidier node
                        # label; the trace never reads that copy, and for wide models it dominated.
                        trim_selects=False,
                    )
                    leaves, reason, transform = _scan_lineage(pipeline, schema, node, name, is_union)
                    if reason == "unresolved_column" and lineage_scope is not None:
                        # With no schema for a table, the pre-built scope keeps a
                        # bare column unresolved; re-qualifying resolves it when
                        # only one table is in scope and leaves real ambiguity.
                        retry = lineage(name, qualified, dialect="bigquery", copy=True)
                        again = _scan_lineage(pipeline, schema, retry, name, is_union)
                        if again[1] != "unresolved_column":
                            leaves, reason, transform = again
                except Exception as exc:
                    diagnostics.append(
                        PipelineDiagnostic(key, "lineage_error", f"{name}: {str(exc).splitlines()[0]}")
                    )
                    records[ref] = ColumnLineage(ref, frozenset(), "unknown", "unknown", "lineage_error")
                    continue
                if reason:
                    records[ref] = ColumnLineage(ref, frozenset(leaves), "unknown", "unknown", reason)
                elif not leaves:
                    records[ref] = ColumnLineage(ref, frozenset(), "constant", "constant")
                else:
                    records[ref] = ColumnLineage(ref, frozenset(leaves), "traced", transform)
                direct[ref] = frozenset(leaves)
            if skipped_columns:
                diagnostics.append(
                    PipelineDiagnostic(
                        key,
                        "lineage_skipped",
                        f"{skipped_columns} of {len(names)} columns were not traced because tracing this model "
                        "took longer than the time budget (KUMOSQL_LINEAGE_MODEL_SECONDS / KUMOSQL_LINEAGE_SECONDS); "
                        "its reads and readers are still in the graph",
                    )
                )

        tracing.finish()
        reverse: dict[ColumnRef, set[ColumnRef]] = defaultdict(set)
        for column, parents in direct.items():
            for parent in parents:
                reverse[parent].add(column)

        templated_models: set[str] = set()
        for key, tables in sorted(unresolved_tables.items()):
            unknown = sorted(t for t in tables if t not in schema)
            templated = [t for t in unknown if _TEMPLATE_TOKEN.search(t)]
            unknown = [t for t in unknown if t not in templated]
            if unknown:
                diagnostics.append(
                    PipelineDiagnostic(key, "external_tables", "reads tables outside the pipeline: " + ", ".join(unknown))
                )
            if templated:
                templated_models.add(key)
                # A table named by a ${...} expression that could not be resolved: what it reads is unknown.
                diagnostics.append(
                    PipelineDiagnostic(
                        key,
                        "unresolved_template",
                        "reads a table named by a template expression that was not resolved; its dependency is unknown",
                    )
                )

        for key, tables in sorted(ambiguous_tables.items()):
            diagnostics.append(
                PipelineDiagnostic(
                    key,
                    "ambiguous_reference",
                    "reads names that match more than one table, so no edge was drawn: " + ", ".join(sorted(tables)),
                )
            )

        for key in sorted(blind_models):
            diagnostics.append(
                PipelineDiagnostic(
                    key,
                    "unknown_reads",
                    "could not be analysed and declares no dependencies; its reads are unknown and dead-column detection is disabled",
                )
            )

        return cls(
            upstream=upstream,
            order=order,
            parsed=parsed,
            outputs=outputs,
            lineage=direct,
            records=records,
            reverse_lineage={k: frozenset(v) for k, v in reverse.items()},
            consumed=consumed,
            opaque_readers_of=opaque_readers_of,
            # A table named by an unresolved template might be any model, so no column can be called dead.
            blind=bool(blind_models) or bool(templated_models),
            diagnostics=diagnostics,
            statements_total=statements_total,
            statements_matched=statements_matched,
            statements_by_model=statements_by_model,
            external_reads={key: frozenset(tables) for key, tables in unresolved_tables.items()},
        )


_TRANSFORM_RANK = {"passthrough": 0, "renamed": 1, "expression": 2, "aggregate": 3, "window": 4}


def _scan_lineage(
    pipeline: "Pipeline",
    schema: dict[str, dict[str, str]],
    node,
    name: str,
    is_union: bool,
) -> tuple[set[ColumnRef], str | None, str]:
    """Leaf columns, the reason the trace is incomplete (or None), and the transform."""

    leaves: set[ColumnRef] = set()
    reason: str | None = None
    transform = "passthrough"
    reads_column = False
    literal_unnest = False
    for item in node.walk():
        if item.downstream:
            projection = item.expression
            if isinstance(projection, exp.Alias):
                projection = projection.this
            if item is node:
                reads_column = projection.find(exp.Column) is not None
            kind = _transform_kind(projection, item.name.split(".")[-1])
            if _TRANSFORM_RANK.get(kind, 0) > _TRANSFORM_RANK[transform]:
                transform = kind
            continue
        if isinstance(item.source, exp.Table):
            owner = pipeline.resolve(item.source) or _table_name_for_schema(item.source)
            leaf = ColumnRef(owner, item.name.split(".")[-1].strip('"`'))
            known = {c.lower() for c in schema.get(owner, {})}
            if known and leaf.column.lower() not in known:
                reason = reason or "unknown_column"
            else:
                leaves.add(leaf)
        elif isinstance(item.source, exp.Placeholder):
            reason = reason or "unresolved_column"
        elif isinstance(item.source, exp.Unnest) and item.source.find(exp.Column) is None:
            literal_unnest = True  # UNNEST of literals reads no column
        elif item is not node:
            reason = reason or "untraceable_source"
    if reason is None and not leaves and reads_column and not literal_unnest:
        reason = "unresolved_column"
    return leaves, reason, "union" if is_union else transform


def _transform_kind(projection: exp.Expression, name: str) -> str:
    if isinstance(projection, exp.Column):
        return "passthrough" if projection.name.lower() == name.lower() else "renamed"
    if projection.find(exp.Window) is not None:
        return "window"
    if projection.find(exp.AggFunc) is not None:
        return "aggregate"
    if projection.find(exp.Column) is None:
        return "constant"
    return "expression"


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
        diagnostics.extend(
            PipelineDiagnostic(key, "cycle", "dependency cycle between models") for key in cyclic
        )
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
