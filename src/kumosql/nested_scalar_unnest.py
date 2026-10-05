"""Scalar lookups over an UNNEST as one unknown function of the array they read.

GA4 and Snowplow queries read a value out of a repeated field with a correlated scalar subquery::

    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location')

The subquery reads only the array it unnests, so its value is a function of that array and of nothing
else. :class:`Lookups` replaces each such subquery in both queries by ``kumosql_unnest_lookup(k, array)``,
where ``k`` numbers the lookup: two lookups share a number exactly when they read the same way once
the spelling is removed. That spelling is the alias (``p`` or none), the order of the WHERE conjuncts, a
test written ``'k' = key`` or ``key = 'k'``, a ``SELECT AS VALUE`` wrapper and whether the element's fields
are written ``p.key`` or ``key``. The function itself stays unknown, so lookups that read differently (another
key, ``value.int_value`` against ``value.string_value``, ``MAX`` against the plain read) are different
unknowns and are never equated.

A lookup is replaced only when everything it reads is the array (an unqualified name must be a field of the
array's element, which the declared column type shows) and everything it computes is deterministic. Anything
else (a second source, ``LIMIT``, ``ORDER BY``, ``WITH OFFSET``, ``DISTINCT``, ``GROUP BY``, a column of the
outer query other than the array, a function outside a short list) is left as written.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import faithful_sql, select_sources
from .nested_values import NestedType, parse_type

ASSUMPTION = "scalar subqueries over UNNEST return at most one row (BigQuery raises an error otherwise)"
FUNCTION = "kumosql_unnest_lookup"
_ELEMENT = "_e"
_AGGREGATES = (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg)
_ALLOWED = (
    exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Cast, exp.TryCast, exp.DataType,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Between,
    exp.Like, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.Lower, exp.Upper, exp.Coalesce, exp.If, exp.Case,
    exp.Abs, exp.Length, exp.Trim, exp.Concat, exp.Distinct, *_AGGREGATES,
)
_MIRROR = {exp.EQ: exp.EQ, exp.NEQ: exp.NEQ, exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}
_READ = frozenset({"expressions", "kind", "from_", "from", "where"})  # any other clause changes which rows are read


def _split_and(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _parts(column: exp.Column) -> list[str]:
    return [p.name.lower() for p in (column.args.get("catalog"), column.args.get("db"), column.args.get("table"), column.this) if p is not None]


def _constant(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean))


class Lookups:
    """The numbering of lookups shared by the two queries of a comparison."""

    def __init__(self, dialect: str = "bigquery", types=None):
        self.dialect = dialect
        self.types = {str(k).lower(): {str(c).lower(): t for c, t in (cols or {}).items()} for k, cols in (types or {}).items()}
        self.classes: list[str] = []
        self.unproven = 0  # replaced lookups that are not aggregates: they need the at-most-one-row assumption

    # ---- reading the lookup

    def _element_type(self, node: exp.Subquery, array: exp.Column) -> NestedType | None:
        """The declared element type of ``array``, found through the FROM clauses around the subquery."""

        column = array.name.lower()
        qualifier = array.table.lower()
        scope = node.parent
        while scope is not None:
            if isinstance(scope, exp.Select):
                sources = select_sources(scope)
                tables = [s for s in sources if isinstance(s, exp.Table)]
                if qualifier:
                    for source in sources:
                        if (source.alias_or_name or "").lower() == qualifier:
                            if not isinstance(source, exp.Table):
                                return None
                            return self._column_element(source, column)
                else:
                    hits = [t for t in tables if column in self.types.get(_key(t), {})]
                    if len(hits) == 1 and len(hits) == len(tables) == len(sources):
                        return self._column_element(hits[0], column)
                    if hits or len(tables) != len(sources):
                        return None
            scope = scope.parent
        return None

    def _column_element(self, table: exp.Table, column: str) -> NestedType | None:
        declared = self.types.get(_key(table), {}).get(column)
        parsed = parse_type(declared) if declared else None
        return parsed.element if parsed is not None and parsed.kind == "ARRAY" else None

    def key(self, node: exp.Subquery) -> tuple[str, exp.Column, bool] | None:
        """``(canonical text, array column, aggregate?)`` of a lookup, or ``None`` when ``node`` is not one."""

        select = node.this
        if not isinstance(select, exp.Select) or len(select.expressions) != 1:
            return None
        if select.args.get("kind") not in (None, "VALUE") or any(value for name, value in select.args.items() if name not in _READ):
            return None
        source = select.args.get("from_") or select.args.get("from")
        unnest = source.this if source is not None else None
        if not isinstance(unnest, exp.Unnest) or unnest.args.get("offset") or len(unnest.expressions) != 1:
            return None
        array = unnest.expressions[0]
        if not isinstance(array, exp.Column) or isinstance(array.this, exp.Star) or len(_parts(array)) > 2:
            return None
        alias = unnest.args.get("alias")
        columns = alias.args.get("columns") if alias is not None else None
        if alias is not None and (len(columns or []) != 1 or alias.this is not None):
            return None
        name = columns[0].name.lower() if columns else None
        element = self._element_type(node, array)
        fields = {n.lower() for n, _ in element.fields if n} if element is not None and element.kind == "STRUCT" else set()
        if element is not None and element.kind not in ("STRUCT", "ARRAY") and name is None:
            return None

        def canonical(expression: exp.Expression) -> exp.Expression | None:
            holder = exp.Tuple(expressions=[expression.copy()])  # so that a bare column can be replaced too
            expression = holder.expressions[0]
            for inner in expression.walk():
                if not isinstance(inner, _ALLOWED) and not (isinstance(inner, exp.Star) and isinstance(inner.parent, exp.Count)):
                    return None
            for column in list(expression.find_all(exp.Column)):
                parts = _parts(column)
                if isinstance(column.this, exp.Star) or len(parts) > 3:
                    return None
                if parts[0] == name:
                    rest = parts[1:]
                elif parts[0] in fields:
                    rest = parts
                else:
                    return None  # something outside the array
                canon = [_ELEMENT, *rest]
                column.replace(exp.column(canon[-1], table=canon[-2] if len(canon) > 1 else None, db=canon[-3] if len(canon) > 2 else None, catalog=canon[-4] if len(canon) > 3 else None))
            return holder.expressions[0]

        projection = select.expressions[0]
        if isinstance(projection, exp.Alias):
            projection = projection.this
        if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)):
            return None
        body = canonical(projection)
        if body is None:
            return None
        conjuncts = []
        where = select.args.get("where")
        for conjunct in _split_and(where.this) if where is not None else []:
            if isinstance(conjunct, tuple(_MIRROR)) and _constant(conjunct.this) and not _constant(conjunct.expression):
                conjunct = _MIRROR[type(conjunct)](this=conjunct.expression, expression=conjunct.this)  # 'k' = key is key = 'k'
            conjunct = canonical(conjunct)
            if conjunct is None or any(isinstance(n, _AGGREGATES) for n in conjunct.walk()):
                return None
            conjuncts.append(faithful_sql(conjunct, self.dialect))
        aggregate = any(isinstance(n, _AGGREGATES) for n in body.walk())
        shape = element.sql() if element is not None else "?"
        text = f"{shape} :: {faithful_sql(body, self.dialect)} :: {' AND '.join(sorted(set(conjuncts)))}"
        return text, array, aggregate

    # ---- replacing it

    def replace(self, tree: exp.Expression) -> int:
        """Replace every lookup in ``tree`` by its function call; returns how many were replaced."""

        count = 0
        for node in list(tree.find_all(exp.Subquery)):
            if isinstance(node.parent, (exp.From, exp.Join, exp.In, exp.Exists, exp.CTE, exp.SetOperation, exp.Subquery, exp.Table, exp.TableAlias)):
                continue
            found = self.key(node)
            if found is None:
                continue
            text, array, aggregate = found
            if text not in self.classes:
                self.classes.append(text)
            if not aggregate:
                self.unproven += 1
            node.replace(exp.Anonymous(this=FUNCTION, expressions=[exp.Literal.number(self.classes.index(text)), array.copy()]))
            count += 1
        return count


def _key(table: exp.Table) -> str:
    return ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None).lower()
