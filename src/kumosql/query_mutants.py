"""Faulty variants (mutants) of a query, one small mistake each.

Mutation operators follow the classes used to grade SQL queries (XData, Chandra
et al., "Data generation for testing and grading SQL queries", VLDB J. 2015):
a wrong comparison operator or boundary, a wrong constant, AND for OR, a missing
or negated predicate, the wrong join type, a missing or extra DISTINCT, a wrong
aggregate, a wrong arithmetic operator and a changed grouping. Each mutant is an
independent change at one site of the parsed query. A mutant can still be
equivalent to the original (a dropped redundant predicate, say); this module
does not decide that, the caller classifies survivors.

No code or data of XData is used. The operators are written for sqlglot trees.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

OPERATORS = (
    "comparison_boundary",
    "comparison_flip",
    "constant_shift",
    "connective_swap",
    "predicate_dropped",
    "predicate_negated",
    "join_type",
    "distinct_toggle",
    "aggregate_swap",
    "arithmetic_swap",
    "grouping_changed",
    "coalesce_dropped",
    "between_exclusive",
    "limit_changed",
)

_BOUNDARY = {exp.LT: exp.LTE, exp.LTE: exp.LT, exp.GT: exp.GTE, exp.GTE: exp.GT}
_FLIP = {exp.EQ: exp.NEQ, exp.NEQ: exp.EQ, exp.LT: exp.GT, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.GTE: exp.LTE}
_ARITH = {exp.Add: exp.Sub, exp.Sub: exp.Add, exp.Mul: exp.Div}
_AGGREGATES = {
    exp.Sum: (exp.Min, exp.Max, exp.Avg),
    exp.Avg: (exp.Sum,),
    exp.Min: (exp.Max,),
    exp.Max: (exp.Min,),
}
_PREDICATES = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.In, exp.Like, exp.Exists, exp.Between, exp.Is)


@dataclass(frozen=True)
class Mutant:
    operator: str
    site: int
    sql: str


def _in_condition(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.Where, exp.Having, exp.Join)):
            return True
        parent = parent.parent
    return False


def _swap_class(node: exp.Expression, cls) -> exp.Expression:
    return cls(this=node.this, expression=node.args.get("expression"))


def _apply(operator: str, node: exp.Expression):
    """Replacement nodes for ``node`` under ``operator`` (possibly several, possibly none)."""

    out = []
    if operator == "comparison_boundary" and type(node) in _BOUNDARY:
        out.append(_swap_class(node, _BOUNDARY[type(node)]))
    elif operator == "comparison_flip" and type(node) in _FLIP:
        out.append(_swap_class(node, _FLIP[type(node)]))
    elif operator == "constant_shift" and isinstance(node, exp.Literal) and not node.is_string:
        if isinstance(node.parent, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between)):
            try:
                out.append(exp.Literal.number(int(node.name) + 1))
            except ValueError:
                out.append(exp.Literal.number(float(node.name) + 1))
    elif operator == "connective_swap" and isinstance(node, (exp.And, exp.Or)):
        other = exp.Or if isinstance(node, exp.And) else exp.And
        out.append(other(this=node.this, expression=node.expression))
    elif operator == "predicate_dropped" and isinstance(node, exp.And):
        out.extend([node.this.copy(), node.expression.copy()])
    elif operator == "predicate_negated" and isinstance(node, _PREDICATES) and _in_condition(node):
        if not isinstance(node.parent, exp.Not):
            out.append(exp.Not(this=exp.Paren(this=node.copy())))
    elif operator == "predicate_negated" and isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
        out.append(node.this.copy())
    elif operator == "join_type" and isinstance(node, exp.Join):
        side, kind = node.args.get("side"), node.args.get("kind")
        if not side and kind in (None, "", "INNER") and node.args.get("on") is not None:
            copy = node.copy()
            copy.set("side", "LEFT")
            out.append(copy)
        elif side == "LEFT":
            copy = node.copy()
            copy.set("side", None)
            out.append(copy)
    elif operator == "distinct_toggle":
        if isinstance(node, exp.Select):
            copy = node.copy()
            copy.set("distinct", None if node.args.get("distinct") else exp.Distinct())
            out.append(copy)
        elif isinstance(node, exp.Union):
            copy = node.copy()
            copy.set("distinct", not node.args.get("distinct", True))
            out.append(copy)
    elif operator == "aggregate_swap":
        if type(node) in _AGGREGATES:
            out.extend(cls(this=node.this) for cls in _AGGREGATES[type(node)])
        elif isinstance(node, exp.Count):
            if isinstance(node.this, exp.Distinct):
                out.append(exp.Count(this=node.this.expressions[0].copy()))
            elif isinstance(node.this, exp.Star):
                pass
            else:
                out.append(exp.Count(this=exp.Star()))
                out.append(exp.Count(this=exp.Distinct(expressions=[node.this.copy()])))
    elif operator == "arithmetic_swap" and type(node) in _ARITH:
        out.append(_swap_class(node, _ARITH[type(node)]))
    elif operator == "grouping_changed" and isinstance(node, exp.Group):
        items = node.expressions
        select = node.parent if isinstance(node.parent, exp.Select) else None
        projected = " ".join(e.sql() for e in select.expressions) if select is not None else ""
        if len(items) > 1:
            for index in range(len(items)):
                if items[index].sql() in projected:
                    continue  # a projected, ungrouped column would not be a valid query
                copy = node.copy()
                copy.set("expressions", [e for i, e in enumerate(copy.expressions) if i != index])
                out.append(copy)
    elif operator == "coalesce_dropped" and isinstance(node, exp.Coalesce):
        out.append(node.this.copy())
    elif operator == "between_exclusive" and isinstance(node, exp.Between):
        x, low, high = node.this, node.args["low"], node.args["high"]
        out.append(exp.And(this=exp.GT(this=x.copy(), expression=low.copy()), expression=exp.LT(this=x.copy(), expression=high.copy())))
    elif operator == "limit_changed" and isinstance(node, exp.Limit):
        value = node.expression
        if isinstance(value, exp.Literal) and not value.is_string:
            out.append(exp.Limit(expression=exp.Literal.number(int(value.name) + 1)))
    return out


def mutate(sql: str, *, dialect: str = "bigquery", operators: tuple[str, ...] = OPERATORS) -> list[Mutant]:
    """Every distinct single-site mutant of ``sql`` that renders differently from it.

    Mutants are returned in a fixed order (operator, then tree position), so the
    same query always yields the same list. A query with no ORDER BY gets no
    ``limit_changed`` mutant, because a LIMIT over an unordered result has no
    defined rows to compare.
    """

    tree = sqlglot.parse_one(sql, read=dialect)
    original = tree.sql(dialect=dialect)
    nodes = list(tree.walk())
    has_order = tree.find(exp.Order) is not None
    seen = {original}
    result: list[Mutant] = []
    for operator in operators:
        if operator == "limit_changed" and not has_order:
            continue
        for site, node in enumerate(nodes):
            for index, _ in enumerate(_apply(operator, node)):
                copy = tree.copy()
                target = list(copy.walk())[site]
                replacement = _apply(operator, target)[index]
                if target is copy:
                    mutated = replacement
                else:
                    target.replace(replacement)
                    mutated = copy
                try:
                    text = mutated.sql(dialect=dialect)
                except sqlglot.errors.SqlglotError:
                    continue
                if text in seen:
                    continue
                seen.add(text)
                result.append(Mutant(operator, site, text))
    return result
