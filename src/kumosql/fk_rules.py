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


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _same_table(spelled: str, declared: str) -> bool:
    a, b = spelled.lower().split("."), declared.lower().split(".")
    short = min(len(a), len(b))
    return a[-short:] == b[-short:]


def drop_fk_join(select: exp.Select, keys, not_null, foreign_keys) -> exp.Expression | None:
    if not foreign_keys:
        return None
    joins = select.args.get("joins") or []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or not joins:
        return None
    sources = {}
    for source in [from_.this] + [j.this for j in joins]:
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
            pairs[mine[0].name.lower()] = other[0].name.lower()
        child = sources.get(child_alias or "")
        if not ok or child is None or not pairs:
            continue
        child_name, parent_name = child.name.lower(), parent.name.lower()
        declared = {c.lower() for c in (not_null or {}).get(child_name, frozenset())}
        wanted = {child_col: parent_col for parent_col, child_col in pairs.items()}
        covered = any(
            _same_table(parent_name, fk_parent) and {c.lower(): p.lower() for c, p in zip(cols, parent_cols)} == wanted
            for cols, fk_parent, parent_cols in (foreign_keys.get(child_name) or [])
        )
        if not covered or not set(pairs.values()) <= declared or not any(k <= set(pairs) for k in key_sets.get(parent_name, [])):
            continue
        copy = select.copy()
        copy_join = copy.args["joins"][index]
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
        copy.set("joins", [j for i, j in enumerate(copy.args["joins"]) if i != index] or None)
        return copy
    return None
