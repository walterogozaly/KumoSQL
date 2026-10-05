"""Apply joins (``JOIN LATERAL``) read as plain joins, and a few EXISTS identities around them.

CockroachDB's ``inner-join-apply`` is SQL's ``CROSS JOIN LATERAL``. The prover cannot compare a query that
keeps a LATERAL join, so these rules take the lateral away whenever nothing correlated is left in it:

1. A pinned outer reference. ``FROM o CROSS JOIN LATERAL (SELECT i.k AS c, i.k + o.k AS d FROM i) AS l
   WHERE l.c = o.k`` keeps only lateral rows whose ``c`` equals ``o.k``, so ``o.k`` read inside the lateral
   is ``i.k``: ``(SELECT i.k AS c, i.k + i.k AS d FROM i)``. Nothing correlated is left, and the LATERAL
   keyword goes. The equality may sit in the ``WHERE`` of the select or in the ``ON`` of the (inner) lateral
   join itself, and the lateral may be a ``UNION`` whose every branch outputs a column of its own.
2. A lateral over a row without a source. ``CROSS JOIN LATERAL (SELECT o.a AS x, o.a + 1 AS y)`` is a
   projection of the outer row: ``l.x`` is ``o.a`` and ``l.y`` is ``o.a + 1`` wherever they are read.
   Over ``UNION ALL`` of such rows each branch is its own copy of the query (a join distributes over
   ``UNION ALL``).
3. ``EXISTS (SELECT 1)`` over a select with no source is TRUE.
4. ``EXISTS (SELECT .. FROM (SELECT .. FROM t WHERE p) AS d WHERE q)`` is ``EXISTS (SELECT 1 FROM t WHERE p
   AND q)``: only whether some row exists matters, so the derived table can be read through.
5. A range test carried across an equality. In ``WHERE o.k > 5 AND NOT EXISTS (SELECT .. FROM s WHERE
   s.k = o.k)`` a row of ``s`` that matches has ``s.k = o.k`` and so ``s.k > 5``: the test may be stated inside.

Why the pin of rule 1 is sound. A row of the lateral that the filter keeps has ``l.c = o.k`` TRUE, so both
sides are non-NULL and equal; wherever such a row reads ``o.k`` it reads the value ``i.k`` holds. A row the
filter drops stays dropped, because the output ``c`` is untouched (the pinned expression must not read the
outer row). Only integer-typed pairs qualify (equal there means identical, unlike ``'1' = 1`` or ``2.50 = 2.5``).
The substitution never reaches into a derived table of the lateral (it cannot see its siblings), and it
refuses outer joins that could change which rows a changed ``ON`` keeps (see ``_replaceable``).
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts
from .cast_rules import expression_type

_INNER = ("", "INNER", "CROSS")
_BLOCKING = ("group", "having", "qualify", "windows", "limit", "offset", "order", "with", "with_", "laterals", "pivots", "connect", "match")
_COMPARISONS = {exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}


def apply_join_rules(select: exp.Select, types: dict | None) -> exp.Select | None:
    """The one entry the normalizer calls: each rule below returns a rewritten copy or None."""

    return (
        pin_lateral_outputs(select, types)
        or lateral_projection(select)
        or tidy_over_lateral(select)
        or unwrap_lateral_passthrough(select)
        or read_correlated_derived(select)
        or flatten_lateral(select)
        or lateral_union_projection(select)
        or exists_over_constant_row(select)
        or exists_read_derived(select)
        or carry_range_into_exists(select, types)
        or name_laterals(select)
    )


# --- shared helpers -------------------------------------------------------------------------------


def _from(select: exp.Select):
    return select.args.get("from_") or select.args.get("from")


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = _from(select)
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def _alias(source: exp.Expression) -> str:
    return (source.alias_or_name or "").lower()


def _strip(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, (exp.Subquery, exp.Paren)) and node.this is not None and not node.args.get("alias"):
        node = node.this
    return node


def _branches(node: exp.Expression | None) -> list[exp.Select] | None:
    """The selects of a (possibly nested) UNION, or None when anything else is in the way."""

    node = _strip(node)
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.Union):
        if node.args.get("order") or node.args.get("limit") or node.args.get("offset") or node.args.get("with_") or node.args.get("with"):
            return None
        left, right = _branches(node.this), _branches(node.expression)
        return left + right if left is not None and right is not None else None
    return None


def _plain_lateral(join: exp.Join):
    """``(lateral, alias)`` for ``[CROSS|INNER] JOIN LATERAL (subquery) AS alias``, else None."""

    lateral = join.this
    if not isinstance(lateral, exp.Lateral) or lateral.args.get("view") or lateral.args.get("outer"):
        return None
    if not isinstance(lateral.this, exp.Subquery) or join.args.get("side") or (join.args.get("kind") or "").upper() not in _INNER:
        return None
    alias = lateral.args.get("alias")
    if alias is None or alias.args.get("columns") or not alias.name or join.args.get("using") is not None:
        return None
    return lateral, alias.name.lower()


def _scope_aliases(select: exp.Select) -> set[str]:
    return {_alias(s) for s in _sources(select)} - {""}


def _free_columns(body: exp.Expression) -> list[exp.Column] | None:
    """Columns of ``body`` whose qualifier no select inside ``body`` declares; None when one has no qualifier."""

    found = []
    for column in body.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            return None
        table = column.table.lower()
        if not table:
            return None
        node = column.parent
        bound = False
        while node is not None:
            if isinstance(node, exp.Select) and table in _scope_aliases(node):
                bound = True
                break
            if node is body:
                break
            node = node.parent
        if not bound:
            found.append(column)
    return found


def _unaliased(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _clean_outputs(branch: exp.Select) -> list[str] | None:
    names = []
    for item in branch.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        if not item.alias_or_name:
            return None
        names.append(item.alias_or_name.lower())
    return names if len(set(names)) == len(names) else None


def _has_outer_join(select: exp.Select, kinds=("LEFT", "RIGHT", "FULL")) -> bool:
    return any((j.args.get("side") or "").upper() in kinds for j in select.args.get("joins") or [])


# --- rule 1: a pinned outer reference ------------------------------------------------------------


def _pins(select: exp.Select, join: exp.Join, alias: str, earlier: set[str]) -> list[tuple[str, exp.Column]]:
    """``(lateral output name, outer column)`` for the equalities that hold on every row the select keeps."""

    parts: list[exp.Expression] = []
    where = select.args.get("where")
    if where is not None:
        parts += conjuncts(where.this)
    on = join.args.get("on")
    if on is not None and (join.args.get("kind") or "").upper() == "INNER":
        parts += conjuncts(on)
    found = []
    for part in parts:
        if not isinstance(part, exp.EQ):
            continue
        for lat, out in ((part.this, part.expression), (part.expression, part.this)):
            if (
                isinstance(lat, exp.Column)
                and isinstance(out, exp.Column)
                and isinstance(lat.this, exp.Identifier)
                and isinstance(out.this, exp.Identifier)
                and lat.table.lower() == alias
                and out.table.lower() in earlier
            ):
                found.append((lat.name.lower(), out))
                break
    return found


def _crossing(column: exp.Column, scope: exp.Select) -> exp.Select | None:
    """The outermost derived table of ``scope`` that ``column`` sits in (None when it sits in ``scope`` itself)."""

    found = None
    node = column.parent
    while node is not None and node is not scope:
        if isinstance(node, exp.Select) and _is_source_select(node):
            found = node
        node = node.parent
    return found


def _is_source_select(select: exp.Select) -> bool:
    """A select that is a FROM or JOIN item (derived table or lateral), which cannot see its siblings."""

    parent = select.parent
    if isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.Subquery, exp.Exists, exp.In, exp.Union)):
        parent = parent.parent
        return isinstance(parent, (exp.From, exp.Join, exp.Lateral))
    return False


def _shadows(column: exp.Column, scope: exp.Select, needed: set[str]) -> bool:
    node = column.parent
    while node is not None and node is not scope:
        if isinstance(node, exp.Select) and _scope_aliases(node) & needed:
            return True
        node = node.parent
    return False


def _on_allows(column: exp.Column, scope: exp.Select, needed: set[str]) -> bool:
    """An ON clause of an outer join decides which rows are padded: the value may only read sources that
    come before that join, none of them null-extended by it."""

    node = column.parent
    while node is not None and node is not scope:
        if isinstance(node, exp.Join) and node.parent is scope and any(a is node.args.get("on") for a in _chain(column, node)):
            side = (node.args.get("side") or "").upper()
            if side in ("RIGHT", "FULL"):
                return False
            if side == "LEFT":
                joins = scope.args["joins"]
                preceding = {_alias(s) for s in [_from(scope).this] + [j.this for j in joins[: joins.index(node)]]}
                if _alias(node.this) in needed or not needed <= preceding:
                    return False
        node = node.parent
    return True


def _chain(node: exp.Expression, stop: exp.Expression):
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _plain_scope(scope: exp.Select) -> bool:
    return (
        not any(scope.args.get(k) for k in _BLOCKING)
        and scope.find(exp.AggFunc) is None
        and scope.find(exp.Window) is None
        and _from(scope) is not None
        and not any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in scope.args.get("joins") or [])
    )


def _pin_into_scope(scope: exp.Select, value: exp.Expression, owner: str, name: str, depth: int = 0) -> bool | None:
    """Replace the free ``owner.name`` in ``scope`` by ``value`` (an expression over the sources of ``scope``).

    Returns True when something changed, False when ``scope`` never reads the column, None when it must be left alone
    (a read where ``value`` cannot be written, or a shape the argument does not cover).
    """

    if depth > 6 or not _plain_scope(scope):
        return None
    free = _free_columns(scope)
    if free is None:
        return None
    uses = [c for c in free if c.table.lower() == owner and c.name.lower() == name]
    if not uses:
        return False
    reads = list(value.find_all(exp.Column))
    if not reads or any(not c.table or isinstance(c.this, exp.Star) for c in reads):
        return None
    needed = {c.table.lower() for c in reads}
    if not needed <= _scope_aliases(scope):
        return None
    direct, nested = [], {}
    for column in uses:
        derived = _crossing(column, scope)
        if derived is None:
            direct.append(column)
        else:
            nested.setdefault(id(derived), (derived, []))[1].append(column)
    for column in direct:
        if _shadows(column, scope, needed) or not _on_allows(column, scope, needed):
            return None
    plans = []
    for derived, _ in nested.values():
        # the value must be a plain read of one output of that derived table, which then pins its own source
        if not (isinstance(value, exp.Column) and isinstance(derived.parent, exp.Subquery) and _alias(derived.parent) == value.table.lower()):
            return None
        names = _clean_outputs(derived)
        if names is None or value.name.lower() not in names:
            return None
        source = derived.parent.parent
        join = source if isinstance(source, exp.Join) else None
        if join is not None and (join.args.get("side") or "").upper() in ("RIGHT", "FULL"):
            return None
        if join is not None and (join.args.get("side") or "").upper() == "LEFT":
            return None  # rows of a padded side: the value reaches the derived table only through its own ON
        plans.append((derived, _unaliased(derived.expressions[names.index(value.name.lower())])))
    results = []
    for derived, inner_value in plans:
        if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select, exp.Anonymous, exp.Rand)) for n in inner_value.walk()):
            return None
        done = _pin_into_scope(derived, inner_value, owner, name, depth + 1)
        if done is None:
            return None
        results.append(done)
    for column in direct:
        column.replace(value.copy())
    return bool(direct) or any(results)


def _pin_into_branch(branch: exp.Select, position: int, owner: str, name: str, outer_type, types) -> bool | None:
    """Replace the free ``owner.name`` in ``branch`` by its output at ``position``; None when it must be left alone."""

    names = _clean_outputs(branch)
    if names is None or position >= len(names) or not _plain_scope(branch):
        return None
    value = _unaliased(branch.expressions[position])
    if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select, exp.Anonymous, exp.Rand)) for n in value.walk()):
        return None
    reads = list(value.find_all(exp.Column))
    if not reads or any(not c.table or isinstance(c.this, exp.Star) for c in reads):
        return None
    if any(c.table.lower() not in _scope_aliases(branch) for c in reads):  # reads the outer row: not a pin
        return None
    inner_type = expression_type(value, branch, types)
    if not inner_type or inner_type[0] != "int" or not outer_type or outer_type[0] != "int":
        return None
    return _pin_into_scope(branch, value, owner, name)


def pin_lateral_outputs(select: exp.Select, types: dict | None) -> exp.Select | None:
    if not types or not (select.args.get("joins")):
        return None
    for index, join in enumerate(select.args["joins"]):
        info = _plain_lateral(join)
        if info is None:
            continue
        lateral, alias = info
        sources = _sources(select)
        earlier = {_alias(s) for s in [_from(select).this] + [j.this for j in select.args["joins"][:index]]} if _from(select) is not None else set()
        counts = [_alias(s) for s in sources]
        if counts.count(alias) != 1:
            continue
        for out_name, outer in _pins(select, join, alias, earlier):
            if counts.count(outer.table.lower()) != 1:
                continue
            copy = select.copy()
            new_join = copy.args["joins"][index]
            new_lateral = new_join.this
            branches = _branches(new_lateral.this)
            outer_type = expression_type(outer, copy, types)
            if not branches or len({id(b) for b in branches}) != len(branches):
                continue
            changed, ok = False, True
            for branch in branches:
                names = _clean_outputs(branch)
                if names is None or out_name not in names:
                    ok = False
                    break
                done = _pin_into_branch(branch, names.index(out_name), outer.table.lower(), outer.name.lower(), outer_type, types)
                if done is None:
                    ok = False
                    break
                changed = changed or done
            if not ok or not changed:
                continue
            _drop_lateral_if_closed(new_join)
            return copy
    return None


def _drop_lateral_if_closed(join: exp.Join) -> None:
    lateral = join.this
    if not isinstance(lateral, exp.Lateral):
        return
    free = _free_columns(lateral.this)
    if free is None or free:
        return
    join.set("this", exp.Subquery(this=lateral.this.this, alias=lateral.args["alias"].copy()))


# --- rule 2: a lateral over a row without a source ----------------------------------------------------


def _row_branch(branch: exp.Select) -> bool:
    return (
        _from(branch) is None
        and not any(branch.args.get(k) for k in _BLOCKING + ("where", "distinct", "joins"))
        and _clean_outputs(branch) is not None
        and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select, exp.AggFunc, exp.Window, exp.Anonymous, exp.Rand, exp.Star)) for item in branch.expressions for n in _unaliased(item).walk())
        and all(c.table for item in branch.expressions for c in _unaliased(item).find_all(exp.Column))
    )


def _other_joins_inner(select: exp.Select, join: exp.Join) -> bool:
    return all(not j.args.get("side") and (j.args.get("kind") or "").upper() in _INNER for j in select.args.get("joins") or [] if j is not join)


def _reads_alias(select: exp.Select, alias: str):
    """Columns of ``select`` that read ``alias``, or None when a nested select redeclares it or a bare column could."""

    reads = []
    for column in select.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        if column.table.lower() == alias:
            reads.append(column)
    for nested in select.find_all(exp.Select):
        if nested is not select and alias in _scope_aliases(nested):
            return None
    return reads


def _replace_reads(select: exp.Select, alias: str, values: dict[str, exp.Expression]) -> bool:
    """Write ``values[name]`` for every read of ``alias.name`` in ``select``; False when that is not safe."""

    reads = _reads_alias(select, alias)
    if reads is None or any(c.name.lower() not in values for c in reads):
        return False
    if any(not c.table and not isinstance(c.this, exp.Star) for c in select.find_all(exp.Column)):
        return False  # a bare name might be one of its columns
    for column in reads:
        replacement = values[column.name.lower()].copy()
        if isinstance(column.parent, exp.Select) and column.arg_key == "expressions":
            replacement = exp.alias_(replacement, column.name)  # the output keeps its name
        column.replace(replacement)
    return True


def _only_lateral(select: exp.Select, alias: str) -> bool:
    return [_alias(s) for s in _sources(select)].count(alias) == 1


def lateral_projection(select: exp.Select) -> exp.Select | None:
    for join in select.args.get("joins") or []:
        info = _plain_lateral(join)
        if info is None:
            continue
        lateral, alias = info
        body = _strip(lateral.this)
        on = join.args.get("on")
        if on is not None and not (isinstance(on, exp.Boolean) and on.this):
            continue
        if not isinstance(body, exp.Select) or not _row_branch(body) or not _other_joins_inner(select, join) or not _only_lateral(select, alias):
            continue
        if any(select.args.get(k) for k in ("group", "having", "qualify", "windows")):
            continue
        names = _clean_outputs(body)
        copy = select.copy()
        if not _replace_reads(copy, alias, {n: _unaliased(e) for n, e in zip(names, body.expressions)}):
            continue
        copy.set("joins", [j for j in copy.args["joins"] if _plain_lateral(j) is None or _plain_lateral(j)[1] != alias] or None)
        return copy
    return None


def flatten_lateral(select: exp.Select) -> exp.Select | None:
    """``o JOIN LATERAL (SELECT .. FROM a [LEFT] JOIN c ON p WHERE q)`` is ``o JOIN a ON q [LEFT] JOIN c ON p``.

    The body keeps one output row per row of its own join, so its sources can join the enclosing query directly
    (``q`` becomes the ON of ``a``, which is where it sits relative to the left joins: it reads ``a`` and the
    outer row only). The body must be a plain select-project-join: no grouping, DISTINCT, LIMIT or windows, and no
    RIGHT or FULL join.
    """

    for join in select.args.get("joins") or []:
        info = _plain_lateral(join)
        if info is None:
            continue
        lateral, alias = info
        body = _strip(lateral.this)
        on = join.args.get("on")
        if on is not None and not (isinstance(on, exp.Boolean) and on.this):
            continue
        if not isinstance(body, exp.Select) or not _plain_scope(body) or body.args.get("distinct") or not _only_lateral(select, alias):
            continue
        if any(select.args.get(k) for k in ("group", "having", "qualify", "windows")) and False:
            continue
        names = _clean_outputs(body)
        if names is None:
            continue
        inner_joins = body.args.get("joins") or []
        if any(j.args.get("using") is not None or j.args.get("method") or isinstance(j.this, exp.Lateral) for j in inner_joins):
            continue
        sources = _sources(body)
        if any(not (isinstance(s, (exp.Table, exp.Subquery)) and _alias(s)) for s in sources) or any(isinstance(s, exp.Table) and (s.args.get("pivots") or s.args.get("joins")) for s in sources):
            continue
        if any(isinstance(s, exp.Subquery) and _free_columns(s.this) != [] for s in sources):
            continue  # a derived table that reads the outer row can only stay inside the lateral
        inner_aliases = [_alias(s) for s in sources]
        if len(set(inner_aliases)) != len(inner_aliases) or set(inner_aliases) & (_scope_aliases(select) | {alias}):
            continue
        # no other select of the query may use these names either (a nested read of an outer name could be captured)
        if any(set(inner_aliases) & _scope_aliases(n) for n in select.find_all(exp.Select) if n is not select and not _inside(n, lateral)):
            continue
        if any(not c.table for c in body.find_all(exp.Column) if not isinstance(c.this, exp.Star)):
            continue
        where = body.args.get("where")
        outside_the_first = set(inner_aliases[1:])
        if where is not None and inner_joins and any(c.table.lower() in outside_the_first for c in where.find_all(exp.Column)):
            continue
        if where is not None and any((j.args.get("side") or "").upper() == "LEFT" for j in inner_joins) is False:
            pass
        copy = select.copy()
        new_join = next(j for j in copy.args["joins"] if isinstance(j.this, exp.Lateral) and (_plain_lateral(j) or (None, ""))[1] == alias)
        new_body = _strip(new_join.this.this)
        values = {n: _unaliased(e) for n, e in zip(names, new_body.expressions)}
        if not _replace_reads(copy, alias, values):
            continue
        first = _from(new_body).this
        where = new_body.args.get("where")
        has_left = any((j.args.get("side") or "").upper() == "LEFT" for j in new_body.args.get("joins") or [])
        position = copy.args["joins"].index(new_join)
        pieces = []
        if where is not None and has_left:
            pieces.append(exp.Join(this=first, kind="INNER", on=where.this.copy()))
        else:
            pieces.append(exp.Join(this=first, kind="CROSS" if where is None else "INNER", **({"on": exp.true()} if where is not None and False else {})))
            if where is not None:
                copy_where = copy.args.get("where")
                joined = where.this.copy()
                copy.set("where", exp.Where(this=exp.And(this=copy_where.this.copy(), expression=joined) if copy_where is not None else joined))
        pieces += [j.copy() for j in new_body.args.get("joins") or []]
        copy.set("joins", copy.args["joins"][:position] + pieces + copy.args["joins"][position + 1 :])
        return copy
    return None


def unwrap_lateral_passthrough(select: exp.Select) -> exp.Select | None:
    """``LATERAL (SELECT d.a AS x, d.b AS y FROM (SELECT .. ) AS d)`` is the inner select with those outputs."""

    for index, join in enumerate(select.args.get("joins") or []):
        info = _plain_lateral(join)
        body = _strip(join.this.this) if info is not None else None
        if not isinstance(body, exp.Select) or any(body.args.get(k) for k in _BLOCKING + ("where", "distinct", "joins")):
            continue
        from_ = _from(body)
        if from_ is None or not isinstance(from_.this, exp.Subquery) or not isinstance(from_.this.this, exp.Select) or not _alias(from_.this):
            continue
        inner = from_.this.this
        if any(inner.args.get(k) for k in _BLOCKING + ("distinct",)) or inner.find(exp.AggFunc) is not None or inner.find(exp.Window) is not None:
            continue
        inner_names = _clean_outputs(inner)
        outer_names = _clean_outputs(body)
        if inner_names is None or outer_names is None:
            continue
        alias = _alias(from_.this)
        picks = [_unaliased(e) for e in body.expressions]
        if any(not (isinstance(c, exp.Column) and c.table.lower() == alias and c.name.lower() in inner_names) for c in picks):
            continue
        copy = select.copy()
        target = copy.args["joins"][index]
        new_inner = _strip(target.this.this).args["from_" if "from_" in _strip(target.this.this).args else "from"].this.this
        values = {n: _unaliased(e) for n, e in zip(inner_names, new_inner.expressions)}
        new_inner.set("expressions", [exp.alias_(values[c.name.lower()].copy(), name) for c, name in zip(picks, outer_names)])
        target.this.set("this", exp.Subquery(this=new_inner))
        return copy
    return None


def _has_lateral(select: exp.Select) -> bool:
    return any(isinstance(j.this, exp.Lateral) for j in select.args.get("joins") or [])


def tidy_over_lateral(select: exp.Select) -> exp.Select | None:
    """Drop a ``WHERE TRUE`` and a pure pass-through wrapper around a select that keeps a lateral join.

    The other rules read such wrappers through, but not past a lateral; two queries that differ only in
    these layers are the same query.
    """

    where = select.args.get("where")
    if _has_lateral(select) and where is not None and isinstance(where.this, exp.Boolean) and where.this.this:
        copy = select.copy()
        copy.set("where", None)
        return copy
    from_ = _from(select)
    if from_ is None or not isinstance(from_.this, exp.Subquery) or not isinstance(from_.this.this, exp.Select):
        return None
    inner = from_.this.this
    if (
        not _has_lateral(inner)
        or select.args.get("joins")
        or any(select.args.get(k) for k in _BLOCKING + ("distinct",))
        or (where is not None and not (isinstance(where.this, exp.Boolean) and where.this.this))
        or not _plain_scope(inner)
        or inner.args.get("distinct")
    ):
        return None
    inner_names = _clean_outputs(inner)
    outer_names = _clean_outputs(select)
    alias = _alias(from_.this)
    if inner_names is None or outer_names is None or not alias:
        return None
    picks = [_unaliased(e) for e in select.expressions]
    if any(not (isinstance(c, exp.Column) and c.table.lower() == alias and c.name.lower() in inner_names) for c in picks):
        return None
    copy = select.copy()
    new_inner = _from(copy).this.this
    values = {n: _unaliased(e) for n, e in zip(inner_names, new_inner.expressions)}
    new_inner.set("expressions", [exp.alias_(values[c.name.lower()].copy(), name) for c, name in zip(picks, outer_names)])
    return new_inner


def name_laterals(select: exp.Select) -> exp.Select | None:
    """Give a lateral that stays in the query a name that depends on where it sits, not on how it was spelled."""

    depth, up = 0, select.parent
    while up is not None:
        depth += isinstance(up, exp.Select)
        up = up.parent
    ordinal = 0
    for index, join in enumerate(select.args.get("joins") or []):
        info = _plain_lateral(join)
        if info is None:
            continue
        alias = info[1]
        wanted = f"kumosql_lat{depth}_{ordinal}"
        ordinal += 1
        if alias == wanted:
            continue
        if wanted in _scope_aliases(select) or not _only_lateral(select, alias):
            return None
        reads = _reads_alias(select, alias)
        if reads is None:
            return None
        copy = select.copy()
        for column in _reads_alias(copy, alias) or []:
            column.set("table", exp.to_identifier(wanted))
        copy.args["joins"][index].this.args["alias"].set("this", exp.to_identifier(wanted))
        return copy
    return None


def read_correlated_derived(select: exp.Select) -> exp.Select | None:
    """Read a correlated derived table of one table through: ``(SELECT t.a AS x FROM t WHERE t.b = o.b) AS d``.

    A derived table that reads an outer row sits inside a lateral. Its filter moves to the select that reads it
    (the WHERE for the first source, the ON clause for a joined one, which is where an outer join keeps it) and
    ``d.x`` becomes ``t.a``.
    """

    from_ = _from(select)
    if from_ is None or not _plain_scope(select) and select.args.get("where") is None and not select.args.get("joins"):
        pass
    items = [(from_.this, None)] + [(j.this, j) for j, _ in zip(select.args.get("joins") or [], range(10**6))] if from_ is not None else []
    for source, join in items:
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or not _alias(source):
            continue
        inner = source.this
        if any(inner.args.get(k) for k in _BLOCKING + ("distinct", "joins")) or inner.find(exp.AggFunc) is not None or inner.find(exp.Window) is not None:
            continue
        table = _from(inner).this if _from(inner) is not None else None
        if not isinstance(table, exp.Table) or not _alias(table) or table.args.get("joins") or table.args.get("pivots"):
            continue
        free = _free_columns(inner)
        if not free:  # None (unknown) or nothing correlated
            continue
        side = (join.args.get("side") or "").upper() if join is not None else ""
        if side in ("RIGHT", "FULL") or any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in select.args.get("joins") or []):
            continue
        names = _clean_outputs(inner)
        if names is None or _alias(table) in (_scope_aliases(select) - {_alias(source)}):
            continue
        if any(not c.table for c in inner.find_all(exp.Column) if not isinstance(c.this, exp.Star)):
            continue
        copy = select.copy()
        position = items.index((source, join))
        new_source = [(_from(copy).this, None)] + [(j.this, j) for j in copy.args.get("joins") or []]
        new_source, new_join = new_source[position]
        new_inner = new_source.this
        values = {n: _unaliased(e) for n, e in zip(names, new_inner.expressions)}
        if not _replace_reads(copy, _alias(source), values):
            continue
        condition = new_inner.args["where"].this.copy() if new_inner.args.get("where") is not None else None
        table_copy = _from(new_inner).this.copy()
        if new_join is None:
            _from(copy).set("this", table_copy)
            if condition is not None:
                old = copy.args.get("where")
                copy.set("where", exp.Where(this=exp.And(this=old.this.copy(), expression=condition) if old is not None else condition))
        else:
            new_join.set("this", table_copy)
            if condition is not None:
                on = new_join.args.get("on")
                if on is None or (isinstance(on, exp.Boolean) and on.this):
                    new_join.set("on", condition)
                else:
                    new_join.set("on", exp.And(this=on.copy(), expression=condition))
                if (new_join.args.get("kind") or "").upper() == "CROSS":
                    new_join.set("kind", "INNER")
        return copy
    return None


def lateral_union_projection(select: exp.Select) -> exp.Select | exp.Union | None:
    """``o JOIN LATERAL (row UNION ALL row)`` is the union of ``o JOIN LATERAL row`` for each row."""

    if select.parent is None and False:
        return None
    for join in select.args.get("joins") or []:
        info = _plain_lateral(join)
        if info is None:
            continue
        lateral, alias = info
        body = _strip(lateral.this)
        if not isinstance(body, exp.Union) or body.args.get("distinct") or not isinstance(body, exp.Union):
            continue
        branches = _branches(body)
        if not branches or len(branches) < 2 or not all(_row_branch(b) for b in branches):
            continue
        if any(not isinstance(n, exp.Union) or n.args.get("distinct") for n in body.find_all(exp.SetOperation)):
            continue
        if not _other_joins_inner(select, join) or any(select.args.get(k) for k in _BLOCKING + ("distinct",)) or select.find(exp.AggFunc) is not None:
            continue
        if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in select.walk() if n is not select and n.find_ancestor(exp.Select) is select and n is not lateral.this and not _inside(n, lateral.this)):
            continue
        names = {tuple(_clean_outputs(b) or ()) for b in branches}
        if len(names) != 1 or () in names:
            continue
        copies = []
        for branch in branches:
            copy = select.copy()
            target = next(j for j in copy.args["joins"] if isinstance(j.this, exp.Lateral) and (_plain_lateral(j) or (None, ""))[1] == alias)
            target.this.set("this", exp.Subquery(this=branch.copy()))
            copies.append(copy)
        union = copies[0]
        for other in copies[1:]:
            union = exp.Union(this=union, expression=other, distinct=False)
        return exp.Subquery(this=union) if select.parent is not None and not isinstance(select.parent, (exp.Subquery,)) else union
    return None


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


# --- rules 3 to 5: EXISTS ---------------------------------------------------------------------------------


def _exists_nodes(select: exp.Select):
    return [e for e in select.find_all(exp.Exists) if e.find_ancestor(exp.Select) is select]


def _constant_row(body: exp.Expression | None) -> bool:
    """A select that always yields exactly one row: no source, no filter, no grouping, nothing nested."""

    return (
        isinstance(body, exp.Select)
        and _from(body) is None
        and not any(body.args.get(k) for k in _BLOCKING + ("where", "distinct", "joins"))
        and not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Exists)) for e in body.expressions for n in e.walk())
    )


def exists_over_constant_row(select: exp.Select) -> exp.Select | None:
    """``EXISTS (SELECT expr)`` with no source, filter, grouping or limit is TRUE (the one row is always there)."""

    if not any(_constant_row(_strip(e.this)) for e in _exists_nodes(select)):
        return None
    copy = select.copy()
    for node in _exists_nodes(copy):
        if _constant_row(_strip(node.this)):
            node.replace(exp.true())
    return copy


def exists_read_derived(select: exp.Select) -> exp.Select | None:
    """Read a filter-and-project derived table through, inside an EXISTS test."""

    for node in _exists_nodes(select):
        body = _strip(node.this)
        if not isinstance(body, exp.Select) or any(body.args.get(k) for k in _BLOCKING + ("distinct", "joins")):
            continue
        from_ = _from(body)
        if from_ is None or not isinstance(from_.this, exp.Subquery) or not from_.this.alias:
            continue
        inner = from_.this.this
        if (
            not isinstance(inner, exp.Select)
            or any(inner.args.get(k) for k in _BLOCKING + ("distinct",))
            or inner.find(exp.AggFunc) is not None
            or inner.find(exp.Window) is not None
            or _from(inner) is None
        ):
            continue
        names = _clean_outputs(inner)
        if names is None:
            continue
        alias = from_.this.alias.lower()
        inner_aliases = _scope_aliases(inner)
        if len(inner_aliases) != len(_sources(inner)):
            continue
        # the aliases it brings into the test must not capture a name used further out
        outside = set()
        scope = node.find_ancestor(exp.Select)
        while scope is not None:
            outside |= _scope_aliases(scope)
            scope = scope.find_ancestor(exp.Select)
        if inner_aliases & (outside | {alias}):
            continue
        if any(isinstance(n, exp.Select) and n is not inner and _scope_aliases(n) & inner_aliases for n in body.walk()):
            continue
        values = {n: _unaliased(e) for n, e in zip(names, inner.expressions)}
        reads = [c for c in body.find_all(exp.Column) if c.table.lower() == alias]
        if any(not c.table for c in body.find_all(exp.Column)) or any(c.name.lower() not in values for c in reads):
            continue
        if any(isinstance(n, exp.Select) and n is not body and _scope_aliases(n) & {alias} for n in body.walk()):
            continue
        copy = select.copy()
        target = [e for e in _exists_nodes(copy)][[e for e in _exists_nodes(select)].index(node)]
        new_body = _strip(target.this)
        new_from = _from(new_body)
        new_inner = new_from.this.this
        new_values = {n: _unaliased(e) for n, e in zip(_clean_outputs(new_inner), new_inner.expressions)}
        for column in [c for c in new_body.find_all(exp.Column) if c.table.lower() == alias]:
            column.replace(new_values[column.name.lower()].copy())
        conditions = []
        if new_inner.args.get("where") is not None:
            conditions.append(new_inner.args["where"].this.copy())
        if new_body.args.get("where") is not None:
            conditions.append(new_body.args["where"].this.copy())
        flat = exp.Select(expressions=[exp.Literal.number(1)])
        flat.set("from_" if "from_" in new_body.args or "from_" in exp.Select.arg_types else "from", new_inner.args.get("from_") or new_inner.args.get("from"))
        if new_inner.args.get("joins"):
            flat.set("joins", new_inner.args["joins"])
        if conditions:
            where = conditions[0]
            for more in conditions[1:]:
                where = exp.And(this=where, expression=more)
            flat.set("where", exp.Where(this=where))
        target.set("this", flat)
        return copy
    return None


def _int_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.is_int


def carry_range_into_exists(select: exp.Select, types: dict | None) -> exp.Select | None:
    """``o.k > 5 AND EXISTS (.. s.k = o.k ..)`` states ``s.k > 5`` inside the test as well."""

    where = select.args.get("where")
    if where is None or not types:
        return None
    parts = conjuncts(where.this)
    ranges = []
    for part in parts:
        if type(part) in _COMPARISONS and isinstance(part.this, exp.Column) and _int_literal(part.expression) and part.this.table:
            ranges.append((part.this, part))
    if not ranges:
        return None
    for node in _exists_nodes(select):
        body = _strip(node.this)
        if not isinstance(body, exp.Select) or any(body.args.get(k) for k in _BLOCKING + ("distinct",)) or body.args.get("where") is None:
            continue
        if _from(body) is None or body.args.get("joins") or not isinstance(_from(body).this, exp.Table):
            continue
        source = _alias(_from(body).this)
        inner_parts = conjuncts(body.args["where"].this)
        sources_here = _scope_aliases(select)
        for column, comparison in ranges:
            owner = column.table.lower()
            if owner not in sources_here or [_alias(s) for s in _sources(select)].count(owner) != 1 or owner == source:
                continue
            for part in inner_parts:
                if not isinstance(part, exp.EQ):
                    continue
                for inside, outside in ((part.this, part.expression), (part.expression, part.this)):
                    if not (isinstance(inside, exp.Column) and isinstance(outside, exp.Column) and inside.table.lower() == source and outside.table.lower() == owner and outside.name.lower() == column.name.lower()):
                        continue
                    wanted = type(comparison)(this=inside.copy(), expression=comparison.expression.copy())
                    if any(p.sql() == wanted.sql() for p in inner_parts):
                        continue
                    if (expression_type(column, select, types) or ("",))[0] != "int" or (expression_type(inside, body, types) or ("",))[0] != "int":
                        continue
                    if any(c.table.lower() == owner for c in body.find_all(exp.Select) if False):
                        continue
                    if any(owner in _scope_aliases(n) for n in body.find_all(exp.Select)):
                        continue
                    copy = select.copy()
                    target = _exists_nodes(copy)[_exists_nodes(select).index(node)]
                    new_body = _strip(target.this)
                    new_where = new_body.args["where"]
                    new_where.set("this", exp.And(this=new_where.this.copy(), expression=wanted.copy()))
                    return copy
    return None
