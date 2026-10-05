"""Read a column through a model output that casts it: ``CAST(c AS BIGINT)`` also holds ``c``.

A model that lists ``CAST(emps.empid AS BIGINT)`` holds every value of ``emps.empid`` when the column is already an
integer no wider than the target, and ``model_reuse`` can then read ``emps.empid`` (in a filter, a join or an output)
from that model column. This only proposes the reading; the prover folds the cast away only when the declared
types show it cannot change a value, so a cast that does change values (a text column, a narrower target) is
refused there and the replacement is never returned.
"""

from __future__ import annotations

from sqlglot import exp

# casts to these never change an exact integer or decimal; whether the column fits is the prover's call
_WIDENING = {
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.INT,
    exp.DataType.Type.DECIMAL,
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT,
}


def cast_source(node: exp.Expression) -> exp.Column | None:
    """The column ``c`` of an output ``CAST(c AS <number type>)`` (an alias around it is looked through), or None."""

    if isinstance(node, exp.Alias):
        node = node.this
    if type(node) is not exp.Cast or not isinstance(node.this, exp.Column):
        return None
    target = node.args.get("to")
    if isinstance(target, exp.DataType) and target.this in _WIDENING:
        return node.this
    return None
