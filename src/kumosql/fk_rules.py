"""Drop an inner join that a declared foreign key makes redundant.

``SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id`` is
``SELECT id FROM orders`` when three things are declared: ``orders(customer_id)``
references ``customers(id)`` (every non-NULL value has a parent), ``orders.customer_id``
is NOT NULL (no row is lost to a NULL), and ``customers(id)`` is unique (no row is
repeated). Take any one away and the rewrite changes results, which is why all three must
be declared; the prover's assumptions list them. Reads of the parent's joined columns
become reads of the equal child columns (``c.id`` is ``o.customer_id``); any other read of
the parent keeps the join.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts as _conjuncts


def _same_table(spelled: str, declared: str) -> bool:
    a, b = spelled.lower().split("."), declared.lower().split(".")
    short = min(len(a), len(b))
    return a[-short:] == b[-short:]


def drop_fk_join(select: exp.Select, keys, not_null, foreign_keys) -> exp.Expression | None:
    result = _drop_fk_join(select, keys, not_null, foreign_keys)
    if result is not None or not foreign_keys:
        return result
    # INNER JOIN is symmetric. The parent may be the FROM source instead.
    source = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    if source is None or len(joins) != 1 or not isinstance(source.this, exp.Table):
        return None
    join = joins[0]
    if (not isinstance(join.this, exp.Table) or join.side or join.kind not in ("", "INNER")
            or join.args.get("using") or select.args.get("laterals")):
        return None
    swapped = select.copy()
    swapped.args["joins"][0].set("this", source.this.copy())
    (swapped.args.get("from_") or swapped.args.get("from")).set("this", join.this.copy())
    return _drop_fk_join(swapped, keys, not_null, foreign_keys)


def _padded_before(all_sources, sides, child_alias: str, index: int) -> bool:
    """Whether the child's rows can be NULL-extended when the join at ``index`` (source ``index + 1``) runs.

    The child's own LEFT or FULL join pads it, and so does a RIGHT or FULL join between the child and this join.
    """

    position = next((i for i, s in enumerate(all_sources) if (s.alias_or_name or "").lower() == child_alias), None)
    if position is None:
        return True
    return (position > 0 and sides[position - 1] in ("LEFT", "FULL")) or any(side in ("RIGHT", "FULL") for side in sides[position:index])


def _drop_fk_join(select: exp.Select, keys, not_null, foreign_keys) -> exp.Expression | None:
    if not foreign_keys:
        return None
    joins = select.args.get("joins") or []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or not joins:
        return None
    sources = {}
    all_sources = [from_.this] + [j.this for j in joins]
    join_sides = [(j.args.get("side") or "").upper() for j in joins]
    for source in all_sources:
        if isinstance(source, exp.Table):
            sources[source.alias_or_name.lower()] = source
    if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)):
        return None
    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}
    for index, join in enumerate(joins):
        parent = join.this
        side = (join.args.get("side") or "").upper()
        if not isinstance(parent, exp.Table) or side or (join.args.get("kind") or "").upper() not in ("", "INNER") or join.args.get("on") is None:
            continue
        p_alias = parent.alias_or_name.lower()
        pairs: dict[str, str] = {}  # parent column -> child column
        child_alias = None
        ok = True
        for part in _conjuncts(join.args["on"]):
            sides = (part.this, part.expression) if isinstance(part, exp.EQ) else ()
            mine = [x for x in sides if isinstance(x, exp.Column) and x.table.lower() == p_alias]
            other = [x for x in sides if x not in mine]
            if len(mine) != 1 or len(other) != 1 or not isinstance(other[0], exp.Column) or not other[0].table or other[0].table.lower() == p_alias:
                ok = False
                break
            if child_alias not in (None, other[0].table.lower()):
                ok = False
                break
            child_alias = other[0].table.lower()
            if pairs.setdefault(mine[0].name.lower(), other[0].name.lower()) != other[0].name.lower():
                ok = False  # one parent column equated with two child columns
                break
        child = sources.get(child_alias or "")
        if not ok or child is None or not pairs:
            continue
        if len(set(pairs.values())) != len(pairs):
            continue  # one child column equated with two parent columns: the ON clause says more than the foreign key
        child_name, parent_name = child.name.lower(), parent.name.lower()
        declared = {c.lower() for c in (not_null or {}).get(child_name, frozenset())}
        if _padded_before(all_sources, join_sides, child_alias, index):
            declared = set()  # a null-extended child row has NULL in every column; only a WHERE test can rule it out
        where = select.args.get("where")
        for part in (_conjuncts(where.this) if where is not None else []):
            # a WHERE conjunct ``child.col IS NOT NULL`` makes the column non-NULL for every row that is kept
            if isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null):
                column = part.this.this
                if isinstance(column, exp.Column) and column.table.lower() == child_alias:
                    declared.add(column.name.lower())
        wanted = {child_col: parent_col for parent_col, child_col in pairs.items()}
        covered = any(
            _same_table(parent_name, fk_parent) and {c.lower(): p.lower() for c, p in zip(cols, parent_cols)} == wanted
            for cols, fk_parent, parent_cols in (foreign_keys.get(child_name) or [])
        )
        if not covered or not set(pairs.values()) <= declared or not any(k <= set(pairs) for k in key_sets.get(parent_name, [])):
            continue
        copy = select.copy()
        copy_join = copy.args["joins"][index]
        output_names = [item.output_name for item in copy.expressions]
        replaced = False
        if any(not c.table for c in copy.find_all(exp.Column) if c.find_ancestor(exp.Join) is not copy_join):
            continue  # an unqualified column may be the parent's
        for column in list(copy.find_all(exp.Column)):
            if column.find_ancestor(exp.Join) is copy_join or column.table.lower() != p_alias:
                continue
            if column.name.lower() not in pairs:
                replaced = None
                break
            column.replace(exp.column(pairs[column.name.lower()], table=child.alias_or_name))
        if replaced is None:
            continue
        copy.set("expressions", [exp.alias_(item, name) if name and item.output_name != name else item
                                 for item, name in zip(copy.expressions, output_names)])
        copy.set("joins", [j for i, j in enumerate(copy.args["joins"]) if i != index] or None)
        return copy
    return None
