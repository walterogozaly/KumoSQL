"""Decide whether every result of one query is also a result of another (query containment).

``check_containment(q1, q2, semantics=...)`` answers "is ``q1`` contained in ``q2``" on every
database satisfying the declared schema:

* ``semantics="set"``: every row ``q1`` returns, ``q2`` also returns (row multiplicities are ignored).
* ``semantics="bag"``: every row ``q1`` returns, ``q2`` returns at least as many times.

The two are different questions: ``SELECT x FROM t`` is set-contained in ``SELECT DISTINCT x FROM t``
and not bag-contained in it; the other direction holds in both. Answers are

* ``contained``: proven (the method is named; the algebraic prover did the proving);
* ``not_contained``: a concrete small database on which ``q1`` returns a row that ``q2`` does not
  return as often is attached (found by random search over databases that respect the schema, then
  re-run, so it is a checked fact, not a claim);
* ``unknown``: neither; the reason says why. ``unsupported`` marks shapes the decision procedures do
  not read, ``timeout`` an exhausted prover budget.

Proof methods, each reducing containment to an equivalence the prover can discharge:

* equal: ``q1`` and ``q2`` are equivalent as bags, which implies both containments;
* pre-filter: the filters of ``q1`` that ``q2`` lacks are added to ``q2`` (only on columns that
  survive any grouping) and the result is proven equivalent to ``q1``; a filtered copy of ``q2`` is
  contained in ``q2`` in every semantics, so equality with it proves ``q1`` is contained;
* post-filter: ``q1`` is rewritten as a filter over the rows of ``q2`` (the model-reuse rewriter
  restricted to an identity projection);
* distinct collapse: ``DISTINCT`` in ``q1`` only removes duplicates, so for bags it is dropped when
  ``q2`` does not have it;
* union (set semantics): ``q1 UNION q2`` is proven equivalent to ``q2 UNION q2``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import sqlglot
from sqlglot import exp

from . import model_reuse as mr
from .random_check import CheckError, Schema, Witness, find_difference
from .smt_equivalence import SmtStatus, TableConstraints


@dataclass(frozen=True)
class Containment:
    status: str  # contained | not_contained | unknown | unsupported | timeout | error
    semantics: str
    reason: str
    method: str | None = None
    witness: Witness | None = None
    assumptions: tuple[str, ...] = ()

    @property
    def contained(self) -> bool:
        return self.status == "contained"


def _prove(a: str, b: str, *, schema, constraints, types, timeout_ms, dialect, exact_arithmetic=False):
    from .algebraic_equivalence import prove_equivalent_algebraic

    try:
        return prove_equivalent_algebraic(
            a,
            b,
            schema=schema,
            constraints=dict(constraints) if constraints else None,
            types={t: dict(c) for t, c in types.items()} if types else None,
            timeout_ms=timeout_ms,
            dialect=dialect,
            compare_names=False,
            exact_arithmetic=exact_arithmetic,
        )
    except Exception:  # noqa: BLE001 - a prover failure is never a proof
        return None


def _prefilter(q1: mr._Block, q2: mr._Block, tree2: exp.Expression, schema: Mapping[str, Sequence[str]], dialect: str):
    """Yield copies of ``q2`` restricted by the conjuncts ``q1`` has and ``q2`` lacks."""

    if len(q1.tables) != len(q2.tables):
        return
    for mapping in mr._mappings(q2.tables, q1.tables):  # q1 alias -> q2 alias
        if len(mapping) != len(q1.tables):
            continue
        q1_conj = [mr._Rewriter._rename(c.copy(), mapping) for c in mr._expand_conjuncts(q1.conjuncts)]
        have = {mr._key(c) for c in mr._expand_conjuncts(q2.conjuncts)}
        extra = [c for c in q1_conj if mr._key(c) not in have]
        if q2.is_aggregate:
            groups = {mr._key(g) for g in q2.group}
            if not all(_over_groups(c, groups) for c in extra):
                continue
        if not extra:
            continue
        restricted = tree2.copy()
        combined = exp.Paren(this=mr._combine(extra))
        where = restricted.args.get("where")
        restricted.set("where", exp.Where(this=exp.And(this=exp.Paren(this=where.this.copy()), expression=combined) if where else combined))
        yield restricted.sql(dialect="postgres")


def _over_groups(node: exp.Expression, groups: set[str]) -> bool:
    """Whether a conjunct reads only grouping expressions (so it filters whole groups)."""

    if mr._key(node) in groups:
        return True
    if isinstance(node, exp.Column):
        return False
    return all(_over_groups(child, groups) for child in node.iter_expressions())


def _strip_distinct(tree: exp.Expression) -> exp.Expression:
    tree = tree.copy()
    if isinstance(tree, exp.Select) and tree.args.get("distinct"):
        tree.set("distinct", None)
    return tree


def check_containment(
    q1_sql: str,
    q2_sql: str,
    *,
    schema: Mapping[str, Sequence[str]],
    constraints: Mapping[str, TableConstraints] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    semantics: str = "set",
    dialect: str = "postgres",
    timeout_ms: int = 5000,
    database: Schema | None = None,
    trials: int = 200,
) -> Containment:
    """Is ``q1`` contained in ``q2`` under ``semantics`` ("set" or "bag")?

    ``database`` (a ``random_check.Schema``) enables the search for a counterexample database;
    without it a failed proof is only ``unknown``.
    """

    if semantics not in ("set", "bag"):
        raise ValueError("semantics must be 'set' or 'bag'")
    base = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}
    prove = lambda a, b: _prove(a, b, schema=base, constraints=constraints, types=types, timeout_ms=timeout_ms, dialect=dialect)  # noqa: E731
    assumptions: tuple[str, ...] = ()

    def proven(a: str, b: str, method: str) -> Containment | None:
        result = prove(a, b)
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            return Containment("contained", semantics, "proven by reducing containment to an equivalence", method, None, tuple(result.assumptions))
        return None

    blocks: tuple | None = None
    try:
        tree1, tree2 = mr._prepare(q1_sql, base, dialect), mr._prepare(q2_sql, base, dialect)
        nn = mr.not_null_columns(constraints)
        blocks = (mr._block(tree1, nn), mr._block(tree2, nn))
        unsupported = ""
    except mr._Unsupported as error:
        unsupported = str(error)
    except sqlglot.errors.SqlglotError as error:
        return Containment("unsupported", semantics, f"parse error: {error}")

    # equal
    done = proven(q1_sql, q2_sql, "equal")
    if done:
        return done
    if blocks is not None:
        b1, b2 = blocks
        # distinct collapse (bags): DISTINCT can only remove rows from q1
        sql1 = q1_sql
        if semantics == "bag" and b1.distinct and not b2.distinct:
            sql1 = _strip_distinct(tree1).sql(dialect="postgres")
            done = proven(sql1, q2_sql, "distinct-collapse+equal")
            if done:
                return done
        bag_ok = not (semantics == "bag" and b2.distinct and not b1.distinct)
        if bag_ok:
            for restricted in _prefilter(b1, b2, tree2, base, dialect):
                done = proven(sql1, restricted, "pre-filter")
                if done:
                    return done
            # post-filter: q1 as a filter over q2's own rows
            width = len(b2.outputs)
            if len(b1.outputs) == width:
                reuse = _post_filter(q1_sql, q2_sql, base, constraints, types, dialect, timeout_ms, width)
                if reuse is not None:
                    return Containment("contained", semantics, "q1 is a filter over the rows of q2", "post-filter", None, reuse.assumptions)
    if semantics == "set":
        done = proven(f"({q1_sql}) UNION ({q2_sql})", f"({q2_sql}) UNION ({q2_sql})", "union")
        if done:
            return done

    if database is not None:
        mode = "subset" if semantics == "set" else "subbag"
        try:
            witness = find_difference(database, q1_sql, q2_sql, mode=mode, trials=trials, dialect=dialect)
        except CheckError as error:
            return Containment("unknown", semantics, f"no proof, and no search was possible: {error}")
        if witness is not None:
            return Containment("not_contained", semantics, "a database where q1 returns a row that q2 does not", "random-search", witness)
    if unsupported:
        return Containment("unsupported", semantics, unsupported)
    return Containment("unknown", semantics, "no proof was found" + ("" if database is not None else " and no counterexample search was requested"))


def _post_filter(q1_sql, q2_sql, schema, constraints, types, dialect, timeout_ms, width):
    reuse = mr.rewrite_over_model(
        q1_sql,
        q2_sql,
        schema=schema,
        constraints=constraints,
        types=types,
        model_name="mv0",
        dialect=dialect,
        timeout_ms=timeout_ms,
        identity=True,
    )
    return reuse if reuse.rewritten else None
