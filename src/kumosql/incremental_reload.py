"""R8: an incremental model that deletes a window of its table and reloads it.

A common Dataform pattern makes late rows safe by re-reading the newest part of the table on every run::

    pre_operations { ${when(incremental(), `DELETE FROM ${self()} WHERE ts >= TIMESTAMP_SUB((SELECT MAX(ts) FROM ${self()}), INTERVAL 2 HOUR)`)} }
    SELECT ... FROM source ${when(incremental(), `WHERE ts > COALESCE((SELECT MAX(ts) FROM ${self()}), TIMESTAMP '1970-01-01')`)}

The pre-operation runs first, in the same script as the table statement, so the watermark reads the
table *after* the delete and reloads everything the delete took away, and whatever is new. This rule
decides when that equals a full refresh. R1 and R2 cannot: they decline any model with a pre-operation.

**Setting.** ``Q`` is the model's full query: a row-wise select-project-filter over one source table
(:func:`kumosql.incremental._row_wise`), with the event time ``tc`` projected unchanged. Write ``Q(S)``
for its output on source state ``S`` and ``tc(r)`` for an output row's event time. Because ``Q`` is
row-wise, ``Q(S')`` is ``Q(S)`` plus the output of the rows that changed. A run turns table ``T`` into
``K`` (the delete), then loads ``L``: ``Q`` restricted by a reload condition ``R`` on ``tc``, read on the
changed sources ``S'`` (an append of ``L``, or a ``MERGE`` of ``L`` on the source key). The invariant is
``T = Q(S)`` before a run; the claim is ``T = Q(S')`` after it. The first run builds ``Q(S_0)``.

Assumptions shared with R1 and R2: the event time is never NULL, every event time is later than the
literal date the ``COALESCE`` falls back to, and a declared source key is unique and non-NULL. Let
``M`` be the largest ``tc`` in ``T`` (so ``M`` is at most the source's newest event time, ``M_S``).
The contract's changes are all at or after the newest event time: ``insert_new`` and ``update_touch``
rows have ``tc' > M_S`` and ``insert_boundary`` rows ``tc' >= M_S``; a changed row is never older.

**The delete.** One statement ``DELETE FROM self WHERE tc >= W`` (or ``tc > W``) whose bound ``W`` does
not depend on the row: a constant, ``(SELECT MAX(tc) FROM self)`` minus a constant non-negative interval,
or a script variable declared before the delete with such a value (it is evaluated before the delete,
on ``T``). Then ``K = {r in T : tc(r) < W}`` (``<=`` for ``>``) and a NULL ``W`` deletes nothing. Write
``D`` for the deleted set of event times.

**Theorem (watermark computed after the delete, shape 1).** Let the reload condition be
``tc > m'`` where ``m' = COALESCE((SELECT MAX(tc) FROM self), old)`` is read after the delete (``m'`` is
the largest ``tc`` in ``K``, or ``old`` when ``K`` is empty). After every run ``T = Q(S')`` when

1. *append* (no ``uniqueKey``): the contract is ``insert_new`` and ``empty``, or also ``insert_boundary``
   when the delete provably removes the rows at ``M``: ``tc >= MAX - c`` with ``c >= 0``, or
   ``tc > MAX - c`` with ``c > 0``;
2. *merge* (``uniqueKey`` equal to the source key, projected unchanged): the contract is ``insert_new``,
   ``update_touch`` (only when ``Q`` has no ``WHERE``) and ``empty``, and ``insert_boundary`` when the
   reload is ``>=`` (also a ``TIMESTAMP_SUB`` lookback, which only lowers the bound) or when the delete
   provably removes the rows at ``M`` as in 1.

*Proof.* (P) Every row of ``K`` has ``tc <= m'``, and every row of ``Q(S)`` with ``tc <= m'`` is in
``K``. If ``K`` is empty, ``Q(S)`` has no row at or below ``old``. If ``W`` is NULL nothing was deleted,
so ``K = T = Q(S)``. Otherwise ``tc <= m' < W`` (or ``<= W`` for ``>``) and the row survives the delete.
Also ``m' <= M <= M_S``. Every new row is loaded: an ``insert_new`` row has ``tc' > M_S >= m'``; an
``insert_boundary`` row has ``tc' >= M_S >= M``, and when the delete removes the rows at ``M`` we have
``m' < W <= M`` (for ``>`` with ``c > 0``: ``m' <= W < M``), so ``tc' > m'``.

*Append.* The load is the output of ``S'`` restricted to ``tc > m'``. By (P), ``K`` is ``Q(S)`` restricted to
``tc <= m'``. The sources only gain rows, so ``Q(S')`` is ``Q(S)`` plus the output of the new rows, and the
load is ``Q(S)`` restricted to ``tc > m'`` plus the output of the new rows (all of which have ``tc' > m'``).
Together ``K`` and the load are ``Q(S')``, and no row is counted twice because ``K`` holds only
``tc <= m'`` and the load only ``tc > m'``. (A ``>=`` reload would load the kept rows at ``m'`` again,
which is why an append requires ``>``.)

*Merge.* Key by key, ``MERGE`` makes the final row of key ``k`` the loaded row when ``k`` is loaded and the
row of ``K`` otherwise. Source keys are unique, so no target row matches two loaded rows, and ``K``
has unique keys because ``T = Q(S)`` does. An unchanged source row is either loaded (and replaces its
own copy) or not loaded, which means ``tc <= m'`` (``tc < m'`` for ``>=``), so by (P) it is in ``K``
already. A changed row has ``tc' >= M_S >= m'`` and, for ``insert_new`` and ``update_touch``,
``tc' > M_S``: it is loaded, and it replaces the old version of its key (in ``K``, or already deleted)
or is inserted. Under ``>=`` an ``insert_boundary`` row is loaded because ``tc' >= M_S >= m'``. Without a
``WHERE`` the new version of a touched row is always in ``Q(S')``; with one, an update could make a
row fail the filter and the merge would not delete the old version, so ``update_touch`` is refused
then. Every key of ``Q(S')`` is therefore the loaded row or the unchanged surviving row, and nothing
else is in the table. ∎

**Theorem (window reloaded through a variable, shape 2).** A script variable ``w`` is declared before the
delete, ``w = COALESCE(MAX(tc) FROM self - c, old)`` with ``c >= 0`` (or without ``c``), the delete is
``tc >= w`` and the incremental query reads ``tc >= w``. ``w`` is the value before the delete, which is
why :func:`kumosql.incremental.effective_model` refuses to substitute it (the delete writes the table the
definition reads) and R8 reads the raw ``pre_operations``. Then the same contracts are safe, under:
``w`` non-NULL (the ``COALESCE`` gives ``old`` on an empty table; without it an empty table makes
``tc >= NULL`` reload nothing), ``w <= M`` when ``T`` is not empty, and the delete and the reload use
the same comparison for an append (merge: every deleted row must be reloaded, so a ``>`` delete may
meet a ``>`` or ``>=`` reload, and a ``>=`` delete a ``>=`` reload). ``insert_boundary`` needs ``w <= M``
with a ``>=`` reload and ``w < M`` with a ``>`` reload.

*Proof.* ``K`` is ``Q(S)`` restricted to the complement of the delete, the load is ``Q(S')`` restricted
to the reload set ``R``. When the same condition is used (append) the two sets split ``Q(S)``: a kept
row is not reloaded and a deleted row is, so ``K + Q(S) restricted to R = Q(S)``. A changed row is new,
with ``tc' > M_S >= M >= w`` (``tc' >= M_S`` for ``insert_boundary``), so it is in ``R``: ``T' = Q(S')``.
Merge: a row of ``Q(S')`` outside ``R`` is outside the delete (``D`` is contained in ``R``), so it is in
``K``, unchanged; one inside ``R`` is loaded and wins its key. ∎

**Near misses, each refused (``tests/test_incremental_reload.py``).** A late row older than the
delete bound is never loaded (``insert_late``); a re-delivered old row is appended again
(``duplicate``); ``update_touch`` without a key leaves the old version; an append that reloads with ``>=``
repeats the kept rows at ``m'``; ``insert_boundary`` with a constant bound, a ``>`` delete with a zero
interval, or a ``>`` reload against a ``w`` that can equal ``M`` lose boundary rows; a delete bound or
watermark read from the *source* table is not tied to the table; a ``w`` without ``COALESCE`` reloads
nothing on an empty table; a delete with a further condition keeps rows that (P) needs deleted; a ``WHERE`` with
``update_touch`` leaves a stale version.

Anything else, including a statement that is not one of these, returns no proof.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .incremental import (
    _DATE_WRAPPERS,
    IncrementalModel,
    SourceTable,
    Verdict,
    _conjuncts,
    _definition_expression,
    _old_date,
    _projected_as,
    _row_wise,
    _strip,
    _strip_old_bounds,
    _substitute_script,
    _watermark_predicate,
    modelled_exactly,
    variable_statement,
)

RULE = "R8 delete-then-reload window"

_CONSTANT_NODES = (
    *_DATE_WRAPPERS,
    exp.Literal,
    exp.Interval,
    exp.Paren,
    exp.Neg,
    exp.Var,
    exp.DataTypeParam,
    exp.TimestampSub,
    exp.TimestampAdd,
    exp.DatetimeSub,
    exp.DatetimeAdd,
)
_SUBTRACT = (exp.TimestampSub, exp.DatetimeSub)


@dataclass(frozen=True)
class Bound:
    """What is known about a delete bound ``W`` (an expression that does not read the table's rows)."""

    # "le": W <= the table's maximum event time whenever the table is not empty; "lt": strictly below it.
    below_max: str | None
    # W is never NULL (a COALESCE to a date before every event time).
    non_null: bool
    # The script variable the bound is, when it is exactly one.
    variable: str | None = None


def _amount(node: exp.Expression) -> float | None:
    amount = node.args.get("expression")
    if isinstance(amount, exp.Interval):
        amount = amount.this
    while isinstance(amount, exp.Paren):
        amount = amount.this
    if not isinstance(amount, exp.Literal):
        return None
    try:
        return float(amount.this)
    except ValueError:
        return None


def _max_of_self(node: exp.Expression, target: str, column: str) -> bool:
    """Exactly ``(SELECT MAX(column) FROM self)``."""

    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Subquery) or any(
        v for k, v in node.args.items() if k != "this"
    ):
        return False
    select = node.this
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return False
    if any(
        v for k, v in select.args.items() if k not in ("expressions", "from", "from_")
    ):
        return False
    agg = select.expressions[0]
    if isinstance(agg, exp.Alias):
        agg = agg.this
    if not (
        isinstance(agg, exp.Max)
        and isinstance(agg.this, exp.Column)
        and agg.this.name.lower() == column.lower()
    ):
        return False
    if any(v for k, v in agg.args.items() if k != "this"):
        return False
    source = select.args.get("from_") or select.args.get("from")
    table = source.this if source is not None else None
    if not (isinstance(table, exp.Table) and table.name.lower() == target.lower()):
        return False
    if any(v for k, v in table.args.items() if k not in ("this", "alias")):
        return False
    return not agg.this.table or agg.this.table.lower() == table.alias_or_name.lower()


def _bound(
    node: exp.Expression,
    target: str,
    column: str,
    column_type: str,
    variables: dict[str, Bound | None],
) -> Bound | None:
    """The :class:`Bound` of an expression that does not read the rows of the table, or None when it is
    not one of the forms R8 reads."""

    if isinstance(node, exp.Column):
        found = variables.get(node.name.lower()) if not node.table else None
        return (
            Bound(found.below_max, found.non_null, node.name.lower())
            if found is not None
            else None
        )
    if isinstance(node, exp.Paren):
        return _bound(node.this, target, column, column_type, variables)
    if _max_of_self(node, target, column):
        return Bound("le", False)
    if isinstance(node, exp.Coalesce):
        if len(node.expressions) != 1 or not _old_date(node.expressions[0]):
            return None
        inner = _bound(node.this, target, column, column_type, variables)
        return Bound(inner.below_max, True) if inner is not None else None
    if isinstance(node, _SUBTRACT):
        amount = _amount(node)
        if amount is None or amount < 0:
            return None
        inner = _bound(node.this, target, column, column_type, variables)
        if inner is None:
            return None
        below = inner.below_max if amount == 0 or inner.below_max is None else "lt"
        return Bound(below, inner.non_null)
    if isinstance(node, exp.Cast):
        if node.to.sql(dialect="bigquery").upper() != column_type.upper():
            return None
        inner = _bound(node.this, target, column, column_type, variables)
        return Bound(inner.below_max, inner.non_null) if inner is not None else None
    if all(isinstance(n, _CONSTANT_NODES) for n in node.walk()):
        return Bound(None, True)
    return None


@dataclass(frozen=True)
class _Delete:
    op: str  # ">=" or ">"
    bound: Bound


def _delete(
    tree: exp.Expression,
    target: str,
    column: str,
    column_type: str,
    variables: dict[str, Bound | None],
) -> _Delete | None:
    """``DELETE FROM self WHERE tc >[=] <bound>`` and nothing more."""

    if not isinstance(tree, exp.Delete) or any(
        v for k, v in tree.args.items() if k not in ("this", "where")
    ):
        return None
    table = tree.this
    if (
        not isinstance(table, exp.Table)
        or table.name.lower() != target.lower()
        or any(v for k, v in table.args.items() if k not in ("this", "alias"))
    ):
        return None
    where = tree.args.get("where")
    condition = where.this if where is not None else None
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if not isinstance(condition, (exp.GT, exp.GTE)):
        return None
    left = condition.left
    if not (
        isinstance(left, exp.Column)
        and left.name.lower() == column.lower()
        and (not left.table or left.table.lower() == table.alias_or_name.lower())
    ):
        return None
    bound = _bound(condition.right, target, column, column_type, variables)
    if bound is None:
        return None
    return _Delete(">=" if isinstance(condition, exp.GTE) else ">", bound)


def _removes_newest_rows(delete: _Delete) -> bool:
    """Whether the delete provably removes the rows at the table's maximum event time, so a row at the
    source's newest time is later than what remains."""

    below = delete.bound.below_max
    return (delete.op == ">=" and below in ("le", "lt")) or (
        delete.op == ">" and below == "lt"
    )


def prove_reload_window(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: frozenset[str],
    tables: tuple[str, ...] | None = None,
) -> Verdict | None:
    """R8 (see the module docstring); ``None`` when the model is not this shape or a condition fails."""

    if not model.pre_operations or not modelled_exactly(model):
        return None
    dialect = model.dialect
    full_script = _substitute_script(model.full_pre_operations, model.full_sql, dialect)
    if full_script is None or full_script[1]:
        return None
    try:
        full = sqlglot.parse_one(full_script[0], read=dialect)
        incremental = sqlglot.parse_one(model.incremental_sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _row_wise(full, ignore) or not isinstance(incremental, exp.Select):
        return None
    source = full.args.get("from_") or full.args.get("from")
    table = sources.get(source.this.name) if source is not None else None
    if table is None or table.time_column is None:
        return None
    tc = table.time_column
    target_tc = _projected_as(full, tc)
    column_type = table.columns.get(tc)
    if target_tc is None or column_type is None:
        return None

    # The script: variables first (each a bound), then one DELETE, nothing after it.
    declared: set[str] = set()
    parsed = [(s, variable_statement(s, dialect)) for s in model.pre_operations]
    names = {n for _, v in parsed if v is not None for n in v.names}
    columns = {c.lower() for t in sources.values() for c in t.columns} | {
        target_tc.lower()
    }
    if names & columns:
        return None
    variables: dict[str, Bound | None] = {}
    delete: _Delete | None = None
    delete_tree: exp.Expression | None = None
    for statement, variable in parsed:
        if delete_tree is not None:
            return None
        if variable is not None:
            if (
                variable.verb != "declare"
                or len(variable.names) != 1
                or variable.names[0] in declared
            ):
                return None
            name = variable.names[0]
            declared.add(name)
            if variable.value is None or (
                variable.kind and variable.kind.upper() != column_type.upper()
            ):
                variables[name] = None
            else:
                variables[name] = _bound(
                    _definition_expression(variable.value),
                    model.target,
                    target_tc,
                    column_type,
                    variables,
                )
            continue
        try:
            delete_tree = sqlglot.parse_one(statement, read=dialect)
        except sqlglot.errors.SqlglotError:
            return None
        delete = _delete(delete_tree, model.target, target_tc, column_type, variables)
        if delete is None:
            return None
    if delete is None or delete_tree is None:
        return None

    full = _strip_old_bounds(full, tc)
    where = incremental.args.get("where")
    conjuncts = _conjuncts(where.this if where else None)
    marks = [
        (c, _watermark_predicate(c, model.target, tc, target_tc)) for c in conjuncts
    ]
    marks = [(c, op) for c, op in marks if op]
    named = [
        (c, ">=" if isinstance(c, exp.GTE) else ">")
        for c in conjuncts
        if isinstance(c, (exp.GT, exp.GTE))
        and isinstance(c.left, exp.Column)
        and c.left.name.lower() == tc.lower()
        and isinstance(c.right, exp.Column)
        and not c.right.table
        and c.right.name.lower() in declared
    ]
    if len(marks) + len(named) != 1:
        return None
    if (
        _strip(incremental, (marks or named)[0][0]).sql()
        != _strip(full, exp.Null()).sql()
    ):
        return None
    reload_op = (marks or named)[0][1]

    if named:
        # Shape 2: the window is reloaded through the variable the delete used.
        variable = delete.bound.variable
        if variable is None or named[0][0].right.name.lower() != variable:
            return None
        if not delete.bound.non_null or delete.bound.below_max is None:
            return None
        if model.unique_key:  # every deleted row must be reloaded
            covers = reload_op == ">=" or delete.op == ">"
        else:  # the deleted and the reloaded rows must be the same rows
            covers = delete.op == reload_op
        if not covers:
            return None
        boundary = delete.bound.below_max == "lt" or reload_op == ">="
    else:
        # Shape 1: the watermark is read after the delete.
        if not model.unique_key and reload_op != ">":
            return None  # a ">=" or lookback reload would append the kept rows at the new maximum again
        boundary = _removes_newest_rows(delete) or (
            bool(model.unique_key) and reload_op == ">="
        )

    allowed = {"insert_new", "empty"} | ({"insert_boundary"} if boundary else set())
    if model.unique_key:
        if set(model.unique_key) != set(table.key) or any(
            _projected_as(full, k) != k for k in table.key
        ):
            return None
        allowed.add("update_touch")
        if "update_touch" in kinds and full.args.get("where"):
            return None  # an update can move a row out of the filter, and a merge never deletes it
    if not kinds <= allowed:
        return None
    how = "merge on the source key" if model.unique_key else "append"
    return Verdict(
        "safe",
        RULE,
        f"{how}; the delete removes the table's newest window and the {'variable' if named else 'watermark read after it'} reloads it; every change is at or after the source's newest event time",
    )
