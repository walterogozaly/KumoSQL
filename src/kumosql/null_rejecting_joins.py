"""Turn a LEFT JOIN into an inner join when the WHERE clause rejects its null-extended rows.

``a LEFT JOIN b ON c WHERE b.x = 'S8'`` is ``a JOIN b ON c WHERE b.x = 'S8'``: the rows of ``a``
with no match carry NULL for every column of ``b``, and a comparison of such a column is never
TRUE, so WHERE drops exactly the rows the outer join adds. The same holds one level down, when
the outer join sits in a derived table that only projects and filters and the WHERE that reads
it rejects an output computed from ``b``'s columns: removing the null-extended rows inside only
removes rows whose output is NULL there, and those never survive the outer WHERE.

A condition rejects a table when it cannot be TRUE once every column of that table is NULL:
a comparison, ``BETWEEN``, ``IN (list)``, ``LIKE`` or ``IS NOT NULL`` whose operand is a column of
the table or NULL-propagating arithmetic on one, an AND with one such part, or an OR whose every
part rejects it. ``<=>``, ``COALESCE``, ``IS NULL``, ``NOT IN`` and subqueries never count.
Only chains of inner, cross and LEFT joins are read; any RIGHT, FULL, semi, anti, NATURAL or
USING join leaves the query as it is.
"""

from __future__ import annotations

from sqlglot import exp

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)
# Operations whose result is NULL as soon as one operand is NULL.
_STRICT_BINARY = (exp.Add, exp.Sub, exp.Mul, exp.Div)
_STRICT_UNARY = (exp.Paren, exp.Neg, exp.Cast, exp.TryCast, exp.Upper, exp.Lower, exp.Abs)
_OPAQUE = "__kumosql_outer__"


def _strict_tables(node: exp.Expression) -> set[str]:
    """Tables any of whose columns being NULL makes ``node`` NULL."""

    if isinstance(node, exp.Column):
        if isinstance(node.this, exp.Star) or not node.table:
            return set()
        return {node.table.lower()}
    if isinstance(node, _STRICT_BINARY):
        return _strict_tables(node.this) | _strict_tables(node.expression)
    if isinstance(node, _STRICT_UNARY):
        if isinstance(node, (exp.Cast, exp.TryCast)) and node.args.get("format") is not None:
            return set()
        return _strict_tables(node.this)
    return set()


def rejected_tables(condition: exp.Expression) -> set[str]:
    """Tables whose columns all being NULL makes ``condition`` NULL or FALSE (never TRUE)."""

    if isinstance(condition, exp.Paren):
        return rejected_tables(condition.this)
    if isinstance(condition, exp.And):
        return rejected_tables(condition.this) | rejected_tables(condition.expression)
    if isinstance(condition, exp.Or):
        return rejected_tables(condition.this) & rejected_tables(condition.expression)
    if isinstance(condition, exp.Not):
        inner = condition.this.this if isinstance(condition.this, exp.Paren) else condition.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return _strict_tables(inner.this)
        return set()
    if isinstance(condition, _COMPARISONS):
        if condition.args.get("escape") is not None:
            return set()
        return _strict_tables(condition.this) | _strict_tables(condition.expression)
    if isinstance(condition, exp.Between):
        return _strict_tables(condition.this) | _strict_tables(condition.args["low"]) | _strict_tables(condition.args["high"])
    if isinstance(condition, exp.In) and condition.expressions and not condition.args.get("query") and not condition.args.get("unnest") and not condition.args.get("field"):
        return _strict_tables(condition.this)
    return set()


def _source_name(source: exp.Expression) -> str:
    if isinstance(source, exp.Table):
        return (source.alias_or_name or "").lower()
    if isinstance(source, exp.Subquery) and source.alias:
        return source.alias.lower()
    return ""


def _sources(select: exp.Select) -> list[tuple[exp.Expression, exp.Join | None]] | None:
    """``(source, join)`` for FROM and each join, or None when the chain is not inner/cross/LEFT only."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("laterals"):
        return None
    sources: list[tuple[exp.Expression, exp.Join | None]] = [(from_.this, None)]
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if join.args.get("method") or join.args.get("using"):
            return None
        if side not in ("", "LEFT") or kind not in ("", "INNER", "CROSS", "OUTER"):
            return None
        if side == "LEFT" and join.args.get("on") is None:
            return None
        sources.append((join.this, join))
    names = [_source_name(s) for s, _ in sources]
    if "" in names or len(set(names)) != len(names):
        return None
    return sources


def _make_inner(join: exp.Join) -> None:
    join.set("side", None)
    join.set("kind", None)


def _convert(select: exp.Select, rejected: set[str]) -> bool:
    """Make every LEFT join of ``select`` whose far side is in ``rejected`` an inner join (in place)."""

    sources = _sources(select)
    if sources is None:
        return False
    changed = False
    for source, join in sources:
        if join is not None and (join.args.get("side") or "").upper() == "LEFT" and _source_name(source) in rejected:
            _make_inner(join)
            changed = True
    return changed


def _plain_derived(inner: exp.Expression) -> bool:
    """A derived select that only joins, filters, projects and optionally deduplicates."""

    if not isinstance(inner, exp.Select):
        return False
    if any(inner.args.get(k) for k in ("group", "having", "limit", "offset", "qualify", "windows", "with_", "with", "order")):
        return False
    distinct = inner.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    for item in inner.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return False
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in item.walk()):
            return False
    return True


def _through_derived(part: exp.Expression, alias: str, inner: exp.Select, only_source: bool) -> set[str]:
    """Tables of ``inner`` that ``part`` rejects, reading ``alias.c`` as the inner output ``c``.

    When the derived table is the select's only source, a bare ``c`` that it outputs is read the
    same way (the innermost scope that has the name wins).
    """

    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in outputs:
            return set()
        outputs[name] = item.this if isinstance(item, exp.Alias) else item
    holder = exp.Paren(this=part.copy())
    for column in list(holder.find_all(exp.Column)):
        if column.name.lower() in outputs and (column.table.lower() == alias or (only_source and not column.table)):
            column.replace(exp.Paren(this=outputs[column.name.lower()].copy()))
        else:
            # Any other column is opaque: it must not be read as one of the inner tables.
            column.replace(exp.column(column.name, table=_OPAQUE))
    return rejected_tables(holder) - {_OPAQUE}


def left_join_to_inner(select: exp.Select) -> exp.Expression | None:
    """``select`` with null-rejected LEFT joins made inner (here or in a derived source), or None."""

    where = select.args.get("where")
    if where is None:
        return None
    sources = _sources(select)
    if sources is None:
        return None
    copy = select.copy()
    if _convert(copy, rejected_tables(where.this)):
        return copy
    copied_sources = _sources(copy) or []
    for source, _ in copied_sources:
        if not isinstance(source, exp.Subquery) or not _plain_derived(source.this):
            continue
        alias = _source_name(source)
        inner_rejected = _through_derived(copy.args["where"].this, alias, source.this, len(copied_sources) == 1)
        if inner_rejected and _convert(source.this, inner_rejected):
            return copy
    return None
