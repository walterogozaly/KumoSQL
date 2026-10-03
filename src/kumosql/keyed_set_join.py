"""Lift a derived DISTINCT through one keyed inner join with an injective projection.

The outer result is already unique when its columns determine every DISTINCT
output and each derived row matches at most one dimension row. Removing the
inner DISTINCT and adding an outer DISTINCT then preserves the exact row bag.
Only direct integer columns and deterministic filter/project/inner-join shapes
are accepted; lossy comparison coercions and NULL padding are excluded.
"""
from __future__ import annotations

from sqlglot import exp

# Any other SELECT argument (GROUP BY, HAVING, QUALIFY, ORDER BY, LIMIT, windows, WITH, laterals,
# samples, CONNECT BY, INTO, ...) declines the rewrite.
_PLAIN_SELECT_ARGS = {"expressions", "from", "from_", "joins", "where", "distinct"}
# Declared integer types: ``=`` between any two of them compares exact values (no
# floating, string or collation coercion), so equal values are identical values.
_INTEGER_TYPES = {"TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT", "INT64"}
_PREDICATE_NODES = (exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ,
                    exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Is, exp.Column,
                    exp.Identifier, exp.Literal, exp.Null, exp.Boolean)


def _modified(select):
    return any(value for key, value in select.args.items() if key not in _PLAIN_SELECT_ARGS)


def _source(select):
    node = select.args.get("from_") or select.args.get("from")
    return node.this if node is not None else None


def _plain_table(table):
    return (isinstance(table, exp.Table)
            and not (table.args.get("alias") and table.args["alias"].args.get("columns"))
            and not any(value for key, value in table.args.items() if key not in ("this", "alias")))


def _inner_join(join):
    return not join.side and join.kind.upper() in ("", "INNER", "CROSS") and not any(
        value for key, value in join.args.items() if key not in ("this", "on", "kind"))


def _declared_integer(types, table, name):
    declared = types.get(table.name.lower(), {}).get(name)
    if declared is None:
        return False
    try:
        parsed = exp.DataType.build(declared, dialect="mysql")
    except Exception:  # noqa: BLE001 - an unreadable type is not an integer type
        return False
    this = parsed.this
    return (this.name if isinstance(this, exp.DataType.Type) else str(this)).upper() in _INTEGER_TYPES


def _integer_column(column, scope, types):
    """Whether qualified ``column`` reads a declared integer column of ``scope``'s own sources.

    A derived source is followed only through a plain column projection; a qualifier that names no
    source (an outer reference) or more than one source is not resolved.
    """

    if not isinstance(column, exp.Column) or not column.table or not isinstance(column.this, exp.Identifier):
        return False
    if column.args.get("db") or column.args.get("catalog"):
        return False
    sources = [_source(scope)] + [j.this for j in scope.args.get("joins") or []]
    matches = [s for s in sources if s is not None and (s.alias_or_name or "").lower() == column.table.lower()]
    if len(matches) != 1:
        return False
    source, name = matches[0], column.name.lower()
    if _plain_table(source):
        return _declared_integer(types, source, name)
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        items = [i for i in source.this.expressions if i.alias_or_name.lower() == name]
        return len(items) == 1 and _integer_column(_value(items[0]), source.this, types)
    return False


def _value(projection):
    return projection.this if isinstance(projection, exp.Alias) else projection


def _column(column):
    return column.table.lower(), column.name.lower()


def _conjuncts(node):
    if isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    elif isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    else:
        yield node


def lift_keyed_set_join(select: exp.Select, keys: dict | None,
                        not_null: dict | None, types: dict | None) -> exp.Expression | None:
    if not keys or not types or select.args.get("distinct") or _modified(select):
        return None
    joins = select.args.get("joins") or []
    if len(joins) != 1 or not _inner_join(joins[0]):
        return None
    sources = [_source(select), joins[0].this]
    derived = [s for s in sources if isinstance(s, exp.Subquery)]
    physical = [s for s in sources if _plain_table(s)]
    if len(derived) != 1 or len(physical) != 1:
        return None
    derived, table = derived[0], physical[0]
    inner = derived.this
    if any(value for key, value in derived.args.items() if key not in ("this", "alias")):
        return None  # a sample or modifier on the derived table reads its deduplicated rows
    if not derived.alias or derived.args["alias"].args.get("columns") or not isinstance(inner, exp.Select):
        return None
    distinct = inner.args.get("distinct")
    if not distinct or distinct.args.get("on") or _modified(inner):
        return None
    inner_sources = [_source(inner)] + [j.this for j in inner.args.get("joins") or []]
    if not all(_plain_table(s) for s in inner_sources) or not all(_inner_join(j) for j in inner.args.get("joins") or []):
        return None
    root = select
    while root.parent is not None:
        root = root.parent
    if root.find(exp.With) is not None:
        return None  # A CTE spelling is not a physical table's declared key.
    d_alias, t_alias = derived.alias.lower(), table.alias_or_name.lower()
    if d_alias == t_alias:
        return None
    aliases = {d_alias, t_alias}
    outputs = []
    for projection in inner.expressions:
        column = _value(projection)
        if not _integer_column(column, inner, types) or not projection.alias_or_name:
            return None
        outputs.append((d_alias, projection.alias_or_name.lower()))
    if len(set(outputs)) != len(outputs):
        return None
    projected = set()
    for projection in select.expressions:
        column = _value(projection)
        if not isinstance(column, exp.Column) or column.table.lower() not in aliases:
            return None
        projected.add(_column(column))
    predicates = [joins[0].args.get("on"), select.args.get("where")]
    predicates = [p.this if isinstance(p, exp.Where) else p for p in predicates if p is not None]
    # Filtering must read only this joined row, without scoped/volatile work.
    for scope in (select, inner):
        scopes = [scope.args.get("where")] + [j.args.get("on") for j in scope.args.get("joins") or []]
        for predicate in (p for p in scopes if p is not None):
            expression = predicate.this if isinstance(predicate, exp.Where) else predicate
            if any(not isinstance(n, _PREDICATE_NODES) for n in expression.walk()):
                return None
            for column in expression.find_all(exp.Column):
                if not _integer_column(column, scope, types):
                    return None
    parent = {}

    def representative(column):
        parent.setdefault(column, column)
        if parent[column] != column:
            parent[column] = representative(parent[column])
        return parent[column]

    for predicate in predicates:
        for part in _conjuncts(predicate):
            if isinstance(part, exp.EQ) and isinstance(part.this, exp.Column) and isinstance(part.expression, exp.Column):
                a, b = _column(part.this), _column(part.expression)
                if a[0] in aliases and b[0] in aliases:
                    parent[representative(a)] = representative(b)
    # Ordinary integer equality is injective on surviving rows; no float cast
    # can make different DISTINCT outputs or key values match one another.
    output_classes = {representative(c) for c in outputs}
    if not output_classes <= {representative(c) for c in projected}:
        return None
    nn = {c.lower() for c in (not_null or {}).get(table.name.lower(), ())}
    if not any(key and set(c.lower() for c in key) <= nn and all(
        representative((t_alias, c.lower())) in output_classes for c in key)
        for key in keys.get(table.name.lower(), ())):
        return None
    copy = select.copy()
    copied_sources = [_source(copy), copy.args["joins"][0].this]
    next(s for s in copied_sources if isinstance(s, exp.Subquery)).this.set("distinct", None)
    copy.set("distinct", exp.Distinct())
    return copy
