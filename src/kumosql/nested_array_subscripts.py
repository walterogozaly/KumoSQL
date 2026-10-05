"""Array subscripts: ``arr[OFFSET(i)]``, ``arr[ORDINAL(i)]``, ``arr[SAFE_OFFSET(i)]`` and ``arr[SAFE_ORDINAL(i)]``.

BigQuery reads an array element by position: ``OFFSET`` counts from 0 and ``ORDINAL`` from 1, and an index outside
the array is an error, except for the ``SAFE_`` forms, which give NULL (as does a NULL array). Three rewrites make
spellings of the same element compare equal, each only where it is sound:

* ``arr[ORDINAL(n)]`` is ``arr[OFFSET(n - 1)]`` and ``arr[SAFE_ORDINAL(n)]`` is ``arr[SAFE_OFFSET(n - 1)]``, for an
  integer literal ``n >= 1``. The failing and the NULL-giving forms are never exchanged: ``arr[OFFSET(0)]`` raises on
  an empty array where ``arr[SAFE_OFFSET(0)]`` is NULL, and whether a stored array is empty cannot be known.
* ``(SELECT e FROM UNNEST(arr) AS e WITH OFFSET AS o WHERE o = k)`` is ``arr[SAFE_OFFSET(k)]`` for an integer
  literal ``k >= 0``, and ``(SELECT e.f FROM ... )`` is ``arr[SAFE_OFFSET(k)].f``. The scalar subquery has at most
  one row (an offset is unique), and none for a short or NULL array, so it is NULL exactly where the safe subscript
  is. The projection must be the element or a field path of it, because only those keep a NULL: ``COALESCE(e, 0)``
  would turn the missing row into 0 but not the missing element. The subquery must be read as a scalar value.
  The same offset filter in a cross join is not this: it drops the row of an empty array, where the subscript
  keeps it with NULL (the pair ``arr[SAFE_OFFSET(0)]`` against ``UNNEST(arr) WITH OFFSET`` with ``o = 0``).
* ``[a, b, c][OFFSET(k)]`` is the element, for an array literal of plain constants of one kind (integers, strings,
  floats or booleans, with NULL fillers) and a ``k`` inside it whose element is not NULL. An element outside the
  array, a NULL element (it would need its type), or mixed kinds (which BigQuery coerces: ``[1, 2.5][OFFSET(0)]`` is
  the float 1.0) is left alone.

Everything else keeps its subscript, and two subscripts are equal only when they are the same text: a negative or
non-literal index (``arr[OFFSET(i)]`` against ``arr[ORDINAL(i + 1)]``), and the bare ``arr[i]`` (BigQuery does not
define a bare subscript on an array, so it is not read as ``OFFSET``). ``sqlglot`` writes the specifier as the
``offset`` argument of ``Bracket`` (0 for ``OFFSET``, 1 for ``ORDINAL``) and ``safe`` for the ``SAFE_`` forms, and
a bare subscript has neither; the same in 26.0.0 and 30.x.
"""

from __future__ import annotations

from sqlglot import exp

_INT64 = 2**63
_SCALAR_PARENTS = (exp.Alias, exp.Binary, exp.Unary, exp.Paren, exp.Where, exp.Having, exp.Case, exp.If, exp.Coalesce, exp.Cast)


def _non_negative_int(node: exp.Expression | None) -> int | None:
    """The value of an integer literal ``>= 0`` (no sign, fraction or exponent)."""

    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit() and int(node.name) < _INT64:
        return int(node.name)
    return None


def _subscript(node: exp.Expression) -> bool:
    """A ``Bracket`` written with an explicit ``OFFSET``/``ORDINAL``/``SAFE_`` specifier and one index."""

    return isinstance(node, exp.Bracket) and node.args.get("offset") in (0, 1) and len(node.expressions) == 1


def _ordinal_to_offset(node: exp.Bracket) -> exp.Expression | None:
    if node.args.get("offset") != 1:
        return None
    index = _non_negative_int(node.expressions[0])
    if index is None or index < 1:
        return None  # ORDINAL(0) is out of range, and a computed index is not shifted
    copy = node.copy()
    copy.set("offset", 0)
    copy.set("expressions", [exp.Literal.number(index - 1)])
    return copy


def _name(node: exp.Expression | None) -> str:
    return node.name.lower() if node is not None else ""


def _unnest_alias(unnest: exp.Unnest) -> str | None:
    alias = unnest.args.get("alias")
    if alias is None:
        return None
    columns = alias.args.get("columns") or []
    if len(columns) == 1 and alias.this is None:
        return columns[0].name.lower()
    if not columns and alias.this is not None:
        return alias.name.lower()
    return None


def _element_path(node: exp.Expression, element: str) -> list[exp.Identifier] | None:
    """The field names read from the unnested element, outermost last: ``e`` is ``[]``, ``e.a.b`` is ``[a, b]``;
    ``None`` for any other expression."""

    if isinstance(node, exp.Column):
        if not node.table and node.name.lower() == element:
            return []
        if node.table and node.table.lower() == element and not node.args.get("db") and not node.args.get("catalog"):
            return [node.this.copy()] if isinstance(node.this, exp.Identifier) else None
        return None
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Identifier):
        inner = _element_path(node.this, element)
        return None if inner is None else [*inner, node.expression.copy()]
    return None


def _scalar_context(subquery: exp.Subquery) -> bool:
    parent = subquery.parent
    if isinstance(parent, exp.Select):
        return subquery.arg_key == "expressions"
    return isinstance(parent, _SCALAR_PARENTS) and not isinstance(parent, (exp.In, exp.Exists, exp.Any, exp.All))


def _offset_lookup(subquery: exp.Subquery) -> exp.Expression | None:
    """``arr[SAFE_OFFSET(k)]`` (with a field path) for a scalar subquery that reads one offset of ``UNNEST(arr)``."""

    if not _scalar_context(subquery) or any(v for k, v in subquery.args.items() if k != "this"):
        return None
    select = subquery.this
    if not isinstance(select, exp.Select):
        return None
    allowed = {"expressions", "from", "from_", "where"}
    if any(v for k, v in select.args.items() if k not in allowed) or len(select.expressions) != 1:
        return None
    source = select.args.get("from_") or select.args.get("from")
    where = select.args.get("where")
    unnest = source.this if source is not None else None
    if not isinstance(unnest, exp.Unnest) or where is None or len(unnest.expressions) != 1:
        return None
    if any(unnest.args.get(k) for k in ("explode", "table", "ordinality")):
        return None
    element = _unnest_alias(unnest)
    offset = unnest.args.get("offset")
    position = _name(offset) if isinstance(offset, exp.Identifier) else ""
    if not element or not position or element == position:
        return None
    condition = where.this
    if not isinstance(condition, exp.EQ):
        return None
    index = None
    for column, literal in ((condition.this, condition.expression), (condition.expression, condition.this)):
        if isinstance(column, exp.Column) and not column.table and column.name.lower() == position:
            index = _non_negative_int(literal)
            break
    if index is None:
        return None
    projected = select.expressions[0]
    if isinstance(projected, exp.Alias):
        projected = projected.this
    path = _element_path(projected, element)
    if path is None:
        return None
    array = unnest.expressions[0]
    if any(isinstance(c, exp.Column) and not c.table and c.name.lower() in (element, position) for c in array.find_all(exp.Column)):
        return None  # the array reads an outer column that the unnest's own names would otherwise hide
    result: exp.Expression = exp.Bracket(this=array.copy(), expressions=[exp.Literal.number(index)], offset=0, safe=True)
    for name in path:
        result = exp.Dot(this=result, expression=name)
    return result


def _constant_kind(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Neg):
        node = node.this
        if not isinstance(node, exp.Literal) or node.is_string:
            return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "string"
        if node.name.isdigit():
            return "int" if int(node.name) < _INT64 else None
        return "float"
    if isinstance(node, exp.Boolean):
        return "bool"
    return None


def _literal_element(node: exp.Bracket) -> exp.Expression | None:
    array = node.this
    if not isinstance(array, exp.Array) or any(v for k, v in array.args.items() if k != "expressions" and k != "struct_name_inheritance"):
        return None
    index = _non_negative_int(node.expressions[0])
    if index is None:
        return None
    if node.args.get("offset") == 1:
        index -= 1
    items = array.expressions
    if not 0 <= index < len(items):
        return None
    kinds = {_constant_kind(item) for item in items if not isinstance(item, exp.Null)}
    if len(kinds) != 1 or None in kinds or isinstance(items[index], exp.Null):
        return None
    return items[index].copy()


def normalize_array_subscripts(tree: exp.Expression, dialect: str = "bigquery") -> exp.Expression:
    """The tree with the array subscripts of the module docstring normalised (BigQuery only)."""

    if dialect != "bigquery":
        return tree

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery):
            return _offset_lookup(node) or node
        if _subscript(node):
            return _literal_element(node) or _ordinal_to_offset(node) or node
        return node

    return tree.transform(step)
