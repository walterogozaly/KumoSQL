"""Recognize distinct-aggregate expansions without forgetting grouping identity.

Each filtered aggregate must select exactly one grouping set. Extra sets,
duplicate sets, or missing outer keys are deliberately not approximated.
"""

from sqlglot import exp

from .grouping_sets import is_grouping_call


class _Decline(Exception):
    pass


def collapse_grouping_expansion(select):
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Subquery):
        return None
    inner = source.this.this
    if not isinstance(inner, exp.Select) or inner.args.get("group") is None:
        return None
    if any(select.args.get(k) for k in ("joins", "where", "having", "distinct", "qualify", "limit", "offset", "order")):
        return None
    if any(inner.args.get(k) for k in ("having", "distinct", "qualify", "limit", "offset", "order")):
        return None
    if inner.find(exp.Window, exp.Subquery, exp.Exists) or select.find(exp.Window):
        return None
    group = inner.args["group"]
    if any(group.args.get(k) for k in ("cube", "rollup", "totals")):
        return None
    grouping_sets = group.args.get("grouping_sets") or [e for e in group.expressions if isinstance(e, exp.GroupingSets)]
    if grouping_sets:
        if len(grouping_sets) != 1 or any(not isinstance(e, exp.GroupingSets) for e in group.expressions):
            return None
        sets = []
        for item in grouping_sets[0].expressions:
            while isinstance(item, exp.Paren):
                item = item.this
            sets.append(list(item.expressions) if isinstance(item, exp.Tuple) else [item])
    else:
        sets = [list(group.expressions)]
    if not sets or any(not isinstance(k, exp.Column) for s in sets for k in s):
        return None
    key = lambda e: e.sql().lower()
    sets = [{key(k) for k in s} for s in sets]
    all_keys = set.union(*sets)
    names = [item.alias_or_name.lower() for item in inner.expressions]
    if not all(names) or len(set(names)) != len(names):
        return None
    values = {name: item.this if isinstance(item, exp.Alias) else item for name, item in zip(names, inner.expressions)}

    def resolve(column):
        if (not isinstance(column, exp.Column) or column.name.lower() not in values
                or column.table and column.table.lower() != source.this.alias.lower()):
            raise _Decline
        return values[column.name.lower()].copy()

    def flag(node, present):
        if isinstance(node, exp.Paren):
            return flag(node.this, present)
        if isinstance(node, exp.Column):
            value = resolve(node)
            if isinstance(value, exp.Column):
                raise _Decline
            return flag(value, present)
        if isinstance(node, exp.Boolean):
            return node.this
        if isinstance(node, exp.Literal) and not node.is_string:
            return int(node.this)
        if is_grouping_call(node):
            mask = 0
            for arg in node.expressions:
                if key(arg) not in all_keys:
                    raise _Decline
                mask = mask * 2 + (key(arg) not in present)
            return mask
        if isinstance(node, exp.EQ):
            return flag(node.left, present) == flag(node.right, present)
        raise _Decline

    try:
        outer_keys = [resolve(k) for k in select.args["group"].expressions] if select.args.get("group") else []
        if any(not isinstance(k, exp.Column) for k in outer_keys):
            return None
        outer_set = {key(k) for k in outer_keys}
        if any(not outer_set <= s for s in sets):
            return None
        count = 0

        def rewrite(node):
            nonlocal count
            if isinstance(node, exp.Filter):
                agg = node.this
                predicate = node.expression.this if isinstance(node.expression, exp.Where) else node.expression
                selected = [s for s in sets if flag(predicate, s) is True]
                if len(selected) != 1:
                    raise _Decline
                chosen = selected[0]
                if isinstance(agg, exp.Count) and not isinstance(agg.this, exp.Distinct):
                    args = [resolve(a) for a in [agg.this] + list(agg.expressions)]
                    if (not args or any(not isinstance(a, exp.Column) for a in args)
                            or chosen != outer_set | {key(a) for a in args}):
                        raise _Decline
                    count += 1
                    return exp.Count(this=exp.Distinct(expressions=args))
                if isinstance(agg, (exp.Min, exp.Max, exp.Sum)) and chosen == outer_set:
                    value = resolve(agg.this)
                    if not isinstance(value, (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg)):
                        raise _Decline
                    count += 1
                    return value
                raise _Decline
            if isinstance(node, exp.AggFunc):
                raise _Decline
            if isinstance(node, exp.Column):
                value = resolve(node)
                if key(value) not in outer_set:
                    raise _Decline
                return value
            if isinstance(node, (exp.Select, exp.Subquery)):
                raise _Decline
            return node

        outputs = [item.transform(rewrite) for item in select.expressions]
        if not count:
            return None
        result = inner.copy()
        result.set("expressions", outputs)
        result.set("group", exp.Group(expressions=outer_keys) if outer_keys else None)
        return result
    except (KeyError, ValueError, _Decline):
        return None
