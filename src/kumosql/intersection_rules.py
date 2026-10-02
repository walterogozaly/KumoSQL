"""The per-branch deduplication/count construction of set intersection."""

from sqlglot import exp


def collapse_counted_intersection(select):
    source = select.args.get("from_") or select.args.get("from")
    group, having = select.args.get("group"), select.args.get("having")
    if source is None or not isinstance(source.this, exp.Subquery) or group is None or having is None:
        return None
    if any(select.args.get(k) for k in ("joins", "where", "distinct", "qualify", "order", "limit", "offset")):
        return None
    if any(group.args.get(k) for k in ("grouping_sets", "cube", "rollup", "totals")):
        return None
    condition = having.this
    if not isinstance(condition, exp.EQ):
        return None
    count, size = condition.left, condition.right
    if not (isinstance(count, exp.Count) and isinstance(count.this, exp.Star)
            and isinstance(size, exp.Literal) and not size.is_string):
        return None
    from .algebraic_equivalence import _aligned_branches

    branches = _aligned_branches(source.this)
    if branches is None or len(branches) < 2 or size.this != str(len(branches)):
        return None
    alias = source.this.alias.lower()
    def columns(items):
        if any(not isinstance(e, exp.Column) or e.is_star or e.table and e.table.lower() != alias for e in items):
            return None
        return [e.name.lower() for e in items]
    wanted = columns(select.expressions)
    grouped = columns(group.expressions)
    if not wanted or grouped is None or set(wanted) != set(grouped) or len(set(wanted)) != len(wanted):
        return None
    parts = []
    for branch in branches:
        bg = branch.args.get("group")
        if bg is None or any(bg.args.get(k) for k in ("grouping_sets", "cube", "rollup", "totals")):
            return None
        if any(branch.args.get(k) for k in ("having", "qualify", "limit", "offset", "distinct")) or branch.find(exp.Window):
            return None
        names = [e.alias_or_name.lower() for e in branch.expressions]
        if len(set(names)) != len(names):
            return None
        mapping = {n: e.this if isinstance(e, exp.Alias) else e for n,e in zip(names, branch.expressions)}
        if any(n not in mapping or not isinstance(mapping[n], exp.Column) for n in wanted):
            return None
        selected = [mapping[n] for n in wanted]
        if {e.sql().lower() for e in selected} != {e.sql().lower() for e in bg.expressions}:
            return None
        part = branch.copy()
        part.set("group", None)
        part.set("expressions", [exp.alias_(e.copy(), n) for n,e in zip(wanted, selected)])
        part.set("distinct", exp.Distinct())
        parts.append(part)
    result = parts[0]
    for part in parts[1:]:
        result = exp.Intersect(this=result, expression=part, distinct=True)
    return result
