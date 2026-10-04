"""Decline every populated sqlglot argument the SMT compiler does not model.

The compiler reads a handful of arguments per node type and used to ignore the rest, so a clause that
sqlglot parses into some other argument (``DEFAULT .. ON CONVERSION ERROR`` on a ``CAST``, ``CLUSTER BY``,
``SETTINGS``, ``FOR UPDATE``, a ``SELECT`` modifier, a join hint, a ``*`` modifier) vanished from the model
and two queries differing only in it were proven equal. :func:`check_args` walks a statement and raises
``UnmodeledConstruct`` when a node of a listed type carries an argument outside that type's allowlist
(an unlisted type is not checked: generic functions already put every argument in the uninterpreted
function's name). Each allowlist names what ``smt_equivalence`` reads or refuses itself, with both spellings
of the ``from`` and ``with`` keys that sqlglot 26 and 30 use.

A new modeled argument has to be added here as well. That is the point: forgetting it declines a proof,
and forgetting to refuse an argument can no longer prove a wrong one.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import UnmodeledConstruct

_TAIL = frozenset({"limit", "offset", "order", "with", "with_"})
_SELECT = frozenset(
    {
        "expressions", "from", "from_", "joins", "where", "group", "having", "distinct", "kind",
        "qualify", "laterals", "pivots", "connect", "match", "prewhere", "windows", "into",
    }
) | _TAIL  # the clauses in the second row are refused by the compiler itself
_SET_OPERATION = frozenset({"this", "expression", "distinct", "by_name", "side", "kind", "on"}) | _TAIL
_SUBQUERY = frozenset({"this", "alias", "pivots"}) | _TAIL

ALLOWED: dict[type, frozenset[str]] = {
    exp.Select: _SELECT,
    exp.Union: _SET_OPERATION,
    exp.Intersect: _SET_OPERATION,
    exp.Except: _SET_OPERATION,
    exp.Subquery: _SUBQUERY,
    exp.Join: frozenset({"this", "on", "side", "kind", "method", "using"}),
    exp.Table: frozenset({"this", "db", "catalog", "alias", "pivots", "laterals"}),
    exp.Star: frozenset({"except", "except_", "replace", "rename"}),  # what smt_equivalence._star_columns models; ILIKE is declined
    exp.Column: frozenset({"this", "table", "db", "catalog"}),
    exp.Ordered: frozenset({"this", "desc", "nulls_first"}),
    exp.Distinct: frozenset({"expressions", "on"}),
    exp.Cast: frozenset({"this", "to"}),
}


def check_args(tree: exp.Expression) -> exp.Expression:
    """``tree`` unchanged, or ``UnmodeledConstruct`` when a listed node holds an argument the compiler ignores."""

    for node in tree.walk():
        allowed = ALLOWED.get(type(node))
        if allowed is None:
            continue
        for key, value in node.args.items():
            if value in (None, False, [], "") or key.startswith("_"):
                continue
            if key not in allowed:
                raise UnmodeledConstruct(f"{type(node).__name__}.{key} is not modeled by the SMT prover")
    return tree
