"""Union compensation: a model that covers part of a query's rows, plus the rest read from the base tables.

Plain reuse needs every row the query returns to be in the model. When the model keeps only the rows
that meet a predicate ``P_V`` (say ``k < 3``) and the query asks for ``P_Q`` (say ``k < 6``), the model
still holds part of the answer, and the rest comes from the base tables:

    Q  =  sigma_residual(V)  UNION ALL  sigma_(P_Q AND (P_V) IS NOT TRUE)(base)

``residual`` is what the query asks beyond the model's own join conditions and filters, read over the
model's columns. The two branches are disjoint and together cover ``P_Q``: ``(P_V) IS NOT TRUE`` is the
complement under SQL's three-valued logic (``NOT (P_V)`` would lose the rows where ``P_V`` is NULL).

* A model without grouping answers the rows of the query. A query that is a plain projection becomes the
  ``UNION ALL`` itself; any other query (``DISTINCT``, ``GROUP BY``, ``ORDER BY``) runs on top of the union
  of the columns it reads.
* A grouped model needs the residual to read only its group keys (so a filter keeps or drops whole groups),
  and the query's aggregates to be re-aggregable. Each branch yields partial aggregates by the query's
  grouping (the model's rows for one branch, an aggregate over the uncovered base rows for the other) and a
  final aggregation combines them: sums of sums and of counts, minimums, maximums, and an average from a sum
  and a count. A distinct aggregate, an average the model has no sum and count for, a model that drops groups
  with ``HAVING``, and ``ROLLUP``/``CUBE``/``GROUPING SETS`` are declined.

Only the proposer lives here: every replacement is proven by the algebraic prover before it is returned, and
:func:`~kumosql.model_reuse.rewrite_over_model` asks for it only when called with ``union_compensation=True``,
because the replacement reads base tables (a caller that needs the model to stand alone leaves it off).
"""

from __future__ import annotations

from typing import Iterator, Mapping, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.qualify import qualify

from .model_reuse import (
    _agg_available,
    _Block,
    _Candidate,
    _combine,
    _expand_conjuncts,
    _inner,
    _key,
    _mappings,
    _merge_grouped_derived,
    _Rewriter,
    _split_and,
)

_U = "u"  # the alias of the union of the two branches
_PARTIAL = (exp.Sum, exp.Count, exp.Min, exp.Max)


def flatten_branches(sql: str, schema: Mapping[str, Sequence[str]]) -> str:
    """``sql`` with the model's inlined definition merged into each branch of the union (a plain derived table
    over a base table becomes that table), so the prover sees branches that differ only by their filters.
    Returns ``sql`` unchanged when it cannot be resolved."""

    try:
        columns = {t: {c: "unknown" for c in cols} for t, cols in schema.items()}
        tree = qualify(sqlglot.parse_one(sql, read="postgres"), schema=columns, dialect="postgres", validate_qualify_columns=False, quote_identifiers=False, identify=False)
        tree = merge_subqueries(tree)
        for select in list(tree.find_all(exp.Select))[::-1]:  # innermost first
            merged = _merge_grouped_derived(select)  # a filter over a grouped model, as a filter of its groups
            if merged is not select:
                if select is tree:
                    tree = merged
                else:
                    select.replace(merged)
                select = merged
            _group_filters_to_where(select)
            _join_conditions_to_where(select)
        return tree.sql(dialect="postgres")
    except Exception:  # noqa: BLE001 - sqlglot's optimizer raises many types; the unflattened SQL is still valid
        return sql


def _group_filters_to_where(select: exp.Select) -> None:
    """Conjuncts of HAVING that read no aggregate filter rows before grouping as well as groups after it."""

    having = select.args.get("having")
    if having is None or not select.args.get("group"):
        return
    moved = [c for c in _split_and(having.this) if not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in c.walk())]
    kept = [c for c in _split_and(having.this) if not any(c is m for m in moved)]
    if not moved:
        return
    where = select.args.get("where")
    select.set("where", exp.Where(this=_combine([*([where.this] if where is not None else []), *moved])))
    select.set("having", exp.Having(this=_combine(kept)) if kept else None)


def _join_conditions_to_where(select: exp.Select) -> None:
    """``a JOIN b ON c WHERE w`` as ``a CROSS JOIN b WHERE c AND w`` (inner joins only), the form the base-table
    branch is written in, so the branches have the same shape and differ only by their filters."""

    joins = select.args.get("joins") or []
    if not joins or any(j.args.get("side") or j.args.get("using") or j.args.get("kind") not in (None, "INNER", "CROSS") for j in joins):
        return
    moved = [j.args["on"].this if isinstance(j.args["on"], exp.Paren) else j.args["on"] for j in joins if j.args.get("on") is not None]
    if not moved:
        return
    for join in joins:
        join.set("on", None)
        join.set("kind", "CROSS")
    where = select.args.get("where")
    conditions = [*moved, *([where.this] if where is not None else [])]
    select.set("where", exp.Where(this=_combine(conditions)))


def _order_keys(query: _Block) -> list[exp.Expression]:
    """The query's ORDER BY items with a bare output name replaced by the expression it names."""

    named = {item.alias: _inner(item) for item in query.outputs if isinstance(item, exp.Alias)}

    def visit(n):
        if isinstance(n, exp.Column) and not n.table and n.name in named:
            return named[n.name].copy()
        return n

    return [_with_this(item, item.this.copy().transform(visit)) for item in query.order]


def union_candidates(query: _Block, model: _Block, names: list[str], model_name: str, contained: bool = False) -> Iterator[_Candidate]:
    """Replacements that read ``model_name`` for the rows it covers and the base tables for the rest.

    With ``contained`` every row of the model is read as it is, so the proof needs the model's rows to be rows of the
    query (a model inside the query's range); otherwise the model's rows are filtered by what the query asks beyond it."""

    if getattr(query, "shape", None) is not None or getattr(model, "shape", None) is not None:
        return  # outer-join blocks carry no usable conjuncts
    if model.having is not None or model.distinct or (model.is_aggregate and not query.is_aggregate):
        return
    if any(isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets)) for g in [*query.group, *model.group]):
        return
    if len(model.tables) != len(query.tables):
        return
    for mapping in _mappings(model.tables, query.tables):  # query alias -> model alias
        yield from _for_mapping(query, model, names, model_name, mapping, contained)


def _for_mapping(query: _Block, model: _Block, names: list[str], model_name: str, mapping: dict[str, str], contained: bool = False) -> Iterator[_Candidate]:
    model_in_query = {m: q for q, m in mapping.items()}  # model alias -> query alias
    model_conj = [_Rewriter._rename(c.copy(), model_in_query) for c in _expand_conjuncts(model.conjuncts)]
    query_conj = _expand_conjuncts(query.conjuncts)
    model_keys, query_keys = {_key(c) for c in model_conj}, {_key(c) for c in query_conj}
    only_model: list[exp.Expression] = []
    for c in model_conj:
        if _key(c) not in query_keys and _key(c) not in {_key(o) for o in only_model}:
            only_model.append(c)
    if not only_model:
        return  # the model keeps every row the query reads: plain reuse covers it
    residual = [c for c in query_conj if _key(c) not in model_keys]

    renamed = _Block(
        [(model_in_query.get(a, a), t) for a, t in model.tables],
        model_conj,
        [_Rewriter._rename(o.copy(), model_in_query) for o in model.outputs],
        [_Rewriter._rename(g.copy(), model_in_query) for g in model.group],
        None,
        False,
    )
    rewriter = _Rewriter(renamed, names, model_name, {}, set())
    rewriter.learn_equalities(model_conj)
    # What the query asks beyond the model, read over the model's columns. A part the model cannot read is
    # dropped, and the prover then has to show that it holds on every row of the model already.
    over_model = [] if contained else [new for new in (rewriter.rewrite(r) for r in residual) if new is not None]

    def model_rows(select_items: list[exp.Expression]) -> exp.Select:
        select = exp.Select(expressions=select_items).from_(exp.to_table(model_name))
        if over_model:
            select.set("where", exp.Where(this=_combine(over_model)))
        return select

    def base_rows(select_items: list[exp.Expression]) -> exp.Select:
        select = exp.Select(expressions=select_items)
        first = True
        for alias, table in query.tables:
            relation = exp.Table(this=exp.to_identifier(table), alias=exp.TableAlias(this=exp.to_identifier(alias)))
            select = select.from_(relation) if first else select.join(relation, join_type="cross")
            first = False
        # P_V is true on a row exactly when it is not in the "IS NOT TRUE" complement; NOT (P_V) alone would drop NULLs
        in_model = exp.Is(this=exp.Paren(this=_combine(only_model)), expression=exp.Boolean(this=True))
        complement = exp.Not(this=exp.Paren(this=in_model))
        select.set("where", exp.Where(this=_combine([*query_conj, complement])))
        return select

    def union(left: exp.Select, right: exp.Select) -> exp.Union:
        return exp.Union(this=left, expression=right, distinct=False)

    if not model.is_aggregate:
        yield from _row_candidates(query, rewriter, model_rows, base_rows, union)
        return
    yield from _aggregate_candidates(query, model, renamed, names, model_name, rewriter, model_rows, base_rows, union)


# ---------------------------------------------------------------------------------------------
# a model of rows


def _row_candidates(query: _Block, rewriter: _Rewriter, model_rows, base_rows, union) -> Iterator[_Candidate]:
    order = _order_keys(query)
    simple = not (query.is_aggregate or query.distinct or order)
    if simple:
        view_items, base_items = [], []
        for item in query.outputs:
            new = rewriter.rewrite(item)
            if new is None:
                return
            alias = item.alias if isinstance(item, exp.Alias) else None
            view_items.append(exp.alias_(new, alias) if alias else new)
            base_items.append(_inner(item).copy() if not alias else exp.alias_(_inner(item).copy(), alias))
        yield _Candidate(union(model_rows(view_items), base_rows(base_items)).sql(dialect="postgres"), "union-rows", flatten=True)
        return
    # anything else runs on top of the union of the columns it reads
    roots = [*query.outputs, *query.group, *order] + ([query.having] if query.having is not None else [])
    columns: dict[str, exp.Column] = {}
    for root in roots:
        for column in root.find_all(exp.Column):
            columns.setdefault(_key(column), column)
    view_items, base_items, slot = [], [], {}
    for index, (key, column) in enumerate(columns.items()):
        new = rewriter.rewrite(column)
        if new is None:
            return
        slot[key] = f"c{index}"
        view_items.append(exp.alias_(new, slot[key]))
        base_items.append(exp.alias_(column.copy(), slot[key]))
    if not columns:  # COUNT(*) and the like read no column; the rows only have to be counted
        view_items, base_items = [exp.alias_(exp.Literal.number(1), "c0")], [exp.alias_(exp.Literal.number(1), "c0")]

    def over_union(node: exp.Expression) -> exp.Expression:
        def visit(n):
            if isinstance(n, exp.Column) and _key(n) in slot:
                return exp.column(slot[_key(n)], table=_U)
            return n

        return node.copy().transform(visit)

    outer = exp.Select(expressions=[exp.alias_(over_union(_inner(o)), o.alias) if isinstance(o, exp.Alias) else over_union(o) for o in query.outputs])
    outer = outer.from_(exp.Subquery(this=union(model_rows(view_items), base_rows(base_items)), alias=exp.TableAlias(this=exp.to_identifier(_U))))
    if query.distinct:
        outer.set("distinct", exp.Distinct())
    if query.group:
        outer.set("group", exp.Group(expressions=[over_union(g) for g in query.group]))
    if query.having is not None:
        outer.set("having", exp.Having(this=over_union(query.having)))
    if order:
        outer.set("order", exp.Order(expressions=[_with_this(o, over_union(o.this)) for o in order]))
    yield _Candidate(outer.sql(dialect="postgres"), "union-rows-regrouped" if query.is_aggregate else "union-rows-on-top", flatten=True)


def _with_this(item: exp.Expression, new: exp.Expression) -> exp.Expression:
    copy = item.copy()
    copy.set("this", new)
    return copy


# ---------------------------------------------------------------------------------------------
# a grouped model


def _aggregate_candidates(query, model, renamed, names, model_name, rewriter, model_rows, base_rows, union) -> Iterator[_Candidate]:
    if query.distinct:
        return
    model_aggs = _agg_available(renamed, names, {})

    group_exprs: list[exp.Expression] = []
    group_slot: dict[str, str] = {}
    for g in query.group:
        if _key(g) not in group_slot:
            group_slot[_key(g)] = f"g{len(group_exprs)}"
            group_exprs.append(g)
    view_groups = [rewriter.rewrite(g) for g in group_exprs]
    if any(g is None for g in view_groups):
        return  # the query groups by something the model's groups do not determine

    partials: list[tuple[exp.Expression, exp.Expression]] = []  # (over the model, over the base tables)
    partial_slot: dict[str, str] = {}

    def partial(base: exp.AggFunc) -> exp.Column | None:
        found = model_aggs.get(_key(base))
        if found is None:
            return None
        key = _key(base)
        if key not in partial_slot:
            partial_slot[key] = f"p{len(partials)}"
            partials.append((exp.column(found[0], table=model_name), base.copy()))
        return exp.column(partial_slot[key], table=_U)

    def combine(agg: exp.AggFunc) -> exp.Expression | None:
        if isinstance(agg.this, exp.Distinct) or isinstance(agg.parent, exp.Filter):
            return None
        if isinstance(agg, _PARTIAL):
            column = partial(agg)
            return (exp.Sum if isinstance(agg, exp.Count) else type(agg))(this=column) if column is not None else None
        if isinstance(agg, exp.Avg):
            total = partial(exp.Sum(this=agg.this.copy()))
            count = partial(exp.Count(this=agg.this.copy())) or partial(exp.Count(this=exp.Star()))
            if total is None or count is None:
                return None
            return exp.Div(this=exp.Sum(this=total), expression=exp.Nullif(this=exp.Sum(this=count.copy()), expression=exp.Literal.number(0)), typed=True)
        return None

    def visit(n: exp.Expression) -> exp.Expression | None:
        if isinstance(n, exp.Alias):
            return visit(n.this)
        if isinstance(n, exp.AggFunc):
            return combine(n)
        if _key(n) in group_slot:
            return exp.column(group_slot[_key(n)], table=_U)
        if isinstance(n, exp.Column):
            return None
        if isinstance(n, (exp.Literal, exp.Null, exp.Boolean)):
            return n.copy()
        copy = n.copy()
        for arg_name, value in n.args.items():
            if isinstance(value, exp.Expression):
                new = visit(value)
                if new is None:
                    return None
                copy.set(arg_name, new)
            elif isinstance(value, list):
                items = []
                for v in value:
                    new = visit(v) if isinstance(v, exp.Expression) else v
                    if new is None:
                        return None
                    items.append(new)
                copy.set(arg_name, items)
        return copy

    outputs = []
    for item in query.outputs:
        new = visit(_inner(item))
        if new is None:
            return
        outputs.append(exp.alias_(new, item.alias) if isinstance(item, exp.Alias) else new)
    having = visit(query.having) if query.having is not None else None
    if query.having is not None and having is None:
        return
    order = []
    for item in _order_keys(query):
        new = visit(item.this)
        if new is None:
            return
        order.append(_with_this(item, new))
    if not group_exprs and not partials:
        return

    view_items = [exp.alias_(g.copy(), group_slot[_key(o)]) for g, o in zip(view_groups, group_exprs)] + [exp.alias_(v, f"p{i}") for i, (v, _) in enumerate(partials)]
    base_items = [exp.alias_(g.copy(), group_slot[_key(g)]) for g in group_exprs] + [exp.alias_(b, f"p{i}") for i, (_, b) in enumerate(partials)]
    base = base_rows(base_items)
    if group_exprs:
        base.set("group", exp.Group(expressions=[g.copy() for g in group_exprs]))
    outer = exp.Select(expressions=outputs).from_(exp.Subquery(this=union(model_rows(view_items), base), alias=exp.TableAlias(this=exp.to_identifier(_U))))
    if group_exprs:
        outer.set("group", exp.Group(expressions=[exp.column(group_slot[_key(g)], table=_U) for g in group_exprs]))
    if having is not None:
        outer.set("having", exp.Having(this=having))
    if order:
        outer.set("order", exp.Order(expressions=order))
    yield _Candidate(outer.sql(dialect="postgres"), "union-aggregate", flatten=True)
