"""A cost model calibrated on completed jobs, and BigQuery pricing as parameters.

Predictions here are always fitted to measurements and report their own error:

* :class:`LinearCost` predicts a job's cost (slot milliseconds, seconds of
  runtime, bytes) as a non-negative weighted sum of features of the work
  (rows scanned, rows joined, rows written, bytes read, ...).
  :func:`calibrate` fits the weights to measured jobs by least squares on the
  relative error, so a 10 ms job and a 10 s job count alike, and reports how
  far predictions land from measurements as q-errors
  (``max(predicted / measured, measured / predicted)``), both on the jobs it was
  fitted to and, by k-fold cross-validation, on jobs it was not.
* :class:`Pricing` turns bytes billed, slot time and stored bytes into one unit.
  Rates are parameters: nothing here knows today's list prices. With no rates
  the compute unit stays bytes billed (on-demand) or slot milliseconds
  (editions), and storage can only be priced against compute once both rates
  are given.

Measured and predicted figures are never added together; callers keep the
basis of every figure (``measured``, ``estimate`` or ``upper_bound``).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import median
from typing import Mapping, Sequence

TIB = 1024 ** 4
GIB = 1024 ** 3
#: BigQuery on-demand bills at least this much per table a query references.
MIN_BYTES_PER_TABLE = 10 * 1024 ** 2
DAYS_PER_MONTH = 30.4375


@dataclass(frozen=True)
class Pricing:
    """How compute and storage turn into one cost unit.

    ``compute`` is ``"on_demand"`` (bytes billed) or ``"editions"`` (slot time).
    ``usd_per_tib`` and ``usd_per_slot_hour`` price compute; ``usd_per_gib_month``
    prices active logical storage. Missing rates are never filled in.
    """

    compute: str = "on_demand"
    usd_per_tib: float | None = None
    usd_per_slot_hour: float | None = None
    usd_per_gib_month: float | None = None

    def __post_init__(self) -> None:
        if self.compute not in ("on_demand", "editions"):
            raise ValueError("compute must be 'on_demand' or 'editions'")
        for name in ("usd_per_tib", "usd_per_slot_hour", "usd_per_gib_month"):
            value = getattr(self, name)
            if value is not None and not (0 < value < 1e9):
                raise ValueError(f"{name} must be a positive number")

    @property
    def priced(self) -> bool:
        return (self.usd_per_tib if self.compute == "on_demand" else self.usd_per_slot_hour) is not None

    @property
    def unit(self) -> str:
        if self.priced:
            return "usd"
        return "bytes_billed" if self.compute == "on_demand" else "slot_ms"

    def compute_cost(self, bytes_billed: float, slot_ms: float) -> float:
        if self.compute == "on_demand":
            return bytes_billed / TIB * self.usd_per_tib if self.usd_per_tib is not None else bytes_billed
        hours = slot_ms / 3_600_000
        return hours * self.usd_per_slot_hour if self.usd_per_slot_hour is not None else slot_ms

    def storage_per_byte_day(self) -> float | None:
        """A stored byte-day in the compute unit, or ``None`` when that needs a rate not given."""

        if self.usd_per_gib_month is None:
            return None
        usd = self.usd_per_gib_month / GIB / DAYS_PER_MONTH
        if self.priced:
            return usd
        if self.compute == "on_demand" and self.usd_per_tib is not None:
            return usd / self.usd_per_tib * TIB
        return None

    def to_json(self) -> dict:
        return {
            "compute": self.compute,
            "unit": self.unit,
            "usd_per_tib": self.usd_per_tib,
            "usd_per_slot_hour": self.usd_per_slot_hour,
            "usd_per_gib_month": self.usd_per_gib_month,
            "storage_priced": self.storage_per_byte_day() is not None,
        }


def billed_bytes(per_table_bytes: Sequence[float]) -> float:
    """On-demand bytes billed for a query that reads these bytes from each referenced table."""

    return float(sum(max(b, MIN_BYTES_PER_TABLE) for b in per_table_bytes))


# ------------------------------------------------------------------ fitting


def q_error(predicted: float, measured: float, floor: float = 1e-9) -> float:
    p, m = max(predicted, floor), max(measured, floor)
    return max(p / m, m / p)


def error_summary(predicted: Sequence[float], measured: Sequence[float]) -> dict:
    """q-error percentiles and the median signed log10 ratio (positive = over-predicted)."""

    errors = sorted(q_error(p, m) for p, m in zip(predicted, measured))
    if not errors:
        return {"jobs": 0}
    logs = sorted(math.log10(max(p, 1e-9) / max(m, 1e-9)) for p, m in zip(predicted, measured))

    def pct(values: Sequence[float], p: float) -> float:
        return values[min(len(values) - 1, max(0, math.ceil(p * len(values)) - 1))]

    return {
        "jobs": len(errors),
        "q_error_p50": round(pct(errors, 0.5), 4),
        "q_error_p90": round(pct(errors, 0.9), 4),
        "q_error_max": round(errors[-1], 4),
        "median_log10_ratio": round(median(logs), 4),
    }


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; ``None`` when singular."""

    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-300:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                factor = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def _nnls(rows: Sequence[Sequence[float]], targets: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Non-negative weighted least squares by active-set elimination (few features)."""

    width = len(rows[0])
    # A feature that is zero on every weighted row carries no signal; leave its weight at zero.
    active = [j for j in range(width) if any(w * r[j] * r[j] > 0 for r, w in zip(rows, weights))]
    while active:
        # Scale each column so the normal equations stay well conditioned.
        scale = [max(math.sqrt(sum(w * r[j] ** 2 for r, w in zip(rows, weights))), 1e-300) for j in active]
        gram = [[sum(w * r[i] * r[j] for r, w in zip(rows, weights)) / (si * sj) for j, sj in zip(active, scale)]
                for i, si in zip(active, scale)]
        rhs = [sum(w * r[i] * t for r, t, w in zip(rows, targets, weights)) / si for i, si in zip(active, scale)]
        solution = _solve(gram, rhs)
        if solution is None:
            active.pop()
            continue
        coef = [s / sc for s, sc in zip(solution, scale)]
        negative = [i for i, c in zip(active, coef) if c < 0]
        if not negative:
            out = [0.0] * width
            for i, c in zip(active, coef):
                out[i] = c
            return out
        worst = min(zip(coef, active))[1]
        active.remove(worst)
    return [0.0] * width


@dataclass(frozen=True)
class LinearCost:
    """``cost = sum(weight[f] * features[f])``; weights are non-negative."""

    features: tuple[str, ...]
    weights: tuple[float, ...]
    unit: str = "seconds"

    def predict(self, values: Mapping[str, float]) -> float:
        return sum(w * float(values.get(f, 0.0)) for f, w in zip(self.features, self.weights))

    def to_json(self) -> dict:
        return {"unit": self.unit, "weights": dict(zip(self.features, self.weights))}


@dataclass
class Calibration:
    model: LinearCost
    fitted: dict
    cross_validated: dict
    folds: int
    jobs: int
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "model": self.model.to_json(),
            "jobs": self.jobs,
            "error_on_fitted_jobs": self.fitted,
            "error_cross_validated": self.cross_validated,
            "folds": self.folds,
            "notes": list(self.notes),
        }


def fit(samples: Sequence[tuple[Mapping[str, float], float]], features: Sequence[str], *, unit: str = "seconds") -> LinearCost:
    """Fit non-negative weights minimising squared relative error."""

    usable = [(f, m) for f, m in samples if m > 0]
    if not usable:
        raise ValueError("no measured jobs to calibrate on")
    rows = [[float(f.get(name, 0.0)) for name in features] for f, _ in usable]
    targets = [m for _, m in usable]
    weights = [1.0 / (m * m) for m in targets]
    return LinearCost(tuple(features), tuple(_nnls(rows, targets, weights)), unit)


def calibrate(
    samples: Sequence[tuple[Mapping[str, float], float]],
    features: Sequence[str],
    *,
    unit: str = "seconds",
    folds: int = 5,
    seed: int = 11,
) -> Calibration:
    """Fit on all samples and report fitted and k-fold cross-validated q-errors."""

    model = fit(samples, features, unit=unit)
    measured = [m for _, m in samples if m > 0]
    fitted = error_summary([model.predict(f) for f, m in samples if m > 0], measured)
    usable = [(f, m) for f, m in samples if m > 0]
    folds = max(2, min(folds, len(usable)))
    order = list(range(len(usable)))
    random.Random(seed).shuffle(order)
    predicted: list[float] = [0.0] * len(usable)
    for k in range(folds):
        test = set(order[k::folds])
        train = [usable[i] for i in range(len(usable)) if i not in test]
        part = fit(train, features, unit=unit) if train else model
        for i in test:
            predicted[i] = part.predict(usable[i][0])
    cross = error_summary(predicted, [m for _, m in usable])
    return Calibration(model, fitted, cross, folds, len(usable))


__all__ = [
    "Calibration",
    "DAYS_PER_MONTH",
    "GIB",
    "LinearCost",
    "MIN_BYTES_PER_TABLE",
    "Pricing",
    "TIB",
    "billed_bytes",
    "calibrate",
    "error_summary",
    "fit",
    "q_error",
]
