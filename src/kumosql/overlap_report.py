"""The "already done elsewhere" section shared by change reports and the graph page.

``OverlapChecker`` wraps ``find_overlaps`` and ``find_rollups`` for one pipeline
and returns a JSON-ready section:

* ``matches``: existing tables that provide the same attributes, ranked by
  match kind (``same_meaning``, ``contains``, ``partial``), each with its
  ``checks`` (``lineage``, ``grain``, ``row_scope`` and their outcomes), the
  table's inferred ``role`` and the evidence for it,
* ``unknown``: tables whose comparison could not be completed, with the reason,
* ``rollups``: tables that hold the same attribute at a finer grain,
* ``summary``: always states how many tables were compared, how many were
  skipped and why. A scope that excludes a table is a skip, never "no match".

A failure while comparing never raises: the section becomes
``status: "unavailable"`` with an error class name (never the message, which
could carry query text) and the caller's report is unaffected (#39). Sections
describe behavior only; they carry no query text.
"""

from __future__ import annotations

from typing import Callable, Mapping

from .overlap import find_overlaps
from .pipeline import Pipeline
from .rollups import find_rollups
from .scopes import Scope
from .table_roles import TableRole, infer_roles

__all__ = ["OverlapChecker", "unavailable_section"]

MAX_ROLE_EVIDENCE = 4
#: changed models compared per report; each comparison profiles the whole pipeline
MAX_COMPARED_MODELS = 40


def unavailable_section(reason: str) -> dict:
    """A section for a comparison that could not be made."""

    return {
        "status": "unavailable",
        "reason": reason,
        "summary": f"The comparison could not be completed ({reason}); no tables were compared",
        "compared": 0,
        "skipped": {},
        "candidates_in_scope": 0,
        "matches": [],
        "unknown": [],
        "rollups": [],
    }


class OverlapChecker:
    """Overlap and roll-up sections for the models of one pipeline.

    ``scope`` limits the candidates (see ``find_overlaps``). ``name_of`` maps a
    pipeline key to the name shown in the section (default: the key).
    """

    def __init__(
        self,
        pipeline: Pipeline,
        *,
        scope: Scope | None = None,
        name_of: Callable[[str], str] | None = None,
    ):
        self.pipeline = pipeline
        self.scope = scope
        self.name_of = name_of or (lambda key: key)
        self._roles: Mapping[str, TableRole] | None = None

    @property
    def roles(self) -> Mapping[str, TableRole]:
        if self._roles is None:
            try:
                self._roles = infer_roles(self.pipeline)
            except Exception:  # noqa: BLE001 - roles are context, not a requirement
                self._roles = {}
        return self._roles

    def section(self, model: str, *, changed: Mapping[str, str] | None = None) -> dict:
        """The section for ``model``; never raises.

        ``changed`` maps pipeline keys to ``added`` or ``modified`` for the
        change being reported. A match that is itself in that set is flagged
        ``in_this_change``: both sides are new or edited, so neither is settled.
        """

        try:
            return self._section(model, changed or {})
        except Exception as exc:  # noqa: BLE001 - one comparison must not sink a report
            return unavailable_section(f"compare_error: {type(exc).__name__}")

    def retired_matches(self, sql: str, retired: set[str]) -> list[dict]:
        """Matches of ``sql`` among ``retired`` tables of this (base) pipeline; never raises.

        A table the change removes is not in the head pipeline, so a new table
        that duplicates it is a replacement, not a duplicate. Undecided
        comparisons are left out: they carry no claim either way.
        """

        try:
            found = find_overlaps(self.pipeline, sql=sql, scope=self.scope, roles=self.roles)
            out = []
            for match in found.matches:
                if match.table in retired and match.kind != "unknown":
                    item = self._match(match, {})
                    item["retiring"] = True
                    out.append(item)
            return out
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------- internals

    def _section(self, model: str, changed: Mapping[str, str]) -> dict:
        found = find_overlaps(self.pipeline, model=model, scope=self.scope, roles=self.roles)
        matches, unknown = [], []
        for match in found.matches:
            item = self._match(match, changed)
            (unknown if match.kind == "unknown" else matches).append(item)
        for rank, item in enumerate(matches, start=1):
            item["rank"] = rank
        section = {
            "status": "ok",
            "summary": found.summary,
            "compared": found.compared,
            "skipped": dict(sorted(found.skipped.items())),
            "candidates_in_scope": found.candidates_in_scope,
            "matches": matches,
            "unknown": unknown,
            "rollups": [],
        }
        try:
            rolled = find_rollups(self.pipeline, model=model, scope=self.scope, roles=self.roles)
            section["rollups"] = [self._rollup(r) for r in rolled.rollups if r.attribute is not None]
            section["rollups_summary"] = rolled.summary
        except Exception as exc:  # noqa: BLE001 - roll-ups must not hide the overlaps
            section["rollups_summary"] = f"Roll-up comparison could not be completed (compare_error: {type(exc).__name__})"
        return section

    def _role(self, key: str, role) -> dict | None:
        """The role with the signals it rests on (kind, direction, strength, detail)."""

        if role is None:
            return None
        info = self.roles.get(key)
        signals = [
            {"kind": s.kind, "points_to": s.points_to, "strength": s.strength, "detail": s.detail}
            for s in (getattr(info, "signals", ()) or ())
            if getattr(s, "available", True)
        ][:MAX_ROLE_EVIDENCE]
        return {**role.to_json(), "reason": getattr(info, "reason", ""), "evidence": signals}

    def _match(self, match, changed: Mapping[str, str]) -> dict:
        data = match.to_json()
        data["role"] = self._role(match.table, match.role)
        data["key"] = match.table
        data["table"] = self.name_of(match.table)
        data["in_this_change"] = match.table in changed
        data["retiring"] = False
        return data

    def _rollup(self, item) -> dict:
        data = item.to_json()
        data["role"] = self._role(item.table, item.role)
        data["key"] = item.table
        data["table"] = self.name_of(item.table)
        return data


def mark_retiring(section: dict, extra: list[dict], removed_note: str) -> dict:
    """Add matches found in the base snapshot for tables the change retires.

    Such a table is not in the head pipeline, so it is compared in the base one.
    """

    if section.get("status") != "ok" or not extra:
        return section
    order = {"same_meaning": 0, "contains": 1, "partial": 2}
    conf = {"high": 0, "medium": 1, "low": 2}
    matches = section["matches"] + extra
    matches.sort(key=lambda m: (order.get(m["kind"], 3), conf.get(m["confidence"], 3), m["table"]))
    for rank, item in enumerate(matches, start=1):
        item["rank"] = rank
    section["matches"] = matches
    section["summary"] += f". {len(extra)} more {'match is' if len(extra) == 1 else 'matches are'} {removed_note}"
    return section
