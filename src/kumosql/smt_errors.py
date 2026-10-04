"""Runtime errors as an outcome of the SMT prover.

BigQuery raises an error for a division by zero, an INT64 overflow, a failed ``CAST`` and the like, and it
promises no evaluation order between ``WHERE``, ``ON`` and the select list: an operation that can fail may run
on a row that a filter would have dropped. Only the branches of ``CASE``, ``IF``, ``COALESCE`` and ``NULLIF``
(which BigQuery evaluates lazily) and the ``SAFE_`` functions keep an operation from running.

So a query's **error exposure** is the set of operations that can fail, each with the condition under which it
fails and the guard that must hold for it to run, over every row of its ``FROM`` (filters do not guard). The
compiler records each as a :class:`Site` and assumes none of them fires when it proves two queries return the same
rows: the proof is equivalence on the databases where neither query errors. This module then compares the two
exposures and says what the rewrite does to errors (:class:`ErrorReport`):

* ``none``: neither query has an operation that can fail, so the proof is exact;
* ``same``: every operation that can fail in one query is covered by one in the other (the same operation, on the
  same row, under a weaker guard): the queries fail on the same databases, up to BigQuery's choice of order;
* ``refines``: every operation that can fail in the rewrite is covered by one in the original, and not the other
  way round: the rewrite never fails where the original succeeds, and the original fails somewhere the rewrite does not;
* ``introduces``: some operation of the rewrite can fail on a database where the original succeeds, shown by a
  concrete database (only when no uninterpreted function stands in for a value of the failing condition);
* ``unknown``: none of the above could be established (a failing condition over an uninterpreted function, a
  solver limit, an operation the original has in a different shape).

One site covers another when there is a map from the covering query's rows to the covered site's rows (rows of the
same table, as the prover maps occurrences for a bag proof) under which the covered site's condition implies
the covering site's: if the rewrite fails on a row tuple, the original fails on the tuple the map picks from the
same database. A map needs every table of the original's ``FROM`` to appear in the rewrite site's ``FROM``,
since the original only fails where every one of its tables has a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools

from .solver_lock import bounded_solver

try:  # pragma: no cover
    import z3
except ImportError:  # pragma: no cover
    z3 = None

NONE, SAME, REFINES, INTRODUCES, UNKNOWN = "none", "same", "refines", "introduces", "unknown"
_MAX_MAPS = 2000


@dataclass
class Site:
    """One operation that can raise a runtime error.

    ``fire`` is TRUE on a row exactly when the operation runs on it (its guards hold) and fails; ``scope`` maps the uid of
    every table occurrence of its ``FROM`` (and enclosing ``FROM`` clauses) to the occurrence; ``certain`` is
    ``fire`` when it mentions no uninterpreted function, so a model of it is a database on which the operation really fails.
    """

    kind: str
    text: str
    fire: object
    scope: dict = field(default_factory=dict)
    certain: bool = False
    under: object = None  # implies the failure for a concrete database (``fire`` itself when ``certain``)

    def describe(self) -> str:
        return f"{self.kind}: {self.text}"


@dataclass(frozen=True)
class ErrorReport:
    verdict: str
    detail: str
    sites: tuple[str, ...] = ()
    original_sites: tuple[str, ...] = ()


def uninterpreted_free(formula) -> bool:
    """Whether ``formula`` applies no uninterpreted function (constants are fine)."""

    seen: set = set()
    stack = [formula]
    while stack:
        node = stack.pop()
        if node.get_id() in seen:
            continue
        seen.add(node.get_id())
        if z3.is_app(node):
            if node.num_args() and node.decl().kind() == z3.Z3_OP_UNINTERPRETED:
                return False
            stack.extend(node.children())
    return True


def _occ_pairs(src, dst) -> list:
    pairs = []
    for name, v in list(src.cols.items()):
        target = dst.col(name)
        pairs.append((v.null, target.null))
        pairs.append((v.val, target.val))
    return pairs


def _maps(src_scope: dict, dst_scope: dict):
    """Every way to send each occurrence of ``src_scope`` to an occurrence of the same table in ``dst_scope``."""

    choices = []
    for occ in src_scope.values():
        options = [d for d in dst_scope.values() if d.table == occ.table]
        if not options:
            return
        choices.append([(occ, d) for d in options])
    for count, combo in enumerate(itertools.product(*choices)):
        if count >= _MAX_MAPS:
            raise _TooMany()
        yield list(combo)


class _TooMany(Exception):
    pass


class _Comparison:
    def __init__(self, prover, facts):
        self.prover = prover
        self.facts = list(facts)
        self.limited = False

    def _solver(self, scope: dict, formula, want: str):
        occs = list(scope.values())
        solver = bounded_solver(self.prover.timeout_ms)
        solver.add(*self.prover._typing(occs))
        solver.add(*self.prover._constraint_facts(occs))
        solver.add(*self.facts)
        solver.add(formula)
        return solver

    def satisfiable(self, site: Site) -> bool | None:
        result = self._solver(site.scope, site.fire, "sat").check()
        if result == z3.unknown:
            self.limited = True
            return None
        return result == z3.sat

    def coverers(self, site: Site, others: list[Site]):
        """The conditions under which ``others`` fail on the rows of ``site``'s tuple, one per map."""

        terms = []
        for other in others:
            for mapping in _maps(other.scope, site.scope):
                pairs = [p for src, dst in mapping for p in _occ_pairs(src, dst)]
                terms.append(z3.substitute(other.fire, *pairs) if pairs else other.fire)
        return terms

    def covered(self, site: Site, others: list[Site]) -> bool:
        try:
            terms = self.coverers(site, others)
        except _TooMany:
            self.limited = True
            return False
        target = z3.Or(*terms) if terms else z3.BoolVal(False)
        solver = self._solver(site.scope, z3.And(site.fire, z3.Not(target)), "sat")
        result = solver.check()
        if result == z3.unsat:
            return True
        if result == z3.unknown:
            self.limited = True
        return False

    def witness(self, site: Site, others: list[Site]) -> bool:
        """A database on which ``site`` fails and no operation of ``others`` does."""

        if site.under is None or not all(o.certain for o in others):
            return False
        try:
            terms = self.coverers(site, others)
        except _TooMany:
            return False
        target = z3.Or(*terms) if terms else z3.BoolVal(False)
        return self._solver(site.scope, z3.And(site.under, z3.Not(target)), "sat").check() == z3.sat


def compare(prover, original: list[Site], rewrite: list[Site], facts=()) -> ErrorReport:
    """What the rewrite does to the errors of the original (see the module docstring)."""

    check = _Comparison(prover, facts)
    live_original = [s for s in original if check.satisfiable(s) is not False]
    live_rewrite = [s for s in rewrite if check.satisfiable(s) is not False]
    described = tuple(s.describe() for s in live_rewrite)
    original_described = tuple(s.describe() for s in live_original)
    if not live_original and not live_rewrite:
        return ErrorReport(NONE, "neither query has an operation that can raise a runtime error")
    new = [s for s in live_rewrite if not check.covered(s, live_original)]
    lost = [s for s in live_original if not check.covered(s, live_rewrite)]
    if not new and not lost:
        return ErrorReport(SAME, "the two queries can raise the same runtime errors", described, original_described)
    if not new:
        return ErrorReport(
            REFINES,
            "the rewrite raises no runtime error that the original cannot; the original can fail where the rewrite returns rows: "
            + "; ".join(s.describe() for s in lost[:3]),
            described,
            original_described,
        )
    shown = [s for s in new if check.witness(s, live_original)]
    if shown:
        return ErrorReport(
            INTRODUCES,
            "the rewrite can raise a runtime error on a database where the original returns rows: " + "; ".join(s.describe() for s in shown[:3]),
            described,
            original_described,
        )
    return ErrorReport(
        UNKNOWN,
        "the rewrite has an operation that can fail and the original has no matching one" + (" (a solver limit was reached)" if check.limited else ""),
        described,
        original_described,
    )
