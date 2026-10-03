"""A canonical text for a SELECT, so copies that differ only in clothing share a fingerprint.

Every step preserves the query's meaning, so two SELECTs with the same canonical text are the same
query:

* source aliases are renamed by position (``o`` and ``base_t`` both become ``_a1``), with every
  qualified column that points at them, correlated references from nested queries included;
* ``AND`` and ``OR`` chains are flattened and sorted;
* a comparison reads ``column OP constant`` (``5 < x`` becomes ``x > 5``); between two columns ``a < b`` becomes
  ``b > a`` and ``=`` / ``<>`` operands are sorted;
* ``IN`` lists are sorted (a list is a set);
* ``x BETWEEN a AND b`` is ``x >= a AND x <= b``, ``x = a OR x = b`` is ``x IN (a, b)``, ``IFNULL`` is ``COALESCE``;
* ``INNER JOIN`` is ``JOIN`` and ``LEFT OUTER JOIN`` is ``LEFT JOIN``.

Output column names, literals, join kinds and everything else stay as written. A SELECT whose
sources are not plain tables or aliased subqueries keeps its own names, so nothing is renamed
that could not be followed.
"""

from __future__ import annotations

import hashlib

from sqlglot import exp

_FLIPPED = {exp.LT: exp.GT, exp.LTE: exp.GTE}
_SYMMETRIC = (exp.EQ, exp.NEQ)


def canonical_copy(select: exp.Expression) -> exp.Expression:
    """A canonicalized copy of ``select``; the original is untouched."""

    copy = select.copy()
    counter = [0]
    _rename(copy, [], counter)
    return _normalize(copy)


# ------------------------------------------------------------------- aliases


def _sources(select: exp.Select) -> list[exp.Expression] | None:
    nodes = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        nodes.append(from_.this)
    for join in select.args.get("joins") or ():
        nodes.append(join.this)
    for node in nodes:
        if isinstance(node, exp.Table):
            if node.args.get("alias") is not None and node.args["alias"].args.get("columns"):
                return None
        elif isinstance(node, exp.Subquery):
            if not node.alias:
                return None
        else:
            return None
    return nodes


def _declared(select: exp.Select) -> list[str]:
    """The lower-case names ``select``'s FROM and JOIN items declare, whatever kind of source they are."""

    from_ = select.args.get("from_") or select.args.get("from")
    nodes = ([from_.this] if from_ is not None else []) + [join.this for join in select.args.get("joins") or ()]
    names = []
    for node in nodes:
        if node.alias_or_name:
            names.append(node.alias_or_name.lower())
        alias = node.args.get("alias")
        if isinstance(node, exp.Unnest) and alias is not None and alias.args.get("columns"):
            names.append(alias.args["columns"][0].name.lower())  # BigQuery: the element is a range variable
    return names


def _rename(node: exp.Expression, stack: list[dict[str, str | None]], counter: list[int]) -> None:
    """Rename aliases in ``node`` and below, resolving each qualifier to its nearest declaring SELECT."""

    renamed: dict[str, str] = {}
    sources = _sources(node) if isinstance(node, exp.Select) else None
    if sources is not None:
        names = [source.alias_or_name for source in sources]
        # Two sources under one name cannot be told apart; leave this SELECT as written.
        if len({name.lower() for name in names}) == len(names):
            for source, name in zip(sources, names):
                counter[0] += 1
                renamed[name.lower()] = f"_a{counter[0]}"
    # Every name this SELECT declares hides an outer alias of that name, renamed here or not (an
    # UNNEST, a table function), so a qualifier that names it never resolves further out.
    mapping: dict[str, str | None] = dict.fromkeys(_declared(node)) if isinstance(node, exp.Select) else {}
    mapping.update(renamed)
    inner = [*stack, mapping] if mapping else stack
    for column in _own_columns(node):
        qualifier = column.args.get("table")
        if qualifier is None:
            continue
        for scope in reversed(inner):
            if qualifier.name.lower() in scope:
                if scope[qualifier.name.lower()] is not None:
                    column.set("table", exp.to_identifier(scope[qualifier.name.lower()]))
                break
    for source in sources or ():
        if source.alias_or_name.lower() in renamed:
            source.set("alias", exp.TableAlias(this=exp.to_identifier(renamed[source.alias_or_name.lower()])))
    for child in _child_queries(node):
        _rename(child, inner, counter)


def _own_columns(node: exp.Expression):
    """Columns that belong to ``node``'s own level, not to a SELECT nested inside it."""

    if not isinstance(node, exp.Select):
        return
    stack = [node]
    while stack:
        current = stack.pop()
        for value in current.args.values():
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, exp.Expression) or isinstance(child, exp.Query):
                    continue
                if isinstance(child, exp.Column):
                    yield child
                stack.append(child)


def _child_queries(node: exp.Expression):
    stack = [node]
    while stack:
        current = stack.pop()
        for value in current.args.values():
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, exp.Expression):
                    continue
                if isinstance(child, exp.Query) and not isinstance(child, exp.Subquery):
                    yield child
                else:
                    stack.append(child)


# --------------------------------------------------------------- expressions


def _text(node: exp.Expression) -> str:
    return node.sql(dialect="bigquery", normalize=True, normalize_functions="upper", comments=False)


def _chain(node: exp.Expression, kind: type) -> list[exp.Expression]:
    if isinstance(node, exp.Paren) and isinstance(node.this, kind):
        return _chain(node.this, kind)
    if isinstance(node, kind):
        return [*_chain(node.left, kind), *_chain(node.right, kind)]
    return [node]


def _constant(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Literal, exp.Boolean, exp.Null)) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal)
    )


_MIRROR = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}


def _group_equalities(parts: list[exp.Expression]) -> list[exp.Expression]:
    """``x = 1 OR x = 2`` is ``x IN (1, 2)``: equalities of one column with constants join into one IN."""

    groups: dict[str, list[exp.EQ]] = {}
    rest: list[exp.Expression] = []
    for part in parts:
        if isinstance(part, exp.EQ) and isinstance(part.left, exp.Column) and _constant(part.right):
            groups.setdefault(_text(part.left), []).append(part)
        else:
            rest.append(part)
    for members in groups.values():
        if len(members) == 1:
            rest.append(members[0])
        else:
            rest.append(exp.In(this=members[0].left.copy(), expressions=sorted((m.right.copy() for m in members), key=_text)))
    return rest


def _normalize(root: exp.Expression) -> exp.Expression:
    """Comparisons read ``column OP constant`` where one side is constant; otherwise ``a > b`` / ``a >= b`` / sorted ``=``."""

    def rewrite(node: exp.Expression) -> exp.Expression | None:
        if isinstance(node, tuple(_MIRROR)):
            left, right = node.left, node.right
            if _constant(left) and not _constant(right):
                swap = True
            elif _constant(right) and not _constant(left):
                swap = False
            elif isinstance(node, tuple(_FLIPPED)):
                swap = True
            elif isinstance(node, _SYMMETRIC):
                swap = _text(left) > _text(right)
            else:
                swap = False
            if not swap:
                return None
            return _MIRROR[type(node)](this=right.copy(), expression=left.copy())
        if isinstance(node, exp.Join):
            kind = (node.args.get("kind") or "").upper()
            if kind == "INNER" or (kind == "OUTER" and node.args.get("side")):
                node.set("kind", None)  # INNER JOIN is JOIN; LEFT OUTER JOIN is LEFT JOIN
            return None
        if isinstance(node, exp.In) and node.args.get("expressions") and not node.args.get("query"):
            node.set("expressions", sorted(node.expressions, key=_text))
            return None
        if (
            isinstance(node, exp.Paren)
            and isinstance(node.parent, (exp.And, exp.Or, exp.Where, exp.Having))
            and isinstance(node.this, (exp.In, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is))
        ):
            return node.this.copy()  # parentheses around one predicate group nothing
        if isinstance(node, exp.Coalesce):
            node.set("is_nvl", None)  # IFNULL(a, b) is COALESCE(a, b)
            node.set("is_null", None)
            node.meta["name"] = "COALESCE"  # the spelling the parser kept
            return None
        if isinstance(node, exp.Between) and isinstance(node.this, exp.Column) and not node.args.get("symmetric"):
            return exp.and_(
                exp.GTE(this=node.this.copy(), expression=node.args["low"].copy()),
                exp.LTE(this=node.this.copy(), expression=node.args["high"].copy()),
                copy=False,
            )
        if isinstance(node, exp.In) and len(node.expressions or ()) == 1 and not node.args.get("query") \
                and isinstance(node.this, exp.Column) and not node.args.get("unnest"):
            return exp.EQ(this=node.this.copy(), expression=node.expressions[0].copy())
        if isinstance(node, (exp.And, exp.Or)):
            kind = type(node)
            parts = [p.copy() for p in _chain(node, kind)]
            if kind is exp.Or:
                parts = _group_equalities(parts)
            parts = sorted(parts, key=_text)
            result = parts[0]
            for part in parts[1:]:
                result = kind(this=result, expression=part)
            return result
        return None

    for node in reversed(list(root.walk())):  # children before parents
        new = rewrite(node)
        if new is not None and node.parent is not None:
            node.replace(new)
    return root


# ---------------------------------------------------------- names from outside


def _with_of(node: exp.Expression) -> exp.With | None:
    return node.args.get("with_") or node.args.get("with")


def visible_ctes(select: exp.Expression) -> dict[str, exp.Expression]:
    """The CTE bodies a SELECT can read by name, nearest definition first."""

    visible: dict[str, exp.Expression] = {}
    child = select
    parent = select.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            with_ = parent.parent
            if isinstance(with_, exp.With):
                for cte in with_.expressions:
                    if cte is parent and not with_.args.get("recursive"):
                        break
                    visible.setdefault(cte.alias_or_name, cte.this)
        else:
            with_ = _with_of(parent)
            if with_ is not None and child is not with_:
                for cte in with_.expressions:
                    visible.setdefault(cte.alias_or_name, cte.this)
        child, parent = parent, parent.parent
    return visible


def free_cte_refs(select: exp.Expression) -> dict[str, exp.Expression]:
    """CTEs defined outside ``select`` that it reads, by name."""

    outside = visible_ctes(select)
    if not outside:
        return {}
    inside = {cte.alias_or_name for w in select.find_all(exp.With) for cte in w.expressions}
    found = {}
    for table in select.find_all(exp.Table):
        name = table.name
        if table.args.get("db") or table.args.get("catalog") or name in inside:
            continue
        if name in outside:
            found[name] = outside[name]
    return found


_SCOPE_KEYS: dict[tuple[int, str], tuple[exp.Expression, str]] = {}
_SCOPE_KEYS_MAX = 50_000


def _cte_part(body: exp.Expression, text, seen: frozenset) -> str:
    """One outside CTE rendered canonically with what it reads in turn, as a digest.

    Cached per body: a model with a long chain of CTEs asks for the same bodies once per SELECT, which
    otherwise renders every upstream CTE again for each one.
    """

    key = (id(body), getattr(text, "__qualname__", ""))
    hit = _SCOPE_KEYS.get(key)
    if hit is not None and hit[0] is body:
        return hit[1]
    try:
        body_text = text(canonical_copy(body)) if isinstance(body, exp.Select) else text(body)
    except Exception:
        body_text = text(body)
    part = hashlib.sha1((body_text + scope_key(body, text, seen | {id(body)})).encode("utf-8")).hexdigest()[:16]
    if len(_SCOPE_KEYS) >= _SCOPE_KEYS_MAX:
        _SCOPE_KEYS.clear()
    _SCOPE_KEYS[key] = (body, part)
    return part


def scope_key(select: exp.Expression, text, _seen: frozenset = frozenset()) -> str:
    """What the outside CTEs a SELECT reads are made of, so equal text over different CTEs differs."""

    refs = free_cte_refs(select)
    if not refs:
        return ""
    parts = []
    for name in sorted(refs):
        body = refs[name]
        if id(body) in _seen:
            parts.append(f"{name}=<recursive>")
            continue
        parts.append(f"{name}={_cte_part(body, text, _seen)}")
    return "|ctes:" + ";".join(parts)


def source_names(select: exp.Expression) -> list[str] | None:
    """The names of a SELECT's FROM/JOIN sources in order, or ``None`` when they cannot be followed."""

    if not isinstance(select, exp.Select):
        return None
    sources = _sources(select)
    if sources is None:
        return None
    names = [source.alias_or_name for source in sources]
    return names if len(set(names)) == len(names) else None


def rename_sources(canonical: exp.Expression, names: list[str] | None) -> exp.Expression:
    """A canonical copy with its top-level sources renamed to ``names`` by position (``_a1`` becomes ``names[0]``).

    Lets variants be compared and merged in one SELECT's own aliases. Unchanged when the counts differ.
    """

    sources = _sources(canonical) if isinstance(canonical, exp.Select) else None
    if not names or sources is None or len(sources) != len(names):
        return canonical
    mapping = {source.alias_or_name: name for source, name in zip(sources, names)}
    for column in canonical.find_all(exp.Column):
        if column.table in mapping:
            column.set("table", exp.to_identifier(mapping[column.table]))
    for source in sources:
        source.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[source.alias_or_name])))
    return canonical
