"""Derived tables that hold an outer join, read through instead of kept whole.

The prover merges a derived table only when it compiles to one select-project-join block, which
an outer join never is, so such a table stays an opaque relation compared by its text, and two
spellings that wrap the same join in different projection layers never meet. Calcite's plans
wrap every outer join that way.

* ``flatten_outer_join_derived``: ``SELECT .. FROM (SELECT f(b.y) AS v .. FROM a LEFT JOIN b ON c
  WHERE w) AS d [JOIN ..] WHERE p(d.v)`` reads ``a LEFT JOIN b ON c [JOIN ..] WHERE w AND p(f(b.y))``.
  The derived table returns one row per row of its join that passes ``w``; its columns are
  deterministic expressions of that row. Joins after it extend the join (left-deep), and a
  grouped select is re-wrapped in ``_wrap_outer_join_aggregate``'s positional form.
* ``lift_derived_expressions``: a computed column of a derived outer join read by another join
  is computed above it instead; on a NULL-extended side only when the expression is NULL on the
  padded row (``null_when_inputs_null``).
* ``order_derived_columns``: the plain columns of a derived outer join in a fixed order, since the
  prover matches its columns by position.
* ``indicator_joins_after_flattening``: Calcite's LEFT JOIN indicator test, once flattening has
  brought it next to its ``IS NULL`` test.
* ``mirror_right_join``: ``LEFT OUTER JOIN`` as ``LEFT JOIN``, a lone ``FULL JOIN`` with an empty
  side as one-sided, and a lone ``RIGHT JOIN`` as the mirrored ``LEFT JOIN``, the form the other
  rules read.
* ``flatten_join_tree``: a parenthesized join tree ``a JOIN (b CROSS JOIN c) ON p`` read left-deep.
* ``constants_into_outer_on``: a column its derived table fixes to a number (``WHERE k = 10``) reads
  as that number in a later outer join's ON clause.
* ``left_join_rejected_by_where``: ``a LEFT JOIN b ON c WHERE b.x = a.y`` is an inner join, the flat
  form of ``outer_filters.strengthen_derived_outer_join`` (flattening runs first).
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY

_OUTER_SIDES = {"LEFT", "RIGHT", "FULL"}
_NONDETERMINISTIC = (exp.Rand,)
_NONDETERMINISTIC_NAMES = {"RAND", "RANDOM", "UUID", "GENERATE_UUID", "NEWID"}


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = _from(select)
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def _plain(select: exp.Select, *, allow_group: bool = False) -> bool:
    banned = ("order", "limit", "offset", "qualify", "windows", "with_", "with", "kind", "into", "locks", "sample", "settings")
    if not allow_group:
        banned += ("distinct", "group", "having")
    return not any(select.args.get(key) for key in banned)


def _deterministic(node: exp.Expression) -> bool:
    for sub in node.walk():
        if isinstance(sub, _NONDETERMINISTIC):
            return False
        if isinstance(sub, exp.Anonymous) and (sub.name or "").upper() in _NONDETERMINISTIC_NAMES:
            return False
    return True


_SCOPED = (exp.Subquery, exp.Select, exp.Exists, exp.Lateral, exp.Unnest, exp.Window, exp.AggFunc, exp.Star)


def _grouped(select: exp.Select) -> bool:
    if select.args.get("group") is not None or select.args.get("having") is not None:
        return True
    return any(agg.find_ancestor(exp.Select) is select for e in select.expressions for agg in e.find_all(exp.AggFunc))


def _clauses(select: exp.Select) -> list[exp.Expression]:
    """The select list, WHERE and ON conditions: everything but the sources."""

    parts = list(select.expressions)
    for key in ("where", "group", "having"):
        if select.args.get(key) is not None:
            parts.append(select.args[key])
    parts.extend(j.args["on"] for j in select.args.get("joins") or [] if j.args.get("on") is not None)
    return parts


def _inner_join_shape(inner: exp.Select) -> list[str] | None:
    """The inner select's source aliases when it is a filter and projection over a join with an outer join."""

    if not _plain(inner) or not inner.args.get("joins"):
        return None
    joins = inner.args["joins"]
    if not any((j.args.get("side") or "").upper() in _OUTER_SIDES for j in joins):
        return None
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        if kind not in ("", "INNER", "CROSS", "OUTER") or join.args.get("using") is not None or join.args.get("method"):
            return None
        if kind == "OUTER" and not join.args.get("side"):
            return None
    sources = _sources(inner)
    aliases = []
    for source in sources:
        if not isinstance(source, (exp.Table, exp.Subquery)) or not source.alias_or_name:
            return None
        if isinstance(source, exp.Table) and (source.args.get("joins") or source.args.get("pivots") or source.args.get("laterals") or not isinstance(source.this, exp.Identifier)):
            return None
        if isinstance(source, exp.Subquery) and not source.alias:
            return None
        aliases.append(source.alias_or_name.lower())
    if len(set(aliases)) != len(aliases):
        return None
    # Subqueries in the select list, WHERE or ON would need their scopes followed.
    for part in _clauses(inner):
        if any(isinstance(n, _SCOPED) for n in part.walk()):
            return None
        # Every column the inner select reads names one of its own sources.
        for column in part.find_all(exp.Column):
            if isinstance(column.this, exp.Star) or not column.table or column.table.lower() not in aliases:
                return None
    return aliases


def flatten_outer_join_derived(select: exp.Select) -> exp.Expression | None:
    """``SELECT .. FROM (SELECT .. FROM a LEFT JOIN b ON c WHERE w) AS d WHERE p`` reads the join directly.

    The derived table must be the select's FROM item; joins after it extend the flattened join
    (``(a LEFT JOIN b) LEFT JOIN x ON q`` is ``a LEFT JOIN b LEFT JOIN x ON q``). A later RIGHT or
    FULL join can pad ``d`` with NULLs, so then ``d`` must not filter and every computed column
    read must be NULL on the padded row; a later join of any kind needs every column qualified.
    """

    from_ = _from(select)
    if from_ is None or not _plain(select, allow_group=True):
        return None
    if select.args.get("distinct") is not None and select.args["distinct"].args.get("on"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    if not source.alias and select.args.get("joins"):
        return None
    if source.args.get("pivots") or source.args.get("laterals") or source.args.get("sample"):
        return None
    alias_node = source.args.get("alias")
    if alias_node is not None and alias_node.args.get("columns"):
        return None
    inner = source.this
    inner_aliases = _inner_join_shape(inner)
    if inner_aliases is None:
        return None
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in inner.expressions):
        return None
    if not all(_deterministic(e) for e in inner.expressions):
        return None
    joins = select.args.get("joins") or []
    grouped = _grouped(select)
    if grouped and not joins and all(isinstance(e.this if isinstance(e, exp.Alias) else e, exp.Column) for e in inner.expressions):
        return None  # plain columns under a grouping are already the canonical shape (_wrap_outer_join_aggregate)
    later = _sources(select)[1:]
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        if kind not in ("", "INNER", "CROSS", "OUTER") or join.args.get("using") is not None or join.args.get("method"):
            return None
        if kind == "OUTER" and not join.args.get("side"):
            return None
        if not isinstance(join.this, (exp.Table, exp.Subquery)) or not join.this.alias_or_name or join.this.args.get("laterals") or join.this.args.get("pivots"):
            return None
    if {s.alias_or_name.lower() for s in later} & (set(inner_aliases) | {source.alias.lower()}):
        return None
    padded = any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in joins)
    if padded and inner.args.get("where") is not None:
        return None
    # The reading select: no subqueries or windows of its own (aggregates and COUNT(*) are fine).
    clauses = _clauses(select)
    for node in (n for part in clauses for n in part.walk()):
        if isinstance(node, exp.Star) and isinstance(node.parent, exp.Count):
            continue
        if isinstance(node, _SCOPED) and not isinstance(node, exp.AggFunc):
            return None
    outputs = {e.alias.lower() for e in select.expressions if isinstance(e, exp.Alias)}
    late = [c for key in ("group", "having") if select.args.get(key) is not None for c in select.args[key].find_all(exp.Column)]
    if any(not c.table and c.name.lower() in outputs for c in late):
        return None  # GROUP BY or HAVING may name an output of the select
    alias = source.alias.lower()
    by_name = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
    for column in (c for part in clauses for c in part.find_all(exp.Column)):
        table = column.table.lower()
        if table and table != alias:
            # A correlated reference to an enclosing query (or a later source): it must not be captured by an inner alias.
            if table in inner_aliases:
                return None
            continue
        if not table and joins:
            return None  # an unqualified column could belong to a later source, or become ambiguous
        if column.name.lower() not in by_name:
            return None
        if padded and not null_when_inputs_null(by_name[column.name.lower()]):
            return None
    copy = select.copy()
    # Select-list items keep their output names once their columns become expressions.
    for index, item in enumerate(list(copy.expressions)):
        if isinstance(item, exp.Column):
            copy.expressions[index].replace(exp.alias_(item.copy(), item.name))
    for column in [c for part in _clauses(copy) for c in part.find_all(exp.Column)]:
        table = column.table.lower()
        if table and table != alias:
            continue
        replacement = by_name[column.name.lower()].copy()
        column.replace(exp.Paren(this=replacement) if isinstance(replacement, (exp.Binary, exp.Not)) else replacement)
    new_inner = inner.copy()
    copy.set(FROM_KEY, new_inner.args.get("from_") or new_inner.args.get("from"))
    copy.set("joins", (new_inner.args.get("joins") or []) + list(copy.args.get("joins") or []))
    conditions = [w.this for w in (new_inner.args.get("where"), copy.args.get("where")) if w is not None]
    if conditions:
        where = conditions[0]
        for part in conditions[1:]:
            where = exp.And(this=exp.Paren(this=where) if isinstance(where, exp.Or) else where, expression=exp.Paren(this=part) if isinstance(part, exp.Or) else part)
        copy.set("where", exp.Where(this=where))
    if grouped:
        # The prover reads an aggregate over an outer join through a derived table of plain columns.
        from .algebraic_equivalence import _wrap_outer_join_aggregate

        wrapped = _wrap_outer_join_aggregate(copy)
        return wrapped if wrapped is not None and wrapped.sql() != select.sql() else None
    return copy


def indicator_joins_after_flattening(select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """Read a LEFT JOIN indicator as ``EXISTS`` once flattening has brought it next to its ``IS NULL`` test.

    ``_left_join_indicator_to_exists`` runs once before derived tables are merged; Calcite writes the
    indicator join inside a derived table and tests it one select above, so it is tried again here.
    """

    from .algebraic_equivalence import _indicator_join

    if not select.args.get("joins"):
        return None
    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}
    copy = select.copy()
    changed = False
    progress = True
    while progress:
        progress = False
        for join in list(copy.args.get("joins") or []):
            if _indicator_join(copy, join, key_sets):
                changed = progress = True
                break
    return copy if changed else None


def outer_join_rules(select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """The rules of this module, as one entry of the normalizer's rule list."""

    return flatten_join_tree(select) or flatten_outer_join_derived(select) or lift_derived_expressions(select) or order_derived_columns(select) or indicator_joins_after_flattening(select, keys) or mirror_right_join(select) or constants_into_outer_on(select) or left_join_rejected_by_where(select)


# --- NULL propagation -----------------------------------------------------------------

_STRICT_BINARY = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)
_STRICT_UNARY = (exp.Paren, exp.Neg, exp.Not, exp.BitwiseNot, exp.Cast, exp.TryCast, exp.Abs, exp.Upper, exp.Lower, exp.Length, exp.Floor, exp.Ceil, exp.Sqrt, exp.Ln, exp.Exp)


def null_when_inputs_null(expr: exp.Expression) -> bool:
    """True when ``expr`` is NULL whenever every column it reads is NULL.

    Such an expression can be computed above an outer join instead of inside its NULL-extended
    side: the padded row gives NULL either way. Constants, ``IS NULL``, ``COALESCE`` and a ``CASE``
    with a non-NULL arm reachable from NULL inputs are not; unknown functions are assumed not.
    """

    if isinstance(expr, exp.Column):
        return not isinstance(expr.this, exp.Star)
    if isinstance(expr, exp.Null):
        return True
    if isinstance(expr, _STRICT_BINARY):
        return null_when_inputs_null(expr.this) or null_when_inputs_null(expr.expression)
    if isinstance(expr, (exp.And, exp.Or)):
        # NULL AND FALSE is FALSE, NULL OR TRUE is TRUE: only NULL on both sides is NULL.
        return null_when_inputs_null(expr.this) and null_when_inputs_null(expr.expression)
    if isinstance(expr, _STRICT_UNARY) and not expr.args.get("expressions") and not expr.args.get("expression"):
        return null_when_inputs_null(expr.this)
    if isinstance(expr, exp.Case):
        operand = expr.this
        default = expr.args.get("default")
        rest = default is None or null_when_inputs_null(default)
        if operand is not None and null_when_inputs_null(operand):
            return rest  # every WHEN compares with NULL, none is taken
        for branch in expr.args.get("ifs") or []:
            # a condition that is NULL never selects its arm; any other may
            if operand is not None or not null_when_inputs_null(branch.this):
                if not null_when_inputs_null(branch.args["true"]):
                    return False
        return rest
    if isinstance(expr, exp.If):
        false = expr.args.get("false")
        rest = false is None or null_when_inputs_null(false)
        if null_when_inputs_null(expr.this):
            return rest
        return null_when_inputs_null(expr.args["true"]) and rest
    return False


# --- lifting computed columns out of a derived outer join -----------------------------


def _padded(select: exp.Select, source: exp.Expression) -> bool:
    """Whether a join of ``select`` can NULL-extend ``source``."""

    joins = select.args.get("joins") or []
    sides = [(j.args.get("side") or "").upper() for j in joins]
    if source is _from(select).this:
        return any(s in ("RIGHT", "FULL") for s in sides)
    position = next(i for i, j in enumerate(joins) if j.this is source)
    return sides[position] in ("LEFT", "FULL") or any(s in ("RIGHT", "FULL") for s in sides[position + 1:])


def lift_derived_expressions(select: exp.Select) -> exp.Expression | None:
    """``a JOIN (SELECT f(b.x) AS y FROM b LEFT JOIN c ON ..) AS d ON .. d.y ..`` computes ``f(d.x)`` above instead.

    A derived table holding an outer join stays opaque to the prover, so two queries that compute
    the same expression inside it and above it differ by text. Every computed output is replaced by
    the columns it reads, and each use ``d.y`` by the expression over them. Where a join of the
    reading select can NULL-extend ``d``, the expression must be NULL on the padded row
    (``null_when_inputs_null``), unless it is only read in ``d``'s own ON clause, which sees real
    rows only.
    """

    from_ = _from(select)
    if from_ is None or not select.args.get("joins"):
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    sources = _sources(select)
    aliases = [(s.alias_or_name or "").lower() for s in sources]
    if "" in aliases or len(set(aliases)) != len(aliases):
        return None
    for position, source in enumerate(sources):
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or not source.alias:
            continue
        alias_node = source.args.get("alias")
        if alias_node is not None and alias_node.args.get("columns"):
            continue
        inner = source.this
        if _inner_join_shape(inner) is None:
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        if "" in names or len(set(names)) != len(names):
            continue
        alias = aliases[position]
        derived = [s for s in sources if isinstance(s, exp.Subquery)]
        uses = []
        clean = True
        for column in select.find_all(exp.Column):
            if any(column is not s and _inside(column, s) for s in derived):
                continue
            if column.find_ancestor(exp.Select) is not select:
                if column.table.lower() == alias or (not column.table and column.name.lower() in names):
                    clean = False  # read from a nested scope
                continue
            if not column.table and column.name.lower() in names:
                clean = False  # could be d's column
            elif column.table.lower() == alias:
                uses.append(column)
        if not clean:
            continue
        own_on = sources[position].parent.args.get("on") if position > 0 else None
        padded = _padded(select, source)
        lift = {}
        for name, item in zip(names, inner.expressions):
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Column) or not _deterministic(value):
                continue
            columns = list(value.find_all(exp.Column))
            if not columns or any(isinstance(n, _SCOPED) for n in value.walk()):
                continue
            reads = [c for c in uses if c.name.lower() == name]
            if padded and not null_when_inputs_null(value) and not all(own_on is not None and _inside(c, own_on) for c in reads):
                continue
            lift[name] = value
        if not lift:
            continue
        copy = select.copy()
        copy_source = _sources(copy)[position]
        copy_inner = copy_source.this
        passthrough = {}
        for item in copy_inner.expressions:
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Column):
                passthrough.setdefault((value.table.lower(), value.name.lower()), item.alias_or_name)
        taken = set(names)
        new_items = []
        replacements = {}
        for item in copy_inner.expressions:
            name = item.alias_or_name.lower()
            if name not in lift:
                new_items.append(item)
                continue
            value = lift[name].copy()
            first = True
            for column in list(value.find_all(exp.Column)):
                key = (column.table.lower(), column.name.lower())
                if key not in passthrough:
                    # the first column takes the lifted output's name, so its position is kept
                    out = item.alias_or_name if first else _fresh(f"{name}_{column.name.lower()}", taken)
                    first = False
                    taken.add(out.lower())
                    passthrough[key] = out
                    new_items.append(exp.alias_(exp.column(column.name, table=column.table), out))
                column.replace(exp.column(passthrough[key], table=copy_source.alias))
            replacements[name] = value
        copy_inner.set("expressions", new_items)
        for column in list(copy.find_all(exp.Column)):
            if column.find_ancestor(exp.Select) is not copy or column.table.lower() != alias:
                continue
            if column.name.lower() not in replacements:
                continue
            value = replacements[column.name.lower()].copy()
            value = exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Not)) else value
            column.replace(exp.alias_(value, column.name) if column.parent is copy else value)
        if copy.sql() != select.sql():
            return copy
    return None


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _fresh(base: str, taken: set[str]) -> str:
    name, n = base, 1
    while name.lower() in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def order_derived_columns(select: exp.Select) -> exp.Expression | None:
    """List the plain columns of a derived outer join in one order: by source, then column name.

    The prover keeps such a derived table whole and matches its columns by position, so two
    spellings that list the same columns in another order would not match. The select reads
    the columns by name (no ``*``), so their order does not change its result.
    """

    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    copy = None
    for position, source in enumerate(_sources(select)):
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or not source.alias:
            continue
        alias_node = source.args.get("alias")
        if alias_node is not None and alias_node.args.get("columns"):
            continue
        inner = source.this
        aliases = _inner_join_shape(inner)
        if aliases is None:
            continue
        values = [item.this if isinstance(item, exp.Alias) else item for item in inner.expressions]
        if not all(isinstance(v, exp.Column) for v in values):
            continue
        keys = [(aliases.index(v.table.lower()), v.name.lower(), item.alias_or_name.lower()) for v, item in zip(values, inner.expressions)]
        if keys == sorted(keys):
            continue
        copy = copy or select.copy()
        target = _sources(copy)[position].this
        items = list(target.expressions)
        target.set("expressions", [item for _, item in sorted(zip(keys, items), key=lambda pair: pair[0])])
    return copy


def mirror_right_join(select: exp.Select) -> exp.Expression | None:
    """``LEFT OUTER JOIN`` is ``LEFT JOIN``, and ``a RIGHT JOIN b ON c`` (the select's only join) is ``b LEFT JOIN a ON c``.

    Most rules test a join's kind and only read LEFT joins. The select lists its columns
    (no ``*``), so the order of its two sources does not change its rows.
    """

    joins = select.args.get("joins") or []
    if not joins:
        return None
    copy = select.copy()
    changed = False
    for join in copy.args["joins"]:
        if (join.args.get("kind") or "").upper() == "OUTER" and (join.args.get("side") or "").upper() in _OUTER_SIDES:
            join.set("kind", None)
            changed = True
    join = copy.args["joins"][0]
    from_ = _from(copy)
    if len(joins) == 1 and (join.args.get("side") or "").upper() == "FULL" and not join.args.get("kind") and from_ is not None:
        from .empty_rules import is_empty

        # A FULL JOIN with a side that can never hold a row keeps only the other side's rows, padded.
        if is_empty(from_.this):
            join.set("side", "RIGHT")
            changed = True
        elif is_empty(join.this):
            join.set("side", "LEFT")
            changed = True
    if (
        len(joins) == 1
        and (join.args.get("side") or "").upper() == "RIGHT"
        and not join.args.get("kind")
        and join.args.get("on") is not None
        and join.args.get("using") is None
        and not join.args.get("method")
        and isinstance(from_.this, (exp.Table, exp.Subquery))
        and isinstance(join.this, (exp.Table, exp.Subquery))
        and not any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in copy.find_all(exp.Star))
    ):
        left, right = from_.this.copy(), join.this.copy()
        from_.set("this", right)
        join.set("this", left)
        join.set("side", "LEFT")
        changed = True
    return copy if changed else None


def _join_tree(node: exp.Expression) -> exp.Table | None:
    """The table of a parenthesized join tree ``(b JOIN c ON ..)`` (an unaliased subquery around a joined table)."""

    if isinstance(node, exp.Subquery) and not node.alias and isinstance(node.this, exp.Table) and node.this.args.get("joins"):
        return node.this
    return None


def flatten_join_tree(select: exp.Select) -> exp.Expression | None:
    """``a JOIN (b CROSS JOIN c) ON p`` is ``a CROSS JOIN b JOIN c ON p``; ``FROM (a LEFT JOIN b ON q) ..`` is ``FROM a LEFT JOIN b ON q ..``.

    A join tree in FROM is read left-deep anyway. One joined in later is flattened only when it and
    all of its joins are inner or cross joins, where an ON condition can move to the last join.
    """

    from_ = _from(select)
    if from_ is None:
        return None
    copy = None
    tree = _join_tree(from_.this)
    if tree is not None:
        copy = select.copy()
        tree = _join_tree(_from(copy).this)
        nested = tree.args["joins"]
        tree.set("joins", None)
        _from(copy).set("this", tree)
        copy.set("joins", list(nested) + list(copy.args.get("joins") or []))
        return copy
    for position, join in enumerate(select.args.get("joins") or []):
        tree = _join_tree(join.this)
        if tree is None or join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS"):
            continue
        if join.args.get("using") is not None or join.args.get("method"):
            continue
        if any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or j.args.get("using") is not None or j.args.get("method") for j in tree.args["joins"]):
            continue
        copy = select.copy()
        outer = copy.args["joins"][position]
        tree = outer.this.this
        nested = list(tree.args["joins"])
        tree.set("joins", None)
        on = outer.args.get("on")
        first = exp.Join(this=tree, kind="CROSS")
        last = nested[-1]
        if on is not None:
            own = last.args.get("on")
            last.set("on", exp.And(this=exp.Paren(this=own), expression=exp.Paren(this=on)) if own is not None else on)
            last.set("kind", None)
        joins = copy.args["joins"]
        copy.set("joins", joins[:position] + [first] + nested + joins[position + 1:])
        return copy
    return None


def _fixed_columns(source: exp.Expression, depth: int = 0) -> dict[str, exp.Expression]:
    """Output columns of a derived table that its own WHERE fixes to a number (``WHERE t.k = 10``)."""

    if depth > 8 or not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return {}
    inner = source.this
    if not _plain(inner) or inner.args.get("joins") or any(isinstance(n, (exp.AggFunc, exp.Window)) for e in inner.expressions for n in e.walk()):
        return {}
    from_ = _from(inner)
    if from_ is None or not isinstance(from_.this, (exp.Table, exp.Subquery)):
        return {}
    own = (from_.this.alias_or_name or "").lower()
    fixed: dict[str, exp.Expression] = {}
    for name, value in _fixed_columns(from_.this, depth + 1).items():
        fixed[name] = value
    where = inner.args.get("where")
    for part in _conjuncts(where.this) if where is not None else []:
        if not isinstance(part, exp.EQ):
            continue
        for column, value in ((part.this, part.expression), (part.expression, part.this)):
            if isinstance(column, exp.Column) and column.table.lower() in ("", own) and isinstance(value, exp.Literal) and not value.is_string:
                fixed[column.name.lower()] = value
    out = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if isinstance(value, exp.Column) and value.table.lower() in ("", own) and value.name.lower() in fixed:
            out[item.alias_or_name.lower()] = fixed[value.name.lower()]
    return out


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def constants_into_outer_on(select: exp.Select) -> exp.Expression | None:
    """``(SELECT .. FROM emp WHERE empno = 10) AS t LEFT JOIN dept ON t.empno = dept.deptno`` tests ``10 = dept.deptno``.

    Every real row of ``t`` has ``empno = 10``, and an ON clause only sees real rows of the sources
    joined before it, so ``t.empno`` reads as the constant there, unless an earlier join could have
    padded ``t`` with NULLs.
    """

    joins = select.args.get("joins") or []
    if not any((j.args.get("side") or "").upper() in _OUTER_SIDES for j in joins):
        return None
    sources = _sources(select)
    aliases = [(s.alias_or_name or "").lower() for s in sources]
    if "" in aliases or len(set(aliases)) != len(aliases):
        return None
    fixed = [_fixed_columns(s) for s in sources]
    if not any(fixed):
        return None
    sides = [(j.args.get("side") or "").upper() for j in joins]
    copy = None
    for index, join in enumerate(joins):
        if sides[index] not in _OUTER_SIDES or join.args.get("on") is None:
            continue
        # sources joined before this join (source i is joins[i - 1].this), still real rows here
        real = {}
        for i in range(index + 1):
            padded_by = sides[:index] if i == 0 else ([sides[i - 1]] if sides[i - 1] in ("LEFT", "FULL") else []) + [s for s in sides[i:index] if s in ("RIGHT", "FULL")]
            if i == 0:
                padded_by = [s for s in padded_by if s in ("RIGHT", "FULL")]
            if not padded_by and fixed[i]:
                real[aliases[i]] = fixed[i]
        if not real:
            continue
        target = (copy or select).args["joins"][index].args["on"]
        columns = [c for c in target.find_all(exp.Column) if c.table.lower() in real and c.name.lower() in real[c.table.lower()] and isinstance(c.parent, exp.EQ)]
        columns = [c for c in columns if c.find_ancestor(exp.Select) is (copy or select)]
        if not columns:
            continue
        copy = copy or select.copy()
        target = copy.args["joins"][index].args["on"]
        for column in list(target.find_all(exp.Column)):
            table = column.table.lower()
            if table in real and column.name.lower() in real[table] and isinstance(column.parent, exp.EQ) and column.find_ancestor(exp.Select) is copy:
                column.replace(real[table][column.name.lower()].copy())
    return copy


_REJECTING = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)


def left_join_rejected_by_where(select: exp.Select) -> exp.Expression | None:
    """``a LEFT JOIN b ON c WHERE b.x = a.y`` is ``a JOIN b ON c WHERE b.x = a.y``.

    The padded rows carry NULL for every column of ``b``, and a comparison of such a column (or
    ``b.x IS NOT NULL``) rejects them, so only matched rows reach the result. Read only for a chain
    of inner and left joins: a later RIGHT or FULL join would decide its own padding from the rows
    the left join keeps.
    """

    joins = select.args.get("joins") or []
    where = select.args.get("where")
    if not joins or where is None:
        return None
    sides = [(j.args.get("side") or "").upper() for j in joins]
    kinds = [(j.args.get("kind") or "").upper() for j in joins]
    if any(side not in ("", "LEFT") for side in sides) or "LEFT" not in sides:
        return None
    if any(kind not in ("", "INNER", "CROSS", "OUTER") for kind in kinds):
        return None
    rejected = set()
    for part in _conjuncts(where.this):
        if isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null):
            columns = [part.this.this]
        elif isinstance(part, _REJECTING):
            columns = [part.this, part.expression]
        else:
            continue
        for column in columns:
            if isinstance(column, exp.Column) and column.table and not isinstance(column.this, exp.Star):
                rejected.add(column.table.lower())
    copy = None
    for index, join in enumerate(joins):
        alias = (join.this.alias_or_name or "").lower()
        if sides[index] != "LEFT" or not alias or alias not in rejected or join.args.get("using") is not None:
            continue
        if sum(1 for s in _sources(select) if (s.alias_or_name or "").lower() == alias) != 1:
            continue
        copy = copy or select.copy()
        target = copy.args["joins"][index]
        target.set("side", None)
        target.set("kind", None)
    return copy
