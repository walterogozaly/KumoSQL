"""Graph coverage and sampled accuracy of impact reports.

``build_coverage`` turns a pipeline report into anonymized aggregates: counts and
ratios only, never asset names, SQL or paths. Sampled accuracy needs a human:
``sample_impact_reports`` draws a deterministic sample and writes a review sheet
for local use (it names assets, so it does not leave the machine); a reviewer
records how many impacted assets were correct, false positives, or missed, keyed
by the sheet's anonymous ids; ``score_verdicts`` turns those into precision,
recall and a confidence interval.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields
from typing import Mapping

VERDICT_KEYS = ("correct", "false_positive", "missed")
_Z95 = 1.96


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def wilson_interval(successes: int, total: int, z: float = _Z95) -> list[float] | None:
    """95% Wilson score interval for a proportion, or None with no observations."""

    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def score_verdicts(verdicts: Mapping[str, Mapping[str, int]] | None) -> dict:
    """Precision, recall and accuracy from reviewer verdicts.

    ``verdicts`` maps an anonymous sample id to counts of ``correct`` (impacted
    assets the report listed rightly), ``false_positive`` (listed wrongly) and
    ``missed`` (impacted but not listed). Pure function; ids are only counted.
    """

    totals = dict.fromkeys(VERDICT_KEYS, 0)
    reviewed = 0
    for counts in (verdicts or {}).values():
        values = {key: int(counts.get(key, 0) or 0) for key in VERDICT_KEYS}
        if any(v < 0 for v in values.values()):
            raise ValueError("verdict counts must not be negative")
        reviewed += 1
        for key, value in values.items():
            totals[key] += value
    listed = totals["correct"] + totals["false_positive"]
    relevant = totals["correct"] + totals["missed"]
    everything = listed + totals["missed"]
    return {
        "sample_size": reviewed,
        "sampled_impact_accuracy": _ratio(totals["correct"], everything),
        "accuracy_interval": wilson_interval(totals["correct"], everything),
        "precision": _ratio(totals["correct"], listed),
        "precision_interval": wilson_interval(totals["correct"], listed),
        "recall": _ratio(totals["correct"], relevant),
        "recall_interval": wilson_interval(totals["correct"], relevant),
        "verdict_totals": totals,
    }


@dataclass(frozen=True)
class Thresholds:
    """Configurable release minimums. ``None`` leaves a measure ungated.

    Ratios are 0-1. ``min_sample_size`` counts reviewed impact reports.
    ``require_complete`` fails the gate while any blocking gap exists.
    """

    min_assets_analyzed: float | None = None
    min_statements_matched: float | None = None
    min_columns_traced: float | None = None
    min_accuracy: float | None = None
    min_precision: float | None = None
    min_recall: float | None = None
    min_sample_size: int | None = None
    require_complete: bool = False

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = getattr(self, spec.name)
            if spec.name == "require_complete":
                if not isinstance(value, bool):
                    raise ValueError("require_complete must be true or false")
            elif value is None:
                continue
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{spec.name} must be a number")
            elif spec.name == "min_sample_size":
                if value < 0 or value != int(value):
                    raise ValueError("min_sample_size must be a non-negative whole number")
            elif not 0 <= value <= 1:
                raise ValueError(f"{spec.name} must be between 0 and 1")

    @classmethod
    def from_json(cls, data: object) -> "Thresholds":
        """Validate a JSON object of thresholds; unknown keys raise ``ValueError``."""

        if not isinstance(data, dict):
            raise ValueError("thresholds must be an object")
        known = {spec.name for spec in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown threshold {', '.join(map(repr, unknown))}; known: {', '.join(sorted(known))}")
        return cls(**data)

    def to_json(self) -> dict:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


_GATED = (
    ("min_assets_analyzed", "assets_analyzed_ratio"),
    ("min_statements_matched", "statements_matched_ratio"),
    ("min_columns_traced", "columns_traced_ratio"),
    ("min_accuracy", "sampled_impact_accuracy"),
    ("min_precision", "precision"),
    ("min_recall", "recall"),
    ("min_sample_size", "sample_size"),
)


def evaluate_gate(coverage: Mapping, thresholds: Thresholds) -> dict:
    """Pass/fail of a coverage block against ``thresholds``. Pure and anonymous.

    A measure that is gated but not measured (no statements, no reviewer
    verdicts) fails: an unmeasured release is not a passing one.
    """

    def actual_of(metric: str):
        if metric in coverage:
            return coverage[metric]
        return (coverage.get("accuracy") or {}).get(metric)

    checks = []
    for name, metric in _GATED:
        minimum = getattr(thresholds, name)
        if minimum is None:
            continue
        actual = actual_of(metric)
        passed = actual is not None and actual >= minimum
        checks.append({
            "check": name,
            "metric": metric,
            "minimum": minimum,
            "actual": actual,
            "passed": passed,
            **({} if actual is not None else {"reason": "not_measured"}),
        })
    if thresholds.require_complete:
        complete = bool(coverage.get("complete"))
        checks.append({
            "check": "require_complete",
            "metric": "complete",
            "minimum": True,
            "actual": complete,
            "passed": complete,
        })
    failed = [c["check"] for c in checks if not c["passed"]]
    return {
        "passed": not failed,
        "failed": failed,
        "checks": checks,
        "thresholds": thresholds.to_json(),
        "scoped": bool(coverage.get("scoped")),
    }


def _impact_sets(graph: Mapping | None) -> dict[str, list[str]]:
    impacted: dict[str, set[str]] = {}
    for edge in (graph or {}).get("edges", ()):
        impacted.setdefault(edge["upstream_id"], set()).add(edge["downstream_id"])
    return {key: sorted(value) for key, value in impacted.items()}


def sample_impact_reports(report: Mapping, size: int, seed: str = "kumosql") -> list[dict]:
    """A deterministic, seeded sample of direct impact reports for review.

    Each row has an anonymous ``sample_id`` plus the asset and the assets the
    report lists as impacted. The sheet is for the reviewer only; do not share it.
    """

    def digest(node: str) -> str:
        return hashlib.sha256(f"{seed}:{node}".encode()).hexdigest()[:12]

    impacted = _impact_sets(report.get("graph"))
    chosen = sorted(impacted, key=digest)[: max(size, 0)]
    return [
        {
            "sample_id": digest(node),
            "asset": node,
            "impacted": impacted[node],
            "verdict": dict.fromkeys(VERDICT_KEYS, 0),
        }
        for node in chosen
    ]


def build_coverage(
    report: Mapping,
    *,
    statements: tuple[int, int] = (0, 0),
    verdicts: Mapping[str, Mapping[str, int]] | None = None,
    window: Mapping[str, str | None] | None = None,
    thresholds: Thresholds | None = None,
    scoped: bool = False,
) -> dict:
    """Anonymized aggregate coverage of a pipeline report.

    With ``thresholds`` the result carries a ``gate`` block (see
    :func:`evaluate_gate`); ``scoped`` marks a coverage computed within a saved scope (the scope's name
    is never included; names can identify people or teams).

    Contains only counts and ratios. ``complete`` is false whenever analysis
    is incomplete, so a high percentage is never read as full coverage.
    """

    completeness = report["completeness"]
    edges = (report.get("graph") or {}).get("edges", [])
    by_source: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    seen: list[str] = []
    for edge in edges:
        by_source[edge["source"]] = by_source.get(edge["source"], 0) + 1
        by_confidence[edge["confidence"]] = by_confidence.get(edge["confidence"], 0) + 1
        seen.extend(t for t in (edge.get("first_seen"), edge.get("last_seen")) if t)
    columns = report.get("column_lineage", [])
    traced = sum(1 for c in columns if c.get("status") != "unknown")
    stars = sum(1 for c in columns if c.get("reason") == "unexpanded_star")
    assets_total = report["models"]
    analyzed = max(assets_total - completeness["assets_not_analyzed"], 0)
    total_statements, matched_statements = statements
    score = score_verdicts(verdicts)
    result = {
        "assets_total": assets_total,
        "assets_analyzed": analyzed,
        "assets_analyzed_ratio": _ratio(analyzed, assets_total),
        "statements_total": total_statements,
        "statements_matched": matched_statements,
        "statements_matched_ratio": _ratio(matched_statements, total_statements),
        "columns_total": len(columns),
        "columns_traced": traced,
        "columns_traced_ratio": _ratio(traced, len(columns)),
        "unexpanded_stars": stars,
        "edges_total": len(edges),
        "edges_by_source": dict(sorted(by_source.items())),
        "edges_by_confidence": dict(sorted(by_confidence.items())),
        "blocking_gaps": sum(1 for g in completeness["gaps"] if g["blocking"]),
        "gaps_by_code": dict(completeness["by_code"]),
        "sampled_impact_accuracy": score["sampled_impact_accuracy"],
        "sample_size": score["sample_size"],
        "accuracy": score,
        "complete": completeness["complete"],
        "window": {
            "start": (window or {}).get("start") or (min(seen) if seen else None),
            "end": (window or {}).get("end") or (max(seen) if seen else None),
        },
    }
    result["scoped"] = scoped
    result["gate"] = evaluate_gate(result, thresholds) if thresholds else None
    return result
