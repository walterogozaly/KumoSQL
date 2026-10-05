"""Overflow of a ``SUM`` over a group, for the error comparison of the SMT prover (``smt_errors``).

A ``SUM`` of INT64 values raises an error when the sum leaves INT64. Which operations fail does not depend on
one row but on a whole group: the rows of the ``FROM`` that pass ``WHERE`` and ``ON``, split by the ``GROUP BY``
keys, with the ``SUM`` argument of each row. So the compiler records one :class:`GroupSum` per ``SUM`` call (the
group's input as formulas over the occurrences of its ``FROM``), and this module decides how two of them relate.

* **Covering** (:func:`covered`): the rewrite's ``SUM`` fails only where the original's does when each of its
  groups is a whole group of the original's, with the same rows and the same argument values. Then equal inputs
  give equal outcomes whatever order BigQuery adds them in. That is a map from the original's table occurrences
  to the rewrite's and two checks: wherever the rewrite's group takes a row, the original's takes it with the same
  key and argument; and a group that the rewrite computes at all, it computes from every row the original puts in
  that group (a ``HAVING`` on a key moved into ``WHERE`` passes the first check and the second; a filter on the
  rows of a group fails the second). A group the original computes and the rewrite does not is a ``SUM`` that only
  the original can overflow: ``refines``.
* **A witness** (:func:`witness`): a database of at most ``_ROWS`` rows of one table on which a group of the
  rewrite certainly overflows (its exact total leaves INT64, which fails however the rows are added) and no
  ``SUM`` of the original can: every group's positive part is at most INT64_MAX and its negative part at least
  INT64_MIN, so no order of additions overflows either. Only INT64 sums over one base table are shown this way.
* **Opaque sums** (:func:`opaque_site`): a relation kept whole (a window, a feature the prover does not model)
  that holds a ``SUM``: it overflows alike in two queries that read the same relation, and is not related to
  anything else.

Anything else stays unknown. BigQuery's rule for a sum whose total is in range but whose partial sums are not is
unverified, so a regrouped sum (a pre-aggregation, a finer or a coarser ``GROUP BY``) is never called the same,
a refinement or an introduction: the partial sums of the original may or may not fail in the order BigQuery
picks.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools

try:  # pragma: no cover
    import z3
except ImportError:  # pragma: no cover
    z3 = None

from . import smt_errors, smt_values

_ROWS = 3  # rows of the witness database
_MAX_MAPS = 120
_MAX_CHECKS = 600  # solver calls one comparison may spend on matching sums
KIND = "overflow in SUM"


@dataclass
class GroupSum:
    """The input of one ``SUM`` call: the rows it adds, as formulas over its ``FROM``'s table occurrences.

    ``cond`` holds on a tuple of rows when it passes ``WHERE``/``ON`` and the argument is not NULL (``SUM`` skips
    NULL); ``keys`` are the ``GROUP BY`` values (none for a global aggregate)."""

    text: str
    distinct: bool
    cls: str | None  # INT64, NUMERIC, FLOAT64.. of the argument, None when unknown
    arg: object  # the argument as a value (null flag, payload)
    maybe: bool = False  # the sum of an AVG: whether AVG can overflow is not known
    guarded: bool = False  # under a CASE/IF branch: whether the aggregate runs only there is not known
    cond: object = None
    keys: tuple = ()
    occs: tuple = ()
    relatable: bool = False  # its input is described by ``cond``, ``keys`` and ``arg`` over ``occs`` alone
    opaque: object = None  # the occurrence of a relation kept whole that holds a SUM (then nothing else is set)
    typed: object = None  # occurrences -> the facts their declared column types give
    copied: object = None  # the compiler's table of each occurrence (a copy is entered there to be typed)


# -- the compiler's side -------------------------------------------------------------------------


def note(compiler, e, arg, distinct: bool, cls, maybe: bool = False) -> None:
    """Record the ``SUM`` call ``e`` (its input is completed by :func:`attach` when its select is)."""

    if compiler.dialect != "bigquery" or compiler._safe:
        return
    text = e.sql(dialect="bigquery") if hasattr(e, "sql") else str(e)
    group = GroupSum(text[:120], distinct, cls, arg, maybe=maybe, guarded=bool(compiler._guard))
    group.typed = compiler.typed_facts
    group.copied = compiler.occ_tables
    site = smt_errors.Site(KIND, group.text, z3.BoolVal(True), group=group)
    compiler.sites.append(site)


def attach(compiler, start: int, block) -> None:
    """Complete the ``SUM`` calls compiled since ``start`` (those of ``block``, an aggregate select)."""

    occs = tuple(block.occs)
    allowed = set()
    for occ in occs:
        for v in occ.cols.values():
            allowed.add(v.null.get_id())
            allowed.add(v.val.get_id())
    for site in compiler.sites[start:]:
        group = site.group
        if group is None or group.cond is not None or group.opaque is not None:
            continue
        group.cond = z3.simplify(z3.And(block.cond.t, z3.Not(group.arg.null)))
        group.keys = tuple(block.keys)
        group.occs = occs
        formulas = [group.cond, group.arg.null, group.arg.val]
        for k in group.keys:
            formulas += [k.null, k.val]
        group.relatable = not block.subs and not group.guarded and _only(formulas, allowed)
        site.fire = group.cond


def opaque_site(compiler, occ, has_sum: bool) -> None:
    """A relation kept whole that the compiler did not read, and that can fail: one site for the relation."""

    if compiler.dialect != "bigquery" or compiler._safe:
        return
    text = "a SUM in a relation the prover keeps whole" if has_sum else "an operation in a relation the prover keeps whole"
    group = GroupSum(text, False, None, None, opaque=occ)
    compiler.sites.append(smt_errors.Site(KIND if has_sum else "runtime error", group.text, z3.BoolVal(True), group=group))


def _only(formulas, allowed: set) -> bool:
    """Whether the only uninterpreted constants in ``formulas`` are the ones in ``allowed`` (the group's own columns)."""

    seen: set = set()
    stack = list(formulas)
    while stack:
        node = stack.pop()
        if node.get_id() in seen:
            continue
        seen.add(node.get_id())
        if z3.is_app(node):
            if node.num_args() == 0:
                if node.decl().kind() == z3.Z3_OP_UNINTERPRETED and node.get_id() not in allowed:
                    return False
            else:
                stack.extend(node.children())
    return True


# -- covering ------------------------------------------------------------------------------------


def covered(check, site, others) -> bool:
    """Whether the rewrite's ``SUM`` ``site`` fails only where one of the original's (``others``) does."""

    return any(o.group is not None and _covers(check, site.group, o.group) for o in others)


def _covers(check, g: GroupSum, o: GroupSum) -> bool:
    if g.opaque is not None or o.opaque is not None:
        return g.opaque is not None and o.opaque is not None and g.opaque.table == o.opaque.table
    if not (g.relatable and o.relatable) or g.distinct != o.distinct or len(g.keys) != len(o.keys) or len(g.occs) != len(o.occs):
        return False
    count = 0
    for mapping in _bijections(o.occs, g.occs):
        pairs = [p for src, dst in mapping for p in smt_errors._occ_pairs(src, dst)]
        for order in _orders(len(g.keys)):
            count += 1
            if count > _MAX_MAPS:
                check.limited = True
                return False
            if _whole_groups(check, g, o, pairs, order):
                return True
    return False


def _bijections(src, dst):
    for perm in itertools.islice(itertools.permutations(dst), _MAX_MAPS):
        if all(a.table == b.table and a.opaque == b.opaque for a, b in zip(src, perm)):
            yield list(zip(src, perm))


def _orders(n: int):
    return itertools.permutations(range(n)) if n <= 3 else [tuple(range(n))]


def _sub(term, pairs):
    return z3.substitute(term, *pairs) if pairs else term


def _same(a, b):
    """Equal values, NULL matching NULL (grouping equality)."""

    return z3.And(a.null == b.null, z3.Or(a.null, a.val == b.val))


_copies = itertools.count()


def _whole_groups(check, g: GroupSum, o: GroupSum, pairs: list, order: tuple) -> bool:
    """Every group of ``g`` holds exactly the rows, with the same values, of one group of ``o`` (see the module docstring)."""

    cond_o = _sub(o.cond, pairs)
    keys_o = [_sub(o.keys[i].null, pairs) for i in order], [_sub(o.keys[i].val, pairs) for i in order]
    keyed = [_val(n, v) for n, v in zip(*keys_o)]
    arg_o = _val(_sub(o.arg.null, pairs), _sub(o.arg.val, pairs))
    first = z3.And(g.cond, z3.Not(z3.And(cond_o, *[_same(a, b) for a, b in zip(g.keys, keyed)], _same(g.arg, arg_o))))
    if not _unsat(check, list(g.occs), first, g):
        return False
    copies = [occ.copy(f"{occ.uid}~{next(_copies)}") for occ in g.occs]
    for occ, clone in zip(g.occs, copies):
        _register(g, occ, clone)
    sigma = [p for occ, clone in zip(g.occs, copies) for p in smt_errors._occ_pairs(occ, clone)]
    keyed_other = [_val(_sub(k.null, sigma), _sub(k.val, sigma)) for k in keyed]
    second = z3.And(cond_o, _sub(g.cond, sigma), *[_same(a, b) for a, b in zip(keyed, keyed_other)], z3.Not(g.cond))
    return _unsat(check, list(g.occs) + copies, second, g, copies)


def _val(null, val):
    from .smt_equivalence import _Val

    return _Val(null, val)


def _register(g: GroupSum, occ, clone) -> None:
    table = g.copied.get(occ.uid)
    if table is not None:
        g.copied[clone.uid] = table


def _unsat(check, occs, formula, g: GroupSum, extra_occs=()) -> bool:
    check.group_checks = getattr(check, "group_checks", 0) + 1
    if check.group_checks > _MAX_CHECKS:  # a query with very many sums: the rest stay unrelated (unknown)
        check.limited = True
        return False
    solver = check._solver({o.uid: o for o in occs}, formula, "sat")
    if extra_occs and g.typed is not None:
        solver.add(*g.typed(list(extra_occs)))
    result = solver.check()
    if result == z3.unknown:
        check.limited = True
    return result == z3.unsat


# -- a witness database --------------------------------------------------------------------------


def certain(g: GroupSum) -> bool:
    """Whether a model of this sum's input is a real database whose group overflows (or not) as the formulas say."""

    if g.opaque is not None or not g.relatable or g.maybe or g.cls != smt_values.INT64 or len(g.occs) != 1:
        return False
    occ = g.occs[0]
    if occ.opaque or occ.table.startswith("<"):
        return False
    formulas = [g.cond, g.arg.val, g.arg.null] + [x for k in g.keys for x in (k.null, k.val)]
    return all(smt_errors.uninterpreted_free(f) for f in formulas)


def witness(check, site, others) -> bool:
    """A database on which the group ``site`` overflows (its total leaves INT64) and no operation of ``others`` fails."""

    g = site.group
    if g is None or not certain(g):
        return False
    table = g.occs[0].table
    rows = [_row(g, table, f"w{next(_copies)}") for _ in range(_ROWS)]
    parts = []
    for other in others:
        o = other.group
        if o is not None:
            if not certain(o) or o.occs[0].table != table:
                return False
            safe = _safe_groups(check, o, rows)
        else:
            if not other.certain:
                return False
            safe = _plain_safe(other, rows, table)
            if safe is None:
                return False
        parts.append(safe)
    solver = check._solver({r.uid: r for r in rows}, z3.And(_overflows(g, rows), *parts), "sat")
    if g.typed is not None:
        solver.add(*g.typed(rows))
    return solver.check() == z3.sat


def _row(g: GroupSum, table: str, uid: str):
    from .smt_equivalence import _Occ

    occ = g.occs[0]
    row = _Occ(table, uid, occ.columns)
    for name in occ.cols:
        row.col(name)
    _register(g, occ, row)
    return row


def _bind(g: GroupSum, row):
    """The group's columns read from ``row`` (a copy of its occurrence)."""

    for name in g.occs[0].cols:
        row.col(name)
    pairs = smt_errors._occ_pairs(g.occs[0], row)
    cond = _sub(g.cond, pairs)
    keys = [_val(_sub(k.null, pairs), _sub(k.val, pairs)) for k in g.keys]
    arg = _val(_sub(g.arg.null, pairs), _sub(g.arg.val, pairs))
    return cond, keys, arg


def _members(g: GroupSum, rows):
    """For each row taken as the group's anchor: the rows of its group (a condition per row) and their numbers."""

    from .smt_equivalence import _value_sort

    V = _value_sort()
    bound = [_bind(g, r) for r in rows]
    groups = []
    for cond_a, keys_a, _ in bound:
        member = []
        for index, (cond, keys, arg) in enumerate(bound):
            same_key = z3.And(*[_same(a, b) for a, b in zip(keys_a, keys)]) if keys else z3.BoolVal(True)
            here = z3.And(cond_a, cond, same_key, V.is_Num(arg.val))
            if g.distinct:  # an equal value earlier in the group is not added again
                for earlier in range(index):
                    c2, k2, a2 = bound[earlier]
                    again = z3.And(cond_a, c2, V.is_Num(a2.val), *[_same(a, b) for a, b in zip(keys_a, k2)], V.num(a2.val) == V.num(arg.val))
                    here = z3.And(here, z3.Not(again))
            member.append((here, V.num(arg.val)))
        groups.append((cond_a, member))
    return groups


def _overflows(g: GroupSum, rows) -> object:
    """Some group of the rows has a total outside INT64."""

    terms = []
    for cond_a, member in _members(g, rows):
        total = sum((z3.If(here, num, z3.RealVal(0)) for here, num in member), z3.RealVal(0))
        terms.append(z3.And(cond_a, z3.Or(total > smt_values.INT64_MAX, total < smt_values.INT64_MIN)))
    return z3.Or(*terms)


def _safe_groups(check, o: GroupSum, rows) -> object:
    """No group of ``o`` over the rows can overflow in any order: its positive part and its negative part are in range."""

    terms = []
    for cond_a, member in _members(o, rows):
        positive = sum((z3.If(here, z3.If(num > 0, num, z3.RealVal(0)), z3.RealVal(0)) for here, num in member), z3.RealVal(0))
        negative = sum((z3.If(here, z3.If(num < 0, num, z3.RealVal(0)), z3.RealVal(0)) for here, num in member), z3.RealVal(0))
        terms.append(z3.Implies(cond_a, z3.And(positive <= smt_values.INT64_MAX, negative >= smt_values.INT64_MIN)))
    return z3.And(*terms)


def _plain_safe(site, rows, table: str):
    """An operation that fails on one row tuple does not fail on any tuple of the witness rows (None: not decidable here)."""

    occs = list(site.scope.values())
    mine = [o for o in occs if o.table == table and not o.opaque]
    if len(mine) < len(occs):
        return z3.BoolVal(True)  # a table that has no row in the witness: the operation never runs
    if len(mine) > 2:
        return None
    terms = []
    for combo in itertools.product(rows, repeat=len(mine)):
        pairs = []
        for occ, row in zip(mine, combo):
            for name in occ.cols:
                row.col(name)
            pairs += smt_errors._occ_pairs(occ, row)
        terms.append(z3.Not(_sub(site.fire, pairs)))
    return z3.And(*terms)
