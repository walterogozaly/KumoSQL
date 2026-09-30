"""First recommendation record: where work repeats, who relies on it, the
proposed change and how it would be verified.

Inputs are a small source-agnostic contract (:class:`RecommendationInput`) so
the builder does not depend on how repeats are found or ranked. Anything the
caller does not supply renders as the explicit string ``"unknown"``; nothing is
inferred or guessed. Consumers come from a :class:`~kumosql.graph.GraphResult`
and the evidence label from :class:`~kumosql.rewrite.VerificationStatus`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Mapping

from .graph import GraphResult
from .rewrite import Verification, VerificationStatus

UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepeatLocation:
    """One place the same work is computed."""

    node: str
    where: str | None = None

    def to_json(self) -> dict[str, str]:
        return {"node": self.node, "where": self.where or UNKNOWN}


@dataclass(frozen=True)
class RecommendationInput:
    """What the builder needs from repeat detection and ranking."""

    repeats: tuple[RepeatLocation, ...] = ()
    title: str | None = None
    proposed_change: str | None = None
    rule: str | None = None
    #: Label the change must reach before acceptance.
    required_label: VerificationStatus | str | None = None
    #: Human-readable steps that would be run. Nothing here is a result.
    verification_plan: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    title: str
    repeats: tuple[RepeatLocation, ...]
    #: Sorted downstream node keys, or ``None`` when consumers are unknown.
    consumers: tuple[str, ...] | None
    consumers_completeness: str  # "partial" | "unknown"
    consumer_gaps: tuple[str, ...]
    proposed_change: str
    rule: str
    verification_required: str
    verification_plan: tuple[str, ...]
    evidence_label: str
    evidence_reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "title": self.title,
            "repeats": [r.to_json() for r in self.repeats] or UNKNOWN,
            "consumers": list(self.consumers) if self.consumers is not None else UNKNOWN,
            "consumers_completeness": self.consumers_completeness,
            "consumer_gaps": list(self.consumer_gaps),
            "proposed_change": self.proposed_change,
            "rule": self.rule,
            "verification": {
                "required": self.verification_required,
                "plan": list(self.verification_plan),
                "plan_known": bool(self.verification_plan),
            },
            "evidence": {"label": self.evidence_label, "reason": self.evidence_reason},
        }

    def to_text(self) -> str:
        lines = [self.title, ""]
        lines.append("Where the work repeats:")
        if self.repeats:
            lines += [f"  - {r.node}: {r.where or UNKNOWN}" for r in self.repeats]
        else:
            lines.append(f"  {UNKNOWN}")
        lines.append(f"Who relies on it ({self.consumers_completeness}):")
        if self.consumers is None:
            lines.append(f"  {UNKNOWN}")
        elif self.consumers:
            lines += [f"  - {c}" for c in self.consumers]
        else:
            lines.append("  none found in the supplied graph")
        lines += [f"  gap: {g}" for g in self.consumer_gaps]
        lines.append("Proposed change:")
        lines.append(f"  {self.proposed_change}")
        lines.append(f"  rule: {self.rule}")
        lines.append("How it would be verified:")
        lines.append(f"  required: {self.verification_required}")
        if self.verification_plan:
            lines += [f"  - {s}" for s in self.verification_plan]
        else:
            lines.append(f"  plan: {UNKNOWN}")
        lines.append(f"Evidence so far: {self.evidence_label} ({self.evidence_reason})")
        return "\n".join(lines)


def _label(value: VerificationStatus | str | None) -> str:
    if value is None:
        return UNKNOWN
    return value.value if isinstance(value, VerificationStatus) else str(value)


def _consumers(
    repeats: Iterable[RepeatLocation], graph: GraphResult | None
) -> tuple[tuple[str, ...] | None, str, tuple[str, ...]]:
    if graph is None:
        return None, "unknown", ("no graph supplied",)
    keys: dict[str, str] = {}
    labels: dict[str, str] = {}
    for node in graph.nodes:
        keys[node.identity.stable_key] = node.identity.stable_key
        keys[node.identity.key] = node.identity.stable_key
        labels[node.identity.stable_key] = node.identity.key
    children: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        children[edge.upstream.stable_key].add(edge.downstream.stable_key)

    gaps: list[str] = []
    start = []
    for r in repeats:
        if r.node in keys:
            start.append(keys[r.node])
        else:
            gaps.append(f"repeat node not in graph: {r.node}")
    if not start:
        return None, "unknown", tuple(gaps or ["no repeat nodes to look up"])
    seen = set(start)
    queue = deque(start)
    found: set[str] = set()
    while queue:
        for nxt in children[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                found.add(nxt)
                queue.append(nxt)
    if not any(e.observed for e in graph.edges):
        gaps.append("no observed reads supplied; readers outside the loaded project are invisible")
    gaps += [f"{d['code']}: {d['count']}" for d in graph.to_json()["diagnostics"]]  # type: ignore[union-attr]
    # A graph never proves that nothing outside it reads the output.
    return tuple(sorted(labels[k] for k in found)), "partial", tuple(gaps)


def build_recommendation(
    spec: RecommendationInput,
    *,
    graph: GraphResult | None = None,
    verification: Verification | Mapping[str, object] | None = None,
) -> Recommendation:
    """Assemble the four-part record; unsupplied parts stay ``"unknown"``."""

    consumers, completeness, gaps = _consumers(spec.repeats, graph)
    if isinstance(verification, Verification):
        label, reason = verification.status.value, verification.reason
    elif verification is not None:
        label = _label(verification.get("status"))  # type: ignore[arg-type]
        reason = str(verification.get("reason") or UNKNOWN)
    else:
        label, reason = UNKNOWN, "no verification has been run"
    return Recommendation(
        title=spec.title or UNKNOWN,
        repeats=tuple(spec.repeats),
        consumers=consumers,
        consumers_completeness=completeness,
        consumer_gaps=gaps,
        proposed_change=spec.proposed_change or UNKNOWN,
        rule=spec.rule or UNKNOWN,
        verification_required=_label(spec.required_label),
        verification_plan=tuple(spec.verification_plan),
        evidence_label=label,
        evidence_reason=reason,
    )
