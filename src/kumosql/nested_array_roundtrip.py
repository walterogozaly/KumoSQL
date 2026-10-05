"""ARRAY(SELECT .. ORDER BY offset) and UNNEST round trips (BigQuery).

Three exact identities over arrays and ``UNNEST``:

* **Round trip.** ``ARRAY(SELECT x FROM UNNEST(a) AS x WITH OFFSET o ORDER BY o)`` is ``a``: the subquery
  reads each element once, in offset order, with nothing filtered and nothing added. It is folded only when ``a``
  cannot be NULL, because ``ARRAY(..)`` over a NULL array is ``[]``, not NULL. That holds for an array literal
  and for a column the schema types as ``ARRAY<..>`` of a table (a stored array is never NULL: BigQuery stores a
  NULL array as empty), read from the table's own row and not from the null-extended side of an outer join.
  An array computed any other way (``ARRAY_CONCAT``, a struct field, a view) can be NULL and is left alone.
* **UNNEST of a round trip.** ``FROM UNNEST(ARRAY(SELECT x FROM UNNEST(a) AS x WITH OFFSET o ORDER BY o))`` is
  ``FROM UNNEST(a)`` for any ``a``: a NULL array and an empty one both unnest to no rows, and the offsets agree
  because the inner subquery keeps the order. (Not applied to ``x IN UNNEST(..)``, where a NULL array is not
  obviously the empty one.)
* **UNNEST of literals is a UNION ALL.** ``FROM UNNEST([1, 2]) AS x WITH OFFSET AS o`` is
  ``FROM (SELECT 1 AS x, 0 AS o UNION ALL SELECT 2, 1) AS x``, offsets counted from 0. The rows are the same bag;
  BigQuery fixes no order for either side, so the rewrite is declined for a statement whose result could depend on
  the row order (a window, LIMIT or OFFSET, ARRAY_AGG, STRING_AGG, ANY_VALUE, FIRST/LAST). Only scalar literals of
  one kind (integers, decimals, strings or booleans, with NULLs allowed) are rewritten, and only as a FROM source or
  an inner/cross join, never as an outer-joined side.

Anything else (a filter, DISTINCT, ``ORDER BY`` anything but the offset, a descending order, ``SELECT AS STRUCT``,
a computed element, ``ARRAY_AGG`` without ``ORDER BY``) is left as written.
"""

from __future__ import annotations

import re

from sqlglot import exp

ASSUMPTION = (
    "a stored ARRAY column is never NULL (BigQuery stores a NULL array as empty), so ARRAY(SELECT .. FROM UNNEST(column) .. ORDER BY offset) is the column"
)
_MAX_LITERALS = 64
# the select's own clauses a round-trip subquery may carry; anything else (WHERE, DISTINCT, GROUP BY, LIMIT, ...) changes the rows
_ALLOWED_ARGS = {"expressions", "from_", "from", "order"}
# constructs whose value can depend on the order rows arrive in
_ORDER_SENSITIVE = (exp.Window, exp.Limit, exp.Offset, exp.Fetch, exp.ArrayAgg, exp.ArrayConcatAgg, exp.GroupConcat, exp.AnyValue, exp.First, exp.Last, exp.Rand)


def _name(node: exp.Expression | None) -> str:
    return (node.name if node is not None else "").lower()


def _element_alias(unnest: exp.Unnest) -> str:
    alias = unnest.args.get("alias")
    if alias is None:
        return ""
    columns = alias.args.get("columns") or []
    if len(columns) == 1:
        return columns[0].name.lower()
    return "" if columns else alias.name.lower()


def _offset_alias(unnest: exp.Unnest) -> str | None:
    """The WITH OFFSET alias (``offset`` when unnamed), or None when there is no WITH OFFSET."""

    offset = unnest.args.get("offset")
    if isinstance(offset, exp.Identifier):
        return offset.name.lower()
    if offset is True or isinstance(offset, exp.Boolean) and offset.this:
        return "offset"
    return None


def _roundtrip_source(array: exp.Expression) -> exp.Expression | None:
    """``a`` when ``array`` is ``ARRAY(SELECT x FROM UNNEST(a) AS x WITH OFFSET o ORDER BY o)`` exactly, else None."""

    if not isinstance(array, exp.Array) or len(array.expressions) != 1:
        return None
    select = array.expressions[0]
    if not isinstance(select, exp.Select) or any(value for key, value in select.args.items() if value and key not in _ALLOWED_ARGS):
        return None
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Unnest):
        return None
    unnest = source.this
    element, offset = _element_alias(unnest), _offset_alias(unnest)
    if not element or not offset or element == offset or len(unnest.expressions) != 1:
        return None
    if len(select.expressions) != 1:
        return None
    item = select.expressions[0]
    if not isinstance(item, exp.Column) or item.table or _name(item) != element:
        return None
    order = select.args.get("order")
    if order is None or len(order.expressions) != 1:
        return None
    ordered = order.expressions[0]
    if not isinstance(ordered, exp.Ordered) or ordered.args.get("desc"):
        return None
    key = ordered.this
    if not isinstance(key, exp.Column) or key.table or _name(key) != offset:
        return None
    return unnest.expressions[0]


def _is_array_literal(node: exp.Expression) -> bool:
    if isinstance(node, exp.Cast):
        node = node.this
    return isinstance(node, exp.Array) and bool(node.expressions) and not any(isinstance(e, (exp.Select, exp.Subquery)) for e in node.expressions)


def _sources(select: exp.Select) -> list[tuple[exp.Expression, exp.Join | None]]:
    from_ = select.args.get("from_") or select.args.get("from")
    out: list[tuple[exp.Expression, exp.Join | None]] = [(from_.this, None)] if from_ is not None else []
    return out + [(join.this, join) for join in select.args.get("joins") or []]


def _is_array_type(type_text: str) -> bool:
    return type_text.strip().upper().startswith("ARRAY")


def _stored_array(column: exp.Expression, anchor: exp.Expression, types: dict[str, dict[str, str]]) -> bool:
    """True when ``column`` (read inside ``anchor``) names an ARRAY column of a table the select around it reads directly."""

    if not isinstance(column, exp.Column) or column.args.get("db") or column.args.get("catalog") or not column.name:
        return False
    qualifier, name = column.table.lower(), column.name.lower()
    scope = anchor.find_ancestor(exp.Select)
    while scope is not None:
        sources = _sources(scope)
        if any(join is not None and join.args.get("side") in ("RIGHT", "FULL") for _, join in sources):
            return False  # a RIGHT or FULL join can null-extend any earlier table
        matches = []
        for source, join in sources:
            if qualifier:
                if (source.alias_or_name or "").lower() != qualifier:
                    continue
                matches.append((source, join))
            elif isinstance(source, exp.Table) and not source.args.get("db") and not source.args.get("catalog"):
                columns = types.get(source.name.lower())
                if columns is None:
                    return False  # a table whose columns are unknown may own the name
                if name in columns:
                    matches.append((source, join))
            else:
                return False  # a derived table, UNNEST or other source may own an unqualified name
        if matches:
            if len(matches) != 1:
                return False
            source, join = matches[0]
            if not isinstance(source, exp.Table) or source.args.get("db") or source.args.get("catalog"):
                return False
            if join is not None and join.args.get("side"):
                return False  # the null-extended side of an outer join reads NULL for an unmatched row
            alias = source.args.get("alias")
            if alias is not None and alias.args.get("columns"):
                return False
            columns = types.get(source.name.lower())
            return columns is not None and _is_array_type(columns.get(name, ""))
        scope = scope.find_ancestor(exp.Select)
    return False


def _fold_roundtrips(tree: exp.Expression, types: dict[str, dict[str, str]], assumptions: set[str] | None) -> exp.Expression:
    for array in reversed(list(tree.find_all(exp.Array))):
        source = _roundtrip_source(array)
        if source is None or array.parent is None:
            continue
        if _is_array_literal(source):
            array.replace(source.copy())
        elif _stored_array(source, array, types):
            array.replace(source.copy())
            if assumptions is not None:
                assumptions.add(ASSUMPTION)
    return tree


def _unnest_of_roundtrip(tree: exp.Expression) -> exp.Expression:
    for unnest in list(tree.find_all(exp.Unnest)):
        if not isinstance(unnest.parent, (exp.From, exp.Join)) or len(unnest.expressions) != 1:
            continue
        source = _roundtrip_source(unnest.expressions[0])
        if source is not None:
            unnest.expressions[0].replace(source.copy())
    return tree


def _literal_kind(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Null):
        return "null"
    if isinstance(node, exp.Boolean):
        return "bool"
    if isinstance(node, exp.Neg):
        node = node.this
        if not isinstance(node, exp.Literal) or node.is_string:
            return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "str"
        return "int" if re.fullmatch(r"\d+", node.this) else "decimal"
    return None


def _order_sensitive(tree: exp.Expression) -> bool:
    return any(True for _ in tree.find_all(*_ORDER_SENSITIVE))


def _literals_to_union(tree: exp.Expression) -> exp.Expression:
    if _order_sensitive(tree):
        return tree
    for unnest in list(tree.find_all(exp.Unnest)):
        parent = unnest.parent
        if isinstance(parent, exp.Join):
            if parent.args.get("side") or parent.args.get("on") or parent.args.get("using") or parent.args.get("kind") not in (None, "", "CROSS", "INNER"):
                continue
        elif not isinstance(parent, exp.From):
            continue
        if len(unnest.expressions) != 1:
            continue
        literals = unnest.expressions[0]
        if not isinstance(literals, exp.Array) or not 0 < len(literals.expressions) <= _MAX_LITERALS:
            continue
        kinds = {_literal_kind(e) for e in literals.expressions}
        kinds.discard("null")
        if None in kinds or len(kinds) != 1:
            continue
        element, offset = _element_alias(unnest), _offset_alias(unnest)
        if not element or element == offset:
            continue
        has_offset = bool(unnest.args.get("offset"))
        if has_offset and not offset:
            continue
        rows = []
        for index, value in enumerate(literals.expressions):
            # the first branch names the columns, as in a hand-written UNION ALL
            items = [exp.alias_(value.copy(), element) if index == 0 else value.copy()]
            if has_offset:
                items.append(exp.alias_(exp.Literal.number(index), offset) if index == 0 else exp.Literal.number(index))
            rows.append(exp.select(*items))
        body: exp.Expression = rows[0]
        for row in rows[1:]:
            body = exp.Union(this=body, expression=row, distinct=False)
        unnest.replace(exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(element))))
    return tree


def fold_array_roundtrips(
    tree: exp.Expression, types: dict[str, dict[str, str]] | None = None, assumptions: set[str] | None = None
) -> exp.Expression:
    """Apply the round-trip, UNNEST-of-round-trip and UNNEST-of-literals identities to ``tree`` (see the module docstring)."""

    lowered = {table.lower(): {column.lower(): kind for column, kind in columns.items()} for table, columns in (types or {}).items()}
    tree = _fold_roundtrips(tree, lowered, assumptions)
    tree = _unnest_of_roundtrip(tree)
    return _literals_to_union(tree)
