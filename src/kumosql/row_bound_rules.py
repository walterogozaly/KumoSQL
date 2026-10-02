"""Remove limits/orderings that a structural row bound makes redundant."""

from sqlglot import exp


def row_bound(query):
    if query.args.get("offset"):
        return None
    limit = query.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal) and not limit.expression.is_string:
        if any(v for k,v in limit.args.items() if k != "expression"):
            return None
        try:
            n = int(limit.expression.this)
            return n if n >= 0 else None
        except ValueError:
            return None
    if limit:
        return None
    if isinstance(query, exp.Subquery):
        return row_bound(query.this)
    if isinstance(query, exp.Union):
        a,b = row_bound(query.left), row_bound(query.right)
        return a+b if a is not None and b is not None else None
    if isinstance(query, (exp.Intersect, exp.Except)):
        return row_bound(query.left)
    if not isinstance(query, exp.Select) or query.args.get("group") or query.args.get("joins") or query.args.get("laterals"):
        return None
    if any(a.find_ancestor(exp.Select) is query and not a.find_ancestor(exp.Window) for a in query.find_all(exp.AggFunc)):
        return 1
    source = query.args.get("from_") or query.args.get("from")
    if source is None:
        return 1
    return row_bound(source.this) if isinstance(source.this, exp.Subquery) else None


def trim_redundant_row_clauses(tree):
    for query in list(tree.walk())[::-1]:
        if not isinstance(query, (exp.Select, exp.SetOperation, exp.Subquery)):
            continue
        limit = query.args.get("limit")
        if limit is not None and not query.args.get("offset"):
            bound = row_bound(query)
            body = query.copy()
            body.set("limit", None)
            before = row_bound(body)
            if bound is not None and before is not None and before <= bound:
                query.set("limit", None)
        if query.args.get("limit") is None and query.args.get("order") is not None:
            bound = row_bound(query)
            if bound is not None and bound <= 1:
                query.set("order", None)
    return tree
