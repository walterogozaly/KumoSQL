"""The bag-equivalence backend as a last resort behind the existing provers.

``prover_context.prove`` calls :func:`prove_last_resort` only when the algebraic prover (rule modules and
SMT) found no proof, so the backend can add proofs and never removes one. It never raises: any internal
error, timeout or unsupported query is "no proof here" and the caller keeps the result it already had.

The backend gets the same front-end checks the algebraic prover applies before it proves anything (string
and number literals, types BigQuery rejects, masked template text, mixed-type comparisons, the numeric
reading of strings under MySQL, and the parser-disagreement check), so a pair those refuse is refused here.
The backend's own work is capped: each z3 check by the prover's time limit, the whole proof by
``BUDGET_FACTOR`` times it.
"""

from __future__ import annotations

import dataclasses

from ..parse_check import refuse_misread_proofs
from ..solver_lock import serialized

BUDGET_FACTOR = 2  # the backend may spend at most this many time limits on one pair
ENABLED = True  # tests and callers can switch the hook off


def _decline(reason: str):
    from ..smt_equivalence import SmtEquivalenceResult, SmtStatus

    return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"bag procedure: {reason}")


# The backend reads a cast of an integer to an integer type as the identity (translate._cast). That holds for 64-bit
# signed integers only: a narrower or unsigned target wraps or clamps (CAST(-2 AS UNSIGNED) is 18446744073709551614).
# Until the translation models it, the hook refuses a pair with such a cast.
_NARROW_INTEGERS = (
    "TINYINT", "SMALLINT", "MEDIUMINT", "UTINYINT", "USMALLINT", "UMEDIUMINT", "UINT", "UBIGINT", "INT128", "UINT128", "INT256", "UINT256", "BIT",
)


def _narrow_integer_cast(sql: str, dialect: str) -> str | None:
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the backend reports the parse error itself
        return None
    wide_int = ("INT",) if dialect == "bigquery" else ()
    for cast in tree.find_all(exp.Cast, exp.TryCast):
        target = cast.args.get("to")
        name = getattr(getattr(target, "this", None), "name", "")
        if name in _NARROW_INTEGERS or (name == "INT" and name not in wide_int):
            return f"a cast to {target.sql(dialect=dialect)}, which the backend would read as a 64-bit signed integer"
    return None


def _front_end(left_sql: str, right_sql: str, kwargs: dict):
    """``(left, right, reason)``: the texts to prove, or why the pair is refused before any proof is tried."""

    from ..sqlx_fragments import masked_template_problem
    from ..string_literals import invalid_literal
    from ..type_names import invalid_type_name
    from .. import string_number_compare, string_number_literals
    from ..set_operation_types import mixed_types

    dialect = kwargs.get("dialect", "bigquery")
    types = kwargs.get("types")
    left_sql, right_sql = (string_number_literals.normalize(sql, dialect, types) for sql in (left_sql, right_sql))
    if dialect == "bigquery" and (invalid_literal(left_sql) or invalid_literal(right_sql)):
        return None, None, "a single-quoted literal holds a line break"
    if dialect == "bigquery":
        unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
        if unknown_type:
            return None, None, f"BigQuery would reject the query: {unknown_type}"
    masked = masked_template_problem(left_sql, right_sql, dialect=dialect or "bigquery")
    if masked:
        return None, None, str(masked)
    mixed = mixed_types(left_sql, types, dialect) or mixed_types(right_sql, types, dialect)
    if mixed:
        return None, None, str(mixed)
    compared = string_number_compare.problem(left_sql, dialect, types, plain_ok=True) or string_number_compare.problem(
        right_sql, dialect, types, plain_ok=True
    )
    if compared:
        return None, None, str(compared)
    narrow = _narrow_integer_cast(left_sql, dialect) or _narrow_integer_cast(right_sql, dialect)
    if narrow:
        return None, None, narrow
    return left_sql, right_sql, None


def _prove(left_sql: str, right_sql: str, **kwargs):
    from . import prove_bag_equivalent
    from ..set_operation_types import ASSUMPTION as SET_TYPES_ASSUMPTION, unchecked_types

    left, right, reason = _front_end(left_sql, right_sql, kwargs)
    if reason:
        return _decline(reason)
    dialect = kwargs.get("dialect", "bigquery")
    timeout_ms = kwargs.get("timeout_ms", 5000)
    result = prove_bag_equivalent(
        left,
        right,
        schema=kwargs.get("schema"),
        constraints=kwargs.get("constraints"),
        types=kwargs.get("types"),
        dialect=dialect,
        exact_arithmetic=bool(kwargs.get("exact_arithmetic", False)),
        compare_names=kwargs.get("compare_names", True),
        group_by_constants=bool(kwargs.get("group_by_constants", False)),
        timeout_ms=timeout_ms,
        budget_s=max(timeout_ms * BUDGET_FACTOR / 1000.0, 1.0),
    )
    if result.proven and (unchecked_types(left, kwargs.get("types"), dialect) or unchecked_types(right, kwargs.get("types"), dialect)):
        result = dataclasses.replace(result, assumptions=tuple(dict.fromkeys(tuple(result.assumptions) + (SET_TYPES_ASSUMPTION,))))
    return result


@refuse_misread_proofs
@serialized
def _guarded(left_sql: str, right_sql: str, **kwargs):
    from .. import numeric_column_reading

    return numeric_column_reading.checked(_prove, left_sql, right_sql, kwargs, _decline)


def prove_last_resort(left_sql: str, right_sql: str, **kwargs):
    """A proof of ``left_sql == right_sql`` from the bag backend, or ``None``. Never raises.

    ``kwargs`` are the arguments the algebraic prover was called with (``schema``, ``constraints``, ``types``,
    ``dialect``, ``exact_arithmetic``, ``compare_names``, ``group_by_constants``, ``timeout_ms``); others are
    ignored.
    """

    if not ENABLED:
        return None
    try:
        result = _guarded(left_sql, right_sql, **kwargs)
        return result if result.proven else None
    except BaseException as error:  # noqa: BLE001 - the hook only ever adds proofs
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return None
