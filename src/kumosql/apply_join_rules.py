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
        or lateral_union_projection(select)
        or exists_over_constant_row(select)
        or exists_read_derived(select)
        or carry_range_into_exists(select, types)
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


def _visible(column: exp.Column, branch: exp.Select, needed: set[str]) -> bool:
    """Can an expression over the ``needed`` aliases of ``branch`` be written where ``column`` stands?"""

    node = column.parent
    while node is not None and node is not branch:
        if isinstance(node, exp.Select):
            parent = node.parent
            # a derived table or a nested lateral cannot see the siblings of the select around it
            if isinstance(parent, (exp.From, exp.Join, exp.Lateral)) or (isinstance(parent, exp.Subquery) and isinstance(parent.parent, (exp.From, exp.Join, exp.Lateral))):
                return False
            if _scope_aliases(node) & needed:
                return False
        node = node.parent
    return node is branch


def _replaceable(column: exp.Column, branch: exp.Select, value: exp.Expression, needed: set[str]) -> bool:
    if not _visible(column, branch, needed):
        return False
    # An ON clause of an outer join decides which rows are padded: the value may only read sources that
    # come before that join, none of them null-extended by it.
    join = column.find_ancestor(exp.Join)
    if join is not None and join.find_ancestor(exp.Select) is branch:
        on = join.args.get("on")
        if on is not None and any(a is on for a in _chain(column, join)):
            side = (join.args.get("side") or "").upper()
            if side in ("RIGHT", "FULL"):
                return False
            if side == "LEFT":
                if _alias(join.this) in needed:
                    return False
                joins = branch.args["joins"]
                preceding = {_alias(s) for s in [(_from(branch)).this] + [j.this for j in joins[: joins.index(join)]]}
                if not needed <= preceding:
                    return False
    return True


def _chain(node: exp.Expression, stop: exp.Expression):
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _pin_into_branch(branch: exp.Select, position: int, owner: str, name: str, outer_type, types) -> bool | None:
    """Replace the free ``owner.name`` in ``branch`` by its output at ``position``; None when it must be left alone."""

    if any(branch.args.get(k) for k in _BLOCKING) or branch.find(exp.AggFunc) is not None or branch.find(exp.Window) is not None:
        return None
    if _from(branch) is None or any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in branch.args.get("joins") or []):
        return None
    names = _clean_outputs(branch)
    if names is None or position >= len(names):
        return None
    value = _unaliased(branch.expressions[position])
    if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select, exp.Anonymous, exp.Rand)) for n in value.walk()):
        return None
    reads = [c for c in value.find_all(exp.Column)]
    if not reads or any(not c.table or isinstance(c.this, exp.Star) for c in reads):
        return None
    if any(c.table.lower() not in _scope_aliases(branch) for c in reads):  # reads the outer row: not a pin
        return None
    inner_type = expression_type(value, branch, types)
    if not inner_type or inner_type[0] != "int" or not outer_type or outer_type[0] != "int":
        return None
    needed = {c.table.lower() for c in reads}
    uses = [c for c in (_free_columns(branch) or []) if c.table.lower() == owner and c.name.lower() == name]
    if _free_columns(branch) is None:
        return None
    if not uses:
        return False
    if any(not _replaceable(c, branch, value, needed) for c in uses):
        return None
    for column in uses:
        column.replace(value.copy())
    return True


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


def lateral_projection(select: exp.Select) -> exp.Select | None:
    for index, join in enumerate(select.args.get("joins") or []):
        info = _plain_lateral(join)
        if info is None:
            continue
        lateral, alias = info
        body = _strip(lateral.this)
        on = join.args.get("on")
        if on is not None and not (isinstance(on, exp.Boolean) and on.this):
            continue
        if not isinstance(body, exp.Select) or not _row_branch(body) or not _other_joins_inner(select, join):
            continue
        if select.args.get("laterals") or any(select.args.get(k) for k in ("group", "having", "qualify", "windows")):
            continue
        names = _clean_outputs(body)
        values = {n: _unaliased(e) for n, e in zip(names, body.expressions)}
        reads = _reads_alias(select, alias)
        if reads is None or [_alias(s) for s in _sources(select)].count(alias) != 1:
            continue
        if any(c.name.lower() not in values for c in reads):
            continue
        bare = [c for c in select.find_all(exp.Column) if not c.table and not isinstance(c.this, exp.Star)]
        if bare:
            continue
        copy = select.copy()
        copy_reads = _reads_alias(copy, alias)
        for column in copy_reads or []:
            replacement = values[column.name.lower()].copy()
            if column.parent is copy and isinstance(column, exp.Column) and column.arg_key == "expressions":
                replacement = exp.alias_(replacement, column.name)
            elif isinstance(column.parent, exp.Select) and column.arg_key == "expressions":
                replacement = exp.alias_(replacement, column.name)
            column.replace(replacement)
        copy.set("joins", [j for j in copy.args["joins"] if _plain_lateral(j) is None or _plain_lateral(j)[1] != alias] or None)
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
