"""``ROWS`` frames read as the ``RANGE`` frames they equal when the window's order keys are unique.

A ``RANGE`` frame includes every peer of the current row (every row that ties on the ``ORDER BY`` keys);
a ``ROWS`` frame counts physical rows and, among peers, follows an order the query does not fix. When no
two rows of a partition can tie they have no peers, so the two frames hold the same rows, and
``SUM(v) OVER (PARTITION BY k ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`` is the
running total ``SUM(v) OVER (PARTITION BY k ORDER BY id)`` that ``window_canonical`` already spells without
the frame.

``unique_order_frames`` rewrites ``ROWS`` to ``RANGE`` on a window when all of these hold:

* the function is ``SUM``, ``COUNT``, ``MIN``, ``MAX``, ``AVG``, ``COUNTIF``, ``LOGICAL_AND``/``LOGICAL_OR``,
  ``BIT_AND``/``BIT_OR``/``BIT_XOR`` (a value that depends only on the set of rows in the frame) or
  ``FIRST_VALUE``/``LAST_VALUE``/``NTH_VALUE`` (a value that depends on the frame's rows and their order,
  which are the same too when nothing ties). It is not wrapped in ``IGNORE NULLS`` and takes no ``FILTER``;
* the frame is bounded by ``UNBOUNDED PRECEDING``, ``CURRENT ROW`` or ``UNBOUNDED FOLLOWING`` only. An offset
  frame (``1 PRECEDING``) means a number of rows with ``ROWS`` and a distance in the order key with
  ``RANGE``: different frames even without ties. ``EXCLUDE`` is left alone;
* the window has an ``ORDER BY``, and its select reads one plain table (no join, derived table or ``LATERAL``)
  whose declared key is entirely among the ``PARTITION BY`` and ``ORDER BY`` expressions (plain columns:
  a partition column is the same on every row of a partition), and every column of that key is declared
  NOT NULL. The key is unique across the table, so no two rows of any partition (or of the rows left after
  ``WHERE``) have equal keys: no row has a peer, so the order is total (``window_order_keys``). Without
  the declaration the rewrite does not fire: ties are possible, and with them ``ROWS`` and ``RANGE`` differ.

The other direction (a ``RANGE`` frame to ``ROWS``) is never needed: ``window_canonical`` already drops the
spellings of the default frame. This rule runs just before it so that a ``ROWS`` frame that equals the
default frame reaches the same text.
"""

from __future__ import annotations

from sqlglot import exp

from .window_canonical import _NAVIGATION, _ORDER_BLIND, _bound
from .window_order_keys import covers_a_key


def unique_order_frames(
    tree: exp.Expression,
    keys: dict[str, list[tuple[str, ...]]] | None,
    not_null: dict[str, frozenset[str]] | None,
) -> exp.Expression:
    """Spell ``ROWS`` frames over a unique order as ``RANGE`` frames (module doc); the tree is rewritten in place."""

    if not keys:
        return tree
    for select in tree.find_all(exp.Select):
        windows = [w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]
        if not windows or select.args.get("windows"):
            continue
        for window in windows:
            if _applies(window) and covers_a_key(select, window.args.get("partition_by"), window.args["order"].expressions, keys, not_null):
                window.args["spec"].set("kind", "RANGE")
    return tree


def _applies(window: exp.Window) -> bool:
    spec = window.args.get("spec")
    if spec is None or window.args.get("order") is None or window.args.get("alias") or window.args.get("first"):
        return False
    if not isinstance(window.this, _ORDER_BLIND + _NAVIGATION):  # IgnoreNulls, Filter and the rest are other classes
        return False
    shape = _bound(spec)
    return shape is not None and shape[0] == "ROWS"
