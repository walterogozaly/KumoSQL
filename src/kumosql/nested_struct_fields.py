"""Reading one field of a STRUCT that the query itself constructs, for ``algebraic_equivalence.normalize``.

``STRUCT(a AS x, b AS y).x`` is just ``a``; the struct is built and taken apart in the same expression. The same
holds for the field of a ``SELECT AS STRUCT`` scalar subquery (``(SELECT AS STRUCT a AS x, b AS y FROM t WHERE w).x``
is ``(SELECT a FROM t WHERE w)``) and for the field of a struct column a derived table builds
(``SELECT d.s.f FROM (SELECT STRUCT(a AS f) AS s FROM t) AS d`` reads ``a`` through a new column of ``d``). Folding
them lets the prover see the plain query underneath. BigQuery field names are case-insensitive, so ``.Y`` finds ``y``.

Also here: ``(s).f`` and ``s.f`` are the same field read of a stored struct column, so the parenthesized spelling is
written as the plain path.

What is deliberately **not** done, because it is wrong or needs more than the query shows:

* A struct with an unnamed or repeated field name is left alone (BigQuery would not resolve the field either, or it
  would pick one of two).
* Nothing is concluded about ``s IS NULL``: a struct can be NULL while its fields are NULL, and a struct that is not
  NULL can hold only NULL fields, so ``s IS NULL`` is never read as ``s.a IS NULL AND s.b IS NULL``. A constructed
  struct is not rebuilt from its fields either (``STRUCT(s.a AS a, s.b AS b)`` is not NULL when ``s`` is).
* Whole-struct equality is positional and a NULL field makes it NULL; it is not touched.
* A field is not read out of a struct whose other fields hold an aggregate, a window function or a subquery (dropping
  them could change how many rows the query returns, or whether it errors), nor out of a ``SELECT AS STRUCT`` with
  ``DISTINCT`` (the duplicates it removes depend on the dropped fields).

The rewrite assumes, like the rest of the prover, that the dropped fields raise no runtime error.
"""

from __future__ import annotations

from sqlglot import exp

_MAX_ROUNDS = 32
_CLAUSES = ("group", "having", "qualify", "order", "where", "windows")


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


_ATOMIC = (exp.Column, exp.Literal, exp.Paren, exp.Func, exp.Case, exp.Null, exp.Boolean, exp.Subquery)


def _wrapped(value: exp.Expression) -> exp.Expression:
    """A copy of ``value`` safe to put where a field read stood, inside any operator."""

    return value.copy() if isinstance(value, _ATOMIC) else exp.paren(value.copy())


def _nondeterministic(value: exp.Expression) -> bool:
    from .equivalence import _VALUE_NONDETERMINISTIC_NAMES, _VALUE_NONDETERMINISTIC_TYPES

    for node in value.walk():
        if type(node).__name__ in _VALUE_NONDETERMINISTIC_TYPES or type(node).__name__.startswith("Approx"):
            return True
        if isinstance(node, exp.Anonymous) and str(node.this).upper() in _VALUE_NONDETERMINISTIC_NAMES:
            return True
    return False


def _named_fields(items: list[exp.Expression]) -> dict[str, exp.Expression] | None:
    """``{lowercase field name: value}`` when every item is ``value AS name`` with distinct names, else ``None``."""

    fields: dict[str, exp.Expression] = {}
    for item in items:
        if isinstance(item, exp.PropertyEQ):
            name, value = item.this, item.expression
            if not isinstance(name, exp.Identifier):
                return None
        elif isinstance(item, exp.Alias):
            name, value = item.args.get("alias"), item.this
            if not isinstance(name, exp.Identifier):
                return None
        else:
            return None  # an unnamed field (or a bare column, whose implicit name is not relied on)
        key = name.name.lower()
        if not key or key in fields or isinstance(value, exp.Star):
            return None
        fields[key] = value
    return fields


def _droppable(values: list[exp.Expression]) -> bool:
    """Whether dropping ``values`` cannot change the number of rows the query returns."""

    return not any(
        v.find(exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Star, exp.Lateral, exp.Unnest) is not None for v in values
    )


def _field_name(node: exp.Dot) -> str | None:
    field = node.expression
    return field.name.lower() if isinstance(field, exp.Identifier) and field.name else None


def _constructed(node: exp.Struct) -> dict[str, exp.Expression] | None:
    if node.args.get("this") is not None:  # a typed constructor
        return None
    return _named_fields(list(node.expressions))


def _fold_constructor(node: exp.Dot, struct: exp.Struct) -> exp.Expression | None:
    fields = _constructed(struct)
    name = _field_name(node)
    if fields is None or name is None or name not in fields:
        return None
    if not _droppable([v for key, v in fields.items() if key != name]):
        return None
    return _wrapped(fields[name])


def _struct_select_items(select: exp.Select) -> list[exp.Expression] | None:
    """The fields ``SELECT AS STRUCT ...`` / ``SELECT AS VALUE STRUCT(...)`` builds, as select items."""

    kind = select.args.get("kind")
    kind = kind.upper() if isinstance(kind, str) else kind
    if kind == "STRUCT":
        return list(select.expressions)
    if kind == "VALUE" and len(select.expressions) == 1:
        only = _unparen(select.expressions[0])
        if isinstance(only, exp.Struct) and only.args.get("this") is None:
            return list(only.expressions)
    return None


def _references(select: exp.Select, names: set[str]) -> bool:
    """Whether a clause of ``select`` may read one of the select-list ``names`` (or a column position)."""

    for key in _CLAUSES:
        clause = select.args.get(key)
        if clause is None:
            continue
        for column in clause.find_all(exp.Column):
            if not column.table and column.name.lower() in names:
                return True
        if key in ("group", "order") and clause.find(exp.Literal) is not None:
            return True  # a position, or an expression this check does not read
    return False


def _plain(select: exp.Select) -> bool:
    return not any(
        select.args.get(key) for key in ("distinct", "limit", "offset", "group", "having", "qualify", "order", "windows", "where", "joins", "laterals", "from_", "from", "with_", "with")
    )


def _fold_subquery(node: exp.Dot, subquery: exp.Subquery) -> exp.Expression | None:
    """``(SELECT AS STRUCT .. AS f ..).f`` as the scalar subquery that selects just that item."""

    select = subquery.this
    if not isinstance(select, exp.Select) or subquery.args.get("alias") or select.args.get("distinct"):
        return None
    items = _struct_select_items(select)
    name = _field_name(node)
    if items is None or name is None:
        return None
    fields = _named_fields(items)
    if fields is None or name not in fields:
        return None
    dropped = {key for key in fields if key != name}
    if not _droppable([fields[key] for key in dropped]) or _references(select, dropped):
        return None
    kept = fields[name].copy()
    if _plain(select) and _droppable([kept]):
        return _wrapped(kept)  # a FROM-less SELECT is its one value
    narrowed = select.copy()
    narrowed.set("kind", None)
    # the clauses keep reading the field under its name when they read it at all
    narrowed.set("expressions", [exp.Alias(this=kept, alias=exp.to_identifier(name)) if _references(select, {name}) else kept])
    return exp.Subquery(this=narrowed)


def _column_path(node: exp.Dot) -> exp.Column | None:
    """``(a.s).f`` / ``(s).f`` as the column path ``a.s.f`` / ``s.f``."""

    inner = _unparen(node.this)
    name = node.expression
    if not isinstance(inner, exp.Column) or not isinstance(name, exp.Identifier) or isinstance(inner.this, exp.Star):
        return None
    if inner.args.get("catalog") is not None:
        return None
    parts = [inner.args.get(key) for key in ("catalog", "db", "table", "this")]
    parts = [p for p in parts if p is not None] + [name.copy()]
    column = exp.Column(this=parts[-1])
    for key, part in zip(("table", "db", "catalog"), reversed(parts[:-1])):
        column.set(key, part.copy())
    return column


def _alias_names(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for node in tree.find_all(exp.Table, exp.Subquery, exp.Unnest, exp.CTE):
        alias = node.args.get("alias")
        if alias is not None and alias.name:
            names.add(alias.name.lower())
        if isinstance(node, exp.Table) and node.name:
            names.add(node.name.lower())
    return names


def _fold_dots(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    changed = False
    aliases = _alias_names(tree)
    for node in list(tree.find_all(exp.Dot))[::-1]:  # innermost first
        if node.parent is None and node is not tree:
            continue  # already replaced as part of an enclosing rewrite
        inner = _unparen(node.this)
        replacement: exp.Expression | None = None
        if isinstance(inner, exp.Struct):
            replacement = _fold_constructor(node, inner)
        elif isinstance(inner, exp.Subquery):
            replacement = _fold_subquery(node, inner)
        elif isinstance(inner, exp.Column) and isinstance(node.this, exp.Paren):
            # an unqualified ``(s).f`` may be a field of a table's whole row when ``s`` is a table alias
            if inner.table or inner.name.lower() not in aliases:
                replacement = _column_path(node)
        if replacement is None:
            continue
        if node is tree:
            return replacement, True
        node.replace(replacement)
        changed = True
    return tree, changed


def _owning_select(column: exp.Column, alias: str) -> exp.Select | None:
    """The nearest enclosing select whose FROM or JOIN source is named ``alias``."""

    scope = column.find_ancestor(exp.Select)
    while scope is not None:
        sources = []
        from_ = scope.args.get("from_") or scope.args.get("from")
        if from_ is not None:
            sources.append(from_.this)
        sources += [j.this for j in scope.args.get("joins") or []]
        for source in sources:
            if source.alias and source.alias.lower() == alias:
                return scope if isinstance(source, exp.Subquery) else None
        if any(isinstance(s, (exp.Table, exp.Unnest)) and (s.alias_or_name or "").lower() == alias for s in sources):
            return None
        scope = scope.find_ancestor(exp.Select)
    return None


def _derived(scope: exp.Select, alias: str) -> exp.Subquery:
    sources = []
    from_ = scope.args.get("from_") or scope.args.get("from")
    if from_ is not None:
        sources.append(from_.this)
    sources += [j.this for j in scope.args.get("joins") or []]
    return next(s for s in sources if s.alias and s.alias.lower() == alias)


def _reads_whole_rows(scope: exp.Select, alias: str) -> bool:
    """Whether ``scope`` (or a subquery in it) reads ``alias`` as a whole: a star, or the bare alias as a value."""

    for node in scope.find_all(exp.Star):
        parent = node.parent
        if isinstance(parent, exp.Column):
            if parent.table.lower() == alias:
                return True  # ``alias.*`` lists the columns this rewrite adds or renames
        elif not isinstance(parent, exp.Count) and node.find_ancestor(exp.Select) is scope:
            return True  # a bare ``*`` too
    return any(column.name.lower() == alias and not column.table for column in scope.find_all(exp.Column))


def _fresh(existing: set[str], base: str) -> str:
    name, n = base, 1
    while name in existing:
        n += 1
        name = f"{base}_{n}"
    return name


def _output_names(select: exp.Select) -> set[str] | None:
    names = set()
    for item in select.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        if item.alias_or_name:
            names.add(item.alias_or_name.lower())
    return names


def _fold_derived(tree: exp.Expression) -> bool:
    """``d.s.f`` over a derived table whose column ``s`` is ``STRUCT(.. AS f ..)`` as a new column of ``d``."""

    for column in list(tree.find_all(exp.Column)):
        db, struct_name, field = column.args.get("db"), column.args.get("table"), column.this
        if db is None or column.args.get("catalog") is not None or not isinstance(field, exp.Identifier):
            continue
        alias = db.name.lower()
        scope = _owning_select(column, alias)
        if scope is None:
            continue
        derived = _derived(scope, alias)
        select = derived.this
        if not isinstance(select, exp.Select) or select.args.get("kind") or select.args.get("distinct"):
            continue
        names = _output_names(select)
        if names is None or _reads_whole_rows(scope, alias):
            continue
        matches = [i for i in select.expressions if (i.alias_or_name or "").lower() == struct_name.name.lower()]
        if len(matches) != 1 or not isinstance(matches[0], exp.Alias):
            continue
        struct = _unparen(matches[0].this)
        if not isinstance(struct, exp.Struct):
            continue
        fields = _constructed(struct)
        key = field.name.lower()
        if fields is None or key not in fields or _nondeterministic(fields[key]):
            continue
        added = _fresh(names, f"{struct_name.name.lower()}__{key}")
        select.append("expressions", exp.Alias(this=fields[key].copy(), alias=exp.to_identifier(added)))
        column.replace(exp.Column(this=exp.to_identifier(added), table=exp.to_identifier(alias)))
        return True
    return False


def _fold_value_tables(tree: exp.Expression) -> bool:
    """``(SELECT AS STRUCT a AS x ..) AS d`` read only as ``d.x`` is the ordinary derived table ``(SELECT a AS x ..) AS d``."""

    for subquery in list(tree.find_all(exp.Subquery)):
        select = subquery.this
        alias = subquery.alias.lower() if subquery.alias else ""
        if not alias or not isinstance(select, exp.Select) or not isinstance(subquery.parent, (exp.From, exp.Join)):
            continue
        items = _struct_select_items(select)
        if items is None:
            continue
        fields = _named_fields(items)
        scope = subquery.find_ancestor(exp.Select)
        if fields is None or scope is None or _reads_whole_rows(scope, alias):
            continue
        reads = [c for c in scope.find_all(exp.Column) if c.table.lower() == alias]
        if any(c.args.get("db") is not None or c.name.lower() not in fields for c in reads):
            continue
        if any(not c.table and c.name.lower() in fields for c in scope.find_all(exp.Column)):
            continue  # an unqualified read could belong to another source
        select.set("kind", None)
        select.set("expressions", [exp.Alias(this=v.copy(), alias=exp.to_identifier(select_name)) for select_name, v in _spell(items)])
        return True
    return False


def _spell(items: list[exp.Expression]):
    for item in items:
        if isinstance(item, exp.PropertyEQ):
            yield item.this.name, item.expression
        else:
            yield item.alias, item.this


def fold_struct_fields(tree: exp.Expression) -> exp.Expression:
    """``tree`` with the reads of constructed STRUCT fields folded (module docstring)."""

    for _ in range(_MAX_ROUNDS):
        tree, changed = _fold_dots(tree)
        changed = _fold_derived(tree) or changed
        changed = _fold_value_tables(tree) or changed
        if not changed:
            break
    return tree
