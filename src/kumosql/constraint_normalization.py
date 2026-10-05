"""Conservative bag-preserving identities justified by declared uniqueness."""

from sqlglot import exp

from .ast_utils import declared_key


def _parts(node):
    if isinstance(node, exp.Paren):
        return _parts(node.this)
    if isinstance(node, exp.And):
        return _parts(node.left) + _parts(node.right)
    return [node]


def normalize_key_counts(select, keys):
    """COUNT(DISTINCT a unique column) = COUNT(column), even for nullable keys.

    NULLs do not contribute to either count. Restrict this to a single table:
    joins may repeat its key, and a window observes a different input relation.
    """
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Table) or select.args.get("joins"):
        return None
    table = source.this
    singles = {k[0].lower() for k in (keys or {}).get(declared_key(table), []) if len(k) == 1}
    copy = select.copy()
    changed = False
    for count in copy.find_all(exp.Count):
        if count.find_ancestor(exp.Select) is not copy or count.find_ancestor(exp.Window):
            continue
        arg = count.this
        if not isinstance(arg, exp.Distinct) or len(arg.expressions) != 1:
            continue
        column = arg.expressions[0]
        if (isinstance(column, exp.Column) and column.name.lower() in singles
                and (not column.table or column.table.lower() == table.alias_or_name.lower())):
            count.set("this", column.copy())
            changed = True
    return copy if changed else None


def keyed_join_to_exists(select, keys, not_null):
    """A DISTINCT projection of a left key turns an inner join into existence.

    A nullable UNIQUE key suffices only if every component is rejected when
    NULL by an equality conjunct in ON (or is declared NOT NULL).
    """
    distinct = select.args.get("distinct")
    source = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    if (distinct is None or distinct.args.get("on") or source is None
            or len(joins) != 1 or not isinstance(source.this, exp.Table)):
        return None
    join, left = joins[0], source.this
    if (not isinstance(join.this, exp.Table) or join.side
            or join.kind not in ("", "INNER") or join.args.get("using")
            or join.args.get("on") is None):
        return None
    if any(select.args.get(k) for k in ("group", "having", "qualify", "limit", "offset", "order", "laterals")):
        return None
    if select.find(exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Star):
        return None
    alias, right_alias = left.alias_or_name.lower(), join.this.alias_or_name.lower()
    if alias == right_alias:
        return None
    if any(not c.table or c.table.lower() not in (alias, right_alias) for c in select.find_all(exp.Column)):
        return None
    outputs = set()
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Column) or value.table.lower() != alias:
            return None
        outputs.add(value.name.lower())
    nonnull = {c.lower() for c in (not_null or {}).get(declared_key(left), ())}
    for part in _parts(join.args["on"]):
        if isinstance(part, exp.EQ):
            nonnull.update(c.name.lower() for c in (part.left, part.right)
                           if isinstance(c, exp.Column) and c.table.lower() == alias)
    if not any(set(c.lower() for c in k) <= outputs & nonnull
               for k in (keys or {}).get(declared_key(left), []) if k):
        return None
    copy = select.copy()
    probe = exp.select("1").from_(join.this.copy()).where(join.args["on"].copy())
    if select.args.get("where"):
        probe.where(select.args["where"].this.copy(), copy=False)
    copy.set("joins", None)
    copy.set("distinct", None)
    copy.set("where", exp.Where(this=exp.Exists(this=probe)))
    return copy
