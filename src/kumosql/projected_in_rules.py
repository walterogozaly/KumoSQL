"""Normalize two-valued projected membership through plain column projections."""

from sqlglot import exp


def _unsafe_scope(select: exp.Select) -> bool:
    return any(select.args.get(k) for k in ("group", "having", "qualify", "windows")) or any(
        node.find_ancestor(exp.Select) is select
        for node in select.find_all(exp.AggFunc, exp.Window)
    )


def _identity_aliases(select: exp.Select) -> bool:
    parent = select.parent
    if not isinstance(parent, exp.Subquery) or not isinstance(parent.parent, (exp.From, exp.Join)):
        return False
    # Exposing a NOT NULL base table can trigger other nullability rules.
    # An aggregate ancestor may manufacture an empty-input row whose bare
    # columns are NULL; retain its projection boundary conservatively.
    ancestor = select.find_ancestor(exp.Select)
    while ancestor is not None:
        if _unsafe_scope(ancestor):
            return False
        ancestor = ancestor.find_ancestor(exp.Select)
    if any(select.args.get(key) for key in (
        "joins", "where", "group", "having", "distinct", "qualify", "windows",
        "order", "limit", "offset", "with", "with_",
    )):
        return False
    if not select.expressions or any(
        not isinstance(item.unalias(), exp.Column) or isinstance(item.unalias().this, exp.Star)
        for item in select.expressions
    ):
        return False
    changed = False
    for item in list(select.expressions):
        if isinstance(item, exp.Alias) and item.this.this == item.args.get("alias"):
            item.replace(item.this.copy())
            changed = True
    return changed


def _source(select: exp.Select) -> exp.Table | None:
    from_ = select.args.get("from_") or select.args.get("from")
    table = from_.this if from_ else None
    if (
        not isinstance(table, exp.Table) or table.db or table.catalog
        or select.args.get("joins")
        or any(value for key, value in table.args.items() if key not in ("this", "db", "catalog", "alias"))
        or table.args.get("alias") and table.args["alias"].args.get("columns")
        or select.args.get("with") or select.args.get("with_")
    ):
        return None
    return table


def normalize_projected_in(select: exp.Select, not_null: dict | None) -> exp.Select | None:
    """Non-null scalar IN is equality EXISTS, including when its input is empty.

    Names are resolved case-insensitively as in the prover, and parentheses
    around a column do not change its nullability. Restrict both scopes to a
    lone base table; outer-join padding therefore cannot invalidate NOT NULL.
    Exact identity aliases are removed first so existing capture-safe rules
    can inline derived column projections on a later normalization pass.
    """
    from .algebraic_equivalence import _qualified_outer_columns

    changed = _identity_aliases(select)
    # A global aggregate can produce an empty-input row whose bare columns
    # are NULL in permissive dialects, despite a base-column NOT NULL fact.
    # Grouping expansion can also pad keys. Do not reuse base nullability.
    if _unsafe_scope(select):
        return select if changed else None
    base = _source(select)
    if base is None:
        return select if changed else None
    nn = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    for node in list(select.find_all(exp.In)):
        if node.find_ancestor(exp.Select) is not select:
            continue
        left = node.this.unnest()
        query = node.args.get("query")
        inner = query.this if isinstance(query, exp.Subquery) else None
        if (
            not isinstance(left, exp.Column) or left.db or left.catalog
            or left.table.lower() not in ("", base.alias_or_name.lower())
            or left.name.lower() not in nn.get(base.name.lower(), set())
            or not isinstance(inner, exp.Select) or len(inner.expressions) != 1
            or node.args.get("expressions") or node.args.get("unnest")
        ):
            continue
        # Only select-list membership is needed here. Its UNKNOWN result
        # remains observable, so both NOT NULL checks below are mandatory.
        holder = node.parent
        while holder is not None and holder is not select:
            if isinstance(holder, (exp.Where, exp.Having, exp.Join, exp.Group, exp.Order)):
                break
            holder = holder.parent
        if holder is not select:
            continue
        table = _source(inner)
        value = inner.expressions[0].unalias().unnest()
        if (
            table is None or not isinstance(value, exp.Column) or value.db or value.catalog
            or value.table.lower() not in ("", table.alias_or_name.lower())
            or value.name.lower() not in nn.get(table.name.lower(), set())
            or any(inner.args.get(k) for k in (
                "group", "having", "distinct", "limit", "offset", "qualify",
                "windows", "with", "with_", "order",
            ))
            or any(inner.find_all(exp.AggFunc, exp.Window, exp.Subquery, exp.Exists))
        ):
            continue
        probe = inner.copy()
        refs = _qualified_outer_columns([left], select, probe, nn)
        if refs is None:
            continue
        value = probe.expressions[0].unalias().unnest().copy()
        match = exp.EQ(this=value, expression=refs[0])
        where = probe.args.get("where")
        probe.set("where", exp.Where(this=exp.and_(where.this.copy(), match) if where else match))
        probe.set("expressions", [exp.Literal.number(1)])
        node.replace(exp.Exists(this=probe))
        changed = True
    return select if changed else None
