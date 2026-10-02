"""Remove CASE arms that grouped row counts cannot make true.

A missing LEFT JOIN row makes the count NULL, not zero. The identities here
are only used in searched CASE WHEN, where FALSE and UNKNOWN both skip an arm.
"""

from sqlglot import exp


def fold_grouped_count_cases(tree, not_null):
    for select in tree.find_all(exp.Select):
        counts = {}
        source = select.args.get("from_") or select.args.get("from")
        sources = ([source.this] if source else []) + [j.this for j in select.args.get("joins") or []]
        for relation in sources:
            if not isinstance(relation, exp.Subquery) or not isinstance(relation.this, exp.Select):
                continue
            inner = relation.this
            group = inner.args.get("group")
            base = inner.args.get("from_") or inner.args.get("from")
            if (group is None or not group.expressions or any(not isinstance(k, exp.Column) for k in group.expressions)
                    or any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "totals"))
                    or inner.args.get("joins") or base is None or not isinstance(base.this, exp.Table)):
                continue
            table = base.this
            nn = {n.lower() for n in (not_null or {}).get(table.name.lower(), ())}
            for item in inner.expressions:
                value = item.this if isinstance(item, exp.Alias) else item
                if not isinstance(value, exp.Count) or value.expressions:
                    continue
                arg = value.this
                if isinstance(arg, exp.Star) or (isinstance(arg, exp.Column) and arg.name.lower() in nn
                        and (not arg.table or arg.table.lower() == table.alias_or_name.lower())):
                    counts[(relation.alias.lower(), item.alias_or_name.lower())] = relation.alias.lower()

        def rowcount(node):
            return counts.get((node.table.lower(), node.name.lower())) if isinstance(node, exp.Column) else None

        for case in list(select.find_all(exp.Case)):
            if case.find_ancestor(exp.Select) is not select or case.this is not None:
                continue
            kept = []
            for arm in case.args.get("ifs") or []:
                condition = arm.this
                impossible = False
                if isinstance(condition, exp.EQ):
                    for count, zero in ((condition.left, condition.right), (condition.right, condition.left)):
                        if rowcount(count) and isinstance(zero, exp.Literal) and not zero.is_string and zero.this == "0":
                            impossible = True
                if isinstance(condition, (exp.LT, exp.GT, exp.NEQ)):
                    a, b = rowcount(condition.left), rowcount(condition.right)
                    impossible = a is not None and a == b
                if not impossible:
                    kept.append(arm)
            if len(kept) == len(case.args.get("ifs") or []):
                continue
            case.set("ifs", kept)
            if not kept:
                case.replace(case.args.get("default", exp.Null()).copy())
            elif (len(kept) == 1 and isinstance(kept[0].args.get("true"), exp.Boolean)
                    and kept[0].args["true"].this is True
                    and isinstance(case.args.get("default"), exp.Boolean) and case.args["default"].this is False):
                predicate = kept[0].this
                inner = predicate.this if isinstance(predicate, exp.Not) else predicate
                if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                    case.replace(predicate.copy())
    return tree
