"""A global aggregate over a grouped model whose every key the query fixes to a constant.

``SELECT COUNT(DISTINCT x) FROM t WHERE name = 'a'`` read from a model grouped by ``name`` that holds
``COUNT(DISTINCT x)``: the filter leaves at most one model row (one group per key value), and that row holds the
answer. The query's global aggregate still returns a row over no input, so each model column is read through an
aggregate over that at-most-one row: ``SUM`` or ``MIN``/``MAX`` of the column gives its value (NULL over no
row, as the query's own ``SUM``, ``MIN``, ``MAX`` and ``AVG`` read over no rows) and ``COALESCE(SUM(col), 0)``
gives a count (0 over no row). This only proposes the reading; the prover decides.
"""

from __future__ import annotations

from sqlglot import exp


def fixed_single_group(model_group_keys: set[str], fixed_keys: set[str], query_group: list, query_having: list) -> bool:
    """Whether the model's keys are all fixed by the query and the query is one global group."""

    return bool(model_group_keys) and not query_group and not query_having and model_group_keys <= fixed_keys


def read_single_group(agg: exp.AggFunc, column: exp.Expression | None) -> exp.Expression | None:
    """``agg`` over the at-most-one model row, read from that row's column holding ``agg``, or None."""

    if column is None:
        return None
    if isinstance(agg, exp.Count):
        return exp.Coalesce(this=exp.Sum(this=column), expressions=[exp.Literal.number(0)])
    if isinstance(agg, (exp.Min, exp.Max)):
        return type(agg)(this=column)
    if isinstance(agg, (exp.Sum, exp.Avg)):
        return exp.Sum(this=column)
    return None
