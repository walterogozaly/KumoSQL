"""Readiness gate for refactoring proposals.

A proposal is ready only when every affected consumer has a verification
result and each result is ``proven`` or ``unchanged`` (the strictest reading:
``planner_checked`` does not count). Anything missing is reported as
``unknown``, never as a pass:

* a consumer without a result,
* a proposal without a consumer list, or with a list flagged incomplete
  (an explicit ``<unresolved readers>`` row is added),
* a result whose label is not one of the known evidence labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .rewrite import Verification, VerificationCheck, VerificationStatus, verify_rewrite

UNKNOWN = "unknown"
UNRESOLVED_CONSUMERS = "<unresolved readers>"
KNOWN_LABELS = frozenset(status.value for status in VerificationStatus)
READY_LABELS = frozenset({"proven", "unchanged"})


@dataclass(frozen=True)
class ConsumerResult:
    """Verification outcome for one consumer, with the reason behind it."""

    node: str
    label: str
    reason: str = ""

    def to_json(self) -> dict[str, str]:
        return {"node": self.node, "label": self.label}


def _label_of(result: object) -> tuple[str, str]:
    """Normalize a result (label string, ConsumerResult, Verification) to (label, reason)."""

    if isinstance(result, ConsumerResult):
        result_label, reason = result.label, result.reason
    elif isinstance(result, Verification):
        result_label, reason = result.status.value, result.reason
    elif isinstance(result, VerificationStatus):
        result_label, reason = result.value, ""
    elif isinstance(result, str):
        result_label, reason = result, ""
    else:
        return UNKNOWN, "The verification result was not understood."
    if result_label == UNKNOWN:
        return UNKNOWN, reason
    if result_label not in KNOWN_LABELS:
        return UNKNOWN, f"Unrecognized verification label {result_label!r}."
    return result_label, reason


def assess_proposal(
    proposal: Mapping[str, object],
    results: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the proposal in the UI shape with a label per consumer and ``ready``.

    ``proposal`` carries ``consumers`` (a list of ``{"node": ...}`` mappings or
    node names) and optionally ``consumers_complete`` (false when the graph
    could not enumerate every reader). ``results`` maps a consumer node to its
    verification result. A label already on the consumer entry is used when
    ``results`` has none for that node.
    """

    results = results or {}
    raw = proposal.get("consumers")
    entries: list[tuple[str, object]] = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(item, Mapping):
            node = item.get("node")
            embedded = item.get("label")
        else:
            node, embedded = item, None
        if not isinstance(node, str) or not node:
            continue
        entries.append((node, results.get(node, embedded)))

    consumers: list[dict[str, str]] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for node, result in entries:
        if node in seen:
            continue
        seen.add(node)
        if result is None:
            label, reason = UNKNOWN, "No verification result for this consumer."
        else:
            label, reason = _label_of(result)
        consumers.append({"node": node, "label": label})
        if label not in READY_LABELS:
            reasons.append(f"{node}: {label}" + (f" ({reason})" if reason else ""))

    if not entries:
        reasons.append("The proposal has no consumer list, so its effect is unknown.")
    elif proposal.get("consumers_complete") is False:
        reasons.append("The consumer list is incomplete; some readers were not identified.")
    if not entries or proposal.get("consumers_complete") is False:
        consumers.append({"node": UNRESOLVED_CONSUMERS, "label": UNKNOWN})

    ready = bool(consumers) and all(item["label"] in READY_LABELS for item in consumers)
    return {
        **{key: value for key, value in proposal.items() if key not in ("consumers", "consumers_complete")},
        "consumers": consumers,
        "ready": ready,
        "not_ready_reasons": reasons,
    }


def verify_consumer(
    before_sql: str | None,
    after_sql: str | None,
    *,
    node: str = "",
    planner_check: VerificationCheck | None = None,
) -> ConsumerResult:
    """Verify one consumer with the pipeline's rewrite verification.

    ``before_sql`` and ``after_sql`` must be the consumer's effective query
    before and after the proposal (with the changed model's definition
    inlined, when the change is inside a model the consumer reads); comparing
    the consumer's own unchanged text proves nothing about an upstream change.
    When either text is missing or verification raises, the result is
    ``unknown``.
    """

    if not before_sql or not after_sql:
        return ConsumerResult(node, UNKNOWN, "The consumer's before/after query is not available.")
    try:
        verification = verify_rewrite(before_sql, after_sql, planner_check=planner_check)
    except Exception as exc:  # verification must never turn an error into a pass
        return ConsumerResult(node, UNKNOWN, f"Verification could not run: {exc}")
    return ConsumerResult(node, verification.status.value, verification.reason)


def verify_consumers(
    proposal: Mapping[str, object],
    queries: Mapping[str, tuple[str | None, str | None]],
) -> dict[str, ConsumerResult]:
    """Run ``verify_consumer`` for every consumer of a proposal.

    ``queries`` maps a consumer node to its (before, after) effective query.
    A consumer with no entry is reported as ``unknown``.
    """

    out: dict[str, ConsumerResult] = {}
    for item in proposal.get("consumers") or ():
        node = item.get("node") if isinstance(item, Mapping) else item
        if not isinstance(node, str) or not node:
            continue
        before, after = queries.get(node, (None, None))
        out[node] = verify_consumer(before, after, node=node)
    return out


def assess_proposals(
    proposals: Iterable[Mapping[str, object]],
    results: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Assess several proposals; ``results`` maps proposal id to its per-consumer results."""

    results = results or {}
    return [assess_proposal(p, results.get(str(p.get("id")))) for p in proposals]
