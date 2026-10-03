"""Structural rewrites of aggregate queries, used by ``algebraic_equivalence.normalize``.

Each rule takes one ``SELECT`` and returns its rewrite, or ``None`` when it does not apply. They bring
queries that compute the same aggregates in different shapes to one form:

- ``_empty_global_aggregate``: an aggregate with no ``GROUP BY`` over no rows is one row of constants
  (``COUNT`` 0, every other aggregate NULL).
- ``_fromless_aggregate``: an aggregate of constants with no ``FROM`` sees exactly one row.
- ``_pull_shared_filter``: ``SUM(CASE WHEN p THEN x END)`` and the like, with the same ``p`` in every
  aggregate of a global select, is ``SUM(x) .. WHERE p``.
- ``_key_expression_aggregates``: ``MAX(f(k))`` under ``GROUP BY k`` is ``f(k)``; ``COUNT(DISTINCT k)``
  is 1, or 0 for the NULL group.
- ``_count_of_filtered_value``: ``COUNT(x)`` is ``COUNT(*)`` when ``WHERE`` rejects NULL ``x``.
- ``_drop_nonempty_group_having``: ``HAVING COUNT(*) >= 1`` holds for every group.
- ``_coalesce_counted_sum``: ``COALESCE(SUM(c), 0)`` of per-branch counts in a grouped select is
  ``SUM(c)``.
- ``_filter_into_having``: a filter on an aggregate output of a grouped derived table is its
  ``HAVING``.
- ``_lift_aggregate_expressions``: ``f(MAX(x)) AS v`` inside an inner-joined grouped derived table is
  read as ``f(d.m)`` outside it, over ``MAX(x) AS m``.
- ``_split_compound_aggregates``: ``f(SUM(x), COUNT(x))`` and ``BOOL_AND``/``BOOL_OR`` over a
  ``UNION ALL`` are split into per-branch partial aggregates, as plain aggregates already are.

Every rule only fires on shapes whose meaning it fully knows, so an unknown shape stays unproven.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

from sqlglot import exp

# Aggregates that skip NULL inputs, so a NULL argument is the same as a missing row.
_NULL_IGNORING = (exp.Sum, exp.Min, exp.Max, exp.Avg, exp.Count, exp.LogicalAnd, exp.LogicalOr)
# Aggregate -> how per-branch partial results combine.
_COMBINE = {exp.Count: exp.Sum, exp.Sum: exp.Sum, exp.Min: exp.Min, exp.Max: exp.Max, exp.LogicalAnd: exp.LogicalAnd, exp.LogicalOr: exp.LogicalOr}
# Deterministic, row-local scalar operations: equal inputs give equal outputs.
_SCALAR = (
    exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Neg, exp.Not, exp.Binary, exp.Cast, exp.TryCast,
    exp.Coalesce, exp.Case, exp.If, exp.Abs, exp.Upper, exp.Lower, exp.Length, exp.Substring, exp.Round, exp.Floor,
    exp.Ceil, exp.Is, exp.Between, exp.DataType, exp.Identifier, exp.Concat, exp.Mod, exp.Sign, exp.Trim, exp.In,
    exp.Nullif, exp.Greatest, exp.Least, exp.DataTypeParam, exp.Var,
)

_lift_counter = itertools.count()


def rewrite_aggregates(select: exp.Select) -> exp.Expression | None:
    """The first structural aggregate rewrite that applies to ``select``, or ``None``."""

    for rule in (
        _empty_global_aggregate,
        _fromless_aggregate,
        _pull_shared_filter,
        _key_expression_aggregates,
        _count_of_filtered_value,
        _drop_nonempty_group_having,
        _coalesce_counted_sum,
        _filter_into_having,
        _lift_aggregate_expressions,
        _split_compound_aggregates,
        _having_existence_to_where,
        _merge_joined_aggregates,
        _distribute_over_aggregating_branches,
        _merge_projection_over_grouped_join,
    ):
        rewritten = rule(select)
        if rewritten is not None:
            return rewritten
    return None


# --- helpers ------------------------------------------------------------------------


def _own(select: exp.Select, kind=exp.AggFunc) -> list[exp.Expression]:
    """Aggregate calls of ``select`` itself: not of its subqueries, and not window functions."""

    return [n for n in select.find_all(kind) if n.find_ancestor(exp.Select) is select and not isinstance(n.find_ancestor(exp.Window, exp.Select), exp.Window)]


def _plain(select: exp.Select, *, group: bool) -> bool:
    """No clause that runs after aggregation besides the select list (and ``GROUP BY`` if allowed)."""

    banned = ["distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having"]
    if not group:
        banned.append("group")
    if any(select.args.get(k) for k in banned) or any(select.find_all(exp.Window)):
        return False
    grouping = select.args.get("group")
    if grouping is not None and (
        not grouping.expressions or any(grouping.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals"))
    ):
        return False
    return not any(isinstance(e, exp.Star) for e in select.expressions)


def _from(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _value(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _deterministic(node: exp.Expression, leaf=None) -> bool:
    """``node`` is built only from deterministic scalar operations (``leaf`` decides whole subtrees first)."""

    if leaf is not None:
        verdict = leaf(node)
        if verdict is not None:
            return verdict
    if not isinstance(node, _SCALAR):
        return False
    if isinstance(node, exp.In) and (node.args.get("query") or node.args.get("unnest")):
        return False
    return all(_deterministic(child, leaf) for child in node.iter_expressions())


def _is_false(node: exp.Expression | None) -> bool:
    node = _unparen(node) if node is not None else None
    return isinstance(node, exp.Boolean) and not node.this


def _non_null_literal(node: exp.Expression) -> bool:
    node = _unparen(node)
    return isinstance(node, (exp.Literal, exp.Boolean))


def _aggregate_arg(agg: exp.Expression) -> tuple[exp.Expression | None, bool]:
    """``(argument, distinct)`` of a one-argument aggregate; ``(None, ..)`` for ``COUNT(*)`` or several."""

    arg = agg.this
    if isinstance(arg, exp.Distinct):
        if len(arg.expressions) != 1:
            return None, True
        return arg.expressions[0], True
    if isinstance(arg, exp.Star) or arg is None or agg.args.get("expressions"):
        return None, False
    return arg, False


def _with_arg(agg: exp.Expression, arg: exp.Expression, distinct: bool) -> exp.Expression:
    copy = agg.copy()
    copy.set("this", exp.Distinct(expressions=[arg]) if distinct else arg)
    return copy


def _fold_coalesce(node: exp.Expression) -> exp.Expression:
    """``COALESCE(NULL, .., x, ..)`` is ``COALESCE(x, ..)``, and ``x`` alone when nothing follows."""

    def step(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Coalesce):
            parts = [n.this, *n.expressions]
            while len(parts) > 1 and isinstance(_unparen(parts[0]), exp.Null):
                parts = parts[1:]
            if len(parts) == 1:
                return parts[0]
            if len(parts) < 1 + len(n.expressions):
                return exp.Coalesce(this=parts[0], expressions=parts[1:])
        return n

    return node.transform(step)


# --- rules --------------------------------------------------------------------------


def _empty_global_aggregate(select: exp.Select) -> exp.Expression | None:
    """``SELECT COUNT(*), SUM(x) FROM t WHERE FALSE`` is ``SELECT 0, NULL``: one row over no input."""

    where = select.args.get("where")
    if where is None or not _is_false(where.this) or not _plain(select, group=False) or select.args.get("group"):
        return None
    aggregates = _own(select)
    if not aggregates or any(not isinstance(a, _NULL_IGNORING) for a in aggregates):
        return None
    items = []
    for item in select.expressions:
        copy = item.transform(
            lambda n: (exp.Literal.number(0) if isinstance(n, exp.Count) else exp.Null())
            if isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Select) is None
            else n
        )
        if any(isinstance(n, (exp.Column, exp.Select, exp.Subquery, exp.AggFunc)) for n in copy.walk()):
            return None
        items.append(_fold_coalesce(copy))
    return exp.Select(expressions=items)


def _fromless_aggregate(select: exp.Select) -> exp.Expression | None:
    """``SELECT COUNT(*), MAX(1)`` with no ``FROM`` reads one row: ``SELECT 1, 1``."""

    if _from(select) is not None or select.args.get("joins") or select.args.get("where") or select.args.get("group"):
        return None
    if not _plain(select, group=False):
        return None
    aggregates = _own(select)
    if not aggregates:
        return None
    replacements = []
    for agg in aggregates:
        arg, distinct = _aggregate_arg(agg)
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Star):
            replacements.append((agg, exp.Literal.number(1)))
        elif isinstance(agg, exp.Count) and arg is not None and not distinct and _non_null_literal(arg):
            replacements.append((agg, exp.Literal.number(1)))
        elif isinstance(agg, (exp.Sum, exp.Min, exp.Max)) and arg is not None and not any(
            isinstance(n, (exp.Column, exp.Select, exp.Subquery, exp.AggFunc)) for n in arg.walk()
        ) and _deterministic(arg):
            replacements.append((agg, arg.copy()))
        else:
            return None
    for agg, value in replacements:
        agg.replace(value)
    return select


def _filter_conjuncts(arg: exp.Expression) -> tuple[list[exp.Expression], exp.Expression] | None:
    """``CASE WHEN a AND b THEN x END`` -> ``([a, b], x)``: the rows an aggregate argument keeps."""

    from .algebraic_equivalence import _conjuncts

    arg = _unparen(arg)
    if isinstance(arg, exp.Case):
        if arg.this is not None or len(arg.args.get("ifs") or []) != 1:
            return None
        default = arg.args.get("default")
        if default is not None and not isinstance(_unparen(default), exp.Null):
            return None
        test, value = arg.args["ifs"][0].this, arg.args["ifs"][0].args.get("true")
    elif isinstance(arg, exp.If):
        default = arg.args.get("false")
        if default is not None and not isinstance(_unparen(default), exp.Null):
            return None
        test, value = arg.this, arg.args.get("true")
    else:
        return None
    if value is None:
        return None
    parts = []
    for part in _conjuncts(test):
        part = _unparen(part)
        # a filter keeps the rows where its test is TRUE, so "p IS TRUE" filters like "p"
        if isinstance(part, exp.Is) and isinstance(part.expression, exp.Boolean) and part.expression.this:
            part = _unparen(part.this)
        parts.append(part)
    return parts, value


def _pull_shared_filter(select: exp.Select) -> exp.Expression | None:
    """``SELECT SUM(CASE WHEN p THEN x END), COUNT(CASE WHEN p THEN 1 END) FROM t`` is ``SELECT SUM(x), COUNT(*) FROM t WHERE p``.

    A global aggregate returns one row however many rows it reads, and aggregates that skip NULL
    ignore a row whose argument the filter turns into NULL. So a condition shared by every aggregate
    can filter the rows instead. A ``GROUP BY`` would lose the groups the filter empties, and
    ``COUNT(*)`` counts every row, so neither is rewritten.
    """

    from .algebraic_equivalence import _and_all

    if _from(select) is None or not _plain(select, group=False):
        return None
    aggregates = _own(select)
    if not aggregates or any(not isinstance(a, _NULL_IGNORING) for a in aggregates):
        return None
    for item in select.expressions:
        for column in item.find_all(exp.Column):
            if not isinstance(column.find_ancestor(exp.AggFunc, exp.Select), exp.AggFunc):
                return None
    filters = []
    for agg in aggregates:
        arg, distinct = _aggregate_arg(agg)
        found = _filter_conjuncts(arg) if arg is not None else None
        if found is None:
            return None
        if any(isinstance(n, (exp.AggFunc, exp.Window)) for part in found[0] for n in part.walk()):
            return None
        filters.append((agg, distinct, found))
    shared = None
    for _, _, (parts, _) in filters:
        keys = {p.sql() for p in parts}
        shared = keys if shared is None else shared & keys
    if not shared:
        return None
    copy = select.copy()
    copied = _own(copy)
    pulled = [p for p in filters[0][2][0] if p.sql() in shared]
    for (agg, distinct, (parts, value)), target in zip(filters, copied):
        rest = [p.copy() for p in parts if p.sql() not in shared]
        if rest:
            arg = exp.Case(ifs=[exp.If(this=_and_all(rest), true=value.copy())])
        else:
            arg = value.copy()
        if isinstance(agg, exp.Count) and not distinct and not rest and _non_null_literal(value):
            replacement = exp.Count(this=exp.Star())
        else:
            replacement = _with_arg(target, arg, distinct)
        target.replace(replacement)
    where = copy.args.get("where")
    conditions = ([where.this] if where is not None else []) + [p.copy() for p in pulled]
    copy.set("where", exp.Where(this=_and_all(conditions)))
    return copy


def _keys(select: exp.Select) -> set[str] | None:
    group = select.args.get("group")
    if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    if select.args.get("qualify"):
        return None
    return {k.sql() for k in group.expressions}


def _key_determined(node: exp.Expression, keys: set[str]) -> bool:
    """Every row of a group gives ``node`` the same value: a deterministic expression of group keys."""

    def leaf(n: exp.Expression):
        if n.sql() in keys:
            return True
        if isinstance(n, exp.Column):
            return False
        return None

    return _deterministic(node, leaf)


def _key_expression_aggregates(select: exp.Select) -> exp.Expression | None:
    """``MIN``/``MAX``/``SUM(DISTINCT)`` of an expression of group keys is the expression; ``COUNT(DISTINCT k)`` is 0 or 1.

    Every row of a group has the same keys, so a deterministic expression of them has one value per
    group: ``MAX`` of it is that value (NULL when it is NULL, as the aggregate skips NULL), and a
    distinct count of it is 1, or 0 when the value is NULL.
    """

    keys = _keys(select)
    if keys is None:
        return None
    changed = False
    for agg in _own(select, (exp.Min, exp.Max, exp.Sum, exp.Count)):
        if agg.find_ancestor(exp.Where, exp.Join, exp.Group) is not None:
            continue
        arg, distinct = _aggregate_arg(agg)
        if arg is None or not _key_determined(arg, keys):
            continue
        if isinstance(arg, exp.Column) and not isinstance(agg, exp.Count):
            continue  # a bare key column is _key_aggregates' rewrite
        if isinstance(agg, exp.Count):
            if not distinct:
                continue
            value = arg.copy()
            agg.replace(exp.Case(ifs=[exp.If(this=exp.Is(this=value, expression=exp.Null()), true=exp.Literal.number(0))], default=exp.Literal.number(1)))
        elif isinstance(agg, exp.Sum) and not distinct:
            continue
        else:
            agg.replace(exp.Paren(this=arg.copy()) if isinstance(arg, exp.Binary) else arg.copy())
        changed = True
    if not changed:
        return None
    _fold_case_comparisons(select)
    return select


def _fold_case_comparisons(select: exp.Select) -> None:
    """``CASE WHEN c THEN 0 ELSE 1 END > 2`` is FALSE: both branches compare the same way."""

    import operator

    ops = {exp.GT: operator.gt, exp.GTE: operator.ge, exp.LT: operator.lt, exp.LTE: operator.le, exp.EQ: operator.eq, exp.NEQ: operator.ne}

    def number(node):
        node = _unparen(node)
        if isinstance(node, exp.Literal) and not node.is_string:
            try:
                value = Decimal(node.this)
            except ArithmeticError:
                return None
            return value if value.is_finite() else None
        return None

    def step(node: exp.Expression) -> exp.Expression:
        op = ops.get(type(node))
        if op is None:
            return node
        left, right = _unparen(node.this), node.expression
        if not isinstance(left, exp.Case) or left.this is not None or number(right) is None:
            return node
        outcomes = [number(i.args.get("true")) for i in left.args.get("ifs") or []] + [number(left.args.get("default"))]
        if any(o is None for o in outcomes):
            return node
        # Exact comparison, and the same verdicts again as FLOAT64: a dialect may compare
        # these literals either way, so fold only when both agree (2**53 + 1 vs 2**53 does not).
        target = number(right)
        verdicts = {op(o, target) for o in outcomes} | {op(float(o), float(target)) for o in outcomes}
        return exp.Boolean(this=verdicts.pop()) if len(verdicts) == 1 else node

    having = select.args.get("having")
    if having is not None:
        having.set("this", having.this.transform(step))


def _null_rejected(where: exp.Expression) -> set[str]:
    """Columns that every row passing ``where`` has non-NULL (a top-level comparison or ``IS NOT NULL``)."""

    from .algebraic_equivalence import _conjuncts

    columns: set[str] = set()
    for part in _conjuncts(where):
        part = _unparen(part)
        if isinstance(part, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            for side in (part.this, part.expression):
                if isinstance(_unparen(side), exp.Column):
                    columns.add(_unparen(side).sql())
        elif isinstance(part, exp.Not) and isinstance(_unparen(part.this), exp.Is) and isinstance(_unparen(part.this).expression, exp.Null):
            inner = _unparen(_unparen(part.this).this)
            if isinstance(inner, exp.Column):
                columns.add(inner.sql())
    return columns


def _count_of_filtered_value(select: exp.Select) -> exp.Expression | None:
    """``COUNT(x)`` is ``COUNT(*)`` when ``WHERE`` keeps only rows with ``x`` not NULL (``WHERE x = y``)."""

    where = select.args.get("where")
    if where is None:
        return None
    rejected = _null_rejected(where.this)
    changed = False
    for agg in _own(select, exp.Count):
        arg, distinct = _aggregate_arg(agg)
        if arg is None or distinct or not isinstance(_unparen(arg), exp.Column) or _unparen(arg).sql() not in rejected:
            continue
        agg.set("this", exp.Star())
        changed = True
    return select if changed else None


def _drop_nonempty_group_having(select: exp.Select) -> exp.Expression | None:
    """``HAVING COUNT(*) >= 1`` keeps every group: a group has at least one row."""

    from .algebraic_equivalence import _and_all, _conjuncts

    having = select.args.get("having")
    if having is None or _keys(select) is None:
        return None

    def star_count(node):
        node = _unparen(node)
        return isinstance(node, exp.Count) and isinstance(node.this, exp.Star) and node.find_ancestor(exp.Select) is select

    def literal(node, *values):
        node = _unparen(node)
        return isinstance(node, exp.Literal) and not node.is_string and node.this in values

    def always(part):
        part = _unparen(part)
        if isinstance(part, exp.GTE) and star_count(part.this) and literal(part.expression, "1"):
            return True
        if isinstance(part, exp.LTE) and star_count(part.expression) and literal(part.this, "1"):
            return True
        if isinstance(part, (exp.GT, exp.NEQ)) and star_count(part.this) and literal(part.expression, "0"):
            return True
        if isinstance(part, exp.LT) and star_count(part.expression) and literal(part.this, "0"):
            return True
        return False

    parts = _conjuncts(having.this)
    kept = [p for p in parts if not always(p)]
    if len(kept) == len(parts):
        return None
    select.set("having", exp.Having(this=_and_all(kept)) if kept else None)
    return select


def _branch_values(source: exp.Expression, name: str) -> list[exp.Expression] | None:
    """The expression each branch of a derived select or ``UNION ALL`` gives output column ``name``."""

    from .algebraic_equivalence import _aligned_branches

    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        branches = [source.this]
    elif isinstance(source, exp.Subquery):
        branches = _aligned_branches(source)
    else:
        return None
    if not branches:
        return None
    values = []
    for branch in branches:
        match = [i for i in branch.expressions if i.alias_or_name.lower() == name.lower()]
        if len(match) != 1:
            return None
        values.append(_value(match[0]))
    return values


def _coalesce_counted_sum(select: exp.Select) -> exp.Expression | None:
    """``COALESCE(SUM(d.c), 0)`` under ``GROUP BY`` is ``SUM(d.c)`` when every branch computes ``c`` as a ``COUNT``.

    A group has at least one row and a count is never NULL, so the sum is never NULL.
    """

    if _keys(select) is None or select.args.get("joins"):
        return None
    source = _from(select)
    if not isinstance(source, exp.Subquery) or not source.alias:
        return None
    changed = False
    for node in list(select.find_all(exp.Coalesce)):
        if node.find_ancestor(exp.Select) is not select or len(node.expressions) != 1 or node.expressions[0].sql() != "0":
            continue
        total = _unparen(node.this)
        if not isinstance(total, exp.Sum) or not isinstance(total.this, exp.Column):
            continue
        column = total.this
        if column.table and column.table.lower() != source.alias.lower():
            continue
        values = _branch_values(source, column.name)
        if not values or any(not (isinstance(_unparen(v), exp.Count) or _non_null_literal(v)) for v in values):
            continue
        node.replace(total.copy())
        changed = True
    return select if changed else None


def _filter_into_having(select: exp.Select) -> exp.Expression | None:
    """``SELECT .. FROM (SELECT k, COUNT(*) AS c FROM t GROUP BY k) AS d WHERE d.c = 1`` filters inside: ``HAVING COUNT(*) = 1``.

    The derived table has one row per group, and a condition on its aggregate outputs keeps or drops
    whole groups, which is what ``HAVING`` does.
    """

    from .algebraic_equivalence import _and_all, _conjuncts

    if select.find_ancestor(exp.In, exp.Exists) is not None:
        return None  # _grouped_in_to_derived reads a membership test's HAVING as this very shape
    where = select.args.get("where")
    source = _from(select)
    if where is None or select.args.get("joins") or not isinstance(source, exp.Subquery) or not source.alias:
        return None
    inner = source.this
    if not isinstance(inner, exp.Select) or _keys(inner) is None:
        return None
    if any(inner.args.get(k) for k in ("order", "limit", "offset", "qualify", "windows", "with_", "with", "distinct")) or any(inner.find_all(exp.Window)):
        return None
    keys = _keys(inner)
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in outputs or isinstance(item, exp.Star):
            return None
        value = _value(item)
        # a bare non-key column (MySQL's lenient GROUP BY) has no single value per group
        if _own_value_has_aggregate(value) or _key_determined(value, keys):
            outputs[name] = value
    alias = source.alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        columns = list(part.find_all(exp.Column))
        movable = (
            columns
            and _deterministic(part)
            and all(c.name.lower() in outputs and c.table.lower() in ("", alias) for c in columns)
            and any(_own_value_has_aggregate(outputs[c.name.lower()]) for c in columns)
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    copy = select.copy()
    new_inner = _from(copy).this
    pushed = []
    for part in moved:
        holder = exp.Select(expressions=[part.copy()])
        for column in list(holder.find_all(exp.Column)):
            value = outputs[column.name.lower()].copy()
            column.replace(exp.Paren(this=value) if isinstance(value, exp.Binary) else value)
        pushed.append(holder.expressions[0])
    existing = new_inner.args.get("having")
    new_inner.set("having", exp.Having(this=_and_all(([existing.this] if existing is not None else []) + pushed)))
    copy.set("where", exp.Where(this=_and_all(kept)) if kept else None)
    return copy


def _aggregates_in(value: exp.Expression) -> list[exp.Expression]:
    """The outermost aggregate calls in ``value``, not those of subqueries inside it."""

    found = []

    def visit(node):
        if isinstance(node, exp.AggFunc):
            found.append(node)
            return
        if isinstance(node, (exp.Select, exp.Subquery)):
            return
        for child in node.iter_expressions():
            visit(child)

    visit(value)
    return found


def _own_value_has_aggregate(value: exp.Expression) -> bool:
    return bool(_aggregates_in(value))


def _lift_aggregate_expressions(select: exp.Select) -> exp.Expression | None:
    """``JOIN (SELECT k, f(MAX(x)) AS v .. GROUP BY k) AS d`` reads ``f(d.m)`` over ``MAX(x) AS m``.

    The derived table outputs one row per group; computing ``f`` of its aggregates inside or outside
    gives the same value on every row it joins. Only inner joins qualify (a NULL-extended row would
    compute ``f(NULL)`` outside but NULL inside), ``f`` reads only aggregates, constants and output
    keys. Not inside ``IN``/``EXISTS``, whose decorrelated shapes keep the expression inside.
    """

    if select.find_ancestor(exp.In, exp.Exists) is not None:
        return None
    joins = select.args.get("joins") or []
    if any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or j.args.get("using") for j in joins):
        return None
    if any(isinstance(s, exp.Star) for s in select.find_all(exp.Star) if not isinstance(s.parent, exp.Count)):
        return None
    sources = [_from(select)] + [j.this for j in joins]
    for source in sources:
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not inner.args.get("group") and not _own(inner):
            continue
        if any(inner.args.get(k) for k in ("qualify", "windows", "distinct")) or any(inner.find_all(exp.Window)):
            continue
        names = [i.alias_or_name.lower() for i in inner.expressions]
        if "" in names or len(set(names)) != len(names) or any(isinstance(i, exp.Star) for i in inner.expressions):
            continue
        group_keys = _keys(inner) or set()
        # group keys the derived table also outputs: f may read them as d.<name>
        key_outputs = {_value(i).sql(): i.alias_or_name for i in inner.expressions if isinstance(_value(i), exp.Column) and _value(i).sql() in group_keys}
        targets = []
        for item in inner.expressions:
            value = _value(item)
            if isinstance(_unparen(value), exp.AggFunc) or not _own_value_has_aggregate(value):
                continue
            aggregates = _aggregates_in(value)

            def leaf(n, aggregates=aggregates):
                if any(n is a for a in aggregates):
                    return not any(isinstance(m, (exp.Select, exp.Subquery, exp.Window)) for m in n.walk())
                if isinstance(n, exp.Column):
                    return n.sql() in key_outputs
                return None

            if _deterministic(value, leaf):
                targets.append(item)
        if not targets:
            continue
        lifted_names = {t.alias_or_name.lower() for t in targets}
        # HAVING or ORDER BY may name a select alias; it would lose its meaning
        if any(
            not c.table and c.name.lower() in lifted_names
            for clause in ("having", "order", "group", "where") if inner.args.get(clause) is not None
            for c in inner.args[clause].find_all(exp.Column)
        ):
            continue
        alias = source.alias.lower()
        uses = [c for c in select.find_all(exp.Column) if not _inside(c, source)]
        # with no join, a bare name can only be the derived table's column
        sole = not joins
        if not sole and any(not c.table and c.name.lower() in lifted_names for c in uses):
            continue
        if sum(1 for n in select.walk() if isinstance(n, (exp.Table, exp.Subquery)) and (n.alias_or_name or "").lower() == alias) != 1:
            continue  # the inner select's own tables must not share the derived table's name
        # The inner select outputs each aggregate once; existing plain aggregate outputs are reused.
        existing = {(_value(i)).sql(): i.alias_or_name for i in inner.expressions if isinstance(_value(i), exp.AggFunc)}
        copy = select.copy()
        new_source = next(s for s in copy.find_all(exp.Subquery) if (s.alias or "").lower() == alias and s.this.sql() == inner.sql())
        new_inner = new_source.this
        lifted: dict[str, exp.Expression] = {}
        new_items = []
        for item in new_inner.expressions:
            name = item.alias_or_name.lower()
            if name not in lifted_names:
                new_items.append(item)
                continue
            value = _value(item).copy()
            for agg in _aggregates_in(value):
                key = agg.sql()
                if key not in existing:
                    existing[key] = f"kumosql_lift{next(_lift_counter)}"
                    new_items.append(exp.alias_(agg.copy(), existing[key]))
                agg.replace(exp.column(existing[key], table=source.alias))
            for column in list(value.find_all(exp.Column)):
                if column.table.lower() != alias and column.sql() in key_outputs:
                    column.replace(exp.column(key_outputs[column.sql()], table=source.alias))
            lifted[name] = value
        new_inner.set("expressions", new_items)
        for column in list(copy.find_all(exp.Column)):
            if _inside(column, new_source) or column.name.lower() not in lifted:
                continue
            if column.table.lower() != alias and not (sole and not column.table):
                continue
            value = lifted[column.name.lower()].copy()
            value = exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Case)) else value
            if column.parent is copy:
                column.replace(exp.alias_(value, column.name))
            else:
                column.replace(value)
        return copy
    return None


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is root:
            return True
        parent = parent.parent
    return False


def _split_compound_aggregates(select: exp.Select) -> exp.Expression | None:
    """Split ``f(SUM(x), COUNT(x))``, ``BOOL_AND`` and ``BOOL_OR`` over a derived ``UNION ALL`` per branch.

    ``_split_aggregates`` does this for select items that are one plain aggregate; here an item may
    be an expression over such aggregates, each combined across branches the same way (a count by
    summing, ``BOOL_AND`` by ``BOOL_AND``), and the expression applied to the combined values.
    """

    from .algebraic_equivalence import MAX_BRANCHES, _SPLIT_ALIAS, _aligned_branches, _branch_copy, _plain_sources

    if any(select.args.get(k) for k in ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "having")):
        return None
    if any(select.find_all(exp.Window)):
        return None
    group = select.args.get("group")
    if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    sources = _plain_sources(select)
    if not sources or len(sources) != 1:
        return None
    source = sources[0]
    if source.alias == _SPLIT_ALIAS or select.args.get("joins") or _from(select) is not source:
        return None
    branches = _aligned_branches(source)
    if branches is None or len(branches) > MAX_BRANCHES:
        return None
    if any(b.args.get("group") or _own(b) for b in branches):
        return None  # already per-branch partial aggregates
    keys = list(group.expressions) if group else []
    if any(not isinstance(k, exp.Column) for k in keys):
        return None
    key_sql = {k.sql() for k in keys}

    def combinable(agg):
        return type(agg) in _COMBINE and _aggregate_arg(agg)[1] is False and not any(
            isinstance(n, (exp.Select, exp.AggFunc)) for n in agg.this.walk() if n is not agg
        ) if agg.this is not None else False

    partial_items: list[exp.Expression] = []
    outer_items: list[exp.Expression] = []
    names: dict[str, str] = {}
    novel = False
    for item in select.expressions:
        inner = _value(item)
        alias = item.alias if isinstance(item, exp.Alias) else (inner.output_name if isinstance(inner, exp.Column) else "")
        if isinstance(inner, exp.Column) and inner.sql() in key_sql:
            partial_items.append(exp.alias_(inner.copy(), alias or inner.name))
            outer_items.append(exp.column(alias or inner.name))
            continue
        aggregates = [a for a in inner.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select]
        if not aggregates or any(not combinable(a) for a in aggregates):
            return None

        def leaf(n, aggregates=aggregates):
            if any(n is a for a in aggregates):
                return True
            if isinstance(n, exp.Column):
                return False
            return None

        if not _deterministic(inner, leaf):
            return None
        if not isinstance(inner, exp.AggFunc) or isinstance(inner, (exp.LogicalAnd, exp.LogicalOr)):
            novel = True
        def combine(node):
            if not isinstance(node, exp.AggFunc):
                return node
            key = node.sql()
            if key not in names:
                names[key] = f"kumosql_p{len(names)}"
                partial_items.append(exp.alias_(node.copy(), names[key]))
            return _COMBINE[type(node)](this=exp.column(names[key]))

        value = inner.transform(combine)
        outer_items.append(exp.alias_(value, alias) if alias else value)
    if not novel:
        return None  # plain aggregates are _split_aggregates' rewrite
    projected = {i.this.sql() for i in partial_items if not isinstance(i.this, exp.AggFunc)}
    for n, key in enumerate(k for k in keys if k.sql() not in projected):
        # a grouping key the select does not show still splits groups: carry it hidden
        partial_items.append(exp.alias_(key.copy(), f"kumosql_g{n}"))

    partials: list[exp.Select] = []
    for branch in branches:
        partial = select.copy()
        partial.set("expressions", [i.copy() for i in partial_items])
        partials.append(_branch_copy(partial, source, branch))
    union: exp.Expression = partials[0]
    for partial in partials[1:]:
        union = exp.Union(this=union, expression=partial, distinct=False)
    outer = exp.select(*outer_items).from_(exp.Subquery(this=union, alias=exp.TableAlias(this=exp.to_identifier(_SPLIT_ALIAS))))
    if keys:
        outer = outer.group_by(*[exp.column(i.alias) for i in partial_items if not isinstance(i.this, exp.AggFunc)])
    return outer


def _existence_test(part: exp.Expression, select: exp.Select) -> exp.Expression | None:
    """``p`` when ``part`` says some row of the group has ``p`` TRUE: ``SUM(CASE WHEN p THEN 1 ELSE 0 END) >= 1``."""

    part = _unparen(part)
    if isinstance(part, (exp.GTE, exp.GT)):
        agg, bound = _unparen(part.this), _unparen(part.expression)
    elif isinstance(part, (exp.LTE, exp.LT)):
        agg, bound = _unparen(part.expression), _unparen(part.this)
    else:
        return None
    strict = isinstance(part, (exp.GT, exp.LT))
    if not isinstance(bound, exp.Literal) or bound.is_string or bound.this != ("0" if strict else "1"):
        return None
    if not isinstance(agg, (exp.Sum, exp.Count)) or agg.find_ancestor(exp.Select) is not select:
        return None
    arg, distinct = _aggregate_arg(agg)
    if arg is None:
        return None
    arg = _unparen(arg)
    if not isinstance(arg, exp.Case) or arg.this is not None or len(arg.args.get("ifs") or []) != 1:
        return None
    test, value = arg.args["ifs"][0].this, _unparen(arg.args["ifs"][0].args.get("true"))
    default = arg.args.get("default")
    default = _unparen(default) if default is not None else None
    if isinstance(agg, exp.Count):
        # COUNT of a non-NULL value where p holds, NULL elsewhere: at least one row has p
        if not _non_null_literal(value) or (default is not None and not isinstance(default, exp.Null)):
            return None
    else:
        # SUM of a positive integer where p holds and 0 or NULL elsewhere: at least one row has p
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit() and int(value.this) >= 1):
            return None
        if default is not None and not isinstance(default, exp.Null) and not (isinstance(default, exp.Literal) and default.this == "0"):
            return None
        if distinct:
            return None
    if any(isinstance(n, (exp.AggFunc, exp.Select, exp.Subquery, exp.Window)) for n in test.walk()):
        return None
    return test


def _having_existence_to_where(select: exp.Select) -> exp.Expression | None:
    """``SELECT k .. GROUP BY k HAVING SUM(CASE WHEN p THEN 1 ELSE 0 END) >= 1`` is ``SELECT k .. WHERE p GROUP BY k``.

    The groups kept are those with a row where ``p`` holds, and filtering the rows by ``p`` first
    leaves exactly those groups. Only a select that outputs nothing but its keys qualifies: any other
    aggregate would see the rows the filter drops.
    """

    from .algebraic_equivalence import _and_all, _conjuncts

    having = select.args.get("having")
    keys = _keys(select)
    if having is None or keys is None or any(select.args.get(k) for k in ("windows", "qualify")) or any(select.find_all(exp.Window)):
        return None
    if any(not _key_determined(_value(i), keys) for i in select.expressions) or any(isinstance(i, exp.Star) for i in select.expressions):
        return None
    order = select.args.get("order")
    if order is not None and any(n.find_ancestor(exp.Select) is select for n in order.find_all(exp.AggFunc)):
        return None  # ``ORDER BY COUNT(*)`` would count only the rows the filter keeps
    parts = _conjuncts(having.this)
    tests = [(p, _existence_test(p, select)) for p in parts]
    found = [(p, t) for p, t in tests if t is not None]
    others = [p for p, t in tests if t is None]
    if len(found) != 1 or any(_own_value_has_aggregate(p) for p in others):
        return None
    test = found[0][1].copy()
    where = select.args.get("where")
    select.set("where", exp.Where(this=_and_all(([where.this] if where is not None else []) + [test])))
    select.set("having", exp.Having(this=_and_all(others)) if others else None)
    return select


def _positional_body(select: exp.Select) -> tuple[str, list[str]] | None:
    """The SQL of a select's ``FROM .. WHERE .. GROUP BY .. HAVING`` with tables renamed by position, and the original names."""

    sources = [_from(select)] + [j.this for j in select.args.get("joins") or []]
    if any(not isinstance(src, exp.Table) for src in sources):
        return None
    names = [src.alias_or_name.lower() for src in sources]
    if len(set(names)) != len(names):
        return None
    copy = select.copy()
    copy.set("expressions", [exp.Literal.number(1)])
    copy.set("having", None)
    renames = {name: f"kumosql_b{i}" for i, name in enumerate(names)}
    for column in copy.find_all(exp.Column):
        if column.table:
            if column.table.lower() not in renames:
                return None  # a correlated reference
            column.set("table", exp.to_identifier(renames[column.table.lower()]))
    for i, table in enumerate([_from(copy)] + [j.this for j in copy.args.get("joins") or []]):
        table.set("alias", exp.TableAlias(this=exp.to_identifier(f"kumosql_b{i}")))
    return copy.sql(), names


def _merge_joined_aggregates(select: exp.Select) -> exp.Expression | None:
    """Inner-joined copies of one grouped query, joined on all their keys, are that one query.

    ``(SELECT k, SUM(x) AS a FROM t GROUP BY k) AS d1 JOIN (SELECT k, MAX(y) AS b FROM t GROUP BY k)
    AS d2 ON d1.k <=> d2.k`` has one row per group of ``t`` with both aggregates: ``(SELECT k, SUM(x),
    MAX(y) FROM t GROUP BY k)``. Each side has exactly one row per key, so the NULL-safe key equality
    pairs them one to one (an ordinary ``=`` would drop the NULL group, so it does not qualify); with no
    keys both sides are one row and join ``ON TRUE``. Different ``HAVING`` filters keep the groups that
    pass both.
    """

    from .algebraic_equivalence import _and_all, _conjuncts

    joins = select.args.get("joins") or []
    if not joins or any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or j.args.get("using") for j in joins):
        return None
    sources = [_from(select)] + [j.this for j in joins]
    derived = []
    for source in sources:
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            return None
        inner = source.this
        if any(inner.args.get(k) for k in ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")) or any(inner.find_all(exp.Window)):
            return None
        group = inner.args.get("group")
        if group is not None and _keys(inner) is None:
            return None
        if group is None and not _own(inner):
            return None
        body = _positional_body(inner)
        if body is None:
            return None
        names = [i.alias_or_name.lower() for i in inner.expressions]
        if "" in names or len(set(names)) != len(names) or any(isinstance(i, exp.Star) for i in inner.expressions):
            return None
        # the outputs are renamed below, so a HAVING or GROUP BY that reads one by its alias would lose it
        aliases = {i.alias.lower() for i in inner.expressions if isinstance(i, exp.Alias) and not (isinstance(i.this, exp.Column) and i.this.name.lower() == i.alias.lower())}
        if any(
            not c.table and c.name.lower() in aliases
            for clause in (inner.args.get("having"), inner.args.get("group"))
            if clause is not None
            for c in clause.find_all(exp.Column)
        ):
            return None
        derived.append((source.alias.lower(), inner, body))
    if len({d[2][0] for d in derived}) != 1 or len({d[0] for d in derived}) != len(derived):
        return None
    first_group = derived[0][1].args.get("group")
    keys_renamed = {_rename_tables(k.copy(), derived[0][2][1]).sql() for k in first_group.expressions} if first_group else set()

    def key_of(alias: str, column: exp.Column) -> str | None:
        """The key, with tables renamed by position, that output ``alias.column`` holds."""

        _, inner, (_, names) = next(d for d in derived if d[0] == alias)
        for item in inner.expressions:
            if item.alias_or_name.lower() == column.name.lower():
                renamed = _rename_tables(_value(item).copy(), names).sql()
                return renamed if renamed in keys_renamed else None
        return None

    # every later copy is linked to an earlier one on every key, and the ON clauses say nothing else
    seen = {derived[0][0]}
    for join, (alias, _, _) in zip(joins, derived[1:]):
        on = join.args.get("on")
        linked: set[str] = set()
        for part in _conjuncts(on) if on is not None else []:
            part = _unparen(part)
            if isinstance(part, exp.Boolean) and part.this:
                continue
            if not isinstance(part, exp.NullSafeEQ):
                return None
            left, right = _unparen(part.this), _unparen(part.expression)
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                return None
            if left.table.lower() == alias:
                left, right = right, left
            if right.table.lower() != alias or left.table.lower() not in seen:
                return None
            a, b = key_of(left.table.lower(), left), key_of(alias, right)
            if a is None or a != b:
                return None
            linked.add(a)
        if linked != keys_renamed:
            return None
        seen.add(alias)
    if select.args.get("where") is not None and any(
        c.table.lower() not in seen for c in select.args["where"].find_all(exp.Column) if c.table
    ):
        return None
    if any(not c.table for c in select.find_all(exp.Column) if not any(_inside(c, s) for s in sources)):
        return None
    # the merged select: the first copy, with every copy's outputs renamed into its tables
    first_alias, first, (_, first_names) = derived[0]
    merged = first.copy()
    items, renames, havings = [], {}, []
    for alias, inner, (_, names) in derived:
        mapping = dict(zip(names, first_names))
        for item in inner.expressions:
            value = _value(item).copy()
            for column in value.find_all(exp.Column):
                if column.table:
                    column.set("table", exp.to_identifier(mapping[column.table.lower()]))
            name = f"kumosql_j{len(items)}"
            items.append(exp.alias_(value, name))
            renames[(alias, item.alias_or_name.lower())] = name
        if inner.args.get("having") is not None:
            having = inner.args["having"].this.copy()
            for column in having.find_all(exp.Column):
                if column.table:
                    column.set("table", exp.to_identifier(mapping[column.table.lower()]))
            havings.append(having)
    merged.set("expressions", items)
    merged.set("having", exp.Having(this=_and_all(havings)) if havings else None)
    copy = select.copy()
    copy.set("joins", None)
    copy.set("from", None)
    copy.set("from_", exp.From(this=exp.Subquery(this=merged, alias=exp.TableAlias(this=exp.to_identifier(first_alias)))))
    for column in list(copy.find_all(exp.Column)):
        if _inside(column, merged):
            continue
        key = (column.table.lower(), column.name.lower())
        if column.table and column.table.lower() in seen:
            if key not in renames:
                return None
            column.set("table", exp.to_identifier(first_alias))
            column.set("this", exp.to_identifier(renames[key]))
    return copy


def _rename_tables(node: exp.Expression, names: list[str]) -> exp.Expression:
    """Name the tables in ``node`` by their position in ``names`` (a lone table also for bare columns)."""

    renames = {name: f"kumosql_b{i}" for i, name in enumerate(names)}
    for column in list(node.find_all(exp.Column)):
        if column.table and column.table.lower() in renames:
            column.set("table", exp.to_identifier(renames[column.table.lower()]))
        elif not column.table and len(names) == 1:
            column.set("table", exp.to_identifier("kumosql_b0"))
    return node


def _distribute_over_aggregating_branches(select: exp.Select) -> exp.Expression | None:
    """``SELECT f FROM (A UNION ALL B) AS t WHERE p`` is ``SELECT f FROM A AS t WHERE p UNION ALL ..`` when A or B aggregate.

    ``_distribute`` does this when no aggregate appears anywhere below the select; the branches'
    own aggregates do not matter, only that the select itself does not aggregate.
    """

    from .algebraic_equivalence import MAX_BRANCHES, _aligned_branches, _branch_copy, _no_extras, _plain_sources

    if not _no_extras(select, allow_group=False) or _own(select) or not any(select.find_all(exp.AggFunc)):
        return None
    sources = _plain_sources(select)
    if not sources:
        return None
    branch_lists = [_aligned_branches(s) for s in sources]
    if any(b is None for b in branch_lists):
        return None
    total = 1
    for branches in branch_lists:
        total *= len(branches)
    if total > MAX_BRANCHES:
        return None
    copies = []
    for combination in itertools.product(*branch_lists):
        copy = select.copy()
        for source, branch in zip(sources, combination):
            copy = _branch_copy(copy, source, branch)
        copies.append(copy)
    result: exp.Expression = copies[0]
    for copy in copies[1:]:
        result = exp.Union(this=result, expression=copy, distinct=False)
    return result


def _merge_projection_over_grouped_join(select: exp.Select) -> exp.Expression | None:
    """``SELECT f(d.x) FROM (SELECT p.s * q.c AS x FROM (..) AS p JOIN (..) AS q ON ..) AS d`` reads the join directly.

    Both selects only compute values row by row, so the outer one's expressions can be written over
    the inner one's sources. It is limited to inner joins of grouped subqueries, so that
    ``eager_aggregation.flatten_grouped_join`` sees the join it reads off.
    """

    from .algebraic_equivalence import _and_all

    source = _from(select)
    if select.args.get("joins") or not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    if any(select.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
        return None
    outside = [n for n in select.walk() if not _inside(n, source)]
    if _own(select) or any(isinstance(n, (exp.Window, exp.Star)) or (isinstance(n, exp.Select) and n is not select) for n in outside):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
        return None
    joins = inner.args.get("joins") or []
    if not joins or _own(inner) or any(isinstance(i, exp.Star) for i in inner.expressions):
        return None
    # outer joins are left to the rules that turn a null-rejected one into an inner join first
    if any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") for j in joins):
        return None
    if any(isinstance(n, exp.Window) for i in inner.expressions for n in i.walk()):
        return None
    inner_sources = [_from(inner)] + [j.this for j in joins]
    if not any(isinstance(s, exp.Subquery) and isinstance(s.this, exp.Select) and (s.this.args.get("group") or _own(s.this)) for s in inner_sources):
        return None
    outputs = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        value = _value(item)
        if not name or name in outputs or not _deterministic(value):
            return None
        outputs[name] = value
    alias = source.alias.lower()
    copy = select.copy()
    for column in list(copy.find_all(exp.Column)):
        if _inside(column, _from(copy)):
            continue
        if column.table.lower() not in (alias, ""):
            return None  # a correlated reference to an enclosing query
        if column.name.lower() not in outputs:
            return None
        value = outputs[column.name.lower()].copy()
        value = exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Case)) else value
        column.replace(exp.alias_(value, column.name) if column.parent is copy else value)
    merged = inner.copy()
    merged.set("expressions", copy.expressions)
    where = copy.args.get("where")
    if where is not None:
        existing = merged.args.get("where")
        merged.set("where", exp.Where(this=_and_all(([existing.this] if existing is not None else []) + [where.this])))
    return merged
