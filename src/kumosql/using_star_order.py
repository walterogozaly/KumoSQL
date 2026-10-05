"""``SELECT *`` over a ``USING`` join, with the merged columns first.

The SQL standard (and PostgreSQL, and the prover) list the columns a ``USING`` join merges once, first, and
then the remaining columns of each side. sqlglot's star expansion keeps table order instead, so a replacement
built from its expansion has the right columns in the wrong positions and the prover rightly rejects it.
This expands such a star the way the prover reads it, before sqlglot sees the query.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from sqlglot import exp


def _has_using_star(tree: exp.Expression) -> bool:
    for select in tree.find_all(exp.Select):
        joins = select.args.get("joins") or []
        if not any(j.args.get("using") is not None or str(j.args.get("method") or "").upper() == "NATURAL" for j in joins):
            continue
        if any(isinstance(i, exp.Star) for i in select.expressions):
            return True
    return False


def using_star_order(tree: exp.Expression, schema: Mapping[str, Sequence[str]]) -> exp.Expression:
    """``tree`` with every bare ``*`` over a ``USING`` or ``NATURAL`` join spelled out in standard order.

    Selects the expansion cannot read (a source whose columns are unknown) are left as written."""

    if not _has_using_star(tree):
        return tree
    from .algebraic_equivalence import _expand_stars, _using_to_on

    known = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}
    return _using_to_on(_expand_stars(tree, known), known)  # a derived table's own star is spelled out first
