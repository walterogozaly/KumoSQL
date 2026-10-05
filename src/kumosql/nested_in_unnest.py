"""``x IN UNNEST(arr)`` read as the membership test the prover already knows.

BigQuery defines ``x IN UNNEST(arr)`` as ``x IN (SELECT e FROM UNNEST(arr) AS e)``: TRUE when some element equals
``x``; otherwise NULL when ``x`` is NULL or an element is NULL, FALSE when nothing is NULL (an empty array, and a NULL
array, which UNNEST turns into no rows, give FALSE even for a NULL ``x``). Rewriting one into the other is exact for
any array expression, so the prover compares membership over UNNEST with its ``IN`` subquery, semi-join and
``EXISTS`` machinery, and keeps the three-valued behaviour that separates ``NOT x IN UNNEST(arr)`` from
``NOT EXISTS (... WHERE e = x)``.

Three readings, each applied only when it is exact:

1. ``x IN UNNEST(arr)`` becomes ``x IN (SELECT e FROM UNNEST(arr) AS e)`` (``NOT`` included; ``NOT IN`` is the same
   node under a ``NOT``).
2. A literal array whose elements are plain scalar expressions is an IN list: ``x IN UNNEST([1, 2])`` is
   ``x IN (1, 2)``, with the same NULL behaviour (a NULL element included). An empty literal array is FALSE.
   Arrays with struct, array or query elements are left to reading 1.
3. A NOT NULL constant ``c`` tested against an ARRAY column of a stored table is ``EXISTS (SELECT 1 FROM
   UNNEST(arr) AS e WHERE e = c)``. This needs the storage fact below: with no NULL element and a NOT NULL ``c``
   the test is never NULL, so it equals the existence test, which is what makes ``NOT c IN UNNEST(arr)`` the
   ``NOT EXISTS`` of the same. A nullable ``c`` keeps reading 1 (a NULL ``c`` against a non-empty array is NULL,
   not FALSE), and so does any array that is not a plain column of a stored table (an expression can be NULL or
   hold a NULL element).

Storage fact (recorded as an assumption when reading 3 fires): an ARRAY column of a stored table holds no NULL
element; BigQuery refuses to store one.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

ASSUMPTION = "an ARRAY column of a stored table holds no NULL element (BigQuery cannot store one)"

_SCALAR_TYPES_EXCLUDED = ("STRUCT", "ARRAY", "RANGE")


def _fresh_names() -> "iter":
    count = 0
    while True:
        count += 1
        yield f"in_unnest_{count}"


def _declares(select: exp.Select, name: str) -> exp.Expression | None:
    """The FROM or JOIN item of ``select`` whose alias is ``name``, if any."""

    items = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        items.append(from_clause.this)
    items.extend(join.this for join in select.args.get("joins") or [])
    for item in items:
        if (item.alias_or_name or "").lower() == name:
            return item
        alias = item.args.get("alias") if isinstance(item, exp.Unnest) else None
        if alias is not None and any(c.name.lower() == name for c in alias.args.get("columns") or []):
            return item
    return None


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_clause = select.args.get("from_") or select.args.get("from")
    items = [from_clause.this] if from_clause is not None else []
    items.extend(join.this for join in select.args.get("joins") or [])
    return items


def _resolve(column: exp.Column, types: dict[str, dict[str, str]]) -> tuple[exp.Select, exp.Table] | None:
    """The select and base table a column reads, or None when that is not certain.

    A qualified column names the innermost FROM item with that alias, which must be a plain table. A bare column is
    read from the innermost select whose sources are all plain tables of the typed schema and where exactly one of
    them has the column; any other kind of source in a select (a derived table, an UNNEST, a table the schema does
    not type) could hold the column too, so the answer is None.
    """

    if column.args.get("db") or column.args.get("catalog"):
        return None
    qualifier = column.table.lower()
    name = column.name.lower()
    scope = column.find_ancestor(exp.Select)
    while scope is not None:
        if qualifier:
            item = _declares(scope, qualifier)
            if item is not None:
                if isinstance(item, exp.Table) and not item.db and not item.catalog and item.name.lower() in types:
                    return scope, item
                return None
        else:
            sources = _sources(scope)
            if not sources:
                scope = scope.find_ancestor(exp.Select)
                continue
            if not all(isinstance(s, exp.Table) and not s.db and not s.catalog and s.name.lower() in types for s in sources):
                return None
            owners = [s for s in sources if name in types[s.name.lower()]]
            if len(owners) > 1:
                return None
            if owners:
                return scope, owners[0]
        scope = scope.find_ancestor(exp.Select)
    return None


def _stored_array_column(column: exp.Expression, types: dict[str, dict[str, str]]) -> bool:
    """Whether ``column`` is a plain ARRAY column (of scalars) of a stored table the schema types."""

    if not isinstance(column, exp.Column):
        return False
    found = _resolve(column, types)
    if found is None:
        return False
    declared = types[found[1].name.lower()].get(column.name.lower(), "").strip().upper().replace(" ", "")
    if not declared.startswith("ARRAY<") or not declared.endswith(">"):
        return False
    element = declared[len("ARRAY<") : -1]
    return not element.startswith(_SCALAR_TYPES_EXCLUDED) and "STRUCT<" not in element and "ARRAY<" not in element


def _never_null(value: exp.Expression, types: dict[str, dict[str, str]], not_null: dict[str, set[str]]) -> bool:
    """A literal, or a column the schema declares NOT NULL of a table no outer join pads."""

    if isinstance(value, exp.Paren):
        return _never_null(value.this, types, not_null)
    if isinstance(value, (exp.Literal, exp.Boolean)):
        return True
    if not isinstance(value, exp.Column):
        return False
    found = _resolve(value, types)
    if found is None:
        return False
    scope, table = found
    if any(j.args.get("side") or (j.args.get("kind") or "").upper() in ("SEMI", "ANTI") for j in scope.args.get("joins") or []):
        return False
    return value.name.lower() in not_null.get(table.name.lower(), ())


def _plain_literal_elements(array: exp.Expression) -> list[exp.Expression] | None:
    """The elements of a literal array of scalar expressions, else None."""

    if not isinstance(array, exp.Array):
        return None
    elements = list(array.expressions)
    for element in elements:
        if isinstance(element, (exp.Query, exp.Subquery, exp.Struct, exp.Array, exp.Unnest)) or element.find(exp.Query, exp.Struct, exp.Array, exp.Window, exp.AggFunc):
            return None
    return elements


def _empty_literal(array: exp.Expression) -> bool:
    inner = array.this if isinstance(array, exp.Cast) else array
    return isinstance(inner, exp.Array) and not inner.expressions and not inner.args.get("this")


def _membership(value: exp.Expression, array: exp.Expression, name: str) -> exp.In:
    probe = sqlglot.parse_one(f"SELECT {name} FROM UNNEST(NULL) AS {name}", read="bigquery")
    probe.args["from_" if "from_" in probe.args else "from"].this.set("expressions", [array.copy()])
    return exp.In(this=value.copy(), query=exp.Subquery(this=probe))


def rewrite_in_unnest(
    tree: exp.Expression,
    types: dict[str, dict[str, str]] | None = None,
    not_null: dict[str, set[str]] | None = None,
    assumptions: set[str] | None = None,
) -> exp.Expression:
    """Replace every ``IN UNNEST(array)`` in ``tree`` by the readings above (the tree is returned, rewritten in place)."""

    nodes = [n for n in tree.find_all(exp.In) if n.args.get("unnest") is not None and n.args.get("query") is None and not n.args.get("expressions")]
    if not nodes:
        return tree
    types = {t.lower(): {c.lower(): v for c, v in cols.items()} for t, cols in (types or {}).items()}
    not_null = {t.lower(): {c.lower() for c in cols} for t, cols in (not_null or {}).items()}
    names = _fresh_names()
    for node in nodes:
        unnest = node.args["unnest"]
        if not isinstance(unnest, exp.Unnest) or len(unnest.expressions) != 1 or unnest.args.get("alias") or unnest.args.get("offset") or node.args.get("field"):
            continue
        array = unnest.expressions[0]
        value = node.this
        if _empty_literal(array):
            node.replace(exp.false())
            continue
        elements = _plain_literal_elements(array)
        if elements:
            node.replace(exp.In(this=value.copy(), expressions=[e.copy() for e in elements]))
            continue
        name = next(names)
        if _stored_array_column(array, types) and _never_null(value, types, not_null):
            body = sqlglot.parse_one(f"SELECT 1 FROM UNNEST(NULL) AS {name} WHERE {name} = NULL", read="bigquery")
            body.args["from_" if "from_" in body.args else "from"].this.set("expressions", [array.copy()])
            body.args["where"].this.set("expression", value.copy())
            node.replace(exp.Exists(this=body))
            if assumptions is not None:
                assumptions.add(ASSUMPTION)
            continue
        node.replace(_membership(value, array, name))
    return tree
