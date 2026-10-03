"""A LEFT JOIN on the dropped side of a later outer join whose ON rejects its padded rows is inner.

``a LEFT JOIN b ON c RIGHT JOIN d ON b.x > 10`` is ``a JOIN b ON c RIGHT JOIN d ON b.x > 10``. The
RIGHT JOIN keeps every row of ``d``, but a row of its left operand (``a LEFT JOIN b``) only reaches
the result by meeting some row of ``d`` with the ON clause TRUE. The rows the LEFT JOIN pads carry
NULL for every column of ``b``, so ``b.x > 10`` is never TRUE on them: they meet no row of ``d``,
and dropping them changes neither the matched rows nor which rows of ``d`` go unmatched (and are
padded). The same holds for ``x LEFT JOIN (SELECT .. b.y AS v .. FROM a LEFT JOIN b ON c) AS t
ON t.v > 10``: the ON clause reads only the derived table's outputs, a padded row of ``b`` makes
``v`` NULL, so those rows of ``t`` never match and ``t`` may as well use an inner join.

Only the side an outer join drops when unmatched is read: the right source of a LEFT JOIN and the
sources before a RIGHT JOIN, never a FULL JOIN. Between the LEFT JOIN that is made inner and the
outer join that rejects its rows there may only be inner, cross and LEFT joins, so every row built
from a padded row still carries its NULLs. A condition rejects a table as in
``null_rejecting_joins.rejected_tables`` (strict comparisons, AND, OR of rejecting parts).
"""

from __future__ import annotations

from sqlglot import exp

from .null_rejecting_joins import _convert, _plain_derived, _through_derived, rejected_tables


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")


def _name(source: exp.Expression) -> str:
    if isinstance(source, exp.Table):
        return (source.alias_or_name or "").lower()
    if isinstance(source, exp.Subquery) and source.alias:
        return source.alias.lower()
    return ""


def _plain_join(join: exp.Join) -> bool:
    kind = (join.args.get("kind") or "").upper()
    return not join.args.get("method") and not join.args.get("using") and kind in ("", "INNER", "CROSS", "OUTER")


def _strengthen_derived(source: exp.Expression, on: exp.Expression) -> bool:
    """Make inner (in place) the LEFT joins inside derived ``source`` whose padded rows ``on`` rejects."""

    if not isinstance(source, exp.Subquery) or not _plain_derived(source.this):
        return False
    rejected = _through_derived(on, _name(source), source.this, False)
    return bool(rejected) and _convert(source.this, rejected)


def strengthen_under_outer_on(select: exp.Select) -> exp.Expression | None:
    """``select`` with LEFT joins made inner where a later outer join's ON rejects their padded rows, or None."""

    from_ = _from(select)
    joins = select.args.get("joins") or []
    if from_ is None or not joins or select.args.get("laterals") or not all(_plain_join(j) for j in joins):
        return None
    sources = [from_.this] + [j.this for j in joins]
    names = [_name(s) for s in sources]
    if "" in names or len(set(names)) != len(names):
        return None
    sides = [(j.args.get("side") or "").upper() for j in joins]
    if not any(side in ("LEFT", "RIGHT") for side in sides):
        return None
    copy = select.copy()
    copy_sources = [_from(copy).this] + [j.this for j in copy.args["joins"]]
    changed = False
    for k, join in enumerate(copy.args["joins"]):
        on = join.args.get("on")
        if on is None or sides[k] not in ("LEFT", "RIGHT"):
            continue
        if sides[k] == "LEFT":
            # the dropped side is the joined source alone
            changed |= _strengthen_derived(copy_sources[k + 1], on)
            continue
        # RIGHT: the dropped side is everything joined before; only inner, cross and LEFT joins there
        if any(side not in ("", "LEFT") for side in sides[:k]):
            continue
        rejected = rejected_tables(on)
        for j in range(k):
            if sides[j] == "LEFT" and names[j + 1] in rejected:
                copy.args["joins"][j].set("side", None)
                copy.args["joins"][j].set("kind", None)
                sides[j] = ""
                changed = True
        for i in range(k + 1):
            changed |= _strengthen_derived(copy_sources[i], on)
    return copy if changed else None
