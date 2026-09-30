"""Proposals to extract logic repeated across models into one shared model.

A proposal is a report only. Nothing here edits a model, and ``ready`` is
always false: whether every consumer of a change has been verified is a
separate question answered elsewhere.

The consumer list of a proposal holds every model that would be edited (the
models containing a copy) and, through the query graph, every model that reads
one of them, directly or transitively. When the graph cannot be trusted to show
every reader, the proposal says so in ``consumers_complete`` and
``incomplete_reasons`` instead of implying the list is exhaustive.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING, Iterable, Mapping

from .graph import GraphResult, ObservedRead, build_query_graph

if TYPE_CHECKING:
    from .near_duplicates import NearDuplicateCluster
    from .pipeline import Pipeline

UNKNOWN = "unknown"
# Cluster kinds that have no plain shared SELECT to extract.
_NOT_EXTRACTABLE = {"literal_parameters", "mixed"}


@dataclass(frozen=True)
class Consumer:
    node: str
    role: str  # "edit_site" (contains a copy) or "downstream" (reads an edit site)
    label: str = UNKNOWN  # verification result; unknown until something verifies it
    via: tuple[str, ...] = ()  # edit sites this consumer reads, directly or not

    def to_json(self) -> dict:
        data = {"node": self.node, "label": self.label, "role": self.role}
        if self.via:
            data["via"] = list(self.via)
        return data


@dataclass(frozen=True)
class SharedLogicProposal:
    id: str
    title: str
    origin: str  # "exact_duplicate" or the near-duplicate cluster kind
    shared_sql: str
    sites: tuple[tuple[str, str], ...]  # (model, location) of every copy to replace
    consumers: tuple[Consumer, ...]
    incomplete_reasons: tuple[str, ...] = ()
    cost_rationale: str = UNKNOWN
    observed_reads_included: bool = False
    # Per-site filters the copy would still apply after the extraction.
    residual_filters: Mapping[tuple[str, str], tuple[str, ...]] | None = None
    ready: bool = False

    @property
    def consumers_complete(self) -> bool:
        return not self.incomplete_reasons

    def to_json(self) -> dict:
        data = {
            "id": self.id,
            "kind": "shared_logic",
            "title": self.title,
            "origin": self.origin,
            "cost_rationale": self.cost_rationale,
            "consumers": [c.to_json() for c in self.consumers],
            "consumers_complete": self.consumers_complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "observed_reads_included": self.observed_reads_included,
            "shared_sql": self.shared_sql,
            "sites": [{"node": m, "location": loc} for m, loc in self.sites],
            "ready": False,
        }
        if self.residual_filters:
            data["residual_filters"] = [
                {"node": m, "location": loc, "filters": list(f)}
                for (m, loc), f in self.residual_filters.items()
            ]
        return data


def propose_shared_logic(
    pipeline: "Pipeline",
    observed_reads: Iterable[ObservedRead | Mapping[str, object]] = (),
    *,
    min_nodes: int = 12,
    threshold: float = 0.7,
) -> list[SharedLogicProposal]:
    """Propose extracting repeated SELECTs, exact and near, into shared models."""

    observed = tuple(observed_reads)
    graph = build_query_graph(pipeline, observed)
    readers = _reader_map(pipeline, graph)
    gaps = _gap_reasons(pipeline, graph)

    proposals: list[SharedLogicProposal] = []
    for group in pipeline.duplicate_selects(min_nodes=min_nodes):
        proposals.append(_from_group(group, readers, gaps, bool(observed)))
    for cluster in pipeline.near_duplicate_selects(min_nodes=min_nodes, threshold=threshold):
        proposal = _from_cluster(cluster, readers, gaps, bool(observed))
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def proposals_json(proposals: Iterable[SharedLogicProposal]) -> dict:
    return {"proposals": [p.to_json() for p in proposals]}


# ----------------------------------------------------------------- internals


def _from_group(group, readers, gaps, observed) -> SharedLogicProposal:
    sites = tuple((o.model, o.location) for o in group.occurrences)
    models = {m for m, _ in sites}
    return SharedLogicProposal(
        id=f"shared-{group.fingerprint}",
        title=f"Extract a SELECT repeated in {len(sites)} places into one shared model",
        origin="exact_duplicate",
        shared_sql=group.sql,
        sites=sites,
        consumers=_consumers(models, readers),
        incomplete_reasons=gaps,
        observed_reads_included=observed,
    )


def _from_cluster(cluster: "NearDuplicateCluster", readers, gaps, observed):
    if cluster.kind in _NOT_EXTRACTABLE or not cluster.shared_sql:
        return None
    sites = tuple(occ for v in cluster.variants for occ in v.occurrences)
    models = {m for m, _ in sites}
    residual = {
        occ: v.residual_filters
        for v in cluster.variants
        if v.residual_filters
        for occ in v.occurrences
    }
    digest = hashlib.sha1(
        "|".join(sorted(v.fingerprint for v in cluster.variants)).encode()
    ).hexdigest()[:16]
    return SharedLogicProposal(
        id=f"shared-{digest}",
        title=f"Extract a near-identical SELECT repeated in {len(sites)} places into one shared model",
        origin=cluster.kind,
        shared_sql=cluster.shared_sql,
        sites=sites,
        consumers=_consumers(models, readers),
        incomplete_reasons=gaps,
        observed_reads_included=observed,
        residual_filters=residual or None,
    )


def _reader_map(pipeline: "Pipeline", graph: GraphResult) -> dict[str, set[str]]:
    """Direct readers per node, named by model key where the node is a model."""

    name_of: dict[str, str] = {}
    for key, model in pipeline.models.items():
        identity = model.identity
        if identity is not None:
            name_of[identity.stable_key] = key

    def name(identity) -> str:
        return name_of.get(identity.stable_key, identity.key or identity.stable_key)

    readers: dict[str, set[str]] = {}
    for edge in graph.edges:
        readers.setdefault(name(edge.upstream), set()).add(name(edge.downstream))
    # Edges the pipeline itself knows about stay visible even if the graph
    # could not place them.
    for parent, children in pipeline.downstream.items():
        readers.setdefault(parent, set()).update(children)
    return readers


def _consumers(edit_models: set[str], readers: dict[str, set[str]]) -> tuple[Consumer, ...]:
    via: dict[str, set[str]] = {}
    for start in sorted(edit_models):
        seen = {start}
        queue = deque([start])
        while queue:  # cycle-safe breadth-first walk
            for child in sorted(readers.get(queue.popleft(), ())):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                    via.setdefault(child, set()).add(start)
    consumers = [Consumer(m, "edit_site") for m in sorted(edit_models)]
    consumers += [
        Consumer(node, "downstream", via=tuple(sorted(origins)))
        for node, origins in sorted(via.items())
        if node not in edit_models
    ]
    return tuple(consumers)


def _gap_reasons(pipeline: "Pipeline", graph: GraphResult) -> tuple[str, ...]:
    reasons: list[str] = []
    for diag in pipeline.all_diagnostics():
        if diag.code in ("parse_error", "no_query"):
            reasons.append(f"{diag.model} could not be parsed, so its reads are unknown")
    for model in pipeline.models.values():
        if not model.is_query and not model.declared_dependencies and model.sql.strip():
            reasons.append(f"{model.key or model.path} is not a query and declares no dependencies")
    if graph.unresolved_observation_count:
        reasons.append(
            f"{graph.unresolved_observation_count} observed reads referenced tables the graph could not identify"
        )
    if graph.unattributed_observation_count:
        reasons.append(
            f"{graph.unattributed_observation_count} observed reads had no destination to attribute them to"
        )
    return tuple(dict.fromkeys(reasons))
