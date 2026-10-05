"""ARRAY_LENGTH facts for stored ARRAY columns, used by ``algebraic_equivalence.normalize``.

BigQuery never stores a NULL array: a NULL array written to a table is stored as the empty array, and a stored array
holds no NULL elements. So for a column the schema types as an ARRAY of a stored table:

* ``arr IS NULL`` is FALSE and ``arr IS NOT NULL`` is TRUE;
* ``ARRAY_LENGTH(arr)`` is never NULL (so ``IS NULL`` on it is FALSE and ``IFNULL``/``COALESCE`` around it is a no-op),
  is at least 0, and equals ``(SELECT COUNT(*) FROM UNNEST(arr))``;
* hence ``ARRAY_LENGTH(arr) > 0`` (also ``>= 1``, ``!= 0``, ``NOT ... = 0``) is ``EXISTS (SELECT 1 FROM UNNEST(arr))`` and
  ``ARRAY_LENGTH(arr) = 0`` (also ``<= 0``, ``< 1``) is ``NOT EXISTS (...)``.

Every form is rewritten to one reading, ``ARRAY_LENGTH(arr)`` (compared with 0 as ``> 0`` or ``= 0``), which the prover
encodes. ``ARRAY_LENGTH(arr) > 1`` against EXISTS is left alone: they differ.

The facts hold only for a column of a base table the schema types as ARRAY. They do not hold for a computed array
(``ARRAY_CONCAT`` with a NULL argument and ``ARRAY(SELECT ...)`` over no rows can be NULL, and ``UNNEST(NULL)`` has no rows
while ``ARRAY_LENGTH(NULL)`` is NULL), for a struct field (the struct itself can be NULL), for a column read through a
derived table or CTE, or for a column on the null-extended side of an outer join (its arrays are NULL there). The rule
declines all of those, and records ``ASSUMPTION`` whenever it fires. It is a no-op unless the caller collects assumptions.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY

ASSUMPTION = (
    "a column the schema types as ARRAY is a stored array: never NULL (BigQuery stores a NULL array as empty) and with no NULL "
    "elements, so ARRAY_LENGTH of it is never NULL and equals the row count of its UNNEST (views and other computed tables are "
    "assumed to follow the same storage rule)"
)

_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)
_FLIP = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}
_NEGATE = {"zero": "positive", "positive": "zero", "always": "never", "never": "always"}
_PLAIN_SELECT = ("where", "group", "having", "qualify", "windows", "order", "limit", "offset", "distinct", "joins", "with", "with_", "into", "sample", "laterals", "pivots", "connect", "match", "kind", "hint", "options", "locks", "operation_modifiers")


def _is_array_type(type_name) -> bool:
    text = str(type_name or "").strip().upper()
    return text == "ARRAY" or text.startswith("ARRAY<") or text.startswith("ARRAY ")


def _from_of(select: exp.Select):
    return select.args.get(FROM_KEY)


def _source_alias(source: exp.Expression) -> str | None:
    alias = source.args.get("alias")
    if isinstance(source, exp.Unnest):
        if alias is not None and alias.columns:
            return alias.columns[0].name.lower()
        return alias.name.lower() if alias is not None and alias.name else None
    if alias is not None and alias.name:
        return alias.name.lower()
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
        return source.name.lower()
    return None


def _sources(select: exp.Select) -> list[tuple[str | None, exp.Expression, bool]]:
    """``(alias, source, null_extended)`` for each FROM item of ``select``, in order."""

    items: list[tuple[str | None, exp.Expression, bool]] = []
    from_ = _from_of(select)
    if from_ is not None:
        items.append((_source_alias(from_.this), from_.this, False))
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        if side in ("RIGHT", "FULL"):
            items = [(a, s, True) for a, s, _ in items]
        items.append((_source_alias(join.this), join.this, side in ("LEFT", "FULL")))
    return items


def _projection_names(select: exp.Select) -> set[str]:
    return {e.alias.lower() for e in select.expressions if isinstance(e, exp.Alias)}


class _Resolver:
    def __init__(self, tree: exp.Expression, types: dict[str, dict[str, str]]):
        self.types = types
        self.ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}

    def _table_columns(self, table: exp.Expression) -> dict[str, str] | None:
        """The declared column types of a base table source, or None when it is not a plain known table."""

        if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
            return None
        if table.args.get("pivots") or table.args.get("joins") or table.args.get("catalog"):
            return None
        name = table.name.lower()
        if name in self.ctes or "*" in name:
            return None
        db = table.args.get("db")
        for key in ((f"{db.name.lower()}.{name}",) if db is not None else ()) + (name,):
            if key in self.types:
                return self.types[key]
        return None

    def stored_array(self, column: exp.Expression, scope: exp.Select | None) -> bool:
        """Whether ``column`` reads a stored (never NULL) array column of a base table, not null-extended by an outer join."""

        if not isinstance(column, exp.Column) or column.args.get("db") or column.args.get("catalog") or not isinstance(column.this, exp.Identifier):
            return False
        name, qualifier = column.name.lower(), column.table.lower()
        while scope is not None:
            sources = _sources(scope)
            if name in _projection_names(scope):
                return False  # an output alias may be what the name means in ORDER BY, GROUP BY or HAVING
            if qualifier:
                matches = [(s, nullable) for alias, s, nullable in sources if alias == qualifier]
                if len(matches) > 1:
                    return False
                if matches:
                    if any(qualifier in (self._table_columns(s) or {}) for _a, s, _n in sources):
                        return False  # the qualifier could also be a struct column of another source
                    source, nullable = matches[0]
                    columns = self._table_columns(source)
                    return columns is not None and not nullable and _is_array_type(columns.get(name))
            else:
                owners = []
                for _alias, source, nullable in sources:
                    columns = self._table_columns(source)
                    if columns is None:
                        return False  # an unknown source (derived table, UNNEST, ...) may also own the name
                    if name in columns:
                        owners.append((columns, nullable))
                if len(owners) > 1:
                    return False
                if owners:
                    columns, nullable = owners[0]
                    return not nullable and _is_array_type(columns[name])
            scope = scope.find_ancestor(exp.Select)
        return False


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    return node.find_ancestor(exp.Select)


def _plain_unnest_select(select: exp.Expression) -> exp.Unnest | None:
    """The UNNEST of ``SELECT ... FROM UNNEST(x)`` when nothing else (filter, join, offset, grouping, ...) shapes the rows."""

    if not isinstance(select, exp.Select) or any(select.args.get(key) for key in _PLAIN_SELECT):
        return None
    from_ = _from_of(select)
    unnest = from_.this if from_ is not None else None
    if not isinstance(unnest, exp.Unnest) or unnest.args.get("offset") or len(unnest.expressions) != 1:
        return None
    return unnest


def _element_alias(unnest: exp.Unnest) -> str | None:
    return _source_alias(unnest)


def _count_star(select: exp.Select, unnest: exp.Unnest) -> bool:
    if len(select.expressions) != 1:
        return False
    item = select.expressions[0]
    if isinstance(item, exp.Alias):
        item = item.this
    if not isinstance(item, exp.Count) or item.args.get("distinct") or item.args.get("expressions"):
        return False
    return isinstance(item.this, exp.Star) or (isinstance(item.this, exp.Literal) and not item.this.is_string and item.this.name == "1")


def _constant_projection(select: exp.Select, unnest: exp.Unnest) -> bool:
    """A select list that cannot fail or filter: constants, ``*`` or the element itself."""

    element = _element_alias(unnest)
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            item = item.this
        if isinstance(item, exp.Literal) or isinstance(item, exp.Star):
            continue
        if isinstance(item, exp.Column) and not item.table and element and item.name.lower() == element:
            continue
        return False
    return bool(select.expressions)


def _integer(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Paren):
        return _integer(node.this)
    if isinstance(node, exp.Neg):
        value = _integer(node.this)
        return None if value is None else -value
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        return int(node.name)
    return None


def _length_kind(node: exp.Expression, is_length) -> str | None:
    """How a test of ``ARRAY_LENGTH`` against an integer reads: ``zero``, ``positive``, ``always`` or ``never``."""

    if isinstance(node, exp.Paren):
        return _length_kind(node.this, is_length)
    if isinstance(node, exp.Not):
        inner = _length_kind(node.this, is_length)
        return _NEGATE.get(inner) if inner else None
    if not isinstance(node, _COMPARISONS):
        return None
    op = type(node)
    left, right = node.this, node.expression
    if is_length(right) and not is_length(left):
        left, right, op = right, left, _FLIP[op]
    if not is_length(left):
        return None
    value = _integer(right)
    if value is None:
        return None
    # the length is an integer that is at least 0
    if op is exp.GT:
        return "always" if value < 0 else "positive" if value == 0 else None
    if op is exp.GTE:
        return "always" if value <= 0 else "positive" if value == 1 else None
    if op is exp.LT:
        return "never" if value <= 0 else "zero" if value == 1 else None
    if op is exp.LTE:
        return "never" if value < 0 else "zero" if value == 0 else None
    if op is exp.EQ:
        return "never" if value < 0 else "zero" if value == 0 else None
    if op is exp.NEQ:
        return "always" if value < 0 else "positive" if value == 0 else None
    return None


def _canonical(kind: str, length: exp.Expression) -> exp.Expression:
    if kind == "always":
        return exp.true()
    if kind == "never":
        return exp.false()
    if kind == "positive":
        return exp.GT(this=length.copy(), expression=exp.Literal.number(0))
    return exp.EQ(this=length.copy(), expression=exp.Literal.number(0))


def _once(tree: exp.Expression, resolver: _Resolver) -> bool:
    """Apply every rewrite once; True when the tree changed."""

    def stored(column: exp.Expression, scope: exp.Select | None = None) -> bool:
        return resolver.stored_array(column, scope or _enclosing_select(column))

    def is_length(node: exp.Expression) -> bool:
        return isinstance(node, exp.ArraySize) and stored(node.this)

    for node in list(tree.find_all(exp.Is)):
        target = node.this
        if not isinstance(node.expression, exp.Null) or not (stored(target) or is_length(target)):
            continue
        if isinstance(node.parent, exp.Not):
            node.parent.replace(exp.true())
        else:
            node.replace(exp.false())
        return True

    for node in list(tree.find_all(exp.Coalesce)):
        if is_length(node.this) and node.expressions:
            node.replace(node.this.copy())
            return True

    for node in list(tree.find_all(exp.Subquery)):
        parent = node.parent
        value_context = isinstance(parent, (exp.Binary, exp.Alias, exp.Not, exp.Neg, exp.Case, exp.If, exp.Coalesce)) or (
            isinstance(parent, exp.Select) and node.arg_key == "expressions"
        )
        if not value_context or isinstance(parent, (exp.Where, exp.In)):
            continue
        select = node.this
        unnest = _plain_unnest_select(select)
        if unnest is None or not _count_star(select, unnest) or not stored(unnest.expressions[0], _enclosing_select(node)):
            continue
        node.replace(exp.ArraySize(this=unnest.expressions[0].copy()))
        return True

    for node in list(tree.find_all(exp.Exists)):
        select = node.this
        unnest = _plain_unnest_select(select)
        if unnest is None or not _constant_projection(select, unnest) or not stored(unnest.expressions[0], _enclosing_select(node)):
            continue
        node.replace(exp.GT(this=exp.ArraySize(this=unnest.expressions[0].copy()), expression=exp.Literal.number(0)))
        return True

    for node in list(tree.find_all(exp.Not, *_COMPARISONS)):
        kind = _length_kind(node, is_length)
        if kind is None:
            continue
        length = next(n for n in node.find_all(exp.ArraySize) if is_length(n))
        replacement = _canonical(kind, length)
        if replacement != node:
            node.replace(replacement)
            return True
    return False


def rewrite_array_length(tree: exp.Expression, types: dict[str, dict[str, str]] | None, assumptions: set[str] | None) -> exp.Expression:
    """Apply the stored-array facts; a no-op without column types or a place to record ``ASSUMPTION``."""

    if assumptions is None or not types or not any(_is_array_type(t) for columns in types.values() for t in columns.values()):
        return tree
    resolver = _Resolver(tree, types)
    work = tree.copy()
    changed = False
    for _ in range(64):
        if not _once(work, resolver):
            break
        changed = True
    if not changed:
        return tree
    assumptions.add(ASSUMPTION)
    return work
