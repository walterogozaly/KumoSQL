"""A bag-equivalence decision procedure over the multiplicity algebra (SQLSolver's approach).

Each query becomes a function from output tuples to their multiplicities
(:mod:`.translate`), written as a sum of products of table multiplicities, indicators and
values (:mod:`.normalize`). Two such sums are compared term group by term group with z3
over linear arithmetic (:mod:`.decide`). Declared keys, NOT NULL columns and foreign keys
are axioms. A query outside the fragment, or a check that runs out of time, is unknown:
this module never refutes.

    prove_bag_equivalent(left_sql, right_sql, schema=..., constraints=..., types=...)

returns an :class:`~kumosql.smt_equivalence.SmtEquivalenceResult` that is either proven
or not proven. See ``docs/provers.md`` for the fragment and where it is incomplete.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .translate import Catalog, Translator, Unsupported

__all__ = ["prove_bag_equivalent", "Unsupported"]

DECLARED_CONSTRAINTS_ASSUMPTION = "the declared keys, NOT NULL columns and foreign keys hold"


def _parse(sql: str, dialect: str, schema):
    from ..ast_utils import canonical_negation, check_modeled, expand_alias_columns, strip_positions

    tree = sqlglot.parse_one(sql, read=dialect)
    tree = strip_positions(tree)
    tree = check_modeled(canonical_negation(tree))
    return expand_alias_columns(tree, schema)


def prove_bag_equivalent(
    left_sql: str,
    right_sql: str,
    *,
    schema=None,
    constraints=None,
    types=None,
    dialect: str = "bigquery",
    exact_arithmetic: bool = False,
    compare_names: bool = True,
    group_by_constants: bool = False,
    timeout_ms: int = 5000,
    use_foreign_keys: bool = True,
):
    """Prove that two queries return the same bag of rows on every database, or say not proven."""

    from ..smt_equivalence import (
        BASE_ASSUMPTIONS,
        EXACT_ARITHMETIC_ASSUMPTION,
        TIE_ASSUMPTION,
        SmtEquivalenceResult,
        SmtStatus,
        _split_limit,
    )

    def unknown(reason: str):
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"bag procedure: {reason}")

    try:
        import z3  # noqa: F401
    except ImportError:
        return unknown("z3 is not installed")
    try:
        from ..set_operations import positional_sql_pair
        from ..string_literals import canonical_literals, invalid_literal

        if dialect == "bigquery":
            if invalid_literal(left_sql) or invalid_literal(right_sql):
                return unknown("a literal that is not valid GoogleSQL")
            left_sql, right_sql = canonical_literals(left_sql), canonical_literals(right_sql)
        left_sql, right_sql, problem = positional_sql_pair(left_sql, right_sql, dialect)
        if problem:
            return unknown(f"BY NAME set operation ({problem})")
        assumptions = list(BASE_ASSUMPTIONS)
        left_core, left_spec = _split_limit(left_sql, dialect)
        right_core, right_spec = _split_limit(right_sql, dialect)
        if left_core is None or right_core is None:
            return unknown(str(left_spec if left_core is None else right_spec))
        if left_spec != right_spec:
            return unknown("different ORDER BY .. LIMIT")
        if left_spec is not None and not left_spec[3]:
            assumptions.append(TIE_ASSUMPTION)
        if not use_foreign_keys and constraints:
            from dataclasses import replace

            constraints = {k: replace(v, foreign_keys=()) if hasattr(v, "foreign_keys") else v for k, v in constraints.items()}
        catalog = Catalog(schema, types, constraints, dialect)
        translator = Translator(catalog, exact=exact_arithmetic, group_by_constants=group_by_constants)
        left_q = translator.translate(_parse(left_core, dialect, schema))
        right_q = translator.translate(_parse(right_core, dialect, schema))
        if len(left_q.out) != len(right_q.out):
            return unknown("the queries return different numbers of columns")
        if compare_names and [n.lower() for n in left_q.names] != [n.lower() for n in right_q.names]:
            return unknown("the queries name their columns differently")
        proven = _decide(left_q, right_q, catalog, exact_arithmetic, timeout_ms)
    except Unsupported as error:
        return unknown(f"unsupported: {error}")
    except (sqlglot.errors.SqlglotError, RecursionError) as error:
        return unknown(f"unsupported: {type(error).__name__}")
    except Exception as error:  # noqa: BLE001 - the backend only ever adds proofs
        from ..ast_utils import UnmodeledConstruct

        if isinstance(error, UnmodeledConstruct):
            return unknown(f"unsupported: {error}")
        return unknown(f"internal error: {type(error).__name__}: {error}")
    if proven is None:
        return unknown("timeout")
    if not proven:
        return unknown("no proof found")
    if exact_arithmetic:
        assumptions.append(EXACT_ARITHMETIC_ASSUMPTION)
    if catalog.used_constraints:
        assumptions.append(DECLARED_CONSTRAINTS_ASSUMPTION)
    return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, "proved by the bag procedure (multiplicity algebra)", assumptions=tuple(dict.fromkeys(assumptions)))


def _decide(left_q, right_q, catalog, exact: bool, timeout_ms: int):
    """``True`` proven, ``False`` not proven, ``None`` out of time."""

    from .decide import Prover, Timeout
    from .ir import Ref, subst
    from .normalize import Ctx, normalize

    right_body = subst(right_q.body, {b: Ref(a) for a, b in zip(left_q.out, right_q.out)})
    ctx = Ctx(catalog, exact)
    try:
        a_terms = normalize(left_q.body, ctx)
        b_terms = normalize(right_body, ctx)
        prover = Prover(ctx, timeout_ms=timeout_ms)
        return prover.bag_equal(a_terms, b_terms)
    except Timeout:
        return None
