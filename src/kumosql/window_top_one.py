"""Shared readers for the rewrites of "the first row of each partition" (``rank_interchange`` and ``latest_row_rules``).

Both rules read the same few things: a numbering window (``ROW_NUMBER``, ``RANK``, ``DENSE_RANK``) with a
``PARTITION BY`` and an ``ORDER BY`` and no frame, a comparison that keeps only its first row (``= 1``,
``<= 1``, ``< 2``), whether the window's order is *total* (no two rows of a partition have equal
``PARTITION BY`` and ``ORDER BY`` values, so no two rows are peers) and whether an order key can be NULL.

Totality and NOT NULL come from the facts ``normalize`` is given (declared keys and NOT NULL columns) and
from :mod:`kumosql.output_properties`: the window's input is projected on the ``PARTITION BY`` and
``ORDER BY`` expressions and a unique key of that projection means no two rows tie. A fact that cannot be
shown is "not total" and "may be NULL": the rules decline.

Nothing here changes a tree.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .ast_utils import FROM_KEY
from .smt_equivalence import TableConstraints

NUMBERING = (exp.RowNumber, exp.Rank, exp.DenseRank)
_PLAIN_WINDOW_ARGS = {"this", "partition_by", "order", "over"}
_VOLATILE = (exp.Rand, exp.Anonymous, exp.Subquery, exp.Select, exp.Window, exp.Star, exp.Placeholder, exp.Parameter)
_FLOAT_TYPES = {"FLOAT", "FLOAT64", "DOUBLE", "REAL", "FLOAT32", "FLOAT4", "FLOAT8", "DOUBLE PRECISION"}


@dataclass(frozen=True)
class Key:
    """One ``ORDER BY`` key of a window. sqlglot's parser fills ``nulls_first`` from the dialect (BigQuery and
    MySQL sort NULL first ascending, last descending), so the flag is the engine's own NULL order."""

    expr: exp.Expression
    desc: bool
    nulls_first: bool


def numbering_window(node: exp.Expression) -> bool:
    """A ``ROW_NUMBER``, ``RANK`` or ``DENSE_RANK`` window with only ``PARTITION BY`` and ``ORDER BY``."""

    if not isinstance(node, exp.Window) or not isinstance(node.this, NUMBERING):
        return False
    if any(node.args.get(k) for k in node.args if k not in _PLAIN_WINDOW_ARGS):
        return False
    if node.this.args.get("expressions") or node.this.args.get("this"):
        return False
    return True


def order_keys(window: exp.Window) -> list[Key] | None:
    """The window's ordering keys with their direction and NULL placement; None when one is not fully known."""

    order = window.args.get("order")
    if order is None:
        return []
    keys = []
    for item in order.expressions:
        if not isinstance(item, exp.Ordered):
            return None
        nulls_first = item.args.get("nulls_first")
        if nulls_first is None:
            return None
        keys.append(Key(item.this, bool(item.args.get("desc")), bool(nulls_first)))
    return keys


def one_row_operand(node: exp.Expression) -> exp.Expression | None:
    """``x`` for a comparison that keeps exactly the values ``x`` takes when it is 1: ``x = 1``, ``1 = x``,
    ``x <= 1``, ``1 >= x``, ``x < 2``, ``2 > x``. A numbering window is never below 1, so all of these mean
    "is 1". None for anything else."""

    def number(side: exp.Expression) -> int | None:
        if isinstance(side, exp.Paren):
            return number(side.this)
        if isinstance(side, exp.Literal) and not side.is_string and side.this.isdigit():
            return int(side.this)
        return None

    if isinstance(node, exp.Paren):
        return one_row_operand(node.this)
    table = {exp.EQ: (1, True), exp.LTE: (1, True), exp.LT: (2, True), exp.GT: (2, False), exp.GTE: (1, False)}
    for cls, (bound, operand_left) in table.items():
        if type(node) is not cls:
            continue
        left, right = node.this, node.expression
        if cls is exp.EQ:
            if number(right) == 1:
                return left
            if number(left) == 1:
                return right
            return None
        if operand_left and number(right) == bound:
            return left
        if not operand_left and number(left) == bound:
            return right
        return None
    return None


def owned_windows(select: exp.Select) -> list[exp.Window]:
    return [w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]


def deterministic(node: exp.Expression) -> bool:
    """No subquery, window, aggregate, random or unknown function, star or parameter."""

    return not any(isinstance(n, _VOLATILE) or isinstance(n, exp.AggFunc) for n in node.walk())


def may_be_float(node: exp.Expression, types: dict[str, dict[str, str]] | None, table: str | None) -> bool:
    """Whether ``node`` can be a floating-point (or fixed-point) value, whose NaN, signed zero and rounding the
    ``MIN``/``MAX`` and grouping forms do not share with an ordering. Columns of unknown type are taken as the
    prover takes them (not NaN); a column declared FLOAT64 and anything that computes a float is refused."""

    for n in node.walk():
        if isinstance(n, (exp.Div, exp.Avg, exp.Sqrt, exp.Pow, exp.Exp, exp.Ln, exp.Log)):
            return True
        if isinstance(n, exp.Literal) and not n.is_string and not n.this.lstrip("-").isdigit():
            return True
        if isinstance(n, exp.Cast):
            target = n.args.get("to")
            if target is not None and target.sql().upper().split("(")[0] in _FLOAT_TYPES:
                return True
        if isinstance(n, exp.Column) and types:
            declared = (types.get((n.table or table or "").lower()) or types.get((table or "").lower()) or {}).get(n.name.lower())
            if declared and str(declared).upper().split("(")[0] in _FLOAT_TYPES:
                return True
    return False


class Facts:
    """What ``normalize`` knows about the tables: declared keys and NOT NULL columns (and the schema)."""

    def __init__(self, keys, not_null, schema, types, dialect: str) -> None:
        self.keys = {t.lower(): [tuple(c.lower() for c in k) for k in ks] for t, ks in (keys or {}).items()}
        self.not_null = {t.lower(): frozenset(c.lower() for c in cs) for t, cs in (not_null or {}).items()}
        self.schema = {t.lower(): [c.lower() for c in cs] for t, cs in (schema or {}).items()}
        self.types = {t.lower(): {c.lower(): v for c, v in cols.items()} for t, cols in (types or {}).items()}
        self.dialect = dialect
        tables = set(self.keys) | set(self.not_null)
        self.constraints = {
            t: TableConstraints(not_null=self.not_null.get(t, frozenset()), keys=tuple(self.keys.get(t, ())))
            for t in tables
        }

    def properties(self, select: exp.Select, exprs: list[exp.Expression]):
        """``(unique, non_null)``: whether the rows ``select`` reads (FROM, JOINs, WHERE) are pairwise distinct
        on ``exprs`` (so no two tie), and per expression whether it is never NULL. ``(False, [False..])`` when
        the facts cannot be derived."""

        none = (False, [False] * len(exprs))
        if not exprs or any(not deterministic(e) for e in exprs):
            return none
        if any(select.args.get(k) for k in ("group", "having", "distinct", "laterals", "with_", "with")):
            return none
        from_ = select.args.get(FROM_KEY)
        if from_ is None:
            return none
        probe = exp.Select(expressions=[exp.alias_(e.copy(), f"kqt{i}") for i, e in enumerate(exprs)])
        probe.set(FROM_KEY, from_.copy())
        if select.args.get("joins"):
            probe.set("joins", [j.copy() for j in select.args["joins"]])
        if select.args.get("where") is not None:
            probe.set("where", select.args["where"].copy())
        from .output_properties import infer_properties

        try:
            props = infer_properties(probe.sql(dialect=self.dialect), self.constraints, self.schema, dialect=self.dialect)
        except Exception:  # noqa: BLE001 - a fact that cannot be derived is not known
            return none
        if props.unsupported or len(props.columns) != len(exprs):
            return none
        width = len(exprs)
        unique = any(set(key.positions) <= set(range(width)) for key in props.keys)
        return unique, [bool(c.non_null) for c in props.columns]

    def total_order(self, select: exp.Select, window: exp.Window) -> bool:
        """No two rows of one partition have equal ``PARTITION BY`` and ``ORDER BY`` values."""

        keys = order_keys(window)
        if keys is None:
            return False
        exprs = list(window.args.get("partition_by") or []) + [k.expr for k in keys]
        return self.properties(select, exprs)[0]

    def table_of(self, select: exp.Select) -> str | None:
        from_ = select.args.get(FROM_KEY)
        if from_ is not None and isinstance(from_.this, exp.Table):
            return from_.this.name.lower()
        return None
