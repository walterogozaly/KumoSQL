"""Exact reading of the window relations the SMT prover keeps whole (``kqw*`` derived tables).

``algebraic_equivalence._isolate_windows`` moves a select's window functions into a derived table
``(SELECT cols, WINDOW(..) AS v .. FROM F WHERE c) AS kqw``; the prover names such a relation by its text.
This module says when that is exact and when two differently written bodies are the same relation.

**Tie-free windows.** A window's value for a row is a function of the *bag* of rows in the row's
partition, and of nothing else, when it is

* ``SUM``, ``COUNT``, ``COUNTIF``, ``MIN``, ``MAX`` or ``AVG`` over the whole partition (no frame), over a
  ``RANGE`` frame or the default one (a ``RANGE`` frame is bounded by order-key values, so tied rows enter
  and leave it together), over ``ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`` or over
  ``ROWS BETWEEN CURRENT ROW AND CURRENT ROW``;
* ``RANK``, ``DENSE_RANK``, ``PERCENT_RANK`` or ``CUME_DIST``, which give every row of a tied group the same
  value whatever order the engine puts the group in.

Everything else (``ROW_NUMBER``, ``NTILE``, ``LAG``/``LEAD``, ``FIRST_VALUE``/``LAST_VALUE``/``NTH_VALUE``,
``ROWS`` frames that can end between tied rows, collectors, ``IGNORE NULLS``, any other function or frame) is
*tie-dependent or not modeled*: it stays an opaque relation under the prover's window assumption (equal text,
equal input, tied rows resolve alike), and :func:`same_relation` never identifies two different spellings of it.

**Same relation, different inputs.** Two bodies ``SELECT items FROM F1 WHERE c1`` and
``SELECT items' FROM F2 WHERE c2`` (no grouping, DISTINCT, ORDER BY, LIMIT, set operation or subquery in the
select list) are the same relation when

1. every window in both is tie-free;
2. the select lists match item by item once each column is replaced by the number of its first occurrence in
   the list (so ``SUM(kq0.c) OVER (PARTITION BY kq0.b), kq0.a`` and ``SUM(kq1.c) OVER (PARTITION BY kq1.b),
   kq1.a`` match: the same function of the same three fields, whatever the table aliases are); and
3. the *input bags* ``SELECT col_1, .. col_n FROM F1 WHERE c1`` and ``.. FROM F2 WHERE c2`` (the fields in
   first-occurrence order) are proven equal by the caller's prover.

Then each row of one side is a field tuple of the other's input bag together with values that only depend
on that bag, so the two output bags are equal. The proof is exact: no tie assumption, and a pair that
cannot be shown (anything outside this shape, an unproven input) keeps its two different identities.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import exp

_ORDER_BLIND = {"SUM", "COUNT", "COUNTIF", "MIN", "MAX", "AVG"}
_PEER_STABLE = {"RANK", "DENSERANK", "PERCENTRANK", "CUMEDIST"}
_CLAUSES = ("group", "having", "qualify", "windows", "distinct", "order", "limit", "offset", "with_", "with", "laterals", "pivots", "sample", "locks")


def _function(node: exp.Expression) -> str:
    """The upper-case name of a function node without underscores (``DENSE_RANK`` is ``DENSERANK``)."""

    name = node.name if isinstance(node, exp.Anonymous) else (node.sql_name() if isinstance(node, exp.Func) else "")
    return (name or "").upper().replace("_", "")


def _bound(spec: exp.Expression, side: str) -> str:
    value = spec.args.get(side)
    text = value if isinstance(value, str) else (value.sql() if value is not None else "")
    return f"{text} {spec.args.get(f'{side}_side') or ''}".strip().upper()


def is_tie_free(window: exp.Window) -> bool:
    """Whether the window's value depends only on the bag of rows of its partition (module doc)."""

    function = window.this
    name = _function(function)
    if name in _PEER_STABLE:
        return isinstance(function, exp.Func)
    if name not in _ORDER_BLIND or not isinstance(function, exp.Func):
        return False
    spec = window.args.get("spec")
    if spec is None:
        return True
    kind = str(spec.args.get("kind") or "").upper()
    if kind == "RANGE":
        return True
    if kind != "ROWS":
        return False
    start, end = _bound(spec, "start"), _bound(spec, "end") or "CURRENT ROW"
    return (start, end) in {("UNBOUNDED PRECEDING", "UNBOUNDED FOLLOWING"), ("CURRENT ROW", "CURRENT ROW")}


def depends_on_ties(node: exp.Expression) -> bool:
    """Whether ``node`` holds a window that is not tie-free, so the prover's window assumption is needed."""

    return any(not is_tie_free(window) for window in node.find_all(exp.Window))


def same_relation(left_sql: str, right_sql: str, parse: Callable[[str], exp.Expression], prove: Callable[[str, str], bool]) -> bool | None:
    """Whether two window bodies are provably one relation (module doc); None when neither body has a window.

    ``parse`` reads a body into a tree and ``prove`` says whether two plain selects return the same bag
    (both are the caller's, so this module needs no prover).
    """

    try:
        left, right = parse(left_sql), parse(right_sql)
    except Exception:  # noqa: BLE001 - a body the caller cannot read is not identified
        return False
    if not any(tree.find(exp.Window) for tree in (left, right)):
        return None
    shaped = [_shape(tree) for tree in (left, right)]
    if None in shaped:
        return False
    (left_items, left_inputs), (right_items, right_inputs) = shaped
    if left_items != right_items or len(left_inputs) != len(right_inputs):
        return False
    return prove(_input_sql(left, left_inputs), _input_sql(right, right_inputs))


def _shape(tree: exp.Expression):
    """``(item templates, input columns in first-occurrence order)`` of a plain windowed select, else None."""

    while isinstance(tree, exp.Subquery) and not tree.args.get("alias"):
        tree = tree.this
    if not isinstance(tree, exp.Select) or any(tree.args.get(key) for key in _CLAUSES):
        return None
    if not (tree.args.get("from_") or tree.args.get("from")):
        return None
    windows = [w for w in tree.find_all(exp.Window)]
    if not windows or not all(is_tie_free(w) for w in windows):
        return None
    inputs: list[exp.Column] = []
    seen: dict[str, int] = {}
    templates = []
    for item in tree.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        # COUNT(*) is fine; a star item or table.* is not (its columns are unknown here)
        if isinstance(value, exp.Star) or any(
            isinstance(n, (exp.Subquery, exp.Select, exp.Lambda)) or (isinstance(n, exp.Column) and isinstance(n.this, exp.Star))
            for n in value.walk()
        ):
            return None
        copy = exp.Tuple(expressions=[value.copy()])  # a holder, so that an item that is itself a column can be replaced
        for column in list(copy.find_all(exp.Column)):
            key = column.sql()
            if key not in seen:
                seen[key] = len(inputs)
                inputs.append(column)
            column.replace(exp.Placeholder(this=f"f{seen[key]}"))
        templates.append(copy.expressions[0].sql(dialect="bigquery", normalize_functions="upper"))
    return tuple(templates), [c.copy() for c in inputs]


def _input_sql(tree: exp.Expression, inputs: list[exp.Column]) -> str:
    """The select returning, for every row of ``tree``'s FROM and WHERE, the fields its items read."""

    while isinstance(tree, exp.Subquery) and not tree.args.get("alias"):
        tree = tree.this
    select = tree.copy()
    fields = [exp.alias_(column.copy(), f"kq_f{i}") for i, column in enumerate(inputs)] or [exp.alias_(exp.Literal.number(1), "kq_f0")]
    select.set("expressions", fields)
    return select.sql(dialect="bigquery")
