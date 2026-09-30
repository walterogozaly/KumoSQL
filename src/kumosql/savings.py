"""Ledger of accepted changes and the savings they are validated to deliver.

Success is validated savings from accepted changes, never a count of
suggestions. A suggestion nobody accepted contributes nothing.

A saving claim is always one of three bases:

* ``estimate``: a planner figure recorded when the change is accepted.
* ``upper_bound``: a ceiling that the change cannot exceed, not a forecast.
* ``measured``: observed cost before and after deployment, from comparable
  windows with at least one run on each side.

Savings are ``before - after``: positive is a reduction and negative is a
regression. Regressions are kept, never clamped. Measured and unmeasured
figures are never added into the same total.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import state

SECTION = "savings"
MAX_ENTRIES = 1000
DEFAULT_UNIT = "bytes_scanned"

BASES = ("measured", "estimate", "upper_bound")
STATES = ("accepted", "deployed", "reverted")
BLOCKED_EVIDENCE = ("unproven", "failed")


class LedgerError(ValueError):
    """Raised when a record would misrepresent a saving."""


@dataclass(frozen=True)
class CostWindow:
    """Plain measured cost over a window of runs (input from cost observation)."""

    value: float
    n_runs: int
    window: str

    def validate(self) -> None:
        if self.n_runs < 1:
            raise LedgerError("a measured window needs at least one run")
        if not self.window:
            raise LedgerError("a measured window needs a window label")
        if self.value < 0:
            raise LedgerError("a measured cost cannot be negative")

    def to_json(self) -> dict:
        return {"value": self.value, "n_runs": self.n_runs, "window": self.window}

    @classmethod
    def from_json(cls, data: dict) -> "CostWindow":
        return cls(float(data["value"]), int(data["n_runs"]), str(data["window"]))


@dataclass(frozen=True)
class SavingClaim:
    basis: str = "estimate"
    unit: str = DEFAULT_UNIT
    method: str = ""
    value: float = 0.0  # saving = before - after
    before: Optional[CostWindow] = None
    after: Optional[CostWindow] = None
    caveats: tuple = ()

    def __post_init__(self):
        if self.basis not in BASES:
            raise LedgerError(f"unknown basis {self.basis!r}")
        if self.basis == "measured":
            if self.before is None or self.after is None:
                raise LedgerError("a measured saving needs before and after windows")
            self.before.validate()
            self.after.validate()
            object.__setattr__(self, "value", self.before.value - self.after.value)

    def to_json(self) -> dict:
        out = {
            "basis": self.basis,
            "unit": self.unit,
            "method": self.method,
            "value": self.value,
            "caveats": list(self.caveats),
        }
        if self.before is not None:
            out["before"] = self.before.to_json()
        if self.after is not None:
            out["after"] = self.after.to_json()
        return out

    @classmethod
    def from_json(cls, data: dict) -> "SavingClaim":
        return cls(
            basis=data["basis"],
            unit=data.get("unit", DEFAULT_UNIT),
            method=data.get("method", ""),
            value=float(data.get("value", 0.0)),
            before=CostWindow.from_json(data["before"]) if data.get("before") else None,
            after=CostWindow.from_json(data["after"]) if data.get("after") else None,
            caveats=tuple(data.get("caveats", ())),
        )


@dataclass(frozen=True)
class AcceptedChange:
    id: str
    recommendation_id: str
    models: tuple = ()
    accepted_at: str = ""
    source_ref: str = ""
    state: str = "accepted"
    estimate: Optional[SavingClaim] = None
    measured: Optional[SavingClaim] = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "recommendation_id": self.recommendation_id,
            "models": list(self.models),
            "accepted_at": self.accepted_at,
            "source_ref": self.source_ref,
            "state": self.state,
            "estimate": self.estimate.to_json() if self.estimate else None,
            "measured": self.measured.to_json() if self.measured else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> "AcceptedChange":
        return cls(
            id=data["id"],
            recommendation_id=data["recommendation_id"],
            models=tuple(data.get("models", ())),
            accepted_at=data.get("accepted_at", ""),
            source_ref=data.get("source_ref", ""),
            state=data.get("state", "accepted"),
            estimate=SavingClaim.from_json(data["estimate"]) if data.get("estimate") else None,
            measured=SavingClaim.from_json(data["measured"]) if data.get("measured") else None,
        )


class Ledger:
    """In-memory ledger; use :func:`load` and :func:`save` for local persistence."""

    def __init__(self, changes: Iterable[AcceptedChange] = ()):
        self._changes = {change.id: change for change in changes}

    def __iter__(self):
        return iter(self._changes.values())

    def __len__(self):
        return len(self._changes)

    def get(self, change_id: str) -> AcceptedChange:
        try:
            return self._changes[change_id]
        except KeyError:
            raise LedgerError(f"no accepted change {change_id!r}") from None

    def accept(
        self,
        change_id: str,
        recommendation_id: str,
        models: Iterable[str] = (),
        estimate: Optional[SavingClaim] = None,
        evidence: str = "proven",
        source_ref: str = "",
        accepted_at: Optional[str] = None,
    ) -> AcceptedChange:
        """Record an accepted change with the estimate made at proposal time."""

        if change_id in self._changes:
            raise LedgerError(f"change {change_id!r} is already accepted")
        if estimate is not None and estimate.basis == "measured":
            raise LedgerError("an estimate recorded at accept time cannot be measured")
        if estimate is not None and evidence in BLOCKED_EVIDENCE:
            raise LedgerError(f"a change with {evidence} evidence cannot carry a savings claim")
        change = AcceptedChange(
            id=change_id,
            recommendation_id=recommendation_id,
            models=tuple(models),
            accepted_at=accepted_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source_ref=source_ref,
            estimate=estimate,
        )
        self._changes[change_id] = change
        return change

    def mark_deployed(self, change_id: str) -> AcceptedChange:
        return self._set_state(change_id, "deployed")

    def revert(self, change_id: str) -> AcceptedChange:
        return self._set_state(change_id, "reverted")

    def _set_state(self, change_id: str, new_state: str) -> AcceptedChange:
        change = self.get(change_id)
        if change.state == "reverted":
            raise LedgerError("a reverted change cannot change state")
        updated = replace(change, state=new_state)
        self._changes[change_id] = updated
        return updated

    def record_measurement(
        self,
        change_id: str,
        before: CostWindow,
        after: CostWindow,
        unit: str = DEFAULT_UNIT,
        method: str = "before_after_windows",
        caveats: Iterable[str] = (),
    ) -> AcceptedChange:
        """Attach measured before/after cost; only a deployed change qualifies."""

        change = self.get(change_id)
        if change.state != "deployed":
            raise LedgerError("only a deployed change can be measured")
        claim = SavingClaim("measured", unit, method, before=before, after=after, caveats=tuple(caveats))
        updated = replace(change, measured=claim)
        self._changes[change_id] = updated
        return updated

    def summary(self, unit: str = DEFAULT_UNIT, min_runs: int = 1) -> dict:
        """The ``validated`` payload the UI expects, plus separate detail.

        ``validated_savings`` sums only deployed changes with a measured claim
        of at least ``min_runs`` runs on each side. ``pending_estimates`` sums
        estimates for live changes not yet validated and is reported apart.
        Reverted changes and figures in another unit are excluded from both.
        """

        validated = 0.0
        pending = 0.0
        accepted = 0
        validated_count = 0
        regressions = []
        other_units = []
        for change in self:
            if change.state == "reverted":
                continue
            accepted += 1
            m = change.measured
            if (
                change.state == "deployed"
                and m is not None
                and m.unit == unit
                and m.before.n_runs >= min_runs
                and m.after.n_runs >= min_runs
            ):
                validated += m.value
                validated_count += 1
                if m.value < 0:
                    regressions.append(change.id)
                continue
            est = change.estimate
            if est is None:
                continue
            if est.unit != unit:
                other_units.append(change.id)
                continue
            pending += est.value
        return {
            "accepted_changes": accepted,
            "validated_savings": validated,
            "pending_estimates": pending,
            "unit": unit,
            "validated_changes": validated_count,
            "regressions": regressions,
            "other_units": other_units,
        }

    def to_json(self) -> list:
        return [change.to_json() for change in self]


def load() -> Ledger:
    raw = state.get_section(SECTION, [])
    if not isinstance(raw, list):
        return Ledger()
    changes = []
    for item in raw:
        try:
            changes.append(AcceptedChange.from_json(item))
        except (KeyError, TypeError, ValueError, LedgerError):
            continue
    return Ledger(changes)


def save(ledger: Ledger) -> None:
    state.set_section(SECTION, ledger.to_json()[-MAX_ENTRIES:])


def claim_json(claim: Optional[SavingClaim]) -> Optional[dict]:
    """UI shape for one claim: ``{value, basis}``; no range is invented."""

    if claim is None:
        return None
    return {"value": claim.value, "basis": claim.basis}
