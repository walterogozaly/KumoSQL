"""Choose what to materialize: view selection under a calibrated cost model.

A project's daily cost is the work of everything that runs each day: every
reader query (a dashboard, a scheduled export, a downstream model's refresh)
and every refresh of a materialized model, plus what the stored tables cost to
keep. Which models are stored decides that work. A query that reads a view
recomputes the view each time; a query that reads a table scans its stored
rows, and the table costs a refresh per schedule and its storage.

The problem is the classic view-selection problem (Harinarayan, Rajaraman and
Ullman, SIGMOD 1996), posed here with BigQuery's pricing:

* a :class:`Template` is one kind of work that runs ``runs_per_day`` times; it
  has one or more :class:`Option` s, each with a predicted cost per run and the
  models that must be stored for it to apply (the first option needs nothing:
  it is how the work runs now). Under a set of stored models a template costs
  its cheapest applicable option. A query over a view and the same query over
  its stored copy are two options of one template;
* a :class:`Candidate` is one change a person could accept: store a view as a
  table, turn a table back into a view, or extract repeated logic into a stored
  shared model. It costs ``refresh_per_day`` refreshes and its storage;
* :func:`select` picks the candidates whose combination saves the most per day.
  Up to ``exact_limit`` candidates it tries every combination; beyond that it
  adds the best candidate while one still saves something, then improves the
  set by single additions, removals and swaps. When the problem has the shape
  of uncapacitated facility location (each option needs at most one stored
  model and refresh costs do not depend on each other), :func:`upper_bound`
  gives a Lagrangian bound on the best possible saving, so the gap between the
  chosen set and the optimum is known.

Only candidates whose evidence is ``proven`` can be selected. A change that
needs a condition (fresh enough inputs, an append-only source) is listed with
what it would need, never counted as a saving. Costs are in one unit chosen by
the caller (bytes billed, slot milliseconds, measured seconds or money); units
are never mixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Sequence

#: Evidence labels, strongest first.
EVIDENCE = ("proven", "conditional", "unknown", "changes_results")


@dataclass(frozen=True)
class Evidence:
    """Why a change keeps every result, or what it would need to."""

    label: str
    reason: str
    conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.label not in EVIDENCE:
            raise ValueError(f"unknown evidence label {self.label!r}")

    @property
    def proven(self) -> bool:
        return self.label == "proven"

    def to_json(self) -> dict:
        out: dict = {"label": self.label, "reason": self.reason}
        if self.conditions:
            out["conditions"] = list(self.conditions)
        return out


@dataclass(frozen=True)
class Option:
    """One way to run a template: its predicted cost per run and the stored models it reads."""

    cost: float
    requires: frozenset[str] = frozenset()
    label: str = ""


@dataclass(frozen=True)
class Template:
    """Work that runs ``runs_per_day`` times a day, with the ways it can run."""

    id: str
    runs_per_day: float
    options: tuple[Option, ...]

    def __post_init__(self) -> None:
        if not self.options or self.options[0].requires:
            raise ValueError(f"{self.id}: the first option must need no stored model")

    def option(self, stored: frozenset[str]) -> Option:
        best = self.options[0]
        for option in self.options[1:]:
            if option.cost < best.cost and option.requires <= stored:
                best = option
        return best

    def daily(self, stored: frozenset[str]) -> float:
        return self.runs_per_day * self.option(stored).cost


@dataclass(frozen=True)
class Candidate:
    """One change a person could accept.

    ``node`` is the model whose storage the change flips: ``store`` makes it a
    table (or extracts it as a stored shared model), ``unstore`` makes a table a
    view again. ``refresh_cost`` is per refresh, in the problem's unit.
    """

    id: str
    title: str
    kind: str
    node: str
    refresh_per_day: float
    refresh_cost: float
    storage_bytes: float | None
    evidence: Evidence
    detail: Mapping[str, object] = field(default_factory=dict)
    store: bool = True


@dataclass
class Problem:
    """Templates, candidates and how storage is priced, all in one cost unit."""

    templates: Sequence[Template]
    candidates: Sequence[Candidate]
    unit: str
    #: Cost of keeping one byte stored for a day, in ``unit``; ``None`` leaves storage unpriced.
    storage_per_byte_day: float | None = None
    #: Models stored before any change.
    stored: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self._by_id = {c.id: c for c in self.candidates}
        if len(self._by_id) != len(self.candidates):
            raise ValueError("candidate ids must be unique")

    def candidate(self, cid: str) -> Candidate:
        return self._by_id[cid]

    def stored_after(self, chosen: Iterable[str]) -> frozenset[str]:
        stored = set(self.stored)
        for cid in chosen:
            c = self._by_id[cid]
            (stored.add if c.store else stored.discard)(c.node)
        return frozenset(stored)

    def candidate_daily(self, c: Candidate) -> float:
        """What keeping ``c``'s model stored costs a day: refreshes plus storage."""

        storage = 0.0
        if self.storage_per_byte_day is not None and c.storage_bytes is not None:
            storage = c.storage_bytes * self.storage_per_byte_day
        return c.refresh_per_day * c.refresh_cost + storage

    def daily_cost(self, chosen: Iterable[str] = ()) -> float:
        """Daily cost of the templates, plus the refreshes and storage the chosen changes add or remove."""

        chosen = list(chosen)
        stored = self.stored_after(chosen)
        total = sum(t.daily(stored) for t in self.templates)
        for cid in chosen:
            c = self._by_id[cid]
            total += self.candidate_daily(c) if c.store else -self.candidate_daily(c)
        return total

    def saving(self, chosen: Iterable[str]) -> float:
        return self.daily_cost(()) - self.daily_cost(chosen)

    def storage(self, chosen: Iterable[str]) -> float:
        total = 0.0
        for cid in chosen:
            c = self._by_id[cid]
            if c.storage_bytes:
                total += c.storage_bytes if c.store else -c.storage_bytes
        return total

    @property
    def facility_shaped(self) -> bool:
        """Every option needs at most one stored model, nothing is stored yet and every change stores."""

        return (
            not self.stored
            and all(c.store for c in self.candidates)
            and all(isinstance(t, Template) for t in self.templates)
            and all(len(o.requires) <= 1 for t in self.templates for o in t.options)
        )


@dataclass
class Selection:
    chosen: list[str]
    saving: float
    baseline: float
    method: str
    evaluated: int
    upper_bound: float | None = None
    storage_bytes: float = 0.0

    def to_json(self) -> dict:
        out = {
            "chosen": list(self.chosen),
            "saving_per_day": self.saving,
            "baseline_per_day": self.baseline,
            "method": self.method,
            "combinations_evaluated": self.evaluated,
            "added_storage_bytes": self.storage_bytes,
        }
        if self.upper_bound is not None:
            out["upper_bound_per_day"] = self.upper_bound
            out["share_of_bound"] = (self.saving / self.upper_bound) if self.upper_bound > 0 else 1.0
        return out


def select(
    problem: Problem,
    *,
    budget_bytes: float | None = None,
    exact_limit: int = 12,
    allowed: Iterable[str] | None = None,
) -> Selection:
    """The proven candidates whose combination saves the most per day.

    ``budget_bytes`` caps the extra storage. ``allowed`` restricts the pool
    (default: every proven candidate).
    """

    pool = [c.id for c in problem.candidates if c.evidence.proven]
    if allowed is not None:
        keep = set(allowed)
        pool = [cid for cid in pool if cid in keep]
    baseline = problem.daily_cost(())
    evaluated = 0

    def fits(chosen: Sequence[str]) -> bool:
        return budget_bytes is None or problem.storage(chosen) <= budget_bytes

    if problem.facility_shaped:
        # A change that saves nothing alone saves nothing in any company: facility location is submodular.
        pool = [cid for cid in pool if problem.saving([cid]) > 0]
        evaluated += len(problem.candidates)

    if len(pool) <= exact_limit:
        best: tuple[float, list[str]] = (0.0, [])
        for size in range(1, len(pool) + 1):
            for combo in combinations(pool, size):
                evaluated += 1
                if not fits(combo):
                    continue
                value = problem.saving(combo)
                if value > best[0] + 1e-12:
                    best = (value, list(combo))
        chosen, method = best[1], "exact"
    else:
        chosen, count = _greedy(problem, pool, fits, budget_bytes is not None)
        evaluated += count
        chosen, count = _local_search(problem, pool, chosen, fits)
        evaluated += count
        method = "greedy with local search"
    bound = upper_bound(problem, pool) if problem.facility_shaped and budget_bytes is None else None
    return Selection(
        chosen=sorted(chosen),
        saving=problem.saving(chosen),
        baseline=baseline,
        method=method,
        evaluated=evaluated,
        upper_bound=bound,
        storage_bytes=problem.storage(chosen),
    )


def _greedy(problem: Problem, pool: list[str], fits, per_byte: bool) -> tuple[list[str], int]:
    chosen: list[str] = []
    current = 0.0
    evaluated = 0
    remaining = list(pool)
    while remaining:
        best = None
        for cid in remaining:
            trial = chosen + [cid]
            evaluated += 1
            if not fits(trial):
                continue
            gain = problem.saving(trial) - current
            if gain <= 1e-12:
                continue
            score = gain / max(problem.candidate(cid).storage_bytes or 1.0, 1.0) if per_byte else gain
            if best is None or score > best[0]:
                best = (score, cid, gain)
        if best is None:
            break
        chosen.append(best[1])
        remaining.remove(best[1])
        current += best[2]
    return chosen, evaluated


def _local_search(problem: Problem, pool: list[str], chosen: list[str], fits) -> tuple[list[str], int]:
    """Single additions, removals and swaps until none improves the saving."""

    evaluated = 0
    current = problem.saving(chosen)
    improved = True
    while improved:
        improved = False
        moves: list[list[str]] = []
        outside = [cid for cid in pool if cid not in chosen]
        moves += [chosen + [cid] for cid in outside]
        moves += [[x for x in chosen if x != cid] for cid in chosen]
        moves += [[x for x in chosen if x != out] + [cid] for out in chosen for cid in outside]
        for move in moves:
            evaluated += 1
            if not fits(move):
                continue
            value = problem.saving(move)
            if value > current + 1e-9:
                chosen, current, improved = move, value, True
                break
    return chosen, evaluated


def upper_bound(problem: Problem, pool: Iterable[str] | None = None, *, rounds: int = 400) -> float:
    """A Lagrangian upper bound on the best saving, for facility-shaped problems.

    Saving = sum over templates of the best per-day reduction any stored
    candidate offers, minus each stored candidate's daily cost. Relaxing "each
    template uses at most one candidate" with a price ``lam[t]`` per template
    gives, for every ``lam >= 0``, the bound
    ``sum(lam) + sum over candidates of max(0, sum over t of max(0, gain[t][c] - lam[t]) - cost[c])``.
    Subgradient steps lower it; every value tried is a valid bound.
    """

    if not problem.facility_shaped:
        raise ValueError("the bound needs a facility-shaped problem")
    ids = list(pool) if pool is not None else [c.id for c in problem.candidates if c.evidence.proven]
    node_of = {cid: problem.candidate(cid).node for cid in ids}
    costs = {cid: problem.candidate_daily(problem.candidate(cid)) for cid in ids}
    gains: list[dict[str, float]] = []
    for t in problem.templates:
        base = t.options[0].cost
        row: dict[str, float] = {}
        for cid, node in node_of.items():
            best = min((o.cost for o in t.options if o.requires <= frozenset([node])), default=base)
            gain = t.runs_per_day * (base - best)
            if gain > 0:
                row[cid] = gain
        if row:
            gains.append(row)
    if not gains or not ids:
        return 0.0

    def value(lam: list[float]) -> tuple[float, list[int]]:
        total = sum(lam)
        used = [0] * len(gains)
        for cid in ids:
            inner = 0.0
            touched = []
            for i, row in enumerate(gains):
                g = row.get(cid, 0.0) - lam[i]
                if g > 0:
                    inner += g
                    touched.append(i)
            if inner - costs[cid] > 0:
                total += inner - costs[cid]
                for i in touched:
                    used[i] += 1
        return total, used

    lam = [0.0] * len(gains)
    best, used = value(lam)
    scale = max(max(row.values()) for row in gains)
    for k in range(rounds):
        step = scale / math.sqrt(k + 1)
        lam = [max(0.0, lam[i] - step * (1 - used[i])) for i in range(len(gains))]
        current, used = value(lam)
        best = min(best, current)
    return best


__all__ = [
    "Candidate",
    "EVIDENCE",
    "Evidence",
    "Option",
    "Problem",
    "Selection",
    "Template",
    "select",
    "upper_bound",
]
