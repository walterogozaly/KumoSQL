"""When a proof needs "SUM and AVG do not depend on row order".

A FLOAT64 sum is not associative, so two ways of adding the same values can differ in the last digits. The
repo's own documentation says a FLOAT64 sum has no fixed order in BigQuery (``docs/provers.md``, and the
comparison tolerance of the pipeline evals); the GoogleSQL aggregate page was not consulted for it, so that rule is
**unverified**. The SMT prover treats an aggregate as one value fixed by its argument, its rows and its grouping,
which is right only if the order cannot matter. This module decides, for one proof, which of three things holds:

* **dropped**: no SUM or AVG in either query can add FLOAT64 values (every ``SUM`` reads an INT64, NUMERIC or
  BIGNUMERIC expression and there is no ``AVG``), so the sum is exact in any order and nothing is assumed;
* **narrowed**: every SUM and AVG that could add FLOAT64 values has an identical partner on the other side (the same
  call over the same sources, filter, grouping and ``WITH`` definitions, as text), so both sides add the same rows
  by the same plan. The remaining claim is that one such call returns the same value each time, which BigQuery does
  not promise either (unverified), so it is listed in place of the order assumption;
* **kept**: otherwise (the two sides can add the same values in a different order, or the plan cannot be compared:
  a window, a correlated subquery, an aggregate that differs), the order assumption stays on the proof.

The assumption is never dropped for a call whose argument has an unknown type, and a pair that regroups the additions
(a sum of partial sums, one SUM split into two, a SUM moved across a join) has no partner for its calls, so it keeps
the assumption and the prover cannot prove it anyway (the aggregate values stay free).
"""

from __future__ import annotations

from collections import Counter

from sqlglot import exp

from . import smt_values

SAME_PLAN_ASSUMPTION = (
    "an identical FLOAT64 SUM or AVG over the same rows returns the same value on both sides "
    "(BigQuery does not document the order a sum adds in; unverified)"
)

# A SUM over these is exact in any order (an overflow is an error site, reported by the error verdict).
EXACT_CLASSES = (smt_values.INT64, smt_values.NUMERIC, smt_values.BIGNUMERIC)

# The child slots of a SELECT that name its sources; a query anywhere else (a scalar, EXISTS or IN subquery, ON) may
# be correlated with a row of the outer query, which the call's own text does not show.
_SOURCE_SLOTS = {"from", "from_", "joins", "with", "with_"}
# Parts of a SELECT that come after the aggregation and do not change which rows a call adds.
_AFTER_AGGREGATION = ("expressions", "order", "limit", "offset", "distinct")


def _uncorrelated(select: exp.Select) -> bool:
    node = select
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, (exp.Lateral, exp.Unnest)):
            return False
        if isinstance(parent, exp.Select) and node.arg_key not in _SOURCE_SLOTS:
            return False
        if isinstance(parent, exp.Join) and node.arg_key != "this":
            return False
        node = parent
    return True


def plan_key(node: exp.Expression) -> str | None:
    """The text that fixes which rows a SUM or AVG adds and in what plan: the call, its SELECT without the projection
    list and the ordering that follows it, and every ``WITH`` definition around it. ``None`` when that text does not
    settle it (a window, no enclosing SELECT, a subquery that may be correlated)."""

    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Window):
            return None
        parent = parent.parent
    select = node.parent
    while select is not None and not isinstance(select, exp.Select):
        select = select.parent
    if select is None or not _uncorrelated(select):
        return None
    shell = select.copy()
    for key in _AFTER_AGGREGATION + ("with", "with_"):
        shell.set(key, None)
    shell.set("expressions", [exp.Star()])
    parts = [node.sql(dialect="bigquery", comments=False, normalize_functions="upper"), shell.sql(dialect="bigquery", comments=False, normalize_functions="upper")]
    ancestor = select
    while ancestor is not None:
        for key in ("with", "with_"):
            definitions = ancestor.args.get(key)
            if definitions is not None:
                parts.append(definitions.sql(dialect="bigquery", comments=False, normalize_functions="upper"))
        ancestor = ancestor.parent
    return "\n".join(parts)


def call_keys(statement: exp.Expression) -> list:
    """The plan key of every SUM and AVG in a statement (``None`` for one that cannot be keyed)."""

    return [plan_key(node) for node in statement.find_all(exp.Sum, exp.Avg)]


class Ledger:
    """What one proof learns about its SUM and AVG calls: every call of each side's text, and which keys the compiler
    saw adding an exact type."""

    def __init__(self) -> None:
        self.sides: list[list] = []
        self.exact: dict = {}

    def statement(self, statement: exp.Expression) -> None:
        self.sides.append(call_keys(statement))

    def observe_sum(self, node: exp.Expression, cls: str | None) -> None:
        """A SUM the compiler read, whose argument has type class ``cls`` (``None`` when it is not visible)."""

        key = plan_key(node)
        if key is not None:
            self.exact[key] = self.exact.get(key, True) and cls in EXACT_CLASSES

    def settle(self, assumptions: tuple, label: str) -> tuple:
        """``assumptions`` with the row-order ``label`` dropped, replaced by ``SAME_PLAN_ASSUMPTION`` or kept."""

        if label not in assumptions or len(self.sides) != 2:
            return assumptions
        left, right = ([k for k in side if k is None or not self.exact.get(k, False)] for side in self.sides)
        if not left and not right:
            return tuple(a for a in assumptions if a != label)
        if None not in left and None not in right and Counter(left) == Counter(right):
            return tuple(SAME_PLAN_ASSUMPTION if a == label else a for a in assumptions)
        return assumptions
