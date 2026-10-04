"""Remove column-preserving wrappers without widening an unqualified binding."""

from sqlglot import exp


def remove_passthrough_sources(
    tree: exp.Expression, schema: dict | None
) -> exp.Expression:
    if not schema:
        return tree
    declared = {
        t.lower(): {c.lower() for c in columns} for t, columns in schema.items()
    }
    for source in list(tree.find_all(exp.Subquery)):
        inner = source.this
        if (
            not isinstance(source.parent, (exp.From, exp.Join))
            or not source.alias
            or not isinstance(inner, exp.Select)
        ):
            continue
        # Exposing a base source under aggregation can trigger membership
        # lowering that reads a different empty-input or NULL-padding phase.
        # Keep every naming boundary in those ancestors intact.
        ancestor = source.parent
        guarded = False
        while ancestor is not None:
            if isinstance(ancestor, exp.Select) and (
                any(ancestor.args.get(k) for k in ("group", "having", "qualify", "windows"))
                or any(n.find_ancestor(exp.Select) is ancestor for n in ancestor.find_all(exp.AggFunc, exp.Window))
            ):
                guarded = True
                break
            ancestor = ancestor.parent
        if guarded:
            continue
        if any(
            inner.args.get(k)
            for k in (
                "where",
                "joins",
                "group",
                "having",
                "qualify",
                "distinct",
                "order",
                "limit",
                "offset",
                "windows",
                "with",
                "with_",
                "laterals",
            )
        ):
            continue
        from_ = inner.args.get("from_") or inner.args.get("from")
        table = from_.this if from_ else None
        if (
            not isinstance(table, exp.Table)
            or table.args.get("db")
            or table.args.get("catalog")
            or table.args.get("joins")
            or table.args.get("alias")
            and table.args["alias"].args.get("columns")
        ):
            continue
        base = declared.get(table.name.lower())
        items = [e.unalias() for e in inner.expressions]
        if (
            not base
            or not items
            or any(
                not isinstance(e, exp.Column)
                or e.table.lower() not in ("", table.alias_or_name.lower())
                for e in items
            )
        ):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        if len(set(names)) != len(names) or any(
            name != e.name.lower() or name not in base for name, e in zip(names, items)
        ):
            continue
        # The base exposes additional columns. An unqualified read of one of
        # those names (even in a deeper correlated query) could be captured.
        added = base - set(names)
        if any(
            not c.table and c.name.lower() in added for c in tree.find_all(exp.Column)
        ):
            continue
        if any(
            c.table.lower() == source.alias.lower() and c.name.lower() not in names
            for c in tree.find_all(exp.Column)
            if c.find_ancestor(exp.Select) is not inner
        ):
            continue
        # Stars observe both the extra columns and their base-table order.
        if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
            continue
        replacement = table.copy()
        replacement.set("alias", exp.TableAlias(this=exp.to_identifier(source.alias)))
        source.replace(replacement)
    return tree
