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
) -> dict:
    """Anonymized aggregate coverage of a pipeline report.

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
    return {
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
