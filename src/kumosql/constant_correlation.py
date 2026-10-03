"""Read a correlated column pinned to an integer constant as that constant inside a subquery.

``SELECT .. FROM (SELECT a, k FROM t WHERE t.k = 200) AS d WHERE EXISTS (SELECT 1 FROM s WHERE d.k = s.k)``
is ``.. WHERE EXISTS (SELECT 1 FROM s WHERE 200 = s.k)``, and so is the same query with ``d.k = 200`` as a
conjunct of the outer ``WHERE`` itself. Why it is sound:

* A row that reaches the subquery has ``d.k = 200`` TRUE: either it is a top-level conjunct of the same
  ``WHERE`` (a row whose conjunct is FALSE or UNKNOWN is dropped whatever the subquery says), or every
  row of the derived table ``d`` passed that filter, and ``d`` is not null-extended by a RIGHT or FULL join.
  So the column is non-null and equal to the constant wherever the subquery's value can matter.
* Only integer columns and integer literals are touched, and only where the reference is an operand of a
  comparison whose other operand is an integer too: an equal integer value compares the same way as the
  literal (no string coercion, collation or decimal scale can tell them apart).
* The reference must name a source of this ``SELECT`` that no query between it and the subquery shadows.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts
from .cast_rules import expression_type

_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
_GROUPED = ("group", "having", "qualify", "windows")


def _sources(select: exp.Select) -> list[tuple[exp.Expression, exp.Join | None]]:
    from_ = select.args.get("from_") or select.args.get("from")
    return ([(from_.this, None)] if from_ is not None else []) + [(j.this, j) for j in select.args.get("joins") or []]


def _int_literal(node: exp.Expression) -> exp.Literal | None:
    return node if isinstance(node, exp.Literal) and not node.is_string and node.is_int else None


def _pinned(part: exp.Expression) -> tuple[exp.Column, exp.Literal] | None:
    """``(column, literal)`` when ``part`` is ``column = literal`` (either way round) with an integer literal."""

    if not isinstance(part, exp.EQ):
        return None
    for column, literal in ((part.this, part.expression), (part.expression, part.this)):
        if isinstance(column, exp.Column) and isinstance(column.this, exp.Identifier) and _int_literal(literal) is not None:
            return column, literal
    return None


def _derived_constants(inner: exp.Select) -> dict[str, exp.Literal]:
    """Output names of a filter-and-project ``SELECT`` that every one of its rows holds at a constant."""

    where = inner.args.get("where")
    if where is None or any(inner.args.get(k) for k in _GROUPED) or inner.find(exp.AggFunc) is not None:
        return {}
    pinned = [p for p in map(_pinned, conjuncts(where.this)) if p is not None]
    found: dict[str, exp.Literal] = {}
    for item in inner.expressions:
        value = item.unalias()
        if not isinstance(value, exp.Column) or not item.alias_or_name:
            continue
        for column, literal in pinned:
            if column == value:
                found.setdefault(item.alias_or_name.lower(), literal)
    return found


def _constants(select: exp.Select, parts: list[exp.Expression]) -> dict[tuple[str, str], exp.Literal]:
    aliases = [(source.alias_or_name or "").lower() for source, _ in _sources(select)]
    found: dict[tuple[str, str], exp.Literal] = {}
    for part in parts:
        pinned = _pinned(part)
        if pinned is not None and pinned[0].table and aliases.count(pinned[0].table.lower()) == 1:
            found.setdefault((pinned[0].table.lower(), pinned[0].name.lower()), pinned[1])
    nullable_from = any((j.side or "").upper() in ("RIGHT", "FULL") for _, j in _sources(select) if j is not None)
    for source, join in _sources(select):
        alias = (source.alias_or_name or "").lower()
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or aliases.count(alias) != 1:
            continue
        if (join is None and nullable_from) or (join is not None and (bool(join.side) or join.kind not in ("", "INNER", "CROSS"))):
            continue
        for name, literal in _derived_constants(source.this).items():
            found.setdefault((alias, name), literal)
    return found


def _shadowed(column: exp.Column, select: exp.Select, alias: str) -> bool:
    scope = column.find_ancestor(exp.Select)
    while scope is not None and scope is not select:
        if any((source.alias_or_name or "").lower() == alias for source, _ in _sources(scope)):
            return True
        scope = scope.find_ancestor(exp.Select)
    return scope is None


def propagate_constant_correlations(select: exp.Select, types: dict) -> exp.Select | None:
    where = select.args.get("where")
    if where is None or not types:
        return None
    parts = conjuncts(where.this)
    constants = _constants(select, parts)
    if not constants:
        return None
    changed = False
    for part in parts:
        for column in list(part.find_all(exp.Column)):
            key = ((column.table or "").lower(), column.name.lower())
            literal = constants.get(key)
            inner = column.find_ancestor(exp.Select)
            if literal is None or inner is None or inner is select or not isinstance(column.this, exp.Identifier):
                continue
            comparison = column.parent
            if not isinstance(comparison, _COMPARISONS) or _shadowed(column, select, key[0]):
                continue
            other = comparison.expression if comparison.this is column else comparison.this
            if (expression_type(column, select, types) or ("",))[0] != "int" or (expression_type(other, inner, types) or ("",))[0] != "int":
                continue
            column.replace(literal.copy())
            changed = True
    return select if changed else None
