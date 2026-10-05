"""Re-aggregation over a partitioned ``UNION ALL`` of grouped selects is one aggregate over the whole.

    SELECT u.k, SUM(u.p) FROM (SELECT k, SUM(x) AS p FROM t WHERE c GROUP BY k
                               UNION ALL
                               SELECT k, SUM(x) AS p FROM t WHERE NOT (c IS TRUE) GROUP BY k) AS u GROUP BY u.k

is ``SELECT k, SUM(x) FROM t GROUP BY k``. Each branch splits the rows of one query by a filter and
aggregates its share; the outer select combines the shares group by group. Aggregating shares of rows
and combining them equals aggregating all the rows, for the aggregates that combine: ``SUM`` of sums,
``SUM`` of counts, ``MIN`` of minimums and ``MAX`` of maximums (a ``SUM`` whose values are all NULL is NULL either way).

The rule reverses the split of ``algebraic_equivalence._split_aggregates``, so it fires only when the
branches turn out to be one query split into disjoint filters (:mod:`kumosql.partition_rules` merges
them); any other aggregate over a ``UNION ALL`` is left alone, in the split form the other rules reach.
It reads one shape:

* the outer select has one derived ``UNION ALL`` as its only source, optionally ``GROUP BY`` columns of
  it, and outputs that are those columns or ``SUM``/``MIN``/``MAX`` of a column of it (``HAVING`` and
  ``DISTINCT`` are left alone);
* every branch is a plain select over the same tables and joins (they differ only by ``WHERE``), either
  grouped, outputting grouping expressions and ``SUM``/``COUNT``/``MIN``/``MAX`` calls, or a global aggregate;
* each column the outer select reads is a grouping expression in every branch, or the same aggregate in every
  branch (``COUNT`` read by an outer ``SUM``; ``SUM``, ``MIN`` and ``MAX`` read by the same function).

Without an outer ``GROUP BY`` a ``COUNT`` is recombined only when some branch is a global aggregate
(which always yields a row): the sum of no counts is NULL, while the count of no rows is 0.
"""

from __future__ import annotations

from sqlglot import exp

from .partition_rules import _BRANCH_ARGS, _MAX_BRANCHES, _all_union, _leaves, _merge_partitioned_union, _shape, _union_all, _unwrap, _volatile

_COMBINES = {exp.Sum: exp.Sum, exp.Min: exp.Min, exp.Max: exp.Max, exp.Count: exp.Sum}  # inner aggregate -> outer function
_PLAIN_ARGS = _BRANCH_ARGS | {"group"}


def unsplit_grouped_partitions(tree: exp.Expression) -> exp.Expression:
    """Apply the rule everywhere in ``tree``."""

    return tree.transform(_unsplit)


class _Leaf:
    """One branch: its source (everything but the select list, WHERE and GROUP BY) and what each output is."""

    def __init__(self, select: exp.Select, kinds: list[tuple[str, exp.Expression]], grouped: bool):
        self.select = select
        self.kinds = kinds  # ("key", expression) or ("agg", the aggregate call), per output
        self.grouped = grouped
        rest = select.copy()
        for arg in ("expressions", "where", "group"):
            rest.set(arg, None)
        self.source = _shape(rest)


def _read_leaf(select: exp.Expression) -> _Leaf | None:
    if not isinstance(select, exp.Select) or any(v for k, v in select.args.items() if k not in _PLAIN_ARGS) or _volatile(select):
        return None
    group = select.args.get("group")
    keys: list[exp.Expression] = []
    if group is not None:
        if any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "totals")):
            return None
        keys = list(group.expressions)
        if any(isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)) for g in keys):
            return None
    key_shapes = {_shape(g) for g in keys}
    kinds: list[tuple[str, exp.Expression]] = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if isinstance(value, exp.AggFunc):
            if type(value) not in _COMBINES or value.args.get("expressions") or isinstance(value.this, exp.Distinct) or isinstance(value.parent, exp.Filter):
                return None
            if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in value.this.walk()):
                return None
            if isinstance(value.this, exp.Star) and not isinstance(value, exp.Count):
                return None
            kinds.append(("agg", value))
        elif group is not None and _shape(value) in key_shapes and not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in value.walk()):
            kinds.append(("key", value))
        else:
            return None
    return _Leaf(select, kinds, group is not None)


def _unsplit(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.Select) or any(v for k, v in node.args.items() if k not in ("expressions", "from", "from_", "group")):
        return node
    source = node.args.get("from_") or node.args.get("from")
    derived = source.this if source is not None else None
    if not isinstance(derived, exp.Subquery) or not derived.alias or derived.args["alias"].args.get("columns") or any(derived.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample")):
        return node
    union = _unwrap(derived.this)
    if not _all_union(union):
        return node
    leaves: list[exp.Expression] = []
    _leaves(union, leaves)
    if len(leaves) > _MAX_BRANCHES:
        return node
    read = [_read_leaf(leaf) for leaf in leaves]
    if any(r is None for r in read) or len({len(r.kinds) for r in read}) != 1 or len({r.source for r in read}) != 1:
        return node
    names = [(item.alias_or_name or "").lower() for item in leaves[0].expressions]
    if len(set(names)) != len(names) or "" in names:
        return node
    width = len(names)
    # what each position is, the same in every branch
    positions: list[tuple[str, type | None]] = []
    for j in range(width):
        kinds = {r.kinds[j][0] for r in read}
        if kinds == {"key"} and len({_shape(r.kinds[j][1]) for r in read}) == 1:
            positions.append(("key", None))
        elif kinds == {"agg"} and len({type(r.kinds[j][1]) for r in read}) == 1:
            positions.append(("agg", type(read[0].kinds[j][1])))
        else:
            positions.append(("other", None))
    alias = derived.alias.lower()

    def column_position(column: exp.Expression) -> int | None:
        if not isinstance(column, exp.Column) or (column.table or "").lower() not in ("", alias) or column.name.lower() not in names:
            return None
        return names.index(column.name.lower())

    used: set[int] = set()
    converted_count = [False]
    failed = [False]

    def rewrite(item: exp.Expression) -> exp.Expression:
        def visit(n: exp.Expression) -> exp.Expression:
            if isinstance(n, exp.AggFunc):
                if type(n) not in (exp.Sum, exp.Min, exp.Max) or n.args.get("expressions") or isinstance(n.parent, exp.Filter):
                    failed[0] = True
                    return n
                j = column_position(n.this)
                if j is None or positions[j][0] != "agg" or _COMBINES[positions[j][1]] is not type(n):
                    failed[0] = True
                    return n
                used.add(j)
                converted_count[0] = converted_count[0] or positions[j][1] is exp.Count
                function = exp.Count if positions[j][1] is exp.Count else type(n)
                return function(this=exp.column(f"_c{j}", table=alias))
            if isinstance(n, exp.Column):
                j = column_position(n)
                if j is None or positions[j][0] != "key":
                    failed[0] = True
                    return n
                used.add(j)
                return exp.column(f"_c{j}", table=alias)
            if isinstance(n, (exp.Subquery, exp.Window, exp.Select)):
                failed[0] = True
            return n

        return item.copy().transform(visit)

    items = [rewrite(item) for item in node.expressions]
    group = node.args.get("group")
    outer_group = []
    if group is not None:
        if any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "totals")) or any(isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)) for g in group.expressions):
            return node
        outer_group = [rewrite(g) for g in group.expressions]
    if failed[0] or not used:
        return node
    if group is None and converted_count[0] and not any(not r.grouped for r in read):
        return node  # the sum of no counts is NULL, the count of no rows is 0
    raw = []
    for r in read:
        branch = r.select.copy()
        branch.set("group", None)
        branch.set(
            "expressions",
            [
                exp.alias_(
                    (exp.Literal.number(1) if isinstance(call.this, exp.Star) else call.this.copy()) if kind == "agg" else expression.copy(),
                    f"_c{j}",
                )
                for j in sorted(used)
                for kind, call in [r.kinds[j]]
                for expression in [call]
            ],
        )
        raw.append(branch)
    merged = _merge_partitioned_union(_union_all(raw))
    if not isinstance(merged, exp.Select):
        return node
    outer = exp.Select(expressions=items).from_(exp.Subquery(this=merged, alias=derived.args.get("alias").copy()))
    if outer_group:
        outer.set("group", exp.Group(expressions=outer_group))
    return outer
