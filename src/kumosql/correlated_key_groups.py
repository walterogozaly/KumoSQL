"""Expose a key fixed by a correlation to the existing singleton-group rules.

Within one invocation of a correlated subquery, k = outer.k fixes k.
Adding k to a plain GROUP BY therefore cannot split a group, even on empty
input. When this fixes a declared NOT NULL key, the entire filtered source
has at most one row and its DISTINCT is redundant.
"""
from sqlglot import exp
from .ast_utils import select_sources, visible_ctes, extended_grouping
from .having_rules import _conjuncts
from .cast_rules import expression_type


def _outer(column, select):
    if not isinstance(column, exp.Column) or not column.table or column.args.get("db") or column.args.get("catalog"):
        return None
    node = select.parent
    while node is not None:
        if isinstance(node, exp.Select):
            if any(source.alias_or_name.lower() == column.table.lower() for source in select_sources(node)):
                return node
        node = node.parent
    return None


def _integer(column, select, types):
    for source in select.find_all(exp.Table, exp.Subquery):
        if source.args.get("pivots"):
            return False  # PIVOT can replace an integer-named column with a floating aggregate
        if source.args.get("alias") and source.args["alias"].args.get("columns"):
            return False
        if isinstance(source, exp.Table) and not source.args.get("db") and not source.args.get("catalog") and source.name.lower() in visible_ctes(source):
            return False
    kind = expression_type(column,select,types)
    return kind is not None and kind[0] == "int"


def _fixed_key(select, keys, not_null, types):
    if not keys or not not_null or not types:
        return None  # no declared keys, NOT NULL facts or types: nothing to prove a key fixed
    sources = select_sources(select)
    if len(sources) != 1 or not isinstance(sources[0], exp.Table) or select.args.get("laterals"):
        return None
    table = sources[0]
    if any(table.args.get(k) for k in ("db", "catalog", "pivots", "joins", "laterals")) or table.name.lower() in visible_ctes(table):
        return None
    if table.args.get("alias") and table.args["alias"].args.get("columns"):
        return None
    where = select.args.get("where")
    if where is None:
        return None
    alias, name = table.alias_or_name.lower(), table.name.lower()
    fixed, correlated = set(), False
    for predicate in _conjuncts(where.this):
        if not isinstance(predicate, exp.EQ):
            continue
        for local, other in ((predicate.this, predicate.expression), (predicate.expression, predicate.this)):
            if not isinstance(local, exp.Column) or local.table.lower() != alias or local.args.get("db") or local.args.get("catalog"):
                continue
            if not _integer(local, select, types):
                continue
            outer = _outer(other, select)
            literal = isinstance(other, exp.Literal) and not other.is_string and other.this.isdigit()
            if literal or (outer is not None and other.table.lower() != alias and _integer(other,outer,types)):
                fixed.add(local.name.lower()); correlated |= isinstance(other, exp.Column)
    required = {c.lower() for c in not_null.get(name, ())}
    matches = [tuple(k) for k in keys.get(name, ()) if set(k) <= fixed and set(k) <= required]
    return (table, matches[0]) if correlated and matches else None


def expose_correlated_key_groups(select, keys, not_null, types):
    group = select.args.get("group")
    if group is not None and not extended_grouping(group) and group.expressions and not select.find(exp.Window):
        fixed = _fixed_key(select, keys, not_null, types)
        if fixed is not None:
            table, key = fixed
            grouped = {c.name.lower() for c in group.expressions if isinstance(c, exp.Column) and c.table.lower() == table.alias_or_name.lower()}
            missing = [name for name in key if name.lower() not in grouped]
            if missing or select.args.get("distinct"):
                result = select.copy()
                result.args["group"].set("expressions", list(result.args["group"].expressions)+[exp.column(name,table=table.alias_or_name) for name in missing])
                result.set("distinct", None)
                return result
    # A key-filtered projection can be lifted from CROSS JOIN LATERAL using
    # the existing filter/project reader once its correlation key is exposed.
    for index, join in enumerate(select.args.get("joins") or []):
        lateral = join.this
        if not isinstance(lateral, exp.Lateral) or lateral.args.get("view") or not isinstance(lateral.this, exp.Subquery):
            continue
        body = lateral.this.this
        if not isinstance(body, exp.Select) or join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS"):
            continue
        functions = [node for node in body.find_all(exp.Func) if not isinstance(node, (exp.And, exp.Or, exp.Not))]
        if any(body.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with", "with_")) or functions or body.find(exp.Window, exp.Subquery, exp.Exists):
            continue
        if not all(isinstance(item.unalias(), exp.Column) for item in body.expressions) or any(select.find_all(exp.Star)):
            continue
        fixed = _fixed_key(body, keys, not_null, types)
        if fixed is None:
            continue
        result = select.copy(); target = result.args["joins"][index]; inner = target.this.this.this
        table, key = fixed
        names = {item.alias_or_name.lower() for item in inner.expressions}
        for number, name in enumerate(key):
            output = f"kumosql_fixed_key_{number}"
            while output.lower() in names:
                output += "_"
            inner.append("expressions",exp.alias_(exp.column(name,table=table.alias_or_name),output));names.add(output.lower())
        target.set("kind", "INNER")
        from .algebraic_equivalence import _lateral_joins
        result = _lateral_joins(result)
        if not isinstance(result.args["joins"][index].this, exp.Lateral):
            return result
    return None
