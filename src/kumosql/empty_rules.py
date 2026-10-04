"""Propagate relations that can never hold a row, and drop orderings that cannot matter.

* A select whose FROM source, an inner or cross joined source, or the left
  side of a LEFT JOIN can never hold a row returns no rows (unless it is a
  global aggregate, which always returns one row, or groups by ROLLUP, CUBE,
  GROUPING SETS, () or ALL, whose grand-total row can exist over no input): its
  WHERE becomes FALSE.
* A LEFT JOIN whose right side can never hold a row pads every left row with
  NULLs: the join is dropped and the right side's columns read as NULL.
* ``EXISTS`` over such a relation is FALSE, ``x IN (...)`` over it is FALSE.
* ``ORDER BY`` without ``LIMIT``/``OFFSET`` in a derived table does not change
  the bag of rows and is dropped.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import distinct_on, extended_grouping


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


def _global_aggregate(select: exp.Select) -> bool:
    """Whether the select can return a row over no input: an aggregate without ``GROUP BY`` (in the
    select list, ``HAVING``, ``QUALIFY`` or ``ORDER BY``), or ``ROLLUP``/``CUBE``/``GROUPING SETS``,
    ``GROUP BY ()`` or ``GROUP BY ALL``, whose grand-total row can exist when no row reaches the grouping."""

    group = select.args.get("group")
    if group is not None and (
        extended_grouping(group)
        or group.args.get("all")  # GROUP BY ALL over a select list of aggregates only groups by nothing
        or not group.expressions
        or any(isinstance(e, exp.Tuple) for e in group.expressions)  # GROUP BY () is the grand total
    ):
        return True
    if group is not None:
        return False
    if select.args.get("having") is not None:
        return True  # HAVING without GROUP BY aggregates the whole input into one group
    clauses = list(select.expressions) + [select.args.get(k) for k in ("qualify", "order")]
    for clause in clauses:
        for agg in clause.find_all(exp.AggFunc) if clause is not None else ():
            if agg.find_ancestor(exp.Select) is select and agg.find_ancestor(exp.Window) is None:
                return True
    return False


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
    return alias.name if alias is not None and alias.name else None


def _drop_empty_left_joins(select: exp.Select) -> exp.Select | None:
    joins = select.args.get("joins") or []
    for join in joins:
        if (join.side or "").upper() != "LEFT" or join.args.get("kind") or not is_empty(join.this):
            continue
        name = _alias(join.this) or (join.this.name if isinstance(join.this, exp.Table) else None)
        if not name:
            continue
        copy = select.copy()
        index = joins.index(join)
        copy.args["joins"][index].pop()
        if not copy.args.get("joins"):
            copy.set("joins", None)
        if any(star.table == name for star in copy.find_all(exp.Column) if isinstance(star.this, exp.Star)):
            return None
        for column in list(copy.find_all(exp.Column)):
            if column.table == name:
                column.replace(exp.null())
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
