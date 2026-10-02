"""Window-function rewrites that let two spellings of a windowed query read alike.

A window is computed over all of its input rows at once, so most rules that split or merge a select stay
away from one. These two do not depend on that:

* ``lift_union_projection``: a window over ``(SELECT f(a) AS x .. UNION ALL SELECT f(b) AS x ..)`` reads
  ``f(x)`` over the union of the plain columns. Every branch computes the same ``f`` of one of its own
  columns, so computing it after the union gives the same rows (Calcite pushes such projections into the
  union branches; this pulls them back out).
* ``never_null_counts``: ``c IS NULL`` is FALSE when ``c`` is a derived table's ``COUNT(..)`` (windowed or
  not): COUNT returns 0, never NULL, even over an empty frame or group.
"""

from __future__ import annotations

from sqlglot import exp

_NONDETERMINISTIC = (exp.Rand, exp.Anonymous)


def lift_union_projection(select: exp.Select) -> exp.Select | None:
    """Lift one shared computed column out of a UNION ALL that a windowed select reads (module doc)."""

    if not any(w.find_ancestor(exp.Select) is select for w in select.find_all(exp.Window)):
        return None
    if select.args.get("joins") or any(
        isinstance(s, exp.Star) or isinstance(s, exp.Column) and isinstance(s.this, exp.Star) for s in select.expressions
    ):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias:
        return None
    branches = _branches(source.this)
    if not branches or len(branches) < 2:
        return None
    width = len(branches[0].expressions)
    if any(len(b.expressions) != width for b in branches):
        return None
    names = [item.alias_or_name.lower() for item in branches[0].expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    for position in range(width):
        templates = [_template(_value(b.expressions[position])) for b in branches]
        if any(t is None for t in templates) or len({t[0] for t in templates}) != 1:
            continue
        # the same column of the same table in every branch, so the union does not coerce its type
        if len({_origin(b, next(t[1].find_all(exp.Column))) for b, t in zip(branches, templates)} - {None}) != 1 or any(
            _origin(b, next(t[1].find_all(exp.Column))) is None for b, t in zip(branches, templates)
        ):
            continue
        lifted = _lift(select, source, branches, position, names[position], templates[0][1])
        if lifted is not None:
            return lifted
    return None


def _branches(node: exp.Expression) -> list[exp.Select] | None:
    if isinstance(node, exp.Subquery):
        return _branches(node.this)
    if isinstance(node, exp.Select):
        if any(node.args.get(k) for k in ("group", "having", "distinct", "qualify", "windows", "limit", "offset", "order", "with_", "with")):
            return None
        if any(isinstance(e, exp.Star) or isinstance(e, exp.Column) and isinstance(e.this, exp.Star) for e in node.expressions):
            return None
        return [node]
    if isinstance(node, exp.Union) and not node.args.get("distinct") and not any(
        node.args.get(k) for k in ("order", "limit", "offset", "with_", "with")
    ):
        left, right = _branches(node.this), _branches(node.expression)
        return None if left is None or right is None else left + right
    return None


def _origin(branch: exp.Select, column: exp.Column) -> tuple[str, str] | None:
    from_ = branch.args.get("from_") or branch.args.get("from")
    table = from_.this if from_ is not None else None
    if branch.args.get("joins") or not isinstance(table, exp.Table):
        return None
    if column.table and column.table.lower() != table.alias_or_name.lower():
        return None
    return table.sql(dialect="bigquery").split(" AS ")[0].lower(), column.name.lower()


def window_rules(select: exp.Select) -> exp.Select | None:
    """The first of this module's rewrites that applies to ``select``, or None."""

    return lift_union_projection(select) or never_null_counts(select)


def _value(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _template(value: exp.Expression) -> tuple[str, exp.Expression] | None:
    """``f`` of ``f(col)`` as text with the column as a placeholder, and the expression; None otherwise."""

    if isinstance(value, exp.Column):
        return None  # nothing to lift
    columns = list(value.find_all(exp.Column))
    if len({c.sql().lower() for c in columns}) != 1:
        return None
    if value.find(exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Exists, exp.Star, *_NONDETERMINISTIC):
        return None
    shape = value.copy()
    for column in list(shape.find_all(exp.Column)):
        column.replace(exp.column("kumosql_lift"))
    return shape.sql(dialect="bigquery").lower(), value


def _lift(select, source, branches, position, name, value) -> exp.Select | None:
    alias = source.alias.lower()
    # every read of the column must be a qualified or unambiguous column of this select's scope
    reads = []
    for column in select.find_all(exp.Column):
        if column.name.lower() != name:
            continue
        inside = column
        while inside is not None and inside is not source:
            inside = inside.parent
        if inside is source:
            continue
        if column.table and column.table.lower() != alias:
            continue
        if column.find_ancestor(exp.Select) is not select:
            return None  # a nested query reading it: leave the scoping alone
        reads.append(column)
    result_source = source.copy()
    for branch in _branches(result_source.this):
        item = branch.expressions[position]
        column = next(_value(item).find_all(exp.Column)).copy()
        item.replace(exp.alias_(column, name))
    result = select.copy()
    paths = [_path(select, c) for c in reads]
    for path in paths:
        column = _follow(result, path)
        replacement = value.copy()
        for inner in list(replacement.find_all(exp.Column)):
            inner.replace(exp.column(column.name, table=column.table or None))
        bare = isinstance(column.parent, (exp.Func, exp.Alias, exp.Paren, exp.Ordered, exp.Window, exp.Tuple))
        top = column.parent is result
        new = replacement if bare or not isinstance(replacement, exp.Binary) else exp.Paren(this=replacement)
        column.replace(exp.alias_(new, column.name) if top else new)
    result_from = result.args.get("from_") or result.args.get("from")
    result_from.this.replace(result_source)
    return result


def _counts(select: exp.Select, item: exp.Expression, depth: int = 0) -> bool:
    """Whether output ``item`` of ``select`` is a COUNT, directly or passed through derived tables."""

    value = _value(item)
    if isinstance(value, exp.Window):
        value = value.this
    if isinstance(value, exp.Count):
        return True
    if not isinstance(value, exp.Column) or depth > 8 or select.args.get("joins"):
        return False
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return False
    if value.table and value.table.lower() != (source.alias or "").lower():
        return False
    matches = [i for i in source.this.expressions if i.alias_or_name.lower() == value.name.lower()]
    return len(matches) == 1 and _counts(source.this, matches[0], depth + 1)


def _path(root: exp.Expression, node: exp.Expression) -> list[tuple[str, int | None]]:
    steps = []
    while node is not root:
        parent = node.parent
        key = node.arg_key
        value = parent.args.get(key)
        steps.append((key, value.index(node) if isinstance(value, list) else None))
        node = parent
    return steps[::-1]


def _follow(root: exp.Expression, path) -> exp.Expression:
    node = root
    for key, index in path:
        value = node.args.get(key)
        node = value[index] if index is not None else value
    return node


def never_null_counts(select: exp.Select) -> exp.Select | None:
    """Fold ``d.c IS NULL`` to FALSE in ``WHERE`` when ``c`` is a COUNT of a derived table ``d`` (module doc)."""

    where = select.args.get("where")
    if where is None or select.args.get("joins"):
        return None  # an outer join could null-extend the derived table
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    counts = {item.alias_or_name.lower() for item in inner.expressions if item.alias_or_name and _counts(inner, item)}
    names = [item.alias_or_name.lower() for item in inner.expressions]
    alias = (source.alias or "").lower()
    changed = False
    result = select.copy()
    for test in list(result.args["where"].find_all(exp.Is)):
        column = test.this
        if not isinstance(test.expression, exp.Null) or not isinstance(column, exp.Column):
            continue
        if column.find_ancestor(exp.Select) is not result or column.name.lower() not in counts:
            continue
        if column.table and column.table.lower() != alias or names.count(column.name.lower()) != 1:
            continue
        test.replace(exp.false())
        changed = True
    return result if changed else None
