"""A global ``SUM`` of per-group counts is the count over the groups' rows.

``SELECT SUM(c) FROM (SELECT COUNT(*) AS c FROM r WHERE p GROUP BY k) t`` adds one count per group.
The groups of a plain ``GROUP BY`` partition the rows of ``r WHERE p`` (NULL keys form groups like any
other value), so the sum is ``COUNT(*)`` over those rows, and for ``COUNT(x)`` it is ``COUNT(x)``. The
one difference is the empty input: with no rows there are no groups and ``SUM`` returns NULL, while
the outer select, a global aggregate, still returns one row. So the rule writes ``SUM(t.c)`` as
``CASE WHEN COUNT(*) = 0 THEN NULL ELSE COUNT(x) END`` over the inner ``FROM``/``WHERE``. The result
is still a global aggregate, so it keeps returning exactly one row.

The outer select must be a global aggregate (no ``WHERE``, ``GROUP BY``, ``HAVING``, ``DISTINCT``,
windows or ordering) whose every aggregate is a non-DISTINCT ``SUM`` of such a count column and whose
every column sits inside one of those sums. Any other outer aggregate (``MAX`` or ``AVG`` of the
counts, ``COUNT(*)`` of the groups) depends on how the rows were grouped and is left alone. The inner
select must group plainly (no ROLLUP, CUBE or grouping sets, whose total rows count every row twice),
with no ``HAVING``, ``DISTINCT``, ``LIMIT`` or windows, and the count must not be ``COUNT(DISTINCT x)``:
per-group distinct counts do not add up to the overall distinct count.
"""

from __future__ import annotations

from sqlglot import exp


def _count_outputs(inner: exp.Select) -> dict[str, exp.Count]:
    """The inner select's output names whose value is a plain ``COUNT(*)`` or ``COUNT(x)``."""

    names: dict[str, list[exp.Expression]] = {}
    for item in inner.expressions:
        names.setdefault(item.alias_or_name.lower(), []).append(item)
    counts = {}
    for name, items in names.items():
        if len(items) != 1 or not name:
            continue
        value = items[0].this if isinstance(items[0], exp.Alias) else items[0]
        if not isinstance(value, exp.Count) or value.expressions:
            continue
        argument = value.this
        if argument is None or isinstance(argument, exp.Distinct) or argument.find(exp.Select, exp.AggFunc, exp.Window):
            continue
        counts[name] = value
    return counts


def _plain_grouped(inner: exp.Select) -> bool:
    group = inner.args.get("group")
    if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    if any(inner.args.get(k) for k in ("having", "distinct", "limit", "offset", "fetch", "qualify", "with", "windows", "laterals", "pivots")):
        return False
    if inner.find(exp.Window):
        return False
    return (inner.args.get("from_") or inner.args.get("from")) is not None


def sum_of_grouped_counts(select: exp.Select) -> exp.Expression | None:
    if any(select.args.get(k) for k in ("where", "group", "having", "distinct", "qualify", "order", "limit", "offset", "fetch", "joins", "laterals", "with", "windows", "pivots")):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or source.args.get("pivots"):
        return None
    inner = source.this
    if not _plain_grouped(inner):
        return None
    counts = _count_outputs(inner)
    if not counts:
        return None
    alias = (source.alias or "").lower()
    for item in select.expressions:
        if item.find(exp.Select, exp.Subquery, exp.Window, exp.Star):
            return None
    targets = []
    for item in select.expressions:
        for aggregate in item.find_all(exp.AggFunc):
            argument = aggregate.this
            if not isinstance(aggregate, exp.Sum) or not isinstance(argument, exp.Column) or aggregate.args.get("expressions"):
                return None
            if argument.table.lower() not in ("", alias) or argument.name.lower() not in counts:
                return None
            if aggregate.find_ancestor(exp.AggFunc) is not None:
                return None
            targets.append(aggregate)
        for column in item.find_all(exp.Column):
            if column.find_ancestor(exp.Sum) is None:
                return None
    if not targets:
        return None
    rewritten = select.copy()
    copies = [node for item in rewritten.expressions for node in item.find_all(exp.Sum)]
    for node in copies:
        count = counts[node.this.name.lower()]
        node.replace(
            exp.Case(
                ifs=[exp.If(this=exp.EQ(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(0)), true=exp.Null())],
                default=count.copy(),
            )
        )
    for key in ("from_", "from", "joins", "where"):
        value = inner.args.get(key)
        if value:
            rewritten.set(key, [v.copy() for v in value] if isinstance(value, list) else value.copy())
    return rewritten
