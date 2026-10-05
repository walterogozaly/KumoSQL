"""``a JOIN b USING (k)`` as ``a JOIN b ON a.k = b.k`` when the tables' columns are not known.

``_using_to_on`` in the algebraic prover needs a schema to find which source owns each column. Most joins
do not: the first ``USING`` join reads its left column from the ``FROM`` source, and a later one from the
merged column an earlier ``USING`` already produced. This reads exactly those, for a select that lists
columns (no ``*``), and leaves anything else untouched.

The merged column ``k`` comes from the left source (the right one for ``RIGHT JOIN``, ``COALESCE`` of both
for ``FULL``). Unqualified uses of ``k`` in the same select read that value; a bare select item keeps the
name ``k``.
"""

from __future__ import annotations

from sqlglot import exp

from . import proof_columns


def _alias(source: exp.Expression) -> str:
    return (source.alias_or_name or "").lower() if isinstance(source, (exp.Table, exp.Subquery)) else ""


def _and_all(parts: list[exp.Expression]) -> exp.Expression:
    out = parts[0]
    for part in parts[1:]:
        out = exp.And(this=out, expression=part)
    return out


def using_to_on_unqualified(tree: exp.Expression) -> exp.Expression:
    for select in list(tree.find_all(exp.Select))[::-1]:
        joins = select.args.get("joins") or []
        if not any(j.args.get("using") is not None for j in joins):
            continue
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            continue
        guard = proof_columns.begin(select, "algebraic_using_to_on_unqualified")
        sources = [from_.this] + [j.this for j in joins]
        aliases = [_alias(s) for s in sources]
        if "" in aliases or len(set(aliases)) != len(aliases):
            continue
        if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in select.expressions):
            continue
        merged: dict[str, exp.Expression] = {}
        new_joins: list[exp.Join] = []
        ok = True
        for index, join in enumerate(joins, start=1):
            using = join.args.get("using")
            if using is None:
                new_joins.append(join)
                continue
            if join.args.get("kind") in ("SEMI", "ANTI", "CROSS") or join.args.get("method") or join.args.get("on") is not None:
                ok = False
                break
            side = join.args.get("side")
            conditions = []
            fresh: dict[str, exp.Expression] = {}
            for ident in using:
                name = ident.name.lower()
                if name in merged:
                    left = merged[name]
                elif index == 1:
                    left = exp.column(name, table=exp.to_identifier(aliases[0]))
                else:
                    ok = False  # which earlier source owns the column is not known
                    break
                right = exp.column(name, table=exp.to_identifier(aliases[index]))
                conditions.append(exp.EQ(this=left.copy(), expression=right.copy()))
                fresh[name] = {"RIGHT": right, "FULL": exp.Coalesce(this=left.copy(), expressions=[right.copy()])}.get(side, left)
            if not ok:
                break
            merged.update(fresh)
            new_join = join.copy()
            new_join.set("using", None)
            new_join.set("on", _and_all(conditions))
            new_joins.append(new_join)
        if not ok:
            continue
        # A bare reference to a merged column from a nested query could be a correlated read of it; leave those joins alone.
        if any(
            not c.table and c.name.lower() in merged and c.find_ancestor(exp.Select) is not select
            for c in select.find_all(exp.Column)
        ):
            continue
        guard.snapshot()
        for column in list(select.find_all(exp.Column)):
            if column.table or isinstance(column.this, exp.Star) or column.name.lower() not in merged:
                continue
            value = proof_columns.rebuilt(merged[column.name.lower()].copy(), column)
            if isinstance(column.parent, exp.Select) and isinstance(value, exp.Coalesce):
                value = exp.alias_(value, column.name)
            column.replace(value)
        select.set("joins", new_joins)
        _check(guard, select)
    return tree


def _check(guard, select: exp.Select) -> None:
    """Have ``proof_columns`` re-read the statement after the rewrite; a column that reads other base columns declines the query."""

    from .ast_utils import UnmodeledConstruct

    try:
        guard.check(select.root())
    except proof_columns.ColumnResolutionRefused as refusal:
        raise UnmodeledConstruct(f"independent check of column resolution: {refusal}") from None
