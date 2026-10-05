"""Branchwise congruence for set operations the SMT prover does not model.

The prover reads ``INTERSECT`` and ``EXCEPT`` (duplicates removed) as existence tests but declines
``INTERSECT ALL`` and ``EXCEPT ALL``: they keep ``min(m, n)`` and ``max(m - n, 0)`` copies of a row
that the operands hold ``m`` and ``n`` times. This module proves two queries equal anyway when each is
the same set operation over operands that are equal one by one, using the prover for the operands
(which are ordinary queries, or set operations proved the same way):

* ``A op B`` and ``A' op B'`` are equal when ``A = A'`` and ``B = B'`` as bags, in this order. A set
  operation is a function of its operands' bags and nothing else.
* ``INTERSECT`` and ``UNION`` (ALL or not) are commutative, so ``A = B'`` and ``B = A'`` also works.
* A row that ``A`` lacks is never kept by ``A INTERSECT ALL B`` or ``A EXCEPT ALL B``, and its copies in
  ``B`` subtract from nothing. So when every row of ``A`` satisfies a filter ``f`` (``A = f(A)``), ``B``
  may be replaced by ``f(B)``: a row that fails ``f`` equals no row of ``A``. This is how
  ``SELECT .. WHERE f EXCEPT ALL B`` matches ``SELECT .. FROM (A EXCEPT ALL B) WHERE f`` once the filter
  is pushed into both branches: the left operands are equal, and ``f(B)`` is the pushed right branch.
  ``INTERSECT ALL`` is symmetric, so either operand can play the part of ``A``.

A replacement over a derived set operation is first read as the set operation itself: a WHERE over it
moves into every branch (:mod:`kumosql.union_filter_rules`; the moved conjuncts are remembered as the
``f`` above), and a select list that names each output once, in any order, reorders the columns of
every branch.

Nothing here changes a result the prover already gives: it is tried only after the prover said
"not proven", and a proof needs every operand pair to be proven by the prover. Branch types are
checked as for any set operation (:mod:`kumosql.set_operation_types`).
"""

from __future__ import annotations

from collections.abc import Callable

import sqlglot
from sqlglot import exp

from .set_operation_types import ASSUMPTION as SET_TYPES_ASSUMPTION, mixed_types, unchecked_types
from .smt_equivalence import SmtEquivalenceResult, SmtStatus
from .union_filter_rules import _branches, _conjuncts, _unwrap_identity, push_filter_into_set_operation

_COMMUTATIVE = (exp.Union, exp.Intersect)
_FILTERABLE = (exp.Intersect, exp.Except)
_PROOF_BUDGET = 14  # calls to the prover for one pair; a step that needs more is not tried
_ROW_CLAUSES = ("limit", "offset", "with_", "with", "by_name", "side", "kind", "on")

REASON = "the set operations' operands are proven equal one by one (same operator and operand order)"


def prove_by_congruence(
    left_sql: str,
    right_sql: str,
    prove: Callable[[str, str], SmtEquivalenceResult | None],
    *,
    types: dict[str, dict[str, str]] | None = None,
    dialect: str = "postgres",
) -> SmtEquivalenceResult | None:
    """A proof that ``left_sql`` and ``right_sql`` return the same bag, or None.

    ``prove(a, b)`` is the prover for a pair of queries (None or a result that is not proven means no). Both
    sides must read as the same set operation (see the module comment); otherwise the answer is None.
    """

    try:
        left, right = sqlglot.parse_one(left_sql, read=dialect), sqlglot.parse_one(right_sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if left.find(exp.SetOperation) is None or right.find(exp.SetOperation) is None:
        return None
    if mixed_types(left_sql, types, dialect) or mixed_types(right_sql, types, dialect):
        return None
    run = _Congruence(prove, dialect)
    try:
        proven = run.same(left, right, top=True)
    except RecursionError:
        return None
    if not proven:
        return None
    assumptions = list(run.assumptions)
    if unchecked_types(left_sql, types, dialect) or unchecked_types(right_sql, types, dialect):
        assumptions.append(SET_TYPES_ASSUMPTION)
    return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, REASON, assumptions=tuple(dict.fromkeys(assumptions)))


class _Filter:
    """The conjuncts a WHERE moved into the branches of a set operation, over its output names."""

    def __init__(self, conjuncts: list[exp.Expression], names: list[str], alias: str):
        self.conjuncts, self.names, self.alias = conjuncts, names, alias

    def apply(self, query: exp.Expression) -> exp.Expression | None:
        """``query`` keeping the rows the filter keeps (its columns read by position), or None."""

        names = _names(query)
        if names is None or len(names) != len(self.names):
            return None
        where = None
        for conjunct in self.conjuncts:
            part = conjunct.copy()
            holder = exp.Paren(this=part)
            for column in list(holder.find_all(exp.Column)):
                column.replace(exp.column(names[self.names.index(column.name.lower())], table="_sc_rows"))
            part = holder.this
            part = exp.Paren(this=part) if isinstance(part, exp.Or) else part
            where = part if where is None else exp.And(this=where, expression=part)
        if where is None:
            return None
        source = exp.Subquery(this=_strip(query).copy(), alias=exp.TableAlias(this=exp.to_identifier("_sc_rows")))
        return exp.select(*(exp.column(name, table="_sc_rows") for name in names)).from_(source).where(where)


class _Congruence:
    def __init__(self, prove, dialect: str):
        self.prove = prove
        self.dialect = dialect
        self.calls = 0
        self.assumptions: list[str] = []
        self.memo: dict[tuple[str, str], bool] = {}

    def sql(self, node: exp.Expression) -> str:
        return node.sql(dialect=self.dialect)

    def plain(self, a: exp.Expression, b: exp.Expression) -> bool:
        """Does the prover itself say the two queries are equal?"""

        if self.calls >= _PROOF_BUDGET:
            return False
        self.calls += 1
        try:
            result = self.prove(self.sql(a), self.sql(b))
        except Exception:  # noqa: BLE001 - a prover crash is never a proof
            return False
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            self.assumptions.extend(result.assumptions)
            return True
        return False

    def same(self, a: exp.Expression, b: exp.Expression, top: bool = False) -> bool:
        a, filter_a = _expose(a)
        b, filter_b = _expose(b)
        if a is None or b is None:
            return False
        key = (self.sql(a), self.sql(b))
        if key in self.memo:
            return self.memo[key]
        self.memo[key] = False  # a pair that needs itself proves nothing
        proven = not top and self.plain(a, b)  # at the top the caller's prover has already said no
        if not proven and _matching(a, b):
            proven = self.congruent(a, b, filter_a, filter_b)
        self.memo[key] = proven
        return proven

    def congruent(self, a: exp.Expression, b: exp.Expression, filter_a: _Filter | None, filter_b: _Filter | None) -> bool:
        a_left, a_right, b_left, b_right = a.this, a.expression, b.this, b.expression
        if self.same(a_left, b_left) and self.same(a_right, b_right):
            return True
        if isinstance(a, _COMMUTATIVE) and self.same(a_left, b_right) and self.same(a_right, b_left):
            return True
        if not isinstance(a, _FILTERABLE) or (filter_a is None and filter_b is None):
            return False
        # operand pairs (kept operand of each side, operand to filter of each side); INTERSECT ALL has no left or right
        roles = [((a_left, b_left), (a_right, b_right))]
        if isinstance(a, exp.Intersect):
            roles += [((a_right, b_right), (a_left, b_left)), ((a_left, b_right), (a_right, b_left)), ((a_right, b_left), (a_left, b_right))]
        for (keep_a, keep_b), (other_a, other_b) in roles:
            if self.same(keep_a, keep_b) and self.agree_under_filter(keep_a, keep_b, other_a, other_b, filter_a, filter_b):
                return True
        return False

    def agree_under_filter(self, keep_a, keep_b, other_a, other_b, filter_a: _Filter | None, filter_b: _Filter | None) -> bool:
        """``keep_a = keep_b``; do the other operands agree on the rows the kept one holds?

        With a filter ``f`` that every row of the kept operand satisfies, ``other_b = f(other_a)`` suffices
        (``f`` read from ``b``'s derived set operation), or ``other_a = f(other_b)`` (``f`` read from ``a``'s).
        """

        for flt, kept, source, target in ((filter_b, keep_a, other_a, other_b), (filter_a, keep_b, other_b, other_a)):
            if flt is None:
                continue
            kept_filtered = flt.apply(kept)
            moved = flt.apply(source)
            if kept_filtered is None or moved is None:
                continue
            if self.same(kept, kept_filtered) and self.same(target, moved):
                return True
        return False


def _strip(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren) or (isinstance(node, exp.Subquery) and not node.alias and not any(node.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample"))):
        node = node.this
    return node


def _matching(a: exp.Expression, b: exp.Expression) -> bool:
    """Both are the same set operation, ALL or not, with nothing else attached."""

    if not isinstance(a, exp.SetOperation) or type(a) is not type(b):
        return False
    if bool(a.args.get("distinct")) != bool(b.args.get("distinct")):
        return False
    return not any(n.args.get(k) for n in (a, b) for k in _ROW_CLAUSES)


def _expose(node: exp.Expression) -> tuple[exp.Expression | None, _Filter | None]:
    """The query as a set operation when it is one, or a derived set operation read through a filter or a
    reordering of its columns; the query itself otherwise (None when a LIMIT or similar is attached).
    The filter is what a WHERE moved into the branches, for :meth:`_Congruence.agree_under_filter`."""

    node = _strip(node)
    if isinstance(node, exp.SetOperation):
        if node.args.get("order") is not None and not any(node.args.get(k) for k in ("limit", "offset")):
            node = node.copy()
            node.set("order", None)  # the rows are compared as a bag
        return node, None
    if not isinstance(node, exp.Select) or any(node.args.get(k) for k in ("limit", "offset", "with_", "with")):
        return node, None
    if any(node.args.get(k) for k in ("joins", "laterals", "pivots", "sample", "group", "having", "qualify", "windows")):
        return node, None
    if node.args.get("order") is not None:
        node = node.copy()
        node.set("order", None)
    from_ = node.args.get("from_") or node.args.get("from")
    derived = from_.this if from_ is not None else None
    if isinstance(derived, exp.Subquery) and derived.alias and isinstance(derived.this, exp.Select):
        # a select that only lists the columns of a derived set operation: read the set operation through it
        inner, _ = _expose(derived.this)
        if isinstance(inner, exp.SetOperation) and inner is not derived.this:
            node = node.copy()
            derived = (node.args.get("from_") or node.args.get("from")).this
            derived.set("this", inner.copy())
    if not isinstance(derived, exp.Subquery) or not derived.alias or not isinstance(derived.this, exp.SetOperation):
        return node, None
    branches = _branches(derived.this)
    if not branches:
        return node, None
    names = [item.alias_or_name.lower() for item in branches[0].expressions]
    if "" in names or len(set(names)) != len(names) or any(len(b.expressions) != len(names) for b in branches):
        return node, None
    alias = derived.alias.lower()
    where = node.args.get("where")
    if where is None:
        return _unwrap_identity(node.copy(), alias, names) or node, None
    pushed = push_filter_into_set_operation(node.copy())
    if pushed is None:
        return node, None
    kept = {_text(c) for c in _conjuncts(pushed.args["where"].this)} if isinstance(pushed, exp.Select) and pushed.args.get("where") is not None else set()
    moved = [c.copy() for c in _conjuncts(where.this) if _text(c) not in kept]
    if any(not _row_function(c, names, alias) for c in moved):
        return _strip(pushed), None  # the rewrite is exact either way; only the filter is not usable below
    return _strip(pushed), _Filter(moved, names, alias)


def _text(node: exp.Expression) -> str:
    return (node.this if isinstance(node, exp.Paren) else node).sql()


def _row_function(conjunct: exp.Expression, names: list[str], alias: str) -> bool:
    """Whether the conjunct reads only the set operation's outputs, with no subquery, aggregate, window or random value."""

    columns = list(conjunct.find_all(exp.Column))
    return bool(columns) and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Rand, exp.Anonymous)) for n in conjunct.walk()) and all(
        c.name.lower() in names and c.table.lower() in ("", alias) and not isinstance(c.this, exp.Star) for c in columns
    )


def _names(node: exp.Expression) -> list[str] | None:
    """The output names of a query, or None when one is missing or repeated."""

    names = list(_strip(node).named_selects)
    if not names or any(not name for name in names) or len({n.lower() for n in names}) != len(names):
        return None
    return names
