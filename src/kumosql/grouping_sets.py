"""Spell ``ROLLUP``, ``CUBE`` and mixed ``GROUP BY`` lists as one ``GROUPING SETS`` list.

``GROUP BY a, ROLLUP(b, c)`` groups by every set in the cross product of its elements' sets: a plain key
is the one set ``(a)``, ``ROLLUP(b, c)`` is ``(b, c), (b), ()``, ``CUBE(b, c)`` is every subset of its
keys, and a nested ``GROUPING SETS`` list is its own sets (SQL:2011 7.9, the rules Postgres, BigQuery and
DuckDB follow). Once spelled out, ``algebraic_equivalence._grouping_sets_to_union`` reads the list as the
``UNION ALL`` of one grouped select per set.

Two sets with the same keys would emit duplicate groups, and engines disagree about whether they do, so a
list that repeats a set is left alone. So is one that spells out more than ``MAX_SETS`` sets.
"""

from __future__ import annotations

from itertools import combinations

from sqlglot import exp

MAX_SETS = 64


_GROUPING = getattr(exp, "Grouping", ())  # GROUPING() has its own node only in newer sqlglot


def is_grouping_call(node: exp.Expression) -> bool:
    """``GROUPING(k)``: its own node in newer sqlglot, an anonymous function in older ones."""

    return isinstance(node, _GROUPING) or (isinstance(node, exp.Anonymous) and node.name.upper() == "GROUPING")


class _Decline(Exception):
    pass


def expand_grouping_sets(tree: exp.Expression) -> exp.Expression:
    """Rewrite each select whose ``GROUP BY`` uses ``ROLLUP``, ``CUBE`` or a mixed list (see module doc)."""

    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is not None and not group.expressions and not any(group.args.get(k) for k in ("rollup", "cube")):
            lone = group.args.get("grouping_sets") or []
            if len(lone) == 1:
                # older sqlglot keeps a lone GROUPING SETS list out of ``expressions``; read it from there
                group.set("expressions", [lone[0]])
                group.set("grouping_sets", None)
        if group is None or not _needs_expansion(group):
            continue
        try:
            sets = _sets_of_group(group)
        except _Decline:
            continue
        group.set("expressions", [exp.GroupingSets(expressions=[exp.Tuple(expressions=s) for s in sets])])
        for key in ("rollup", "cube", "grouping_sets"):
            group.set(key, None)
    return tree


def _needs_expansion(group: exp.Group) -> bool:
    if any(group.args.get(k) for k in ("rollup", "cube")):
        return True
    elements = list(group.expressions) + list(group.args.get("grouping_sets") or [])
    if not any(isinstance(e, (exp.Rollup, exp.Cube, exp.GroupingSets)) for e in elements):
        return False
    if len(elements) == 1 and isinstance(elements[0], exp.GroupingSets):
        # already one list: only nested ROLLUP / CUBE / GROUPING SETS inside it need spelling out
        return any(isinstance(_unparen(item), (exp.Rollup, exp.Cube, exp.GroupingSets)) for item in elements[0].expressions)
    return True


def _sets_of_group(group: exp.Group) -> list[list[exp.Expression]]:
    if group.args.get("totals") or group.args.get("all"):
        raise _Decline
    plain = list(group.expressions)
    with_rollup, with_cube = group.args.get("rollup") or [], group.args.get("cube") or []
    elements: list[exp.Expression] = []
    if with_rollup or with_cube:
        # ``GROUP BY a, b WITH ROLLUP`` (MySQL) rolls up the whole list; a ROLLUP with keys of its own is a
        # list element that older sqlglot versions keep under this argument instead of ``expressions``
        bare = [node for node in with_rollup + with_cube if not node.expressions]
        if bare:
            if len(bare) != 1 or len(with_rollup) + len(with_cube) != 1:
                raise _Decline
            elements = [type(bare[0])(expressions=[e.copy() for e in plain])]
            plain = []
        else:
            elements = list(with_rollup) + list(with_cube)
    elements = plain + elements + list(group.args.get("grouping_sets") or [])

    sets: list[list[exp.Expression]] = [[]]
    for element in elements:
        sets = [left + right for left in sets for right in _sets_of_element(element)]
        if len(sets) > MAX_SETS:
            raise _Decline
    result, seen = [], set()
    for members in sets:
        unique, keys = [], set()
        for member in members:
            key = member.sql().lower()
            if key not in keys:
                keys.add(key)
                unique.append(member.copy())
        if frozenset(keys) in seen:
            raise _Decline  # a repeated set repeats its groups, which engines do not agree on
        seen.add(frozenset(keys))
        result.append(unique)
    return result


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _members(node: exp.Expression) -> list[exp.Expression]:
    """The keys of one item of a ROLLUP / CUBE / GROUPING SETS list: ``(a, b)`` is two keys, ``()`` none."""

    node = _unparen(node)
    if isinstance(node, (exp.Rollup, exp.Cube, exp.GroupingSets)):
        raise _Decline
    members = list(node.expressions) if isinstance(node, exp.Tuple) else [node]
    if any(isinstance(m, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)) for m in members):
        raise _Decline
    return members


def _sets_of_element(element: exp.Expression) -> list[list[exp.Expression]]:
    element = _unparen(element)
    if isinstance(element, exp.Rollup):
        items = [_members(e) for e in element.expressions]
        if not items:
            raise _Decline
        return [[m for item in items[:size] for m in item] for size in range(len(items), -1, -1)]
    if isinstance(element, exp.Cube):
        items = [_members(e) for e in element.expressions]
        if not items or len(items) > 6:
            raise _Decline
        return [
            [m for i in chosen for m in items[i]]
            for size in range(len(items), -1, -1)
            for chosen in combinations(range(len(items)), size)
        ]
    if isinstance(element, exp.GroupingSets):
        sets = []
        for item in element.expressions:
            inner = _unparen(item)
            sets.extend(_sets_of_element(inner) if isinstance(inner, (exp.Rollup, exp.Cube, exp.GroupingSets)) else [_members(item)])
        if not sets:
            raise _Decline
        return sets
    return [_members(element)]


def grouping_sets_to_union(tree: exp.Expression) -> exp.Expression:
    """``GROUP BY GROUPING SETS (a, (a, b))`` is the ``UNION ALL`` of one grouped select per set.

    A key missing from a set reads as NULL in that branch's select list and ``HAVING``, and ``GROUPING(a, b)``
    is the bit mask of the arguments missing from the set (the first argument is the high bit). The empty set
    is a global aggregate, which returns its one row even over no input, as ``GROUPING SETS (())`` does.
    Only the select's own list and ``HAVING`` change: its sources, ``WHERE`` and aggregate arguments read the
    input rows, where every key keeps its value.
    """

    for select in list(tree.find_all(exp.Select))[::-1]:
        branches = _branches(select)
        if branches is None:
            continue
        body: exp.Expression = branches[0]
        for branch in branches[1:]:
            body = exp.Union(this=body, expression=branch, distinct=False)
        if select is tree:
            tree = body
        else:
            select.replace(body)
    return tree


def _branches(select: exp.Select) -> list[exp.Select] | None:
    group = select.args.get("group")
    if group is None or len(group.expressions) != 1 or not isinstance(group.expressions[0], exp.GroupingSets):
        return None
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals", "all")) or any(
        select.args.get(k) for k in ("order", "limit", "offset", "qualify", "windows", "distinct", "with_", "with")
    ):
        return None
    if select.find(exp.Window):
        return None  # a window reads every set's rows at once
    sets: list[list[exp.Column]] = []
    for item in group.expressions[0].expressions:
        item = _unparen(item)
        members = list(item.expressions) if isinstance(item, exp.Tuple) else [item]
        if not members and not isinstance(item, exp.Tuple) or not all(isinstance(m, exp.Column) for m in members):
            return None
        sets.append(members)
    if not sets or len({frozenset(m.sql().lower() for m in members) for members in sets}) != len(sets):
        return None  # a repeated set repeats its groups, which engines do not agree on
    keys: list[exp.Column] = []
    for members in sets:
        for member in members:
            if all(member.sql().lower() != k.sql().lower() for k in keys):
                keys.append(member)
    if len({k.name.lower() for k in keys}) != len(keys):
        return None  # ``t.a`` and ``s.a`` both keys: an unqualified ``a`` could not be told apart

    try:
        branches = []
        for members in sets:
            present = {m.sql().lower() for m in members}
            branch = select.copy()
            branch.set("group", exp.Group(expressions=[m.copy() for m in members]) if members else None)
            for node in _own_scope(branch):
                if is_grouping_call(node):
                    bits = 0
                    for argument in node.expressions:
                        key = _key_of(argument, keys)
                        if key is None:
                            raise _Decline
                        bits = bits * 2 + (0 if key in present else 1)
                    node.replace(exp.Literal.number(bits))
                elif isinstance(node, exp.Column):
                    key = _key_of(node, keys)
                    if key is None or key in present:
                        continue
                    top = node.parent is branch and node in branch.expressions
                    node.replace(exp.alias_(exp.Null(), node.name) if top else exp.Null())
            if not members and not _aggregates(branch):
                # ``()`` is one group even over no rows; without an aggregate a select with no GROUP BY would
                # instead return a row per input row. Its list is constant here, so it is that one row.
                if branch.args.get("having") is not None or any(e.find(exp.Column) for e in branch.expressions):
                    raise _Decline
                branch = exp.Select(expressions=branch.expressions)
            branches.append(branch)
    except _Decline:
        return None
    return branches


def _aggregates(select: exp.Select) -> bool:
    """Whether ``select``'s own list or ``HAVING`` aggregates; one inside a nested query aggregates that query."""

    roots = list(select.expressions) + [select.args["having"]] if select.args.get("having") else list(select.expressions)
    nested = lambda n: isinstance(n, (exp.Query, exp.Subquery))  # noqa: E731
    return any(
        isinstance(n, exp.AggFunc) and not isinstance(n, _GROUPING) for root in roots for n in root.walk(prune=nested)
    )


def _key_of(column: exp.Expression, keys: list[exp.Column]) -> str | None:
    """The key ``column`` reads (as its lowercased SQL), None for a non-key; declines on a key-named non-key."""

    if not isinstance(column, exp.Column):
        raise _Decline
    for key in keys:
        if key.name.lower() != column.name.lower():
            continue
        if column.table.lower() == key.table.lower() or not column.table or not key.table:
            if column.args.get("db") or key.args.get("db"):
                if column.sql().lower() != key.sql().lower():
                    raise _Decline
            return key.sql().lower()
        raise _Decline  # same name, another table: unknown beats guessing
    return None


def _own_scope(select: exp.Select) -> list[exp.Expression]:
    """Nodes of ``select``'s list and ``HAVING`` outside aggregates, in an order safe to replace in.

    A nested query there that names a key declines: whether it reads the outer key is a scoping question
    this rule does not answer.
    """

    roots = list(select.expressions)
    having = select.args.get("having")
    if having is not None:
        roots.append(having)
    found: list[exp.Expression] = []

    def walk(node: exp.Expression) -> None:
        if isinstance(node, (exp.Subquery, exp.Select, exp.Exists, exp.Query)):
            if any(True for _ in node.find_all(exp.Column)):
                raise _Decline
            return
        if is_grouping_call(node):
            found.append(node)
            return
        if isinstance(node, exp.AggFunc):
            return
        if isinstance(node, exp.Column):
            found.append(node)
            return
        for child in node.iter_expressions():
            walk(child)

    for root in roots:
        walk(root)
    return found
