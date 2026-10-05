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
* Inside a DISTINCT set operation, a ``GROUP BY`` of exactly an operand's outputs is dropped.
* ``merge_same_source`` and ``set_operation_to_exists`` (below, called from the normalizer's
  per-node step): DISTINCT set operations of filters over one table as one filter, and other
  ``INTERSECT``/``EXCEPT`` as ``EXISTS``/``NOT EXISTS`` tests.

A query has no repeated rows when it is a DISTINCT set operation, ``SELECT DISTINCT``, a
``GROUP BY`` that outputs every grouping expression, a global aggregate (one row), or a
projection or set operation that keeps those (see :func:`distinct_rows`).
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .ast_utils import FROM_KEY, distinct_on, star_modified
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
    if not isinstance(node, exp.Select) or node.args.get("kind"):
        return None  # ``SELECT AS STRUCT a, b`` outputs one struct column, not the columns ``a`` and ``b``
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
    if star_modified(item):
        return False
    if isinstance(item, exp.Star):
        return True
    return isinstance(item, exp.Column) and isinstance(item.this, exp.Star) and item.table.lower() == alias


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
    if isinstance(select.parent, exp.Subquery) and isinstance(select.parent.parent, exp.In):
        return None  # the prover reads ``x IN (SELECT .. FROM (set operation))``, not ``x IN (set operation)``
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
        return _drop_operand_grouping(node)
    if isinstance(node, exp.Intersect) and (distinct_rows(left) or distinct_rows(right)):
        copy = node.copy()
        copy.set("distinct", True)
        return _drop_operand_grouping(copy) or copy
    if isinstance(node, exp.Except) and distinct_rows(left):
        copy = node.copy()
        copy.set("distinct", True)
        return _drop_operand_grouping(copy) or copy
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


# --- Filters and EXISTS tests (moved here from set_filters.py) ---------------------------------
#
# Duplicate-removing set operations as filters and EXISTS tests.
#
# Two rewrites of ``UNION``, ``INTERSECT`` and ``EXCEPT`` (the duplicate-removing forms, not ``ALL``):
#
# * ``merge_same_source``: both operands select the same expressions from the same single table and
#   differ only in their filters ``p`` and ``q``. Then ``UNION`` is ``SELECT DISTINCT .. WHERE p OR q``.
#   When ``q`` reads only projected columns, a row's ``q`` depends only on its output values, so
#   ``INTERSECT`` is ``.. WHERE p AND q`` and ``EXCEPT`` is ``.. WHERE p AND NOT COALESCE(q, FALSE)``
#   (for ``INTERSECT`` it is enough that either filter reads only projected columns). Calcite's
#   ``UnionToFilterRule``, ``IntersectToFilterRule`` and ``MinusToFilterRule`` do this.
# * ``set_operation_to_exists``: ``A INTERSECT B`` is ``SELECT DISTINCT a.* FROM (A) a WHERE EXISTS
#   (SELECT 1 FROM (B) b WHERE a.c1 <=> b.d1 AND ..)``, and ``EXCEPT`` is the same with ``NOT EXISTS``;
#   set operations compare rows with NULLs equal, which ``<=>`` spells. It only fires once both operands
#   read plain tables, so that ``merge_same_source`` gets the first chance.
_sf_counter = itertools.count()
_sf_BLOCKERS = ("group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals", "pivots")


def _sf_unwrap(node: exp.Expression) -> exp.Expression:
    # ``((SELECT ..) ORDER BY k LIMIT 1)`` keeps its tail on the parentheses; stepping through them would drop the cut.
    while isinstance(node, (exp.Subquery, exp.Paren)) and not node.alias and not any(node.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample")):
        node = node.this
    return node


def _sf_operand(node: exp.Expression) -> exp.Select | None:
    node = _sf_unwrap(node)
    return node if isinstance(node, exp.Select) else None


def _sf_from(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _sf_deterministic(node: exp.Expression) -> bool:
    return not any(isinstance(n, (exp.Rand, exp.Anonymous, exp.AggFunc, exp.Window, exp.Subquery, exp.Exists, exp.Placeholder)) for n in node.walk())


def _sf_flatten(select: exp.Select) -> exp.Select | None:
    """``SELECT f(d.x) FROM (SELECT g(t.y) AS x FROM t WHERE w) AS d WHERE h(d.x)`` read as one select of ``t``."""

    while True:
        source = _sf_from(select)
        if not isinstance(source, exp.Subquery) or not source.alias:
            return select
        inner = source.this
        if not isinstance(inner, exp.Select) or select.args.get("joins") or any(select.args.get(k) for k in _sf_BLOCKERS):
            return None
        if inner.args.get("distinct") or any(inner.args.get(k) for k in _sf_BLOCKERS):
            return None
        if any(isinstance(n, (exp.Subquery, exp.Exists)) and not _sf_within(n, source) for n in select.walk()):
            return None
        mapping = {}
        for item in inner.expressions:
            name = item.alias_or_name.lower()
            if not name or name in mapping or isinstance(_sf_value(item), exp.Star) or not _sf_deterministic(_sf_value(item)):
                return None
            mapping[name] = _sf_value(item)
        alias = source.alias.lower()
        flat = select.copy()
        flat.set(FROM_KEY, None)
        flat.set("from", None)
        flat_where = flat.args.get("where")
        for column in list(flat.find_all(exp.Column)):
            if column.table.lower() not in ("", alias) or column.name.lower() not in mapping:
                return None
            value = mapping[column.name.lower()].copy()
            column.replace(value if isinstance(value, (exp.Column, exp.Literal)) else exp.Paren(this=value))
        named = []
        for item, original in zip(flat.expressions, select.expressions):
            name = original.alias_or_name
            keep = isinstance(item, exp.Alias) or not name or item.alias_or_name.lower() == name.lower()
            named.append(item if keep else exp.alias_(item, name))
        flat.set("expressions", named)
        inner_from = inner.args.get("from_") or inner.args.get("from")
        if inner_from is None:
            return None
        flat.set(FROM_KEY, inner_from.copy())
        flat.set("joins", [j.copy() for j in inner.args.get("joins") or []] or None)
        parts = [w.this.copy() for w in (inner.args.get("where"), flat_where) if w is not None]
        flat.set("where", exp.Where(this=exp.and_(*[exp.Paren(this=p) for p in parts])) if parts else None)
        select = flat


def _sf_within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _sf_single_table(select: exp.Select):
    """``(table, alias, projections, where)`` for a projection and filter of one table, else None."""

    if any(select.args.get(k) for k in _sf_BLOCKERS) or select.args.get("joins"):
        return None
    table = _sf_from(select)
    if not isinstance(table, exp.Table) or table.args.get("joins"):
        return None
    alias = (table.alias_or_name or "").lower()
    where = select.args.get("where")
    parts = list(select.expressions) + ([where.this] if where is not None else [])
    for part in parts:
        if not _sf_deterministic(part) or isinstance(part, exp.Star):
            return None
        if any(c.table.lower() not in ("", alias) or isinstance(c.this, exp.Star) for c in part.find_all(exp.Column)):
            return None
    return table, alias, select.expressions, where.this if where is not None else None


def _sf_rename(node: exp.Expression, old: str, new: str) -> exp.Expression:
    node = node.copy()
    for column in node.find_all(exp.Column):
        if column.table.lower() in ("", old):
            column.set("table", exp.to_identifier(new))
    return node


def _sf_value(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _sf_reads_only(condition: exp.Expression | None, projected: set[str]) -> bool:
    if condition is None:
        return True
    return all(c.name.lower() in projected for c in condition.find_all(exp.Column))


def _sf_table_identity(table: exp.Table) -> tuple:
    """Everything that picks the rows a table reference reads: project, dataset, name and modifiers
    such as ``FOR SYSTEM_TIME AS OF`` (only the alias is left out). Spelled exactly: BigQuery dataset
    and table names are case-sensitive, so ``p.d.t`` and ``p.D.t`` are two tables."""

    rest = table.copy()
    for key in ("this", "db", "catalog", "alias"):
        rest.set(key, None)
    return (table.name, table.text("db"), table.text("catalog"), rest.sql())


def merge_same_source(node: exp.Expression) -> exp.Expression | None:
    if not isinstance(node, (exp.Union, exp.Intersect, exp.Except)) or not node.args.get("distinct", True):
        return None
    if not _plain_setop(node):
        return None  # ORDER BY, LIMIT, WITH or BY NAME on the set operation would be dropped by the merge
    if isinstance(node, exp.Union) and (node.find_ancestor(exp.SetOperation) is not None or any(isinstance(_sf_unwrap(o), exp.SetOperation) for o in (node.this, node.expression))):
        return None  # a chain of unions is flattened into one n-ary union instead, on both sides alike
    left, right = _sf_operand(node.this), _sf_operand(node.expression)
    if any(o is not None and distinct_on(o) for o in (left, right)):
        return None  # DISTINCT ON picks one row per key: a filter on it is not a filter on the table
    left, right = left and _sf_flatten(left), right and _sf_flatten(right)
    if left is None or right is None:
        return None
    a, b = _sf_single_table(left), _sf_single_table(right)
    if a is None or b is None:
        return None
    (a_table, a_alias, a_items, p), (b_table, b_alias, b_items, q) = a, b
    if _sf_table_identity(a_table) != _sf_table_identity(b_table) or len(a_items) != len(b_items):
        return None
    target = a_table.alias_or_name
    if [_sf_rename(_sf_value(i), a_alias, target).sql() for i in a_items] != [_sf_rename(_sf_value(i), b_alias, target).sql() for i in b_items]:
        return None
    q = _sf_rename(q, b_alias, target) if q is not None else None
    projected = {_sf_value(i).name.lower() for i in a_items if isinstance(_sf_value(i), exp.Column)}
    if isinstance(node, exp.Union):
        if p is None or q is None:
            condition = None
        else:
            condition = exp.Or(this=exp.Paren(this=p.copy()), expression=exp.Paren(this=q))
    elif isinstance(node, exp.Intersect):
        if not (_sf_reads_only(p, projected) or _sf_reads_only(q, projected)):
            return None
        parts = [exp.Paren(this=c.copy()) for c in (p, q) if c is not None]
        condition = exp.and_(*parts) if parts else None
    else:
        if not _sf_reads_only(q, projected):
            return None
        rejected = exp.false() if q is None else exp.Not(this=exp.Coalesce(this=exp.Paren(this=q), expressions=[exp.false()]))
        condition = exp.and_(exp.Paren(this=p.copy()), rejected) if p is not None else rejected
    merged = left.copy()
    merged.set("where", exp.Where(this=condition) if condition is not None else None)
    merged.set("distinct", exp.Distinct())
    return merged


def _sf_plain(select: exp.Select) -> bool:
    sources = [_sf_from(select)] + [j.this for j in select.args.get("joins") or []]
    return all(isinstance(s, exp.Table) for s in sources)


def _sf_names(select: exp.Select) -> list[str] | None:
    names = [item.alias_or_name.lower() for item in select.expressions]
    if any(not n or n == "*" for n in names) or len(set(names)) != len(names):
        return None
    return names


def set_operation_to_exists(node: exp.Expression) -> exp.Expression | None:
    if not isinstance(node, (exp.Intersect, exp.Except)) or not node.args.get("distinct", True) or not _plain_setop(node):
        return None
    left, right = _sf_operand(node.this), _sf_operand(node.expression)
    if left is None or right is None or not all(f is not None and _sf_plain(f) for f in (_sf_flatten(left), _sf_flatten(right))):
        return None
    if left.args.get("kind") or right.args.get("kind"):
        return None  # ``SELECT AS STRUCT a, b`` is one struct column; the test below would compare and return ``a`` and ``b``
    if is_empty(left) or is_empty(right):  # left to the empty-operand folding
        return None
    a, b = _sf_names(left), _sf_names(right)
    if a is None or b is None or len(a) != len(b):
        return None
    n = next(_sf_counter)
    outer, inner = f"kumosql_s{n}", f"kumosql_r{n}"
    match = exp.and_(*[exp.NullSafeEQ(this=exp.column(x, table=outer), expression=exp.column(y, table=inner)) for x, y in zip(a, b)])
    probe = exp.select("1").from_(exp.Subquery(this=right.copy(), alias=exp.TableAlias(this=exp.to_identifier(inner)))).where(match)
    test: exp.Expression = exp.Exists(this=probe)
    if isinstance(node, exp.Except):
        test = exp.Not(this=test)
    out = exp.select(*[exp.alias_(exp.column(x, table=outer), x) for x in a]).from_(
        exp.Subquery(this=left.copy(), alias=exp.TableAlias(this=exp.to_identifier(outer)))
    ).where(test)
    out.set("distinct", exp.Distinct())
    return out
