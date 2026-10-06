"""The three ways an evaluation can end without a result."""

from __future__ import annotations


class Unsupported(Exception):
    """The evaluator does not implement this construct (or cannot be sure how BigQuery reads it).

    Never a verdict about the query: the caller falls back to another engine or reports unknown.
    """


class AnalysisError(Exception):
    """BigQuery would reject the query before running it (a type error, an unknown name, a misplaced aggregate)."""

    code = "invalid_argument"


class EvalError(Exception):
    """BigQuery would fail while running the query on this data (division by zero, overflow, a bad cast...).

    ``SAFE.`` calls and ``SAFE_CAST`` turn these into ``NULL``; nothing else does.
    """

    code = "out_of_range"


def unsupported(what: str) -> Unsupported:
    return Unsupported(what)
