"""Global aggregates that only read the set of values of their argument.

``MIN``, ``MAX``, ``BIT_AND``, ``BIT_OR``, ``LOGICAL_AND``, ``LOGICAL_OR`` and any ``DISTINCT``
aggregate return a function of the set of non-NULL values their argument takes: how often a value
occurs, and which rows have a NULL argument, make no difference. So

    SELECT MAX(num) FROM (SELECT num FROM t GROUP BY num HAVING COUNT(num) = 1)
    SELECT MAX(num) FROM (SELECT num FROM t GROUP BY num HAVING COUNT(*) = 1)

are equal when ``SELECT DISTINCT num ... WHERE num IS NOT NULL`` is the same set on both sides,
although the two derived tables differ (the second keeps a group of NULLs). ``reduce`` turns a pair
of such global aggregates into one pair of DISTINCT queries per output column; the caller proves
those. A plain ``SUM``/``COUNT``/``AVG`` qualifies too when it reads the one column of a derived
table that never repeats a row (``SELECT x FROM t GROUP BY x``), where it is the DISTINCT aggregate.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import extended_grouping, plain_distinct

# Aggregates that ignore duplicates whether or not they are written with DISTINCT.
# sqlglot 26.0.0 has no BitwiseAndAgg/BitwiseOrAgg classes (BIT_AND is an unknown function there), so they are optional.
_SET_FUNCTIONS = {
    getattr(exp, cls): name
    for cls, name in (
        ("Min", "MIN"), ("Max", "MAX"), ("LogicalAnd", "LOGICAL_AND"), ("LogicalOr", "LOGICAL_OR"),
        ("BitwiseAndAgg", "BIT_AND"), ("BitwiseOrAgg", "BIT_OR"),
    )
    if hasattr(exp, cls)
}
# Aggregates that read the set of values only with DISTINCT (or over a source that never repeats).
_COUNTING_FUNCTIONS = {exp.Count: "COUNT", exp.Sum: "SUM", exp.Avg: "AVG"}
_MODIFIERS = ("having_max", "ignore_nulls", "order", "limit", "separator", "respect_nulls")
_BANNED = ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with", "with_", "laterals", "connect", "pivots", "into", "kind")


def _source(select: exp.Select) -> exp.Expression | None:
    from_ = select.args.get("from_") or select.args.get("from")
    return from_.this if from_ is not None else None


def _unique_column(select: exp.Select, column: exp.Column) -> bool:
    """Whether ``column`` reads the only output of a derived table that never repeats a row."""

    if select.args.get("joins"):
        return False
    source = _source(select)
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return False
    inner = source.this
    if len(inner.expressions) != 1 or column.table and column.table.lower() != (source.alias or "").lower():
        return False
    item = inner.expressions[0]
    if isinstance(item, exp.Star) or item.alias_or_name.lower() != column.name.lower():
        return False
    if any(inner.args.get(k) for k in ("limit", "offset", "qualify", "windows", "with", "with_")) or any(inner.find_all(exp.Window)):
        return False
    value = item.this if isinstance(item, exp.Alias) else item
    if any(isinstance(n, exp.AggFunc) for n in value.walk()):
        return False
    if plain_distinct(inner):
        return True
    group = inner.args.get("group")
    if group is None or extended_grouping(group):
        return False
    return [g.sql() for g in group.expressions] == [value.sql()]


def _call(select: exp.Select, node: exp.Expression) -> tuple[str, exp.Expression] | None:
    """``(function, argument)`` for an output that is one set-reading aggregate call."""

    if isinstance(node, exp.Alias):
        node = node.this
    if type(node) not in _SET_FUNCTIONS and type(node) not in _COUNTING_FUNCTIONS:
        return None
    if any(node.args.get(k) for k in _MODIFIERS) or node.args.get("expressions"):
        return None
    target = node.this
    distinct = isinstance(target, exp.Distinct)
    if distinct:
        if len(target.expressions) != 1:
            return None
        target = target.expressions[0]
    if not isinstance(target, exp.Expression) or isinstance(target, exp.Star) or any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in target.walk()):
        return None
    name = next((n for cls, n in _SET_FUNCTIONS.items() if type(node) is cls), None)
    if name is None:
        name = next((n for cls, n in _COUNTING_FUNCTIONS.items() if type(node) is cls), None)
        if name is None:
            return None
        if not distinct and not (isinstance(target, exp.Column) and _unique_column(select, target)):
            return None
    return name, target


def _shape(sql: str, dialect: str) -> tuple[exp.Select, list[tuple[str, exp.Expression]], list[str]] | None:
    import sqlglot

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(tree, exp.Select) or any(tree.args.get(k) for k in _BANNED) or _source(tree) is None:
        return None
    if any(tree.find_all(exp.Window)):
        return None
    calls = []
    for item in tree.expressions:
        call = _call(tree, item)
        if call is None:
            return None
        calls.append(call)
    if not calls:
        return None
    return tree, calls, [item.alias_or_name.lower() for item in tree.expressions]


def _values(select: exp.Select, argument: exp.Expression) -> exp.Select:
    """``SELECT DISTINCT argument FROM ... WHERE ... AND argument IS NOT NULL``."""

    query = select.copy()
    query.set("expressions", [exp.alias_(argument.copy(), "v")])
    operand = argument.copy() if isinstance(argument, (exp.Column, exp.Func, exp.Paren)) else exp.Paren(this=argument.copy())
    present = exp.Not(this=exp.Is(this=operand, expression=exp.Null()))
    where = query.args.get("where")
    query.set("where", exp.Where(this=exp.and_(exp.Paren(this=where.this.copy()), present) if where is not None else present))
    query.set("distinct", exp.Distinct())
    return query


def reduce(left_sql: str, right_sql: str, dialect: str, compare_names: bool = True) -> list[tuple[str, str]] | None:
    """Pairs of DISTINCT queries whose equivalence proves the two global aggregates equal, or ``None``.

    Both sides must be one select with no ``GROUP BY``/``HAVING`` whose outputs are each one
    set-reading aggregate, the same function at each position.
    """

    left, right = _shape(left_sql, dialect), _shape(right_sql, dialect)
    if left is None or right is None or len(left[1]) != len(right[1]):
        return None
    if compare_names and left[2] != right[2]:
        return None
    pairs: list[tuple[str, str]] = []
    for (func_l, arg_l), (func_r, arg_r) in zip(left[1], right[1]):
        if func_l != func_r:
            return None
        pair = (_values(left[0], arg_l).sql(dialect=dialect), _values(right[0], arg_r).sql(dialect=dialect))
        if pair not in pairs:
            pairs.append(pair)
    return pairs
