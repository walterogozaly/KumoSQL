"""Explain a query difference as a verified predicate: "equivalent except when P" (issue #512).

This module is the contract the core, the eval and the surfaces build on. Until the core lands,
``explain_difference`` returns None, which means "no verified predicate" (unknown beats wrong).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DifferenceExplanation:
    """A predicate P, verified both ways, such that the pair is equivalent except on rows where P holds."""

    sql: str  # the predicate over one table's columns, for example "status IS NULL"
    atoms: tuple[str, ...]  # the atoms the predicate is built from, as SQL text
    tables: tuple[str, ...]  # lower-case tables the predicate reads
    exact: bool  # True when the outputs differ exactly where P holds, False when P only covers the difference
    witness: dict = field(default_factory=dict)  # a database (table -> rows) with one row satisfying P where the outputs differ

    def to_json(self) -> dict:
        return {"sql": self.sql, "atoms": list(self.atoms), "tables": list(self.tables), "exact": self.exact}


def explain_difference(left_sql: str, right_sql: str, **prover_options) -> DifferenceExplanation | None:
    """Return a verified difference predicate for two non-equivalent queries, or None when none is verified."""
    return None
