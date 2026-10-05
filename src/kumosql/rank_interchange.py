"""``ROW_NUMBER``, ``RANK`` and ``DENSE_RANK`` read alike where they cannot differ.

The three numbering functions agree on a row exactly when no other row of its partition ties with it. Where
that is guaranteed, or where only "is it 1" is asked, one spelling serves all three. Two rewrites, each with
its preconditions (the rule declines when one cannot be shown; the negative tests pin each):

1. **Total order: ``RANK`` and ``DENSE_RANK`` are ``ROW_NUMBER``.** When no two rows of one partition have equal
   ``PARTITION BY`` and ``ORDER BY`` values, every row is alone in its peer group, so ``ROW_NUMBER``, ``RANK`` and
   ``DENSE_RANK`` give it the same number, wherever the value is read (a filter, the select list, another
   expression). Totality needs the window's input (FROM, JOINs and WHERE of a select that is not grouped) to be
   unique over those expressions: a declared key (``keys=`` with ``not_null=`` in ``normalize``) or whatever
   :mod:`kumosql.output_properties` can show unique. Without it nothing is rewritten: with ties ``ROW_NUMBER``
   numbers peers 1, 2, 3 where ``RANK`` gives them all 1 and ``DENSE_RANK`` gives the next group 2.
2. **Only "is it 1" is asked: ``DENSE_RANK`` is ``RANK``, and ``<= 1`` and ``< 2`` are ``= 1``.** Every peer of
   the first row has ``RANK`` 1 and ``DENSE_RANK`` 1, and no other row has either, so ``DENSE_RANK() OVER w = 1``
   keeps the rows ``RANK() OVER w = 1`` keeps, with no totality needed. This is read in a ``QUALIFY`` condition and
   in a ``WHERE`` condition of the select that reads the window as a column of a derived table. Everything else the
   select reads of that window is its value on a kept row, which is 1 for both functions (also in a ``JOIN ... ON``:
   a joined row is kept only if the derived row's number is 1, so the condition is evaluated with 1 on both sides,
   and a row that the join null-extends has no number to keep). ``ROW_NUMBER = 1`` is never turned into ``RANK = 1`` without a total order (it keeps one of the
   peers, ``RANK`` keeps all of them).

The two together give one spelling: ``ROW_NUMBER`` where the order is total, ``RANK`` where only the first row is
asked and the order may tie; ``QUALIFY w <= 1`` and ``QUALIFY w = 1`` read alike.

A window with a frame (``ROWS``/``RANGE``, which numbering functions do not have in BigQuery) or a named window not
yet inlined is left alone.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY, conjuncts
from .window_top_one import Facts, numbering_window, one_row_operand, owned_windows


def rank_interchange(tree: exp.Expression, keys=None, not_null=None, schema=None, types=None, dialect: str = "bigquery") -> exp.Expression:
    """Apply the module's two rewrites to every select of ``tree`` (module doc)."""

    if not any(isinstance(n, (exp.Rank, exp.DenseRank)) for n in tree.walk()):
        return tree
    facts = Facts(keys, not_null, schema, types, dialect)
    for select in list(tree.find_all(exp.Select))[::-1]:
        _total_order(select, facts)
    for select in list(tree.find_all(exp.Select))[::-1]:
        _first_row(select)
    return tree


def _as(window: exp.Window, cls) -> None:
    window.set("this", cls())


def _total_order(select: exp.Select, facts: Facts) -> None:
    for window in owned_windows(select):
        if isinstance(window.this, (exp.Rank, exp.DenseRank)) and numbering_window(window) and facts.total_order(select, window):
            _as(window, exp.RowNumber)


def _first_row(select: exp.Select) -> None:
    qualify = select.args.get("qualify")
    if qualify is not None:
        aliases = {i.alias.lower(): i.this for i in select.expressions if isinstance(i, exp.Alias)}
        for part in conjuncts(qualify.this):
            window = _window_operand(one_row_operand(part), aliases)
            if window is not None:
                _canonical_first_row(part, window)
    where = select.args.get("where")
    from_ = select.args.get(FROM_KEY)
    if where is None or from_ is None:
        return
    sources = [from_.this] + [j.this for j in select.args.get("joins") or []]
    for part in conjuncts(where.this):
        operand = one_row_operand(part)
        if not isinstance(operand, exp.Column):
            continue
        matches = [
            s for s in sources
            if isinstance(s, exp.Subquery) and s.alias and (operand.table.lower() == s.alias.lower() or (not operand.table and len(sources) == 1))
        ]
        if len(matches) != 1 or not isinstance(matches[0].this, exp.Select):
            continue
        inner = matches[0].this
        items = [i for i in inner.expressions if isinstance(i, exp.Alias) and i.alias.lower() == operand.name.lower()]
        if len(items) != 1 or not isinstance(items[0].this, exp.Window) or not numbering_window(items[0].this):
            continue
        _canonical_first_row(part, items[0].this)


def _window_operand(operand: exp.Expression | None, aliases: dict[str, exp.Expression]) -> exp.Window | None:
    if isinstance(operand, exp.Window) and numbering_window(operand):
        return operand
    if isinstance(operand, exp.Column) and not operand.table:
        target = aliases.get(operand.name.lower())
        if isinstance(target, exp.Window) and numbering_window(target):
            return target
    return None


def _canonical_first_row(condition: exp.Expression, window: exp.Window) -> None:
    """``DENSE_RANK`` becomes ``RANK`` and the comparison reads ``= 1``."""

    if isinstance(window.this, exp.DenseRank):
        _as(window, exp.Rank)
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, (exp.LTE, exp.LT, exp.GTE, exp.GT)):
        operand = one_row_operand(condition)
        if operand is not None:
            condition.replace(exp.EQ(this=operand.copy() if operand.parent is not None else operand, expression=exp.Literal.number(1)))
