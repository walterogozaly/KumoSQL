"""``FLOOR(x TO unit)`` truncations that nest.

Truncating a timestamp to a coarser unit after truncating it to a finer one is the same as truncating it
once: ``FLOOR(FLOOR(x TO SECOND) TO MINUTE)`` is ``FLOOR(x TO MINUTE)``. That is what lets a view grouped by the
finer truncation answer a query grouped by the coarser one (``model_reuse`` reads the view's column through the
coarser truncation, and this rule lets the prover see that the two spellings agree).

Only units that really nest are related: SECOND < MINUTE < HOUR < DAY, then DAY < WEEK and
DAY < MONTH < QUARTER < YEAR. A WEEK does not nest into a MONTH (a week can span two), so those pairs are left alone,
and so is any unit not listed here.
"""

from __future__ import annotations

from sqlglot import exp

# unit -> the units it nests into (itself included): truncating to the key and then to any value equals truncating once
_COARSER: dict[str, frozenset[str]] = {}


def _chain(*units: str) -> None:
    for i, unit in enumerate(units):
        _COARSER[unit] = _COARSER.get(unit, frozenset()) | frozenset(units[i:])


_chain("second", "minute", "hour", "day", "week")
_chain("day", "month", "quarter", "year")
_chain("hour", "day", "month", "quarter", "year")
_chain("minute", "hour", "day", "month", "quarter", "year")
_chain("second", "minute", "hour", "day", "month", "quarter", "year")


def floor_unit(node: exp.Expression) -> str | None:
    """The unit of ``FLOOR(x TO unit)`` (lower case), or None for any other expression."""

    if isinstance(node, exp.Floor) and node.args.get("to") is not None and not node.args.get("decimals"):
        unit = node.args["to"]
        name = getattr(unit, "name", "") or ""
        return name.lower() or None
    return None


def nests_into(finer: str, coarser: str) -> bool:
    """Whether truncating to ``finer`` and then to ``coarser`` equals truncating to ``coarser``."""

    return coarser in _COARSER.get(finer, frozenset())


def collapse_nested_floor(tree: exp.Expression) -> exp.Expression:
    """``FLOOR(FLOOR(x TO a) TO b)`` as ``FLOOR(x TO b)`` when ``a`` nests into ``b``."""

    def visit(node: exp.Expression) -> exp.Expression:
        outer = floor_unit(node)
        if outer is None:
            return node
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        finer = floor_unit(inner)
        if finer is not None and nests_into(finer, outer):
            copy = node.copy()
            copy.set("this", inner.this.copy())
            return copy
        return node

    return tree.transform(visit)


def floor_from_finer(node: exp.Expression, lookup) -> exp.Expression | None:
    """``FLOOR(y TO b)`` read from a column that holds ``FLOOR(y TO a)`` for a finer unit ``a`` that nests into ``b``.

    ``lookup(expression)`` returns the column expression that holds ``expression``, or None. The result is
    ``FLOOR(column TO b)``; it is only a proposal, and the prover checks that it equals ``node``."""

    unit = floor_unit(node)
    if unit is None:
        return None
    for finer, coarser in _COARSER.items():
        if finer == unit or unit not in coarser:
            continue
        for spelling in {finer, finer.upper()}:
            candidate = node.copy()
            candidate.set("to", exp.Var(this=spelling))
            column = lookup(candidate)
            if column is not None:
                rolled = node.copy()
                rolled.set("this", column)
                return rolled
    return None
