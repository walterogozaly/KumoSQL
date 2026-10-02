"""Spell out named windows: ``SUM(x) OVER w ... WINDOW w AS (PARTITION BY a)`` is ``SUM(x) OVER (PARTITION BY a)``.

A ``WINDOW`` clause only names window specifications for the select that declares it, so replacing each
reference by the specification it names, and dropping the clause, keeps the query's meaning. The provers
and the tie-stability check then read the window like any other.
"""

from __future__ import annotations

from sqlglot import exp

_CLAUSES = ("partition_by", "order", "spec")


def inline_named_windows(tree: exp.Expression) -> exp.Expression:
    """Replace every ``OVER w`` by the window ``w`` names, in every select with a ``WINDOW`` clause.

    A reference may add an ``ORDER BY`` its base window lacks, or a frame (``OVER (w ORDER BY x)``), and a
    named window may build on an earlier one. A select is left alone when a name is declared twice, a
    reference names an unknown window or a cycle, a reference repeats a clause its base already has, or a
    base window with a frame is extended (which the standard forbids).
    """

    for select in list(tree.find_all(exp.Select)):
        definitions = select.args.get("windows")
        if not definitions:
            continue
        spelled = _spell_out(select, definitions)
        if spelled is None:
            continue
        for use, window in spelled:
            use.replace(window)
        select.set("windows", None)
    return tree


def _spell_out(select: exp.Select, definitions: list) -> list[tuple[exp.Window, exp.Window]] | None:
    named: dict[str, exp.Window] = {}
    for definition in definitions:
        name = definition.this.name.lower() if isinstance(definition.this, exp.Identifier) else ""
        if not name or name in named:
            return None
        named[name] = definition

    def resolve(window: exp.Window, seen: frozenset) -> exp.Window | None:
        base_name = window.args.get("alias")
        if base_name is None:
            return window
        key = base_name.name.lower()
        if key in seen or key not in named:
            return None
        base = resolve(named[key], seen | {key})
        if base is None:
            return None
        own = {arg: window.args.get(arg) for arg in _CLAUSES}
        inherited = {arg: base.args.get(arg) for arg in _CLAUSES}
        if own["partition_by"] or (own["order"] and inherited["order"]) or (inherited["spec"] and any(own.values())):
            return None
        merged = window.copy()
        merged.set("alias", None)
        for arg in _CLAUSES:
            if inherited[arg] and not own[arg]:
                value = inherited[arg]
                merged.set(arg, [v.copy() for v in value] if isinstance(value, list) else value.copy())
        return merged

    ids = {id(d) for d in definitions}
    spelled = []
    for use in select.find_all(exp.Window):
        if any(id(node) in ids for node in _self_and_ancestors(use, select)):
            continue  # a definition, or inside one
        if use.args.get("alias") is None or _owner(use) is not select:
            continue
        window = resolve(use, frozenset())
        if window is None:
            return None
        spelled.append((use, window))
    return spelled


def _self_and_ancestors(node: exp.Expression, stop: exp.Expression):
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _owner(node: exp.Expression) -> exp.Expression | None:
    """The select whose ``WINDOW`` clause a window reference reads: the nearest enclosing select."""

    return node.find_ancestor(exp.Select)
