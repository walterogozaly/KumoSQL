"""Backtest predicted savings against measured ones.

A recommendation is only as good as its prediction. This module compares, for
a set of changes, the saving the cost model predicted before the change with
the saving measured after it:

* :func:`score` ranks both lists and reports Spearman's and Kendall's rank
  correlation, the share of the predicted top ``k`` that is also in the
  measured top ``k``, how often the sign was right, and the median ratio of
  predicted to measured saving. Each figure carries a percentile bootstrap
  interval (changes resampled with replacement, a fixed seed), so a score
  from 30 changes is not read as more certain than it is.
* :func:`measured_savings` turns recorded job history and a change log into
  measured savings: the cost per day of the jobs a change touches, in a window
  before it and a window after it (``savings.CostWindow``), never mixing in
  jobs it does not touch.

Everything is plain Python; nothing here runs a query.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Mapping, Sequence

from .costs import ObservedJob
from .savings import CostWindow, SavingClaim


# ----------------------------------------------------------------- statistics


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (1-based), ties sharing the mean of their positions."""

    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman's rank correlation; ``None`` when either side is constant."""

    return _pearson(_ranks(x), _ranks(y))


def kendall(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Kendall's tau-b; ``None`` when either side is constant."""

    n = len(x)
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = (x[i] > x[j]) - (x[i] < x[j])
            dy = (y[i] > y[j]) - (y[i] < y[j])
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif dx == dy:
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    if denominator == 0:
        return None
    return (concordant - discordant) / denominator


def top_k_precision(predicted: Sequence[float], measured: Sequence[float], k: int) -> float | None:
    """Share of the ``k`` largest predicted savings that are among the ``k`` largest measured ones."""

    n = len(predicted)
    if n == 0:
        return None
    k = min(k, n)
    top_p = set(sorted(range(n), key=lambda i: (-predicted[i], i))[:k])
    top_m = set(sorted(range(n), key=lambda i: (-measured[i], i))[:k])
    return len(top_p & top_m) / k


def sign_agreement(predicted: Sequence[float], measured: Sequence[float]) -> float | None:
    """Share of changes whose predicted and measured savings have the same sign (zero counts as no saving)."""

    if not predicted:
        return None
    return sum((p > 0) == (m > 0) for p, m in zip(predicted, measured)) / len(predicted)


def median_ratio(predicted: Sequence[float], measured: Sequence[float]) -> float | None:
    """Median of predicted / measured over changes where both are positive."""

    ratios = [p / m for p, m in zip(predicted, measured) if p > 0 and m > 0]
    return median(ratios) if ratios else None


METRICS: dict[str, Callable[[Sequence[float], Sequence[float]], float | None]] = {
    "spearman": spearman,
    "kendall": kendall,
    "sign_agreement": sign_agreement,
    "median_ratio": median_ratio,
}


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    index = min(len(sorted_values) - 1, max(0, int(round(p * (len(sorted_values) - 1)))))
    return sorted_values[index]


def score(
    predicted: Sequence[float],
    measured: Sequence[float],
    *,
    k: int = 10,
    resamples: int = 2000,
    seed: int = 7,
    level: float = 0.95,
    groups: Sequence[object] | None = None,
    skip: Sequence[str] = (),
) -> dict:
    """Rank and calibration metrics with percentile bootstrap intervals.

    ``groups`` labels each change with the thing it shares with others (the query it speeds up, the
    view it stores). When given, whole groups are resampled instead of single changes, so repeated
    measurements of one query do not make an interval look narrower than the evidence is.
    ``skip`` names metrics to leave out (Kendall's tau costs quadratic time per resample, which is
    too slow for thousands of changes).
    """

    if len(predicted) != len(measured):
        raise ValueError("predicted and measured must have the same length")
    if groups is not None and len(groups) != len(predicted):
        raise ValueError("groups must label every change")
    n = len(predicted)
    members: list[list[int]] = []
    if groups is not None:
        by_group: dict[object, list[int]] = {}
        for i, g in enumerate(groups):
            by_group.setdefault(g, []).append(i)
        members = list(by_group.values())
    metrics = {name: fn for name, fn in METRICS.items() if name not in skip}
    metrics[f"top_{k}_precision"] = lambda p, m: top_k_precision(p, m, k)
    point = {name: fn(predicted, measured) for name, fn in metrics.items()}
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {name: [] for name in metrics}
    if n >= 3 and (groups is None or len(members) >= 2):
        for _ in range(resamples):
            if groups is None:
                idx = [rng.randrange(n) for _ in range(n)]
            else:
                idx = [i for _ in members for i in members[rng.randrange(len(members))]]
            p = [predicted[i] for i in idx]
            m = [measured[i] for i in idx]
            for name, fn in metrics.items():
                value = fn(p, m)
                if value is not None and not math.isnan(value):
                    samples[name].append(value)
    low, high = (1 - level) / 2, 1 - (1 - level) / 2
    resampled = n >= 3 and (groups is None or len(members) >= 2)
    out: dict = {"changes": n, "k": k, "level": level, "resamples": resamples if resampled else 0}
    if groups is not None:
        out["groups"] = len(members)
    for name in metrics:
        values = sorted(samples[name])
        interval = [_percentile(values, low), _percentile(values, high)] if values else None
        out[name] = {"value": point[name], "interval": interval}
    return out


# ------------------------------------------------------------ change records


@dataclass(frozen=True)
class Change:
    """One change that was deployed, read from a change log.

    ``nodes`` are the graph nodes whose jobs the change touches: the model
    that was changed, plus readers whose SQL was rewritten to use it.
    """

    id: str
    deployed_at: datetime
    nodes: tuple[str, ...]
    kind: str = ""
    title: str = ""
    reverted_at: datetime | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "Change":
        nodes = data.get("nodes") or ()
        if isinstance(nodes, str):
            nodes = (nodes,)
        reverted = data.get("reverted_at")
        return cls(
            id=str(data["id"]),
            deployed_at=_parse_time(data["deployed_at"]),
            nodes=tuple(str(n) for n in nodes),  # type: ignore[union-attr]
            kind=str(data.get("kind", "")),
            title=str(data.get("title", "")),
            reverted_at=_parse_time(reverted) if reverted else None,
            detail={k: v for k, v in data.items() if k not in {"id", "deployed_at", "nodes", "kind", "title", "reverted_at"}},
        )


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def load_changes(path: str | Path) -> list[Change]:
    """A change log: a JSON array (or JSON lines) of ``{id, deployed_at, nodes, ...}``."""

    text = Path(path).read_text(encoding="utf-8").strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    return [Change.from_json(row) for row in rows]


#: Unit name -> the job figure it sums.
UNITS: dict[str, Callable[[ObservedJob], float]] = {
    "bytes_billed": lambda job: float(job.total_bytes_billed),
    "bytes_processed": lambda job: float(job.total_bytes_processed),
    "slot_ms": lambda job: float(job.total_slot_ms),
}


def measured_savings(
    changes: Iterable[Change],
    jobs: Iterable[ObservedJob],
    touches: Callable[[ObservedJob, Change], bool],
    *,
    window: timedelta = timedelta(days=7),
    unit: str = "slot_ms",
) -> dict[str, SavingClaim | str]:
    """Measured saving per change from job history, or the reason it cannot be measured.

    The before window ends when the change was deployed; the after window starts
    then and ends at ``window`` later, at the change's revert, or at the next
    change touching the same nodes, whichever comes first. Both windows have the
    same length (the shorter of the two), so a daily figure compares like with
    like. ``touches(job, change)`` decides which jobs a change affects.
    """

    value = UNITS[unit]
    changes = sorted(changes, key=lambda c: c.deployed_at)
    timed = [(job, _parse_time(job.creation_time)) for job in jobs if job.counted and job.creation_time]
    out: dict[str, SavingClaim | str] = {}
    for index, change in enumerate(changes):
        start = change.deployed_at
        end = start + window
        if change.reverted_at is not None:
            end = min(end, change.reverted_at)
        for later in changes[index + 1:]:
            if set(later.nodes) & set(change.nodes):
                end = min(end, later.deployed_at)
                break
        length = end - start
        previous = [c for c in changes[:index] if set(c.nodes) & set(change.nodes)]
        begin = start - length
        if previous:
            last = previous[-1]
            settled = last.reverted_at if last.reverted_at is not None and last.reverted_at <= start else last.deployed_at
            if settled > begin:
                begin = settled
                length = start - begin
                end = start + length
        if length <= timedelta(0):
            out[change.id] = "no window: another change touched the same nodes at the same time"
            continue
        before = [j for j, t in timed if begin <= t < start and touches(j, change)]
        after = [j for j, t in timed if start <= t < end and touches(j, change)]
        if not before or not after:
            out[change.id] = "no jobs in the window before or after"
            continue
        days = length.total_seconds() / 86400
        label = f"{days:g} days"
        out[change.id] = SavingClaim(
            "measured",
            unit,
            "before_after_windows",
            before=CostWindow(sum(map(value, before)) / days, len(before), f"{label} before {start.isoformat()}"),
            after=CostWindow(sum(map(value, after)) / days, len(after), f"{label} from {start.isoformat()}"),
        )
    return out


__all__ = [
    "Change",
    "METRICS",
    "UNITS",
    "kendall",
    "load_changes",
    "measured_savings",
    "median_ratio",
    "score",
    "sign_agreement",
    "spearman",
    "top_k_precision",
]
