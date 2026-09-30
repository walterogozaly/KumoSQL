"""Anonymized aggregate of how much changed output has useful evidence.

The summary is built only from evidence labels and check kinds and outcomes,
never from SQL text, names, paths, or the free-text reasons and details on a
verification. It holds counts and percentages only.

Definitions:

* A result is *changed* when its output text differs from its input text.
* *Useful evidence* is a static equivalence proof (label ``proven``). A planner
  check only shows that both statements plan with matching schemas; it is not
  proof, so it is reported separately and never counts toward the headline.
* The label buckets (``proven``, ``planner_checked``, ``unproven``,
  ``failed``) are disjoint and sum to ``changed``. ``planner_passed`` and
  ``planner_failed`` count changed results by their planner check outcome
  regardless of label, so they overlap the buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .rewrite import PipelineResult, RewriteResult, VerificationStatus

# Below this many changed outputs a percentage could identify individual
# outputs, so it is withheld (the counts are still reported).
DEFAULT_MIN_CHANGED = 5

_CHANGED_LABELS = (
    VerificationStatus.PROVEN,
    VerificationStatus.PLANNER_CHECKED,
    VerificationStatus.UNPROVEN,
    VerificationStatus.FAILED,
)


@dataclass(frozen=True)
class EvidenceSummary:
    total: int
    unchanged: int
    changed: int
    proven: int
    planner_checked: int
    unproven: int
    failed: int
    planner_passed: int
    planner_failed: int
    pct_useful_evidence: float | None
    pct_proof: float | None
    pct_planner_only: float | None
    pct_no_evidence: float | None
    pct_planner_passed: float | None

    def to_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "labels": {
                "proven": self.proven,
                "planner_checked": self.planner_checked,
                "unproven": self.unproven,
                "failed": self.failed,
            },
            "planner": {"passed": self.planner_passed, "failed": self.planner_failed},
            "percent_of_changed": {
                "useful_evidence": self.pct_useful_evidence,
                "proof": self.pct_proof,
                "planner_only": self.pct_planner_only,
                "no_evidence": self.pct_no_evidence,
                "planner_passed": self.pct_planner_passed,
            },
        }


def _pct(part: int, changed: int, min_changed: int) -> float | None:
    if changed == 0 or changed < min_changed:
        return None
    return round(100.0 * part / changed, 1)


def summarize_evidence(
    results: Iterable[RewriteResult | PipelineResult],
    *,
    min_changed: int = DEFAULT_MIN_CHANGED,
) -> EvidenceSummary:
    """Aggregate evidence labels over results, one output per result.

    Raises ``ValueError`` if the gate is broken: unchanged text must carry the
    ``unchanged`` (or ``failed``) label and changed text must carry one of the
    changed labels.
    """

    counts = {label: 0 for label in _CHANGED_LABELS}
    total = unchanged = planner_passed = planner_failed = 0
    for result in results:
        total += 1
        status = result.verification.status
        if result.input_sql == result.sql:
            if status not in (VerificationStatus.UNCHANGED, VerificationStatus.FAILED):
                raise ValueError("unchanged output carries a changed-output label")
            unchanged += 1
            continue
        if status not in counts:
            raise ValueError("changed output has no precise evidence label")
        counts[status] += 1
        for check in result.verification.checks:
            if check.kind == "planner":
                planner_passed += check.outcome == "passed"
                planner_failed += check.outcome == "failed"

    changed = total - unchanged
    proven = counts[VerificationStatus.PROVEN]
    planner_only = counts[VerificationStatus.PLANNER_CHECKED]
    none = counts[VerificationStatus.UNPROVEN] + counts[VerificationStatus.FAILED]
    return EvidenceSummary(
        total=total,
        unchanged=unchanged,
        changed=changed,
        proven=proven,
        planner_checked=planner_only,
        unproven=counts[VerificationStatus.UNPROVEN],
        failed=counts[VerificationStatus.FAILED],
        planner_passed=planner_passed,
        planner_failed=planner_failed,
        pct_useful_evidence=_pct(proven, changed, min_changed),
        pct_proof=_pct(proven, changed, min_changed),
        pct_planner_only=_pct(planner_only, changed, min_changed),
        pct_no_evidence=_pct(none, changed, min_changed),
        pct_planner_passed=_pct(planner_passed, changed, min_changed),
    )
