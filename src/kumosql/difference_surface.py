"""Shared wiring for showing a difference explanation ("equivalent except when P", issue #512).

The API, the command line and the change report all ask for the predicate the same way: through
``difference_explanation.explain_difference``, looked up at call time, with any failure meaning "no predicate".
"""

from __future__ import annotations


def except_when(left_sql: str, right_sql: str, **prover_options: object) -> dict | None:
    """``{"sql", "atoms", "tables", "exact"}`` for a verified difference predicate, or None when there is none."""

    from . import difference_explanation

    try:
        explanation = difference_explanation.explain_difference(left_sql, right_sql, **prover_options)
    except Exception:  # noqa: BLE001 - the predicate is an addition; a failed search shows nothing, never an error
        return None
    if explanation is None:
        return None
    payload = explanation.to_json()
    return payload if payload.get("sql") else None


def describe(payload: dict) -> list[str]:
    """Lines for a terminal: the predicate, and whether it is the whole difference."""

    return [f"except when: {payload['sql']}", f"exact: {'yes' if payload.get('exact') else 'no'}"]
