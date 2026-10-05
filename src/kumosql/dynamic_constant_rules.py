"""Read a column pinned to ``CURRENT_TIMESTAMP`` (or ``CURRENT_DATE``) as that call.

``SELECT hiredate, COUNT(*) FROM emp WHERE hiredate = CURRENT_TIMESTAMP GROUP BY mgr, hiredate`` is
``SELECT CURRENT_TIMESTAMP, COUNT(*) FROM emp WHERE hiredate = CURRENT_TIMESTAMP GROUP BY mgr``.

Why it is sound:

* ``CURRENT_TIMESTAMP`` and ``CURRENT_DATE`` read the same value everywhere in one statement (SQL
  standard; MySQL, DuckDB and Calcite all fix them per statement), so both sides of a pair see one value.
* A row that reaches the select list, ``GROUP BY``, ``HAVING`` or ``ORDER BY`` passed every top-level
  conjunct of the ``WHERE`` clause, so ``column = CURRENT_TIMESTAMP`` was TRUE for it: the column is not
  NULL and holds that value. Reading the column as the call in those clauses changes no row's value.
* Only a column whose declared type is a timestamp (for ``CURRENT_TIMESTAMP``) or a date (for
  ``CURRENT_DATE``) qualifies, so equal means identical, with no coercion between a string, a number
  and a time in the comparison.
* A grouping key that is now the call is constant: it splits no group, so it is dropped, unless no other
  key would remain (a ``GROUP BY`` with no key reads an empty input differently).
* Clauses that hold a nested query or a window function are left alone (a correlated reference there
  could name another scope's column of the same name).
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts
from .cast_rules import expression_type

_TIMESTAMP_KINDS = {"timestamp", "timestamptz", "datetime"}
_CLAUSES = ("expressions", "group", "having", "order")


_TIMESTAMP_CLOCK = "KUMOSQL_CLOCK_TIMESTAMP"
_DATE_CLOCK = "KUMOSQL_CLOCK_DATE"


def read_statement_clock(tree: exp.Expression) -> exp.Expression:
    """Spell ``CURRENT_TIMESTAMP`` and ``CURRENT_DATE`` as zero-argument calls the prover treats as unknown constants.

    Opt-in (``statement_clock=True``): it reads both queries of a pair as run at the same instant, the way
    the QED prover and Calcite's optimizer tests do. A pair to be compared across runs at different times
    must not use it (the prover refuses the clock functions by default). The prover reads an unrecognised
    function as an uninterpreted one, so two calls with no arguments agree and nothing else is assumed.
    """

    def spell(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentTimestamp) and not node.args.get("this") and not node.args.get("sysdate"):
            return exp.Anonymous(this=_TIMESTAMP_CLOCK, expressions=[])
        if isinstance(node, exp.CurrentDate) and not node.args.get("this"):
            return exp.Anonymous(this=_DATE_CLOCK, expressions=[])
        return node

    return tree.transform(spell)


def _call_kind(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous) and not node.expressions:
        return {_TIMESTAMP_CLOCK: "timestamp", _DATE_CLOCK: "date"}.get((node.name or "").upper())
    if isinstance(node, exp.CurrentTimestamp):
        return "timestamp"
    if isinstance(node, exp.CurrentDate):
        return "date"
    return None


def _pins(select: exp.Select, types: dict, dialect: str) -> dict[exp.Column, exp.Expression]:
    where = select.args.get("where")
    if where is None:
        return {}
    found: dict[exp.Column, exp.Expression] = {}
    for part in conjuncts(where.this):
        if not isinstance(part, exp.EQ):
            continue
        for column, call in ((part.this, part.expression), (part.expression, part.this)):
            kind = _call_kind(call)
            if kind is None or not isinstance(column, exp.Column) or not isinstance(column.this, exp.Identifier):
                continue
            have = expression_type(column, select, types, dialect=dialect)
            if have is not None and (have[0] in _TIMESTAMP_KINDS if kind == "timestamp" else have[0] == "date"):
                found.setdefault(column.copy(), call)
    return found


def pin_dynamic_constants(select: exp.Select, types: dict, dialect: str = "bigquery") -> exp.Select | None:
    pins = _pins(select, types, dialect)
    if not pins:
        return None
    copy = select.copy()
    for clause in _CLAUSES:
        value = copy.args.get(clause)
        values = value if isinstance(value, list) else [value]
        if any(v is not None and (v.find(exp.Subquery, exp.Window, exp.Select) is not None) for v in values):
            return None
    changed = False

    def read(node: exp.Expression) -> exp.Expression:
        nonlocal changed
        if isinstance(node, exp.Column):
            for column, call in pins.items():
                if node == column:
                    changed = True
                    return call.copy()
        return node

    for clause in _CLAUSES:
        value = copy.args.get(clause)
        if value is None:
            continue
        if isinstance(value, list):
            copy.set(clause, [_keep_alias(item, read) for item in value])
        else:
            copy.set(clause, value.transform(read))
    group = copy.args.get("group")
    if group is not None and not any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        keys = [k for k in group.expressions if _call_kind(k) is None]
        if keys and len(keys) != len(group.expressions):
            group.set("expressions", keys)
            changed = True
    return copy if changed else None


def _keep_alias(item: exp.Expression, read) -> exp.Expression:
    """Replace the columns of a select-list item; a bare column keeps its name as an alias."""

    if isinstance(item, exp.Column):
        replaced = read(item)
        if replaced is not item:
            return exp.alias_(replaced, item.name)
    return item.transform(read)
