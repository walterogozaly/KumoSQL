"""Ties under ``ORDER BY ... LIMIT``: a difference that only shows which of several tied rows an engine returns.

``engine.confirm`` already discards a difference when reloading the same rows in another order changes a
result. That misses ties that two engines (or two spellings of one query) resolve differently while each one
is stable under reordering: a SQLite ``GROUP BY`` returns its groups sorted, DuckDB's hash table in hash
order, so ``GROUP BY d ORDER BY COUNT(x) DESC LIMIT 1`` over two groups with a count of 0 gives each engine
its own pick. Neither is wrong, and the pair is not a false proof.

``tie_dependent`` probes for this on the database a difference was found on. Every query with a ``LIMIT``
or ``OFFSET`` (and a final ``ORDER BY`` when the comparison counts row order) gets all of its output
columns, by position, as the last ``ORDER BY`` keys: once ascending, once descending. The two readings
agree when no tie decides which rows come back (rows that tie on every key and on every output column are
the same row to a bag comparison). When they differ, the query's result depends on an unspecified choice
between tied rows, so a difference found with it is ``nondeterministic``.

The probe works on parsed SQL and runs it through the runner's own engine. Anything it cannot read (a ``*``
in the select list, an unparsable statement, a query the engine rejects) is not probed, so a difference
is only ever discarded on evidence.
"""

from __future__ import annotations

from sqlglot import exp
import sqlglot


def _output_width(node: exp.Expression) -> int | None:
    """How many columns ``node`` returns, or ``None`` when a ``*`` hides it."""

    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    if isinstance(node, exp.SetOperation if hasattr(exp, "SetOperation") else exp.Union):
        return _output_width(node.this)
    if isinstance(node, exp.Select):
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in node.expressions):
            return None
        return len(node.expressions) or None
    return None


def _limited(node: exp.Expression) -> bool:
    return node.args.get("limit") is not None or node.args.get("offset") is not None or node.args.get("fetch") is not None


def _targets(tree: exp.Expression, list_mode: bool) -> list[exp.Expression]:
    """Queries whose result can depend on ties: every limited one, and the top one when row order counts."""

    found = []
    kinds = (exp.Select, exp.SetOperation) if hasattr(exp, "SetOperation") else (exp.Select, exp.Union)
    for node in tree.find_all(*kinds):
        if _limited(node) or (list_mode and node is tree and node.args.get("order") is not None):
            found.append(node)
    return found


def tie_variants(sql: str, dialect: str, list_mode: bool = False) -> tuple[str, str] | None:
    """``sql`` twice, with the output columns appended to every relevant ``ORDER BY`` ascending and descending;
    ``None`` when nothing in ``sql`` can depend on ties (or it cannot be read)."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return None
    if not _targets(tree, list_mode):
        return None
    out = []
    for descending in (False, True):
        copy = tree.copy()
        changed = False
        for node in _targets(copy, list_mode):
            width = _output_width(node)
            if width is None:
                continue
            keys = list(node.args["order"].expressions) if node.args.get("order") is not None else []
            # the descending reading is the exact reverse of the ascending one, NULLs included
            keys += [exp.Ordered(this=exp.Literal.number(i), desc=descending, nulls_first=not descending) for i in range(1, width + 1)]
            node.set("order", exp.Order(expressions=keys))
            changed = True
        if not changed:
            return None
        try:
            out.append(copy.sql(dialect=dialect))
        except Exception:  # a construct sqlglot cannot write again
            return None
    return out[0], out[1]


def tie_dependent(runner, data) -> bool:
    """Whether either query of ``runner.case`` returns different rows when its ties are broken the other way on ``data``."""

    from .engine import QueryError, view

    case = runner.case
    run = getattr(runner, "run_side", None) or (lambda side, sql: runner.run(sql))
    dialect_of = getattr(runner, "dialect_of", None) or (lambda side: "duckdb")
    try:
        runner.load(data)
    except QueryError:
        return False
    for side, sql in enumerate((case.left, case.right)):
        variants = tie_variants(sql, dialect_of(side), list_mode=case.mode == "list")
        if variants is None:
            continue
        try:
            ascending, descending = (run(side, text) for text in variants)
        except Exception:  # an engine error is no evidence of a tie
            continue
        if view(ascending, case.mode, case.float_digits) != view(descending, case.mode, case.float_digits):
            return True
    return False
