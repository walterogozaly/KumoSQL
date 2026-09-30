"""Rank savings opportunities by savings, frequency and downstream reach.

The ranking is pure: it takes opportunity records that already carry cost
figures and returns the ``opportunities[]`` shape the UI documents. Cost
figures are supplied by the caller (see ``OpportunityInput``); nothing here
reads job history or invents numbers.

Savings provenance is never blended. Every priced entry carries exactly one
basis:

* ``measured``: the saving was observed (for example validated before/after
  job statistics).
* ``estimate``: a planner figure or heuristic.
* ``upper_bound``: a ceiling (for example the observed cost of the work that
  contains the repeated logic); the real saving cannot exceed it.

An entry with no savings figure is still listed, last, with ``savings`` set
to ``None``.

Ordering: basis tier (measured, estimate, upper_bound, unpriced), then
savings value descending, then run count descending, then downstream reach
descending, then ``id`` ascending. Values of different bases are never
compared with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .graph import GraphResult

BASES: tuple[str, ...] = ("measured", "estimate", "upper_bound")
_TIER = {"measured": 0, "estimate": 1, "upper_bound": 2}
_UNPRICED_TIER = 3


@dataclass(frozen=True)
class OpportunityInput:
    """One candidate opportunity with the cost data joined to it.

    ``savings_value`` is the saving over the observation window in the
    caller's cost unit; ``None`` means no cost data was joined.
    ``savings_range`` is an optional ``(low, high)`` pair around it.
    ``measured_cost`` is the observed cost of the containing work over the
    same window. ``runs`` and ``window_days`` describe how often it ran.
    ``downstream_reach`` is the number of transitive consumers.
    ``extra`` carries additional documented fields (repeats, consumers, ...)
    through to the output unchanged.
    """

    id: str
    title: str
    savings_value: float | None = None
    savings_basis: str | None = None
    savings_range: tuple[float, float] | None = None
    measured_cost: float | None = None
    runs: int | None = None
    window_days: float | None = None
    downstream_reach: int = 0
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.savings_value is None:
            if self.savings_basis is not None or self.savings_range is not None:
                raise ValueError(f"{self.id}: basis or range given without a savings value")
            return
        if self.savings_basis not in BASES:
            raise ValueError(
                f"{self.id}: savings basis must be one of {', '.join(BASES)}, "
                f"got {self.savings_basis!r}"
            )
        if self.savings_value < 0:
            raise ValueError(f"{self.id}: savings value must not be negative")
        if self.savings_range is not None:
            low, high = self.savings_range
            if not low <= self.savings_value <= high:
                raise ValueError(f"{self.id}: savings value must lie within its range")


def frequency_label(runs: int | None, window_days: float | None) -> str:
    """Describe run frequency; ``unknown`` when no run data exists."""
    if runs is None or window_days is None or window_days <= 0:
        return "unknown"
    per_day = runs / window_days
    if per_day >= 0.9:
        return "daily" if per_day < 1.5 else f"{per_day:.0f} per day"
    if per_day >= 0.9 / 7:
        return "weekly"
    if per_day >= 0.9 / 31:
        return "monthly"
    return f"{runs} runs in {window_days:g} days"


def downstream_reach(graph: GraphResult, node_id: str) -> int:
    """Count transitive downstream nodes of ``node_id`` (its stable key)."""
    children: dict[str, set[str]] = {}
    for edge in graph.edges:
        children.setdefault(edge.upstream.stable_key, set()).add(edge.downstream.stable_key)
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen and child != node_id:
                seen.add(child)
                stack.append(child)
    return len(seen)


def _sort_key(item: OpportunityInput) -> tuple:
    tier = _TIER[item.savings_basis] if item.savings_value is not None else _UNPRICED_TIER
    return (
        tier,
        -(item.savings_value or 0.0),
        -(item.runs or 0),
        -item.downstream_reach,
        item.id,
    )


def rank_opportunities(items: Iterable[OpportunityInput]) -> list[dict[str, object]]:
    """Return ``opportunities[]`` entries, best first, with 1-based ranks."""
    out: list[dict[str, object]] = []
    for index, item in enumerate(sorted(items, key=_sort_key), start=1):
        savings: dict[str, object] | None = None
        if item.savings_value is not None:
            savings = {"value": item.savings_value, "basis": item.savings_basis}
            if item.savings_range is not None:
                savings["range"] = list(item.savings_range)
        entry: dict[str, object] = {
            "id": item.id,
            "rank": index,
            "title": item.title,
            "savings": savings,
            "measured_cost": item.measured_cost,
            "frequency": frequency_label(item.runs, item.window_days),
            "downstream_reach": item.downstream_reach,
        }
        for key, value in item.extra.items():
            entry.setdefault(key, value)
        out.append(entry)
    return out
