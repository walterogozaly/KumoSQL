"""Read a zero-or-one TRUE lateral group as a nullable EXISTS indicator.

Grouping ``key IS NOT NULL`` has one group at most when a WHERE conjunct
rejects NULL keys. A LEFT JOIN ON TRUE then adds one TRUE-or-NULL value for
each left row, without changing its multiplicity. A nullable EXISTS value
preserves that indicator in projections as well as three-valued predicates.
"""

from sqlglot import exp

from .ast_utils import extended_grouping
from .quantified_rules import _binding
from .smt_equivalence import _Compiler, Unsupported

_EXTRAS = ("distinct", "having", "order", "limit", "offset", "qualify", "windows", "with", "with_", "laterals", "into", "locks", "sample", "prewhere", "connect", "match")
_STRICT = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node):
    node = _unparen(node)
    return _conjuncts(node.this) + _conjuncts(node.expression) if isinstance(node, exp.And) else [node]


def _nonnull_key(body, key, table, not_null):
    if key.name.lower() in (not_null or {}).get(table.name.lower(), ()):
        return True
    where = body.args.get("where")
    for part in _conjuncts(where.this) if where else []:
        if isinstance(part, _STRICT) and any(_unparen(side) == key for side in (part.this, part.expression)):
            return True
        if isinstance(part, exp.Not):
            test = _unparen(part.this)
            if isinstance(test, exp.Is) and isinstance(test.expression, exp.Null) and _unparen(test.this) == key:
                return True
    return False


def nullable_lateral_boolean_group(select, schema, not_null=None):
    if not schema or not isinstance(select, exp.Select) or select.args.get("group") or any(select.args.get(k) for k in _EXTRAS):
        return None
    if any(select.find_all(exp.Star, exp.Window, exp.AggFunc)):
        return None
    # A CTE on an enclosing query may shadow a physical table whose schema
    # supplies NOT NULL facts. Keep that entire scope outside this rule.
    ancestor = select
    while ancestor is not None:
        if ancestor.args.get("with") or ancestor.args.get("with_") or isinstance(ancestor, exp.CTE):
            return None
        ancestor = ancestor.parent
    if any(alias.args.get("columns") for alias in select.find_all(exp.TableAlias)):
        return None
    if any(any(value for name, value in table.args.items() if name not in ("this", "alias")) for table in select.find_all(exp.Table)):
        return None
    joins = select.args.get("joins") or []
    if any((j.args.get("side") or "").upper() not in ("", "LEFT") or
           (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") for j in joins):
        return None
    try:
        _Compiler(schema, False, "bigquery")._check_nondeterminism(select)
    except Unsupported:
        return None
    for join in joins:
        source = join.this
        on = _unparen(join.args.get("on"))
        if not isinstance(source, exp.Lateral) or any(value for name, value in source.args.items() if name not in ("this", "alias")) or not source.alias or not isinstance(source.this, exp.Subquery):
            continue
        if (join.args.get("side") or "").upper() != "LEFT" or any(value for name, value in join.args.items() if name not in ("this", "side", "kind", "on")) or not isinstance(on, exp.Boolean) or not on.this:
            continue
        body = source.this.this
        if not isinstance(body, exp.Select) or any(body.args.get(k) for k in _EXTRAS) or body.args.get("joins") or len(body.expressions) != 1:
            continue
        group = body.args.get("group")
        if group is None or extended_grouping(group) or len(group.expressions) != 1:
            continue
        marker = _unparen(body.expressions[0].unalias())
        if marker != _unparen(group.expressions[0]) or not isinstance(marker, exp.Not):
            continue
        null_test = _unparen(marker.this)
        if not isinstance(null_test, exp.Is) or not isinstance(null_test.expression, exp.Null):
            continue
        key = _unparen(null_test.this)
        from_ = body.args.get("from_") or body.args.get("from")
        table = from_.this if from_ else None
        if not isinstance(key, exp.Column) or not isinstance(table, exp.Table) or table.args.get("db") or table.args.get("catalog"):
            continue
        known = next((cols for name, cols in schema.items() if name.lower() == table.name.lower()), None)
        if not known or key.name.lower() not in {c.lower() for c in known} or key.table.lower() != table.alias_or_name.lower() or _binding(key, schema) is not body:
            continue
        if any(body.find_all(exp.Subquery, exp.Window, exp.AggFunc, exp.Anonymous)) or not _nonnull_key(body, key, table, not_null):
            continue
        # Move correlations only into the same scope and only from already available sources.
        outer_from = select.args.get("from_") or select.args.get("from")
        earlier = ([outer_from.this] if outer_from else []) + [j.this for j in joins[:joins.index(join)]]
        available = {s.alias_or_name.lower() for s in earlier}
        if any(_binding(c, schema) is not body and
               (_binding(c, schema) is not select or c.table.lower() not in available) for c in body.find_all(exp.Column)):
            continue
        name = body.expressions[0].alias_or_name.lower()
        uses = [c for c in select.find_all(exp.Column) if c.table.lower() == source.alias.lower() and _binding(c, schema) is select]
        if any(c.find_ancestor(exp.Select) is not select or c.name.lower() != name for c in uses):
            continue
        if any(not c.table and c.name.lower() == name for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select):
            continue
        rows = body.copy()
        rows.set("expressions", [exp.Literal.number(1)])
        rows.set("group", None)
        value = exp.Case(ifs=[exp.If(this=exp.Exists(this=rows), true=exp.true())], default=exp.Null())
        for column in uses:
            replacement = value.copy()
            if column.parent is select and column.arg_key == "expressions":
                replacement = exp.alias_(replacement, column.name)
            column.replace(replacement)
        select.set("joins", [j for j in joins if j is not join] or None)
        return select
    return None
