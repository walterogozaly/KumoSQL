"""Decide when a SELECT DISTINCT cannot remove any row.

Shared by the ``remove_redundant_distinct`` rule and the equivalence prover's
normalizer, so both agree on the same narrow, sound condition.
"""

from __future__ import annotations

from sqlglot import exp


def _plain_column(node: exp.Expression) -> exp.Column | None:
    if isinstance(node, exp.Alias):
        node = node.this
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
        return node
    return None


def _text(column: exp.Column) -> str:
    return column.sql(dialect="bigquery").lower()


def distinct_is_redundant(select: exp.Select) -> bool:
    """True when DISTINCT sits on a plain GROUP BY whose keys are all projected.

    Rows of a grouped query are already unique on the grouping keys, and
    DISTINCT and GROUP BY use the same equality (NULLs compare equal). So when
    every key is projected unchanged, DISTINCT removes nothing. Anything less
    plain (expressions or ordinals as keys, ROLLUP, CUBE, GROUPING SETS, a key
    that is not projected, or an alias that could change how a key resolves) is
    treated as not redundant.
    """

    distinct = select.args.get("distinct")
    group = select.args.get("group")
    if not isinstance(distinct, exp.Distinct) or distinct.args.get("on"):
        return False
    if group is None or not group.expressions:
        return False
    if any(group.args.get(key) for key in ("grouping_sets", "rollup", "cube", "totals", "all")):
        return False
    keys: list[exp.Column] = []
    for key in group.expressions:
        column = _plain_column(key)
        if column is None or column is not key:
            return False
        keys.append(column)
    key_names = {key.name.lower() for key in keys}
    projected: set[str] = set()
    for projection in select.expressions:
        column = _plain_column(projection)
        if column is not None:
            projected.add(_text(column))
        alias = projection.alias if isinstance(projection, exp.Alias) else None
        # An alias equal to a key name but naming something else could change
        # which expression GROUP BY refers to.
        if alias and alias.lower() in key_names:
            if column is None or column.name.lower() != alias.lower():
                return False
    return all(_text(key) in projected for key in keys)
