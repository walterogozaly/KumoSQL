"""Predict how a query's runtime changes when part of it is read from a stored view.

``kumosql.cost_model.calibrate`` fits seconds to the *level* of a query's work (rows scanned,
rows joined). The materialization advisor needs the *difference* between two runs of the same
query, and a level model is a poor tool for that: its errors on the two runs do not cancel, and
the difference is dominated by a few join-order and build-side accidents inside the engine. This
module fits the difference directly, on recorded before/after pairs:

    log(after / before) = intercept + sum(weight[f] * (log(1 + work_after[f]) - log(1 + work_before[f])))

so a predicted saving is anchored to the query's measured (or predicted) time before the change
instead of to an absolute work-to-seconds rate. Weights come from a ridge regression on
standardized features, with the intercept left free. The work vectors are whatever the caller
supplies: the cardinality-estimator features of :mod:`kumosql.joinorder.view_cost`, an engine's
own plan estimates (:func:`plan_work`), or both.

Everything is plain Python. :func:`cross_validate` holds whole groups (for example every pair of
one view) out together, so a score measures prediction for views the model never saw.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Mapping, Sequence

from kumosql.cost_model import _solve

#: A pair never moves the fit by more than a factor of e**5 (about 150x) either way.
CLIP = 5.0


@dataclass(frozen=True)
class Pair:
    """One recorded change: a query ran in ``before`` seconds and, reading the view, in ``after``."""

    query: str
    group: str  # what is held out together in cross-validation, usually the view
    before: float
    after: float
    work_before: Mapping[str, float]
    work_after: Mapping[str, float]

    @property
    def log_ratio(self) -> float:
        return max(-CLIP, min(CLIP, math.log(max(self.after, 1e-9) / max(self.before, 1e-9))))


def log_delta(work_before: Mapping[str, float], work_after: Mapping[str, float], names: Sequence[str]) -> list[float]:
    """``log(1 + after) - log(1 + before)`` for each named feature (a missing one counts as zero)."""

    return [math.log1p(max(float(work_after.get(n, 0.0)), 0.0)) - math.log1p(max(float(work_before.get(n, 0.0)), 0.0))
            for n in names]


@dataclass(frozen=True)
class RatioModel:
    """``log(after / before)`` as an intercept plus a weighted sum of log work ratios."""

    features: tuple[str, ...]
    intercept: float
    weights: tuple[float, ...]
    l2: float = 1.0

    def log_ratio(self, work_before: Mapping[str, float], work_after: Mapping[str, float]) -> float:
        x = log_delta(work_before, work_after, self.features)
        return max(-CLIP, min(CLIP, self.intercept + sum(w * v for w, v in zip(self.weights, x))))

    def after(self, before: float, work_before: Mapping[str, float], work_after: Mapping[str, float]) -> float:
        return before * math.exp(self.log_ratio(work_before, work_after))

    def saving(self, before: float, work_before: Mapping[str, float], work_after: Mapping[str, float]) -> float:
        return before - self.after(before, work_before, work_after)

    def to_json(self) -> dict:
        return {"features": list(self.features), "intercept": self.intercept,
                "weights": dict(zip(self.features, self.weights)), "l2": self.l2}


def fit_ratio(pairs: Sequence[Pair], features: Sequence[str], *, l2: float = 1.0) -> RatioModel:
    """Ridge regression of ``log(after / before)`` on log work ratios (features standardized, intercept free)."""

    if not pairs:
        raise ValueError("no pairs to fit")
    names = tuple(features)
    rows = [log_delta(p.work_before, p.work_after, names) for p in pairs]
    y = [p.log_ratio for p in pairs]
    n = len(pairs)
    mean = [sum(r[j] for r in rows) / n for j in range(len(names))]
    sd = [math.sqrt(sum((r[j] - mean[j]) ** 2 for r in rows) / n) or 1.0 for j in range(len(names))]
    z = [[(r[j] - mean[j]) / sd[j] for j in range(len(names))] for r in rows]
    ybar = sum(y) / n
    k = len(names)
    if k == 0:
        return RatioModel(names, ybar, (), l2)
    gram = [[sum(row[a] * row[b] for row in z) + (l2 if a == b else 0.0) for b in range(k)] for a in range(k)]
    rhs = [sum(row[a] * (t - ybar) for row, t in zip(z, y)) for a in range(k)]
    beta = _solve(gram, rhs) or [0.0] * k
    weights = tuple(b / s for b, s in zip(beta, sd))
    intercept = ybar - sum(w * m for w, m in zip(weights, mean))
    return RatioModel(names, intercept, weights, l2)


def folds_by_group(groups: Sequence[str], folds: int, seed: int = 11) -> list[set[str]]:
    """Split the distinct groups into ``folds`` random parts."""

    distinct = sorted(set(groups))
    if len(distinct) < 2:
        raise ValueError("cross-validation needs at least two distinct groups")
    random.Random(seed).shuffle(distinct)
    folds = max(2, min(folds, len(distinct)))
    return [set(distinct[k::folds]) for k in range(folds)]


def cross_validate(pairs: Sequence[Pair], features: Sequence[str], *, l2: float = 1.0, folds: int = 5, seed: int = 11) -> list[float]:
    """Out-of-fold ``log(after / before)`` for every pair; a group never helps predict itself."""

    predicted = [0.0] * len(pairs)
    for held in folds_by_group([p.group for p in pairs], folds, seed):
        train = [p for p in pairs if p.group not in held]
        if not train:
            continue
        model = fit_ratio(train, features, l2=l2)
        for i, p in enumerate(pairs):
            if p.group in held:
                predicted[i] = model.log_ratio(p.work_before, p.work_after)
    return predicted


# ------------------------------------------------------------------ engine plans


def plan_work(plan: Mapping | None, key: str = "est") -> dict[str, float]:
    """Work features of a DuckDB-style plan tree (``type``, ``est``/``act``, ``scanned``, ``children``).

    ``key`` picks the cardinality: ``"est"`` is what the optimizer expected, ``"act"`` what the
    operators produced. A node with two children counts as a join: its second child is the build
    side. Returns the rows read by scans (``scan``), joined rows produced (``out``), build and
    probe rows (``build``, ``probe``), the largest single operator output (``peak``) and the
    number of joins (``joins``).
    """

    work = {"scan": 0.0, "out": 0.0, "build": 0.0, "probe": 0.0, "peak": 0.0, "joins": 0.0}
    if not plan:
        return work

    def card(node: Mapping) -> float:
        value = node.get(key)
        return float(value) if value is not None else 0.0

    def walk(node: Mapping) -> None:
        children = node.get("children") or []
        work["peak"] = max(work["peak"], card(node))
        if not children:
            scanned = node.get("scanned") if key == "act" else None
            work["scan"] += float(scanned if scanned is not None else card(node))
        if len(children) == 2:
            work["joins"] += 1
            work["out"] += card(node)
            work["probe"] += card(children[0])
            work["build"] += card(children[1])
        for child in children:
            walk(child)

    walk(plan)
    return work


__all__ = ["CLIP", "Pair", "RatioModel", "cross_validate", "fit_ratio", "folds_by_group", "log_delta", "plan_work"]
