"""Unnest pre-aggregated tables (eager aggregation) into one flat aggregate.

SQLSolver proves aggregates over joins and nested queries by reducing the
counting of group members to linear integer arithmetic. The same identities,
applied as rewrites, cover the pre-aggregation shapes found in real
warehouses: a join of a grouped subquery ``D`` (rows ``(k, SUM(e), COUNT(*))``
per key ``k`` of a source ``S``) with other tables, finished by an outer
aggregate. Each row of ``D`` stands for all rows of ``S`` with its key, so

* ``SUM(D.s * w)`` is ``SUM(e * w)`` over the flat join of ``S`` with the rest
  (a sum of sums distributes over the common factor ``w``),
* ``SUM(D.c * w)`` with ``D.c = COUNT(*)`` is ``SUM(w)`` and ``SUM(D.c)`` is
  ``COUNT(*)``,
* ``MIN``/``MAX`` of ``D.m`` (itself a ``MIN``/``MAX``), and ``MIN``, ``MAX`` or a
  ``DISTINCT`` aggregate of anything fixed within a group (keys, other tables),
  do not see how many rows a group holds.

Anything else (an outer ``COUNT(*)`` or ``SUM`` of a value that does not carry a
group's size, a condition on an aggregate column, ``AVG``) is left alone: the
rewrite would change multiplicities. This module only rewrites; the SMT prover
proves the rewritten query. See ``unnest_grouped_source``.
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .ast_utils import conjuncts as _conjuncts

_counter = itertools.count()

_IDEMPOTENT = (exp.Min, exp.Max)
_JOIN_KINDS = {"", "INNER", "CROSS"}


def _from(select: exp.Select):
    return select.args.get("from_") or select.args.get("from")


def _plain(select: exp.Select, *, grouped: bool, allow_having: bool = False) -> bool:
    banned = ["distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with"]
    if not allow_having:
        banned.append("having")
    if not grouped:
        banned += ["group"]
    if any(select.args.get(key) for key in banned):
        return False
    group = select.args.get("group")
    if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False  # a grand-total row exists with no input rows: SUM of counts is NULL there, COUNT is 0
    return not any(select.find_all(exp.Window))


def _ancestors(node: exp.Expression):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _own(column: exp.Expression, select: exp.Select) -> bool:
    """Whether the column belongs to ``select`` itself rather than to a select nested inside it.

    A copied fragment (no select above it) counts as belonging to the select it was taken from.
    """

    owner = column.find_ancestor(exp.Select)
    return owner is None or owner is select


def _own_aggregates(select: exp.Select) -> list[exp.Expression]:
    """Aggregate calls of this select itself, not of the subqueries inside it."""

    return [node for node in select.find_all(exp.AggFunc) if node.find_ancestor(exp.Select) is select]


def _factors(node: exp.Expression) -> list[exp.Expression]:
    """The factors of a product, looking through parentheses."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Mul):
        return _factors(node.this) + _factors(node.expression)
    return [node]


def _product(factors: list[exp.Expression]) -> exp.Expression | None:
    result = None
    for factor in factors:
        factor = factor.copy()
        result = factor if result is None else exp.Mul(this=result, expression=factor)
    return result


class _Grouped:
    """A grouped derived table whose outputs are its keys and plain aggregates."""

    def __init__(self, source: exp.Subquery):
        self.alias = source.alias
        self.select: exp.Select = source.this
        self.keys: dict[str, exp.Expression] = {}
        self.aggs: dict[str, exp.Expression] = {}
        # every grouping key is an output: a GROUP BY key the select hides (``GROUP BY k, j``
        # showing only ``k``) still splits groups, so ``keys`` alone does not identify a row
        self.complete = False
        self.ok = self._read()

    def _read(self) -> bool:
        select = self.select
        if not isinstance(select, exp.Select) or not self.alias or not _plain(select, grouped=True):
            return False
        group = select.args.get("group")
        if not group or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return False
        group_sql = {key.sql() for key in group.expressions}
        for item in select.expressions:
            expr = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name
            if not name or name.lower() in self.keys or name.lower() in self.aggs:
                return False
            if isinstance(expr, (exp.Sum, exp.Count, exp.Min, exp.Max)):
                if expr.args.get("distinct") or isinstance(expr.this, exp.Distinct):
                    return False
                if any(expr.this.find_all(exp.AggFunc)) if expr.this is not None else False:
                    return False
                self.aggs[name.lower()] = expr
            elif expr.sql() in group_sql and not any(expr.find_all(exp.AggFunc)):
                self.keys[name.lower()] = expr
            else:
                return False
        shown = {expr.sql() for expr in self.keys.values()}
        self.complete = all(key.sql() in shown for key in group.expressions)
        return True


def _count_star(agg: exp.Expression) -> bool:
    return isinstance(agg, exp.Count) and (agg.this is None or isinstance(agg.this, exp.Star))


def _sources(select: exp.Select) -> list[exp.Expression] | None:
    from_ = _from(select)
    if from_ is None:
        return None
    items = [from_.this]
    for join in select.args.get("joins") or []:
        kind = (join.args.get("kind") or "").upper()
        if (
            join.args.get("side")
            or kind not in _JOIN_KINDS
            or join.args.get("using") is not None
            or join.args.get("method")
            or join.args.get("global_")
        ):
            return None
        items.append(join.this)
    return items


def _inline(grouped: _Grouped) -> tuple[list[exp.Expression], list[exp.Expression], dict[str, str], str] | None:
    """The sources and conditions of the grouped table's select, under fresh aliases.

    Returns ``(items, conditions, alias map, single alias)``; ``None`` when a name
    cannot be told apart safely.
    """

    inner = grouped.select.copy()
    items = _sources(inner)
    if items is None:
        return None
    if any(inner.find_all(exp.Exists)) or any(
        isinstance(node, exp.Subquery) and node not in items for node in inner.find_all(exp.Subquery)
    ):
        return None
    mapping: dict[str, str] = {}
    for item in items:
        if isinstance(item, exp.Table):
            if item.args.get("joins") or item.args.get("pivots") or item.args.get("laterals"):
                return None
        elif not isinstance(item, exp.Subquery):
            return None
        old = item.alias_or_name
        if not old or old.lower() in mapping:
            return None
        mapping[old.lower()] = f"kumosql_s{next(_counter)}_{old}"
    single = next(iter(mapping.values())) if len(mapping) == 1 else None
    for column in list(inner.find_all(exp.Column)):
        if not _own(column, inner):
            continue  # inside a derived source of the grouped table: its own scope
        table = column.table.lower()
        if table:
            if table not in mapping:
                return None
            column.set("table", exp.to_identifier(mapping[table]))
        elif single is not None:
            column.set("table", exp.to_identifier(single))
        else:
            return None
    conditions: list[exp.Expression] = []
    if inner.args.get("where") is not None:
        conditions.append(inner.args["where"].this)
    for join in inner.args.get("joins") or []:
        if join.args.get("on") is not None:
            conditions.append(join.args["on"])
    new_items = []
    for item in items:
        old = item.alias_or_name.lower()
        item.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[old])))
        if isinstance(item, exp.Table):
            item.set("joins", None)
        new_items.append(item)
    return new_items, conditions, mapping, single or ""


def _zero_when_empty(call: exp.Expression) -> bool:
    """``COALESCE(call, 0)`` or ``NULLIF(call, 0)``: the NULL a SUM gives over no rows and the 0 a COUNT gives read
    the same."""

    def zero(node: exp.Expression | None) -> bool:
        return isinstance(node, exp.Literal) and not node.is_string and node.this == "0"

    parent = call.parent
    if isinstance(parent, exp.Nullif):
        return parent.this is call and zero(parent.expression)
    return isinstance(parent, exp.Coalesce) and parent.this is call and len(parent.expressions) == 1 and zero(parent.expressions[0])


def unnest_grouped_source(select: exp.Select) -> exp.Expression | None:
    """Rewrite an aggregate over a join with a grouped derived table into the flat aggregate."""

    if not _plain(select, grouped=True, allow_having=True):
        return None
    if not (select.args.get("group") or _own_aggregates(select)):
        return None
    items = _sources(select)
    if items is None:
        return None
    # Unqualified columns could change meaning once the grouped table's sources join in.
    for column in select.find_all(exp.Column):
        if not column.table and _own(column, select):
            return None
    chosen = None
    for position, item in enumerate(items):
        if isinstance(item, exp.Subquery):
            grouped = _Grouped(item)
            if grouped.ok:
                chosen = (position, grouped)
                break
    if chosen is None:
        return None
    position, grouped = chosen
    alias = grouped.alias.lower()

    if any(isinstance(node, exp.Subquery) and node not in items for node in select.find_all(exp.Subquery)) or any(
        select.find_all(exp.Exists)
    ):
        return None

    def refs(node: exp.Expression) -> list[exp.Column]:
        return [c for c in node.find_all(exp.Column) if c.table.lower() == alias and _own(c, select)]

    inlined = _inline(grouped)
    if inlined is None:
        return None
    new_items, conditions, mapping, single = inlined
    key_exprs = _renamed_keys(grouped, mapping, single)
    if key_exprs is None:
        return None

    def renamed_arg(agg: exp.Expression) -> exp.Expression | None:
        return _rename(agg.this, mapping, single) if agg.this is not None else None

    # Aggregates of the outer select (not inside the grouped table's own body).
    outer_calls = [
        node
        for node in select.find_all(exp.AggFunc)
        if node.find_ancestor(exp.Select) is select
    ]
    replacements: list[tuple[exp.Expression, exp.Expression]] = []
    for call in outer_calls:
        agg_refs = [c for c in refs(call) if c.name.lower() in grouped.aggs]
        if isinstance(call, _IDEMPOTENT) and not agg_refs:
            continue
        if isinstance(call.this, exp.Distinct) or call.args.get("distinct"):
            if not agg_refs:
                continue
            return None
        if isinstance(call, _IDEMPOTENT):
            # MIN/MAX of the group's own MIN/MAX.
            arg = call.this
            if (
                isinstance(arg, exp.Column)
                and arg.table.lower() == alias
                and type(grouped.aggs.get(arg.name.lower())) is type(call)
            ):
                inner_arg = renamed_arg(grouped.aggs[arg.name.lower()])
                if inner_arg is None:
                    return None
                replacements.append((call, type(call)(this=inner_arg)))
                continue
            return None
        if not isinstance(call, exp.Sum) or len(agg_refs) != 1:
            return None
        factors = _factors(call.this)
        hits = [f for f in factors if isinstance(f, exp.Column) and f.table.lower() == alias and f.name.lower() in grouped.aggs]
        if len(hits) != 1:
            return None
        rest = [f for f in factors if f is not hits[0]]
        if any(c.name.lower() in grouped.aggs for f in rest for c in refs(f)):
            return None
        agg = grouped.aggs[hits[0].name.lower()]
        weight = _product(rest)
        if isinstance(agg, exp.Sum):
            inner_arg = renamed_arg(agg)
            if inner_arg is None:
                return None
            term = inner_arg if weight is None else exp.Mul(this=exp.Paren(this=inner_arg), expression=weight)
            replacements.append((call, exp.Sum(this=term)))
        elif _count_star(agg):
            if weight is None and not select.args.get("group") and not _zero_when_empty(call):
                return None  # a global SUM over no rows is NULL where COUNT(*) is 0
            replacements.append((call, exp.Count(this=exp.Star()) if weight is None else exp.Sum(this=weight)))
        elif isinstance(agg, exp.Count) and agg.this is not None and not isinstance(agg.this, (exp.Star, exp.Distinct)) and not agg.args.get("distinct"):
            # SUM of COUNT(x) counts the rows where x is not NULL; a weight counts only those rows.
            inner_arg = renamed_arg(agg)
            if inner_arg is None:
                return None
            if weight is None and not select.args.get("group") and not _zero_when_empty(call):
                return None  # as above: a global SUM over no rows is NULL, COUNT(x) is 0
            if weight is None:
                replacements.append((call, exp.Count(this=inner_arg)))
            else:
                # a row whose x is NULL adds 0 * w: c * w is 0 (not NULL) for a group of NULLs, NULL only for a NULL w
                present = exp.Case(
                    ifs=[exp.If(this=exp.Not(this=exp.Is(this=inner_arg.copy(), expression=exp.Null())), true=weight.copy())],
                    default=exp.Mul(this=exp.Literal.number(0), expression=exp.Paren(this=weight.copy())),
                )
                replacements.append((call, exp.Sum(this=present)))
        else:
            return None
    # No reference to an aggregate column may survive outside the replaced calls.
    replaced = {id(call) for call, _ in replacements}
    for column in refs(select):
        if column.name.lower() in grouped.aggs:
            if not any(id(a) in replaced for a in _ancestors(column)):
                return None
        elif column.name.lower() not in grouped.keys:
            return None
    # Every other aggregate must not care how many rows a group holds.
    for call in outer_calls:
        if id(call) in replaced:
            continue
        if not (isinstance(call, _IDEMPOTENT) or isinstance(call.this, exp.Distinct) or call.args.get("distinct")):
            return None

    result = select.copy()
    result_items = _sources(result)
    # Locate the copy of the chosen source in the copy by position.
    result_calls = [
        node for node in result.find_all(exp.AggFunc) if node.find_ancestor(exp.Select) is result
    ]
    position_of = {id(call): index for index, call in enumerate(outer_calls)}  # identity: equal calls are distinct nodes
    for call, new in replacements:
        result_calls[position_of[id(call)]].replace(new.copy())
    for column in list(result.find_all(exp.Column)):
        if column.table.lower() == alias and column.name.lower() in key_exprs and column.find_ancestor(exp.Select) is result:
            column.replace(key_exprs[column.name.lower()].copy())
    # Rebuild the FROM list as plain comma-joined sources with every condition in WHERE.
    all_items: list[exp.Expression] = []
    conds: list[exp.Expression] = []
    for index, item in enumerate(items):
        if index == position:
            all_items.extend(i.copy() for i in new_items)
            conds.extend(c.copy() for c in conditions)
        else:
            all_items.append(result_items[index].copy())
    for join in result.args.get("joins") or []:
        if join.args.get("on") is not None:
            conds.append(join.args["on"].copy())
    if result.args.get("where") is not None:
        conds.append(result.args["where"].this.copy())
    result.set("from_", exp.From(this=all_items[0]))
    result.set("joins", [exp.Join(this=i) for i in all_items[1:]])
    condition = None
    for c in conds:
        condition = c if condition is None else exp.And(this=condition, expression=c)
    result.set("where", exp.Where(this=condition) if condition is not None else None)
    return result


def _rename(expr: exp.Expression, mapping: dict[str, str], single: str) -> exp.Expression | None:
    """A copy of ``expr`` (written over the grouped table's sources) over the renamed sources."""

    expr = expr.copy()
    for column in list(expr.find_all(exp.Column)):
        table = column.table.lower()
        if table:
            if table not in mapping:
                return None
            column.set("table", exp.to_identifier(mapping[table]))
        elif single:
            column.set("table", exp.to_identifier(single))
        else:
            return None
    return expr


def _renamed_keys(grouped: _Grouped, mapping: dict[str, str], single: str) -> dict[str, exp.Expression] | None:
    keys = {}
    for name, expr in grouped.keys.items():
        renamed = _rename(expr, mapping, single)
        if renamed is None:
            return None
        keys[name] = renamed
    return keys


def flatten_grouped_join(select: exp.Select) -> exp.Expression | None:
    """A join of grouped subqueries, read off by arithmetic, as one aggregate over the flat join.

    ``SELECT p.k, p.s * q.c FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k) p
    JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) q ON p.k = q.k`` is
    ``SELECT a.k, SUM(a.x) FROM a JOIN b ON a.k = b.k GROUP BY a.k``: a group
    of the flat join is the product of one group from each side, so its sum is
    the product of one side's sum and the other side's count. Every source must
    be a grouped subquery and every condition may mention keys only; each
    output is a key, a MIN/MAX, or a SUM/COUNT times the COUNT(*) of every other
    aggregated source. A subquery with no aggregates (a set of keys) is kept as
    a source and counts once.
    """

    if not _plain(select, grouped=False) or select.args.get("group"):
        return None
    if _own_aggregates(select) or select.args.get("distinct"):
        return None
    items = _sources(select)
    if items is None or len(items) < 2:
        return None
    for column in select.find_all(exp.Column):
        if not column.table and _own(column, select):
            return None
    if any(isinstance(node, exp.Subquery) and node not in items for node in select.find_all(exp.Subquery)) or any(
        select.find_all(exp.Exists)
    ):
        return None
    groups: list[_Grouped] = []
    for item in items:
        if not isinstance(item, exp.Subquery):
            return None
        grouped = _Grouped(item)
        if not grouped.ok or not grouped.complete:
            return None
        groups.append(grouped)
    aggregated = [g for g in groups if g.aggs]
    if not aggregated:
        return None
    by_alias = {g.alias.lower(): g for g in groups}
    if len(by_alias) != len(groups):
        return None

    def ref(column: exp.Column):
        """``(group, name, kind)`` for a column of one of the grouped sources."""

        if not _own(column, select):
            return None
        group = by_alias.get(column.table.lower())
        if group is None:
            return None
        name = column.name.lower()
        if name in group.aggs:
            return group, name, "agg"
        if name in group.keys:
            return group, name, "key"
        return None

    # Conditions mention keys only.
    conditions: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        if join.args.get("on") is not None:
            conditions.extend(_conjuncts(join.args["on"]))
    if select.args.get("where") is not None:
        conditions.extend(_conjuncts(select.args["where"].this))
    for condition in conditions:
        if any(any(find is None or find[2] != "key" for find in [ref(c)]) for c in condition.find_all(exp.Column)):
            return None

    # Inline the aggregated sources (fresh aliases); set-like sources stay as they are.
    inlined: dict[str, tuple] = {}
    flat_items: list[exp.Expression] = []
    flat_conditions: list[exp.Expression] = []
    renamed_keys: dict[str, dict[str, exp.Expression]] = {}
    for group in groups:
        key = group.alias.lower()
        if group.aggs:
            result = _inline(group)
            if result is None:
                return None
            new_items, source_conditions, mapping, single = result
            keys = _renamed_keys(group, mapping, single)
            if keys is None:
                return None
            inlined[key] = (mapping, single)
            renamed_keys[key] = keys
            flat_items.extend(new_items)
            flat_conditions.extend(source_conditions)
        else:
            renamed_keys[key] = {
                name: exp.column(name, table=group.alias) for name in group.keys
            }
            flat_items.append(next(i for i in items if i.alias.lower() == key).copy())

    def key_expr(column: exp.Column) -> exp.Expression:
        found = ref(column)
        return renamed_keys[found[0].alias.lower()][found[1]].copy()

    def substitute_keys(node: exp.Expression) -> exp.Expression:
        node = node.copy()
        for column in list(node.find_all(exp.Column)):
            if ref(column) is not None:
                replacement = key_expr(column)
                if column is node:
                    node = replacement
                else:
                    column.replace(replacement)
        return node

    flat_conditions.extend(substitute_keys(c) for c in conditions)

    def term(factors: list[exp.Expression]) -> exp.Expression | None:
        main = None
        others = []
        weights = []
        for factor in factors:
            found = ref(factor) if isinstance(factor, exp.Column) else None
            if found is not None and found[2] == "agg":
                agg = found[0].aggs[found[1]]
                if _count_star(agg):
                    others.append(found[0])
                else:
                    if main is not None:
                        return None
                    main = (found[0], agg)
            else:
                if any((ref(c) or (None, None, "key"))[2] == "agg" for c in factor.find_all(exp.Column)):
                    return None
                weights.append(factor)
        if main is None:
            # Only counts: the first one is the main term.
            if not others:
                return None
            first, others = others[0], others[1:]
            main = (first, first.aggs[next(n for n, a in first.aggs.items() if _count_star(a))])
            for i, g in enumerate(others):
                pass
        group, agg = main
        # Every other aggregated source must contribute its COUNT(*) exactly once.
        needed = {id(g) for g in aggregated if g is not group}
        have = [id(g) for g in others]
        if isinstance(agg, (exp.Min, exp.Max)):
            if have:
                return None
        elif sorted(have) != sorted(needed) or len(set(have)) != len(have):
            return None
        mapping, single = inlined[group.alias.lower()]
        if _count_star(agg):
            flat = exp.Count(this=exp.Star())
        else:
            argument = _rename(agg.this, mapping, single)
            if argument is None:
                return None
            flat = type(agg)(this=argument)
        if weights:
            weight = _product([substitute_keys(w) for w in weights])
            flat = exp.Mul(this=flat, expression=exp.Paren(this=weight))
        return flat

    def convert(node: exp.Expression) -> exp.Expression | None:
        """Rewrite an output expression; ``None`` when it is not of a supported shape."""

        if isinstance(node, exp.Column):
            found = ref(node)
            if found is None:
                return None
            if found[2] == "key":
                return key_expr(node)
            return term([node])
        if isinstance(node, (exp.Mul, exp.Paren)) and any(
            (ref(c) or (None, None, "key"))[2] == "agg" for c in node.find_all(exp.Column)
        ):
            if isinstance(node, exp.Paren):
                inner = convert(node.this)
                return None if inner is None else exp.Paren(this=inner)
            return term(_factors(node))
        if not any((ref(c) or (None, None, ""))[2] == "agg" for c in node.find_all(exp.Column)):
            if any(ref(c) is None for c in node.find_all(exp.Column)):
                return None
            return substitute_keys(node)
        # An operator over terms: convert each child.
        copy = node.copy()
        for name, child in list(copy.args.items()):
            if isinstance(child, exp.Expression):
                new = convert(child)
                if new is None:
                    return None
                copy.set(name, new)
            elif isinstance(child, list):
                converted = []
                for element in child:
                    if isinstance(element, exp.Expression):
                        new = convert(element)
                        if new is None:
                            return None
                        converted.append(new)
                    else:
                        converted.append(element)
                copy.set(name, converted)
        return copy

    outputs = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        if isinstance(expr, exp.Star) or (isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star)):
            return None
        converted = convert(expr)
        if converted is None:
            return None
        name = item.alias or (item.name if isinstance(item, exp.Column) else "")
        outputs.append(exp.alias_(converted, name) if name else converted)

    group_by = []
    for group in groups:
        group_by.extend(e.copy() for e in renamed_keys[group.alias.lower()].values())
    result = exp.Select(expressions=outputs)
    result.set("from_", exp.From(this=flat_items[0]))
    result.set("joins", [exp.Join(this=i) for i in flat_items[1:]])
    condition = None
    for c in flat_conditions:
        condition = c if condition is None else exp.And(this=condition, expression=c)
    result.set("where", exp.Where(this=condition) if condition is not None else None)
    result.set("group", exp.Group(expressions=group_by))
    return result


def pull_up_aggregate(select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """A keyed table joined to a pre-aggregated one is the aggregate over the flat join.

    ``SELECT c.name, g.s FROM customers c JOIN (SELECT k, SUM(x) AS s FROM orders GROUP BY k) g ON c.id = g.k``
    with ``id`` a declared key of ``customers`` is
    ``SELECT c.name, SUM(o.x) FROM customers c JOIN orders o ON c.id = o.k GROUP BY c.id, c.name``:
    each ``customers`` row is its own group, and it meets exactly the rows of ``orders`` its
    pre-aggregated row summarized. Only inner joins, one keyed table and one grouped table.
    """

    if not keys or not _plain(select, grouped=False) or select.args.get("group") or select.args.get("order"):
        return None
    if _own_aggregates(select):
        return None
    items = _sources(select)
    if items is None or len(items) != 2:
        return None
    tables = [i for i in items if isinstance(i, exp.Table) and i.alias_or_name]
    derived = [i for i in items if isinstance(i, exp.Subquery)]
    if len(tables) != 1 or len(derived) != 1:
        return None
    table, source = tables[0], derived[0]
    declared = keys.get(".".join(p.name for p in table.parts).lower()) or []
    if not declared or table.args.get("joins") or table.args.get("pivots") or table.args.get("laterals"):
        return None
    if any(isinstance(n, exp.Subquery) and n is not source for n in select.find_all(exp.Subquery)) or any(select.find_all(exp.Exists)):
        return None
    grouped = _Grouped(source)
    if not grouped.ok or not grouped.complete or not grouped.aggs or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
        return None
    t_alias, g_alias = table.alias_or_name.lower(), grouped.alias.lower()
    if t_alias == g_alias:
        return None
    # Every column is qualified (an unqualified one could belong to either side).
    own = [c for c in select.find_all(exp.Column) if _own(c, select)]
    if any(not c.table or c.table.lower() not in (t_alias, g_alias) for c in own):
        return None
    if any(c.table.lower() == g_alias and c.name.lower() not in grouped.keys and c.name.lower() not in grouped.aggs for c in own):
        return None
    conditions: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        if join.args.get("on") is not None:
            conditions.extend(_conjuncts(join.args["on"]))
    if select.args.get("where") is not None:
        conditions.extend(_conjuncts(select.args["where"].this))
    # One equality per group key, tying it to a column of the keyed table.
    tie: dict[str, exp.Expression] = {}
    rest: list[exp.Expression] = []
    for condition in conditions:
        if isinstance(condition, exp.EQ):
            a, b = condition.this, condition.expression
            for g_side, t_side in ((a, b), (b, a)):
                if (
                    isinstance(g_side, exp.Column)
                    and g_side.table.lower() == g_alias
                    and g_side.name.lower() in grouped.keys
                    and g_side.name.lower() not in tie
                    and isinstance(t_side, exp.Column)
                    and t_side.table.lower() == t_alias
                ):
                    tie[g_side.name.lower()] = t_side
                    break
            else:
                rest.append(condition)
            continue
        rest.append(condition)
    if set(tie) != set(grouped.keys):
        return None
    if not any(set(k) <= {c.name.lower() for c in own if c.table.lower() == t_alias} | {t.name.lower() for t in tie.values()} for k in declared) and not any(
        set(k) <= {t.name.lower() for t in tie.values()} for k in declared
    ):
        pass  # the key columns are added to the grouping below, whether or not the query mentions them
    inlined = _inline(grouped)
    if inlined is None:
        return None
    new_items, source_conditions, mapping, single = inlined
    key_exprs = _renamed_keys(grouped, mapping, single)
    if key_exprs is None:
        return None
    # Conditions that read an aggregate column become HAVING; the others filter rows.
    where_parts: list[exp.Expression] = [c.copy() for c in source_conditions]
    having_parts: list[exp.Expression] = []
    for name, t_column in tie.items():
        where_parts.append(exp.EQ(this=t_column.copy(), expression=key_exprs[name].copy()))

    def convert(node: exp.Expression) -> exp.Expression | None:
        """Read the grouped table's columns off the flat join."""

        node = node.copy()
        holder = exp.Select(expressions=[node])
        for column in list(holder.find_all(exp.Column)):
            if column.table.lower() != g_alias:
                continue
            name = column.name.lower()
            if name in tie:
                replacement = tie[name].copy()
            else:
                inner_arg = _rename(grouped.aggs[name], mapping, single)
                if inner_arg is None:
                    return None
                replacement = inner_arg
            if column is node:
                node = replacement
                holder.set("expressions", [node])
            else:
                column.replace(exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) else replacement)
        return holder.expressions[0]

    for condition in rest:
        reads_aggregate = any(c.table.lower() == g_alias and c.name.lower() in grouped.aggs for c in condition.find_all(exp.Column))
        converted = convert(condition)
        if converted is None:
            return None
        (having_parts if reads_aggregate else where_parts).append(converted)
    outputs = []
    for item in select.expressions:
        value = convert(item.this if isinstance(item, exp.Alias) else item)
        if value is None:
            return None
        name = item.alias_or_name
        outputs.append(exp.alias_(value, name) if name and not (isinstance(value, exp.Column) and value.name == name) else value)
    key_columns = sorted({c for k in declared[:1] for c in k})
    group_by = [exp.column(c, table=table.alias_or_name) for c in key_columns]
    seen = {g.sql().lower() for g in group_by}
    for column in own:
        if column.table.lower() == t_alias and column.sql().lower() not in seen:
            seen.add(column.sql().lower())
            group_by.append(column.copy())
    result = exp.Select(expressions=outputs)
    result.set("from_", exp.From(this=table.copy()))
    result.set("joins", [exp.Join(this=i) for i in new_items])
    result.set("where", exp.Where(this=_product_and(where_parts)) if where_parts else None)
    result.set("group", exp.Group(expressions=group_by))
    if having_parts:
        result.set("having", exp.Having(this=_product_and(having_parts)))
    return result


def _product_and(parts: list[exp.Expression]) -> exp.Expression:
    result = None
    for part in parts:
        result = part if result is None else exp.And(this=result, expression=part)
    return result
