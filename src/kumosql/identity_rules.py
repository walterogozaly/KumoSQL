"""Small identities the optimizer applies once it knows a column is a key, non-NULL or an integer.

One entry (:func:`identity_rules`) in the normalizer's rule list:

* ``key_distinct_aggregates``: ``SUM(DISTINCT k)``, ``COUNT(DISTINCT k)`` and ``AVG(DISTINCT k)`` read
  one table, and the grouped columns together with ``k`` contain a declared key of it made of NOT NULL
  columns (UNIQUE or PRIMARY KEY; ``k`` alone when there is no GROUP BY): rows of a group agree on the grouped columns, so no two share
  a non-NULL ``k``, and the aggregates skip NULLs, so DISTINCT removes nothing.
* ``truth_tests``: a filter conjunct (WHERE, or the ON of a join) is kept only when it is TRUE, so
  ``p IS TRUE`` and ``p <=> TRUE`` are ``p`` there; and ``p IS NOT FALSE`` (``NOT (p <=> FALSE)``) is
  ``p`` when ``p`` is a comparison of expressions that cannot be NULL (a NOT NULL column, a column a
  null-rejecting conjunct of the same filter or of its derived table keeps non-NULL, a number, or
  arithmetic and casts of those), since ``p`` is then TRUE or FALSE. Repeated conjuncts are dropped.
  Only selects whose joins are all inner are read, so no column is null-extended.
* ``integer_decimal_casts``: ``CAST(i AS DECIMAL(p, s))`` of an integer expression ``i`` listed in a
  select list is ``i``. The value is unchanged; a value too large for the DECIMAL makes the cast an error
  in the engines, not another value, so the fold assumes every integer fits (the proof says so).
  Where the decimal's integer digits cover the integer type the assumption is not needed.
  Only in dialects where ``/`` is never integer division, since an integer read by a division differs from
  a decimal.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts, extended_grouping
from .cast_rules import _EXACT_DIVISION, _decimal_params, expression_type
from .join_rewrites import _lookup, _table_name

DECIMAL_FIT_ASSUMPTION = (
    "an integer cast to a DECIMAL is taken to fit its integer digits (a value that does not fit makes the cast fail, "
    "it does not give another value)"
)

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")


def _name(source: exp.Expression) -> str:
    if isinstance(source, exp.Table):
        return (source.alias_or_name or "").lower()
    if isinstance(source, exp.Subquery) and source.alias:
        return source.alias.lower()
    return ""


# --- DISTINCT aggregates over a key -------------------------------------------------------


def key_distinct_aggregates(select: exp.Select, keys, not_null) -> exp.Expression | None:
    from_ = _from(select)
    if from_ is None or select.args.get("joins") or select.args.get("laterals") or not isinstance(from_.this, exp.Table):
        return None
    table = from_.this
    if table.args.get("pivots") or not isinstance(table.this, exp.Identifier):
        return None
    # a UNIQUE key with a nullable column lets rows repeat (NULLs are never equal), so only keys of NOT NULL columns count
    required = {str(c).lower() for c in _lookup({str(t).lower(): cols for t, cols in (not_null or {}).items()}, _table_name(table)) or ()}
    declared = [
        tuple(str(c).lower() for c in key)
        for key in _lookup({str(t).lower(): ks for t, ks in (keys or {}).items()}, _table_name(table)) or ()
        if key and all(str(c).lower() in required for c in key)
    ]
    if not declared:
        return None
    qualifier = _name(table)
    group = select.args.get("group")
    grouped = set()
    if group is not None:
        if extended_grouping(group):
            return None
        for item in group.expressions:
            if isinstance(item, exp.Column) and not isinstance(item.this, exp.Star) and item.table.lower() in ("", qualifier):
                grouped.add(item.name.lower())
    copy = select.copy()
    changed = False
    for node in list(copy.find_all(exp.Sum, exp.Count, exp.Avg)):
        if node.find_ancestor(exp.Select) is not copy or not isinstance(node.this, exp.Distinct):
            continue
        distinct = node.this
        if distinct.args.get("on") or len(distinct.expressions) != 1:
            continue
        column = distinct.expressions[0]
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or column.table.lower() not in ("", qualifier):
            continue
        # rows of a group agree on the grouped columns, so a key made of those and this column tells them apart
        if not any(key and set(key) <= grouped | {column.name.lower()} for key in declared):
            continue
        node.set("this", column.copy())
        changed = True
    return copy if changed else None


# --- IS TRUE / IS NOT FALSE in a filter -------------------------------------------------------


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _bool_literal(node: exp.Expression, value: bool) -> bool:
    node = _unparen(node)
    return isinstance(node, exp.Boolean) and bool(node.this) is value


def _is_true_test(part: exp.Expression) -> exp.Expression | None:
    """``p`` of ``p IS TRUE`` / ``p <=> TRUE``."""

    part = _unparen(part)
    if isinstance(part, (exp.Is, exp.NullSafeEQ)) and _bool_literal(part.expression, True):
        return part.this
    if isinstance(part, exp.NullSafeEQ) and _bool_literal(part.this, True):
        return part.expression
    return None


def _not_false_test(part: exp.Expression) -> exp.Expression | None:
    """``p`` of ``p IS NOT FALSE`` / ``NOT (p <=> FALSE)`` / ``p IS DISTINCT FROM FALSE``."""

    part = _unparen(part)
    if isinstance(part, exp.Not):
        inner = _unparen(part.this)
        if isinstance(inner, (exp.Is, exp.NullSafeEQ)) and _bool_literal(inner.expression, False):
            return inner.this
        if isinstance(inner, exp.NullSafeEQ) and _bool_literal(inner.this, False):
            return inner.expression
    if isinstance(part, exp.NullSafeNEQ) and _bool_literal(part.expression, False):
        return part.this
    return None


def _strict_columns(condition: exp.Expression) -> set[tuple[str, str]]:
    """Columns a filter conjunct needs non-NULL to be TRUE: operands of a comparison, or ``IS NOT NULL``."""

    found: set[tuple[str, str]] = set()
    for part in conjuncts(condition):
        part = _unparen(part)
        operands: list[exp.Expression] = []
        if isinstance(part, _COMPARISONS):
            operands = [part.this, part.expression]
        elif isinstance(part, exp.Not) and isinstance(_unparen(part.this), exp.Is) and isinstance(_unparen(part.this).expression, exp.Null):
            operands = [_unparen(part.this).this]
        for operand in operands:
            operand = _unparen(operand)
            if isinstance(operand, exp.Column) and not isinstance(operand.this, exp.Star):
                found.add((operand.table.lower(), operand.name.lower()))
    return found


class _NonNull:
    """Which expressions of one select are never NULL, given its own filter and its sources' facts."""

    def __init__(self, select: exp.Select, not_null):
        self.not_null = {str(t).lower(): {str(c).lower() for c in cols} for t, cols in (not_null or {}).items()}
        self.select = select
        self.sources = {_name(s): s for s in ([_from(select).this] if _from(select) is not None else []) + [j.this for j in select.args.get("joins") or []]}
        self.filtered: set[tuple[str, str]] = set()
        where = select.args.get("where")
        if where is not None:
            self.filtered |= _strict_columns(where.this)
        for join in select.args.get("joins") or []:
            if join.args.get("on") is not None:
                self.filtered |= _strict_columns(join.args["on"])

    def column(self, column: exp.Column, scope: "_NonNull | None" = None) -> bool:
        scope = scope or self
        if isinstance(column.this, exp.Star):
            return False
        qualifier = column.table.lower()
        name = column.name.lower()
        if (qualifier, name) in scope.filtered or (not qualifier and any(n == name for _, n in scope.filtered)):
            return True
        source = scope.sources.get(qualifier)
        if isinstance(source, exp.Table):
            return name in (_lookup(self.not_null, _table_name(source)) or ())
        if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            inner = source.this
            if any(inner.args.get(k) for k in ("group", "having", "limit", "offset", "qualify", "windows")) or inner.args.get("distinct") is not None and inner.args["distinct"].args.get("on"):
                return False
            items = [i for i in inner.expressions if i.alias_or_name.lower() == name]
            if len(items) != 1:
                return False
            return self.expression(items[0].this if isinstance(items[0], exp.Alias) else items[0], _NonNull(inner, self.not_null_raw()))
        return False

    def not_null_raw(self):
        return self.not_null

    def expression(self, node: exp.Expression, scope: "_NonNull | None" = None) -> bool:
        scope = scope or self
        node = _unparen(node)
        if isinstance(node, exp.Literal):
            return True
        if isinstance(node, exp.Column):
            return self.column(node, scope)
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
            return self.expression(node.this, scope) and self.expression(node.expression, scope)
        if isinstance(node, exp.Neg):
            return self.expression(node.this, scope)
        if isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast) and node.args.get("format") is None:
            return self.expression(node.this, scope)
        return False


def _all_inner(select: exp.Select) -> bool:
    for join in select.args.get("joins") or []:
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method"):
            return False
    return not select.args.get("laterals")


def _rewrite_filter(condition: exp.Expression, facts: _NonNull | None) -> exp.Expression:
    parts: list[exp.Expression] = []
    seen: set[str] = set()
    for part in conjuncts(condition):
        replacement = _is_true_test(part)
        if replacement is None and facts is not None:
            inner = _not_false_test(part)
            if inner is not None:
                core = _unparen(inner)
                if isinstance(core, _COMPARISONS) and facts.expression(core.this) and facts.expression(core.expression):
                    replacement = inner
        part = (replacement if replacement is not None else part).copy()
        if replacement is not None:
            # the test may itself be a nested conjunction
            for sub in conjuncts(part):
                if sub.sql() not in seen:
                    seen.add(sub.sql())
                    parts.append(sub)
            continue
        if part.sql() not in seen:
            seen.add(part.sql())
            parts.append(part)
    return exp.and_(*parts) if len(parts) > 1 else parts[0]


def truth_tests(select: exp.Select, not_null) -> exp.Expression | None:
    facts = _NonNull(select, not_null) if _all_inner(select) else None
    copy = select.copy()
    changed = False
    holders = []
    if copy.args.get("where") is not None:
        holders.append(copy.args["where"])
    for join in copy.args.get("joins") or []:
        if join.args.get("on") is not None:
            holders.append(join)
    for holder in holders:
        key = "this" if isinstance(holder, exp.Where) else "on"
        original = holder.args[key]
        rewritten = _rewrite_filter(original, facts)
        if rewritten.sql() != original.sql():
            holder.set(key, rewritten)
            changed = True
    return copy if changed else None


# --- integer casts to DECIMAL -------------------------------------------------------------


def integer_decimal_casts(select: exp.Select, types: dict, dialect: str, assumptions: set[str] | None) -> exp.Expression | None:
    if dialect not in _EXACT_DIVISION or not types:
        return None
    copy = select.copy()
    changed = False
    for index, item in enumerate(list(copy.expressions)):
        cast = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(cast, exp.Cast) or isinstance(cast, exp.TryCast) or not isinstance(cast.args.get("to"), exp.DataType):
            continue
        params = _decimal_params(cast.args["to"])
        if params is None or params[0] < params[1]:
            continue
        have = expression_type(cast.this, copy, types, dialect=dialect, bound=True)
        if have is None or have[0] != "int":
            continue
        inner = cast.this.copy()
        if isinstance(item, exp.Alias):
            item.set("this", inner)
        else:
            cast.replace(exp.alias_(inner, item.alias_or_name) if item.alias_or_name else inner)
        if params[0] - params[1] < have[1] and assumptions is not None:
            assumptions.add(DECIMAL_FIT_ASSUMPTION)
        changed = True
    return copy if changed else None


def identity_rules(select: exp.Select, keys=None, not_null=None, types=None, dialect: str = "bigquery", assumptions: set[str] | None = None) -> exp.Expression | None:
    """The rules of this module, as one entry of the normalizer's rule list."""

    return key_distinct_aggregates(select, keys, not_null) or truth_tests(select, not_null) or integer_decimal_casts(select, types or {}, dialect, assumptions)
