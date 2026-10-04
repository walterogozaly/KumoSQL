"""``COUNT(x.c)`` is ``COUNT(*)`` over a derived table whose every row has a non-NULL ``c``.

``SELECT COUNT(x.c) FROM (A UNION ALL B ..) AS x`` counts the rows of ``x`` whose ``c`` is not NULL. When each
branch outputs ``c`` as a non-NULL literal, or as an expression ``E`` that the branch's own WHERE keeps non-NULL
(a top-level conjunct ``E IS NOT NULL``, which is how a ``CROSS JOIN UNNEST`` over literals reads after
:mod:`unnest_literals` splits it), no row of ``x`` has a NULL ``c``, so ``COUNT(x.c)`` and ``COUNT(*)`` agree for
every group of the outer select.

Only a plain ``COUNT(x.c)`` of a select with ``x`` as its only source is read (``COUNT(DISTINCT x.c)`` counts
values, and a join could pad ``x`` with NULLs). A branch must be a plain select (filter, projection, joins,
``DISTINCT``): with a ``GROUP BY``, an aggregate, a window or a ``LIMIT`` its WHERE no longer constrains its
output values.
"""

from __future__ import annotations

from sqlglot import exp

_BRANCH_ARGS = frozenset({"expressions", "from_", "joins", "where", "distinct"})
_UNSAFE = (exp.AggFunc, exp.Window, exp.Subquery, exp.Select)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _branches(node: exp.Expression, out: list[exp.Select]) -> bool:
    while isinstance(node, exp.Subquery) and not any(v for k, v in node.args.items() if k not in ("this", "alias")):
        node = node.this
    if isinstance(node, exp.Union) and not any(v for k, v in node.args.items() if k not in ("this", "expression", "distinct")):
        return _branches(node.this, out) and _branches(node.expression, out)
    if isinstance(node, exp.Select) and not any(v for k, v in node.args.items() if k not in _BRANCH_ARGS):
        out.append(node)
        return True
    return False


def _non_null_column(branch: exp.Select, index: int) -> bool:
    if index >= len(branch.expressions):
        return False
    value = branch.expressions[index].unalias()
    if isinstance(value, exp.Star) or (isinstance(value, exp.Column) and isinstance(value.this, exp.Star)):
        return False
    if isinstance(value, exp.Literal) or (isinstance(value, exp.Boolean)):
        return True
    if any(isinstance(n, _UNSAFE) for n in value.walk()):
        return False
    where = branch.args.get("where")
    for part in _conjuncts(where.this if where is not None else None):
        if isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null) and part.this.this == value:
            return True
    return False


def count_over_non_null_union(select: exp.Select) -> exp.Expression | None:
    """Read ``COUNT(x.c)`` as ``COUNT(*)`` when ``x`` is a union whose branches all keep ``c`` non-NULL."""

    joins = select.args.get("joins")
    from_ = select.args.get("from_") or select.args.get("from")
    if joins or from_ is None or not isinstance(from_.this, exp.Subquery) or not from_.this.alias:
        return None
    source = from_.this
    if source.args.get("alias") is not None and source.args["alias"].args.get("columns"):
        return None
    branches: list[exp.Select] = []
    if not _branches(source.this, branches) or len(branches) < 2:
        return None
    names = [e.alias_or_name.lower() for e in branches[0].expressions]
    if "" in names or len(set(names)) != len(names) or any(len(b.expressions) != len(names) for b in branches):
        return None
    alias = source.alias.lower()
    cache: dict[str, bool] = {}
    targets = []
    for count in select.find_all(exp.Count):
        if count.find_ancestor(exp.Select) is not select or count.find_ancestor(exp.Window) is not None or count.expressions:
            continue
        arg = count.this
        if not isinstance(arg, exp.Column) or isinstance(arg.this, exp.Star) or arg.args.get("db"):
            continue
        if arg.table and arg.table.lower() != alias:
            continue
        name = arg.name.lower()
        if name not in names:
            continue
        if name not in cache:
            index = names.index(name)
            cache[name] = all(_non_null_column(b, index) for b in branches)
        if cache[name]:
            targets.append(count)
    if not targets:
        return None
    copy = select.copy()
    copy_targets = [c for c in copy.find_all(exp.Count) if c.find_ancestor(exp.Select) is copy and c.find_ancestor(exp.Window) is None and not c.expressions]
    changed = False
    for count in copy_targets:
        arg = count.this
        if isinstance(arg, exp.Column) and not isinstance(arg.this, exp.Star) and (not arg.table or arg.table.lower() == alias) and cache.get(arg.name.lower()):
            count.replace(exp.Count(this=exp.Star()))
            changed = True
    return copy if changed else None
