"""Set-operation identities for the algebraic normalizer.

Each rule is an exact bag identity; a shape the rule cannot read is left alone.

* ``SELECT t.a AS a, t.b AS b FROM (X <op> Y) AS t`` lists every output of the derived set
  operation in order, so it is that set operation. With ``DISTINCT`` it is the DISTINCT form
  of a ``UNION ALL`` or ``INTERSECT ALL`` (or the set operation itself when that already
  removes duplicates).
* ``X INTERSECT ALL Y`` is ``X INTERSECT Y`` when either operand has no repeated rows
  (``min(m, n)`` is then 0 or 1), and ``X EXCEPT ALL Y`` is ``X EXCEPT Y`` when ``X`` has none
  (``max(m - n, 0)`` with ``m <= 1``).
* ``X EXCEPT [ALL] X`` has no rows when both operands are the same deterministic query.
* ``X EXCEPT ALL E`` is ``X`` and ``X EXCEPT E`` is ``SELECT DISTINCT`` over ``X`` when ``E``
  can never return a row.

A query has no repeated rows when it is a DISTINCT set operation, ``SELECT DISTINCT``, a
``GROUP BY`` that outputs every grouping expression, a global aggregate (one row), or a
projection or set operation that keeps those (see :func:`distinct_rows`).
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .canonical import canonical_copy
from .empty_rules import is_empty

_COUNTER = itertools.count()
_EXTRAS = ("where", "group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "joins", "laterals", "pivots", "prewhere", "connect", "match", "sample", "locks", "kind", "into", "settings", "format", "options")


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Subquery) and not node.alias and not any(node.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample")):
        node = node.this
    return node


def _plain_setop(node: exp.Expression) -> bool:
    return isinstance(node, exp.SetOperation) and not any(
        node.args.get(k) for k in ("order", "limit", "offset", "with_", "with", "by_name", "side", "kind", "on")
    )


def output_names(node: exp.Expression) -> list[str] | None:
    """The output column names of a query, when every one is known and they are distinct."""

    node = _unwrap(node)
    while isinstance(node, exp.SetOperation):
        node = _unwrap(node.this)
    if not isinstance(node, exp.Select):
        return None
    names = []
    for item in node.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        names.append(item.alias_or_name.lower())
    if "" in names or len(set(names)) != len(names):
        return None
    return names


def _global_aggregate(select: exp.Select) -> bool:
    if select.args.get("group"):
        return False
    return any(
        agg.find_ancestor(exp.Select) is select and agg.find_ancestor(exp.Window) is None
        for item in select.expressions
        for agg in item.find_all(exp.AggFunc)
    )


def _source_columns(select: exp.Select) -> tuple[exp.Expression, str, list[str]] | None:
    """The one derived table of ``select``, its alias and its output names."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or select.args.get("laterals"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias:
        return None
    alias = source.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return None
    names = output_names(source.this)
    if names is None:
        return None
    return source, source.alias.lower(), names


def _column_of(item: exp.Expression, alias: str) -> str | None:
    """The source column a select item passes through unchanged (``t.c`` or ``t.c AS x``)."""

    column = item.this if isinstance(item, exp.Alias) else item
    if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
        return None
    if column.table and column.table.lower() != alias:
        return None
    if column.args.get("db") or column.args.get("catalog"):
        return None
    return column.name.lower()


def _star_of(select: exp.Select, alias: str) -> bool:
    """The select list is just ``*`` or ``t.*`` (every column of the one source, in order)."""

    if len(select.expressions) != 1:
        return False
    item = select.expressions[0]
    if isinstance(item, exp.Star):
        return not any(item.args.get(k) for k in ("except", "replace", "rename"))
    return (
        isinstance(item, exp.Column)
        and isinstance(item.this, exp.Star)
        and not any(item.this.args.get(k) for k in ("except", "replace", "rename"))
        and item.table.lower() == alias
    )


def distinct_rows(node: exp.Expression) -> bool:
    """True when ``node`` can never return the same row twice."""

    node = _unwrap(node)
    if isinstance(node, exp.Subquery):
        node = node.this  # ORDER BY / LIMIT keep a duplicate-free bag duplicate-free
    if isinstance(node, exp.SetOperation):
        if any(node.args.get(k) for k in ("by_name", "side", "kind", "on")):
            return False
        if node.args.get("distinct"):
            return True
        if isinstance(node, exp.Intersect):
            return distinct_rows(node.this) or distinct_rows(node.expression)
        if isinstance(node, exp.Except):
            return distinct_rows(node.this)
        return False
    if not isinstance(node, exp.Select):
        return False
    distinct = node.args.get("distinct")
    if distinct is not None and not distinct.args.get("on"):
        return True
    return bool(_select_keys(node))  # each key is made of this select's own outputs


def _group_key_outputs(select: exp.Select) -> list[str] | None:
    """Output names of a grouped select's keys, when every grouping expression is output as is."""

    group = select.args.get("group")
    if group is None or any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "totals")) or not group.expressions:
        return None
    by_text: dict[str, str] = {}
    aliases = set()
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if isinstance(item, exp.Alias) and not (isinstance(value, exp.Column) and value.name.lower() == item.alias.lower()):
            aliases.add(item.alias.lower())
        if item.alias_or_name:
            by_text.setdefault(value.sql(), item.alias_or_name.lower())
    keys = []
    for g in group.expressions:
        if isinstance(g, exp.Literal) or (isinstance(g, exp.Column) and not g.table and g.name.lower() in aliases):
            return None  # an ordinal, or a name that may mean an output alias
        if g.sql() not in by_text:
            return None
        keys.append(by_text[g.sql()])
    return keys


def _select_keys(select: exp.Select) -> list[frozenset[str]]:
    """Sets of output names that no two rows of ``select`` share (the empty set: at most one row)."""

    if any(isinstance(n, (exp.Explode, exp.Unnest, exp.Posexplode)) for n in select.walk()):
        return []
    if select.args.get("group") is not None:
        keys = _group_key_outputs(select)
        return [] if keys is None else [frozenset(keys)]
    if _global_aggregate(select):
        return [frozenset()]
    found = _source_columns(select)
    if found is None or any(select.args.get(k) for k in ("having", "qualify", "windows", "laterals")) or select.find(exp.Window):
        return []
    source, alias, names = found
    if _star_of(select, alias):
        return _query_keys(source.this)
    passed: dict[str, str] = {}
    for item in select.expressions:
        column = _column_of(item, alias)
        if column is not None and item.alias_or_name:
            passed.setdefault(column, item.alias_or_name.lower())
    return [frozenset(passed[c] for c in key) for key in _query_keys(source.this) if key <= passed.keys()]


def _query_keys(node: exp.Expression) -> list[frozenset[str]]:
    node = _unwrap(node)
    if isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Select):
        distinct = node.args.get("distinct")
        if distinct is not None and not distinct.args.get("on"):
            names = output_names(node)
            return [] if names is None else [frozenset(names)]
        return _select_keys(node)
    names = output_names(node)
    if names is not None and distinct_rows(node):
        return [frozenset(names)]
    return []


def _deterministic(node: exp.Expression) -> bool:
    for n in node.walk():
        if isinstance(n, (exp.Anonymous, exp.Rand, exp.Window, exp.Limit, exp.Offset, exp.Fetch, exp.TableSample, exp.Uuid)):
            return False
        if isinstance(n, exp.Func) and any(word in type(n).__name__.lower() for word in ("rand", "uuid", "random", "sample")):
            return False
    return True


_INTEGER_TYPES = {exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT}


def _output_value(value: exp.Expression) -> exp.Expression:
    """An output ``CAST(1 AS BIGINT)`` holds the same value as ``1`` (types are not compared)."""

    if isinstance(value, exp.Cast) and isinstance(value.this, exp.Literal) and not value.this.is_string:
        to = value.args.get("to")
        text = value.this.name
        if isinstance(to, exp.DataType) and to.this in _INTEGER_TYPES and text.isdigit() and int(text) < 128:
            return value.this
    return value


def _same_query(left: exp.Expression, right: exp.Expression) -> bool:
    """Both operands are the same deterministic query, whatever their aliases and output names."""

    texts = []
    for node in (left, right):
        node = _unwrap(node)
        if not _deterministic(node) or node.find(exp.Order):
            return False
        copy = canonical_copy(node)
        heads = [copy]
        while heads:
            head = _unwrap(heads.pop())
            if isinstance(head, exp.SetOperation):
                heads.extend([head.this, head.expression])
            elif isinstance(head, exp.Select):
                head.set("expressions", [_output_value(e.this if isinstance(e, exp.Alias) else e) for e in head.expressions])
            else:
                return False
        texts.append(copy.sql(dialect="bigquery"))
    return texts[0] == texts[1]


def _empty_like(node: exp.Expression) -> exp.Expression | None:
    names = output_names(node)
    if names is None:
        return None
    return exp.Select(expressions=[exp.alias_(exp.Null(), name) for name in names], where=exp.Where(this=exp.false()))


def _distinct_over(node: exp.Expression) -> exp.Expression | None:
    """``SELECT DISTINCT`` of every column of ``node``, in order."""

    if distinct_rows(node):
        return node.copy()
    names = output_names(node)
    if names is None:
        return None
    alias = f"kumosql_sd{next(_COUNTER)}"
    return exp.Select(
        expressions=[exp.alias_(exp.column(name, table=alias), name) for name in names],
        distinct=exp.Distinct(),
    ).from_(exp.Subquery(this=node.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))


def _identity_over_setop(select: exp.Select) -> exp.Expression | None:
    """``SELECT [DISTINCT] t.a AS a, t.b AS b FROM (X op Y) AS t`` is the set operation (or its DISTINCT form)."""

    if any(select.args.get(k) for k in _EXTRAS):
        return None
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    found = _source_columns(select)
    if found is None:
        return None
    source, alias, names = found
    setop = _unwrap(source.this)
    if not _plain_setop(setop):
        return None
    if not _star_of(select, alias):
        if len(select.expressions) != len(names):
            return None
        for item, name in zip(select.expressions, names):
            if _column_of(item, alias) != name or (item.alias_or_name or "").lower() != name:
                return None
    result = setop.copy()
    if distinct is not None and not distinct_rows(result):
        if isinstance(result, (exp.Intersect, exp.Union)) and type(result) in (exp.Union, exp.Intersect):
            result.set("distinct", True)
        else:
            return None
    return result


def _drop_operand_grouping(node: exp.SetOperation) -> exp.Expression | None:
    """An operand of a DISTINCT set operation is read as a set, so ``GROUP BY`` its outputs (no aggregate) can go."""

    changed = None
    for side in ("this", "expression"):
        operand = _unwrap(node.args[side])
        if not isinstance(operand, exp.Select) or operand.args.get("group") is None:
            continue
        if any(operand.args.get(k) for k in ("having", "qualify", "windows", "order", "limit", "offset", "distinct")) or operand.find(exp.Window):
            continue
        if any(agg.find_ancestor(exp.Select) is operand for agg in operand.find_all(exp.AggFunc)):
            continue
        grouped = {g.sql() for g in operand.args["group"].expressions}
        outputs = [(e.this if isinstance(e, exp.Alias) else e) for e in operand.expressions]
        if _group_key_outputs(operand) is None or any(isinstance(e, exp.Star) or e.sql() not in grouped for e in outputs):
            continue
        if changed is None:
            changed = node.copy()
        target = _unwrap(changed.args[side])
        target.set("group", None)
    return changed


def _filtered_projection(node: exp.Expression):
    """``SELECT [DISTINCT] t.a, t.b FROM t WHERE p``: the select, its table, the column names and ``p``."""

    select = _unwrap(node)
    if not isinstance(select, exp.Select) or any(select.args.get(k) for k in _EXTRAS if k != "where"):
        return None
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    table = from_.this if from_ is not None else None
    if not isinstance(table, exp.Table) or any(table.args.get(k) for k in ("joins", "pivots", "laterals", "sample", "version", "when")):
        return None
    alias = table.alias_or_name.lower()
    if table.args.get("alias") is not None and table.args["alias"].args.get("columns"):
        return None
    names = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or column.table.lower() != alias:
            return None  # an unqualified name could be an outer reference
        names.append(column.name.lower())
    where = select.args.get("where")
    condition = where.this if where is not None else None
    if condition is not None:
        if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select, exp.Window, exp.AggFunc, exp.Star)) for n in condition.walk()) or not _deterministic(condition):
            return None
        if any(c.table.lower() != alias for c in condition.find_all(exp.Column)):
            return None
    key = (".".join(p.name.lower() for p in table.parts), tuple(names))
    return select, alias, key, condition


def _merge_same_table_filters(node: exp.SetOperation) -> exp.Expression | None:
    """DISTINCT set operations of filters over one table that output the same columns, as one filter.

    ``SELECT a, b FROM t WHERE p UNION SELECT a, b FROM t WHERE q`` is ``SELECT DISTINCT a, b FROM t WHERE p OR q``.
    When ``q`` reads only output columns, a row is in ``SELECT a, b FROM t WHERE q`` exactly when it is a row of
    ``SELECT a, b FROM t`` on which ``q`` holds, so INTERSECT is ``p AND q`` and EXCEPT is
    ``p AND NOT COALESCE(q, FALSE)``.
    """

    if not node.args.get("distinct"):
        return None
    left, right = _filtered_projection(node.this), _filtered_projection(node.expression)
    if left is None or right is None or left[2] != right[2]:
        return None
    select, alias, (_, names), p = left
    _, right_alias, _, q = right
    if q is not None:
        q = q.copy()
        for column in q.find_all(exp.Column):
            column.set("table", exp.to_identifier(alias))
    if not isinstance(node, exp.Union) or type(node) is not exp.Union:
        if q is not None and any(c.name.lower() not in names for c in q.find_all(exp.Column)):
            return None
    if type(node) is exp.Union:
        condition = None if p is None or q is None else exp.Or(this=exp.Paren(this=p.copy()), expression=exp.Paren(this=q))
    elif isinstance(node, exp.Intersect):
        parts = [x for x in (p.copy() if p is not None else None, q) if x is not None]
        condition = None if not parts else parts[0] if len(parts) == 1 else exp.And(this=exp.Paren(this=parts[0]), expression=exp.Paren(this=parts[1]))
    elif isinstance(node, exp.Except):
        if q is None:
            return _empty_like(node.this)
        negated = exp.Not(this=exp.Coalesce(this=exp.Paren(this=q), expressions=[exp.false()]))
        condition = negated if p is None else exp.And(this=exp.Paren(this=p.copy()), expression=negated)
    else:
        return None
    merged = select.copy()
    merged.set("where", exp.Where(this=condition) if condition is not None else None)
    merged.set("distinct", exp.Distinct())
    return merged


def _setop_identities(node: exp.SetOperation) -> exp.Expression | None:
    if not _plain_setop(node):
        return None
    left, right = node.this, node.expression
    if isinstance(node, exp.Except):
        if _same_query(left, right):
            return _empty_like(left)
        if is_empty(right):
            if node.args.get("distinct"):
                return _distinct_over(_unwrap(left))
            return _unwrap(left).copy()
    if node.args.get("distinct"):
        return _merge_same_table_filters(node) or _drop_operand_grouping(node)
    if isinstance(node, exp.Intersect) and (distinct_rows(left) or distinct_rows(right)):
        copy = node.copy()
        copy.set("distinct", True)
        return copy
    if isinstance(node, exp.Except) and distinct_rows(left):
        copy = node.copy()
        copy.set("distinct", True)
        return copy
    return None


def normalize_set_operations(tree: exp.Expression) -> exp.Expression:
    """Apply the identities above everywhere in ``tree`` (the normalizer repeats this to a fixed point)."""

    def step(node: exp.Expression) -> exp.Expression:
        replacement = None
        if isinstance(node, exp.Select):
            replacement = _identity_over_setop(node)
        elif isinstance(node, exp.SetOperation):
            replacement = _setop_identities(node)
        if replacement is None:
            return node
        if isinstance(replacement, exp.SetOperation) and isinstance(node.parent, exp.SetOperation):
            return exp.Subquery(this=replacement)
        if isinstance(replacement, exp.Select) and isinstance(node.parent, exp.SetOperation):
            return exp.Subquery(this=replacement)
        return replacement

    return tree.transform(step)
