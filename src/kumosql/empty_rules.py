"""Propagate relations that can never hold a row, and drop orderings that cannot matter.

* A select whose FROM source, an inner or cross joined source, or the left
  side of a LEFT JOIN can never hold a row returns no rows (unless it is a
  global aggregate, which always returns one row, or groups by ``ROLLUP``,
  ``CUBE`` or a grouping set list holding ``()``, whose grand-total row exists
  over no input too): its WHERE becomes FALSE.
* A LEFT JOIN whose right side can never hold a row pads every left row with
  NULLs: the join is dropped and the right side's columns read as NULL. Only
  the columns that read that side become NULL: a nested query that binds the
  same name to a table of its own keeps its columns.
* ``EXISTS`` over such a relation is FALSE, ``x IN (...)`` over it is FALSE.
* ``ORDER BY`` without ``LIMIT``/``OFFSET`` in a derived table does not change
  the bag of rows and is dropped.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import distinct_on, inside, select_sources


def _false(node: exp.Expression | None) -> bool:
    if node is None:
        return False
    if isinstance(node, exp.Paren):
        return _false(node.this)
    if isinstance(node, exp.Boolean):
        return not node.this
    if isinstance(node, exp.EQ) and all(isinstance(side, exp.Literal) and not side.is_string for side in (node.left, node.right)):
        return node.left.name != node.right.name
    if isinstance(node, exp.And):
        return _false(node.left) or _false(node.right)
    return False


def _grand_total(item: exp.Expression) -> bool:
    """Whether a ``GROUP BY`` item can contribute the empty grouping set ``()``."""

    if isinstance(item, (exp.Rollup, exp.Cube)):
        return True
    if isinstance(item, exp.GroupingSets):
        return any(_grand_total(e) for e in item.expressions)
    if isinstance(item, exp.Tuple):
        return all(_grand_total(e) for e in item.expressions)
    return False


def _global_aggregate(select: exp.Select) -> bool:
    """``select`` returns a row even over no input.

    That is an aggregate without ``GROUP BY`` (in its list, ``HAVING`` or ``ORDER BY``; ``SUM(COUNT(*)) OVER ()``
    aggregates too), or a grouping whose sets include the empty one: ``GROUP BY ()``, ``ROLLUP``, ``CUBE``, or
    ``GROUPING SETS`` listing ``()``, since every item's sets are crossed, ``GROUP BY x, ROLLUP (y)`` has none.
    """

    group = select.args.get("group")
    if group is not None:
        if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return True  # older sqlglot keeps these outside group.expressions
        return bool(group.expressions) and all(_grand_total(e) for e in group.expressions)
    if select.args.get("having") is not None:
        return True
    return any(
        agg.find_ancestor(exp.Select) is select and not isinstance(agg.parent, exp.Window)
        for agg in select.find_all(exp.AggFunc)
    )


def is_empty(node: exp.Expression | None) -> bool:
    """True when ``node`` (a select, set operation or derived table) can never return a row."""

    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    if type(node) is exp.Union:
        return is_empty(node.left) and is_empty(node.right)
    if isinstance(node, exp.Intersect):
        return is_empty(node.left) or is_empty(node.right)
    if isinstance(node, exp.Except):
        return is_empty(node.left)
    if not isinstance(node, exp.Select) or _global_aggregate(node):
        return False
    if node.args.get("having") is not None and _false(node.args["having"].this):
        return True
    where = node.args.get("where")
    if where is not None and _false(where.this):
        return True
    limit = node.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.name == "0":
        return True
    return _source_empty(node)


def _source_empty(select: exp.Select) -> bool:
    source = select.args.get("from_") or select.args.get("from")
    if source is None:
        return False
    joins = select.args.get("joins") or []
    # A RIGHT or FULL join can keep rows of its own side whatever the rest holds.
    if any((j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
        return False
    if is_empty(source.this):
        return True
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        if not join.side and kind in ("", "INNER", "CROSS", "SEMI") and is_empty(join.this):
            return True
    return False


def _alias(source: exp.Expression) -> str | None:
    alias = source.args.get("alias")
    return alias.name.lower() if alias is not None and alias.name else None


def _binds(select: exp.Select, name: str) -> bool:
    return any(_alias(source) == name or (isinstance(source, exp.Table) and not _alias(source) and source.name.lower() == name) for source in select_sources(select))


def _reads_source(column: exp.Column, select: exp.Select, name: str) -> bool | None:
    """Whether ``column`` (qualified by ``name``) reads the source ``select`` binds to ``name``.

    False when a nearer query binds ``name`` itself, None when the column sits inside one of ``select``'s own
    FROM or JOIN items, which cannot see their siblings.
    """

    scope = column.find_ancestor(exp.Select)
    while scope is not select:
        if scope is None:
            return None
        if _binds(scope, name):
            return False
        scope = scope.find_ancestor(exp.Select)
    if any(inside(column, source) for source in select_sources(select)):
        return None
    return True


def _drop_empty_left_joins(select: exp.Select) -> exp.Select | None:
    joins = select.args.get("joins") or []
    for join in joins:
        if (join.side or "").upper() != "LEFT" or join.args.get("kind") or not is_empty(join.this):
            continue
        name = _alias(join.this) or (join.this.name.lower() if isinstance(join.this, exp.Table) else None)
        if not name:
            continue
        copy = select.copy()
        index = joins.index(join)
        copy.args["joins"][index].pop()
        if not copy.args.get("joins"):
            copy.set("joins", None)
        reads = []
        for column in copy.find_all(exp.Column):
            if (column.table or "").lower() != name:
                continue
            found = _reads_source(column, copy, name)
            if found is None or (found and isinstance(column.this, exp.Star)):
                return None
            if found:
                reads.append(column)
        for column in reads:
            # a bare column in the list keeps its output name
            column.replace(exp.alias_(exp.null(), column.name) if column.parent is copy else exp.null())
        return copy
    return None


def propagate_empty(tree: exp.Expression) -> exp.Expression:
    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Exists) and is_empty(node.this):
            return exp.false()
        if isinstance(node, exp.In) and isinstance(node.args.get("query"), exp.Expression) and is_empty(node.args["query"]):
            return exp.false()
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select) and isinstance(node.parent, (exp.From, exp.Join)):
            inner = node.this
            # a derived table's rows are a bag, except that DISTINCT ON's order picks the row it keeps per key
            if inner.args.get("order") and not inner.args.get("limit") and not inner.args.get("offset") and not inner.args.get("fetch") and not distinct_on(inner):
                inner.set("order", None)
        if isinstance(node, exp.Select):
            dropped = _drop_empty_left_joins(node)
            if dropped is not None:
                node = dropped
            where = node.args.get("where")
            if not _global_aggregate(node) and not (where is not None and _false(where.this)) and _source_empty(node):
                node = node.copy()
                node.set("where", exp.Where(this=exp.false()))
        return node

    return tree.transform(step)


def canonical_empty(tree: exp.Expression) -> exp.Expression:
    """A whole query that can never return a row, as one plain empty select with the same output names."""

    if not isinstance(tree, exp.Select) or not is_empty(tree):
        return tree
    names = []
    for projection in tree.expressions:
        if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
            return tree
        names.append(projection.alias_or_name)
    return exp.select(*(exp.alias_(exp.null(), name or f"c{i}") for i, name in enumerate(names))).from_(
        exp.select(exp.alias_(exp.null(), "c0")).where(exp.false()).subquery("kumosql_empty")
    ).where(exp.false())
