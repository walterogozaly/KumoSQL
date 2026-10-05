"""Canonical names for bound variables, and lifting inner terms to closed lambdas.

Two terms that differ only in the names of their bound variables (and the order of
commutative operands) get the same canonical form, so they can share a cache entry or
an SMT symbol. Top-level variables of a term are named by table and position
(``R#-1``, ``R#-2``, scalar ``s-1``..), so terms with the same tables use the same names.
"""

from __future__ import annotations

from dataclasses import fields

from .ir import (
    Agg,
    And,
    BoolV,
    Col,
    Exists,
    InRel,
    Iota,
    NAdd,
    NMul,
    NRel,
    NSum,
    Node,
    Not,
    Or,
    Ref,
    Same,
    Scalar,
    SVar,
    Truth,
    TVar,
    free_vars,
)
from .normalize import Term
from .translate import Unsupported

_TOP = 1  # canonical ids of top-level variables: -1, -2, .. per table
_INNER = 100000  # canonical ids of variables bound inside: below -100000


class _Namer:
    def __init__(self):
        self.next_inner = _INNER

    def inner(self, v):
        self.next_inner += 1
        if isinstance(v, TVar):
            return TVar(-self.next_inner, v.table)
        return SVar(-self.next_inner, None)


def _placeholder(v):
    return TVar(0, v.table) if isinstance(v, TVar) else SVar(0, None)


def _rename(n, m: dict):
    """Rename free occurrences of variables per ``m`` (TVar->TVar, SVar->SVar)."""

    if isinstance(n, TVar):
        return m.get(n, n)
    if isinstance(n, SVar):
        return m.get(n, n)
    if isinstance(n, tuple):
        return tuple(_rename(c, m) for c in n)
    if not isinstance(n, Node):
        return n
    vals = [_rename(getattr(n, f.name), m) for f in fields(n)]
    return type(n)(*vals)


def _masked(n, bound: set) -> str:
    """``repr`` with the given bound variables replaced by placeholders (names do not count)."""

    return repr(_rename(n, {v: _placeholder(v) for v in bound}))


def _factors(body) -> list:
    if isinstance(body, NMul):
        return list(body.args)
    return [body]


def _first_vars(n, bound: set, order: list) -> None:
    """Bound variables in order of first occurrence in ``repr`` order of ``n``."""

    if isinstance(n, (TVar, SVar)):
        if n in bound and n not in order:
            order.append(n)
        return
    if isinstance(n, tuple):
        for c in n:
            _first_vars(c, bound, order)
        return
    if isinstance(n, Node):
        for f in fields(n):
            _first_vars(getattr(n, f.name), bound, order)


def canon(n, namer: _Namer | None = None):
    """``n`` with every variable bound inside it renamed canonically."""

    namer = namer or _Namer()
    return _canon(n, namer)


def _canon(n, namer: _Namer):
    if isinstance(n, tuple):
        return tuple(_canon(c, namer) for c in n)
    if not isinstance(n, Node) or isinstance(n, (TVar, SVar)):
        return n
    if isinstance(n, NSum):
        bound = set(n.vars)
        factors = [_canon(f, namer) for f in _factors(n.body)]
        factors.sort(key=lambda f: _masked(f, bound))
        order: list = []
        for f in factors:
            _first_vars(f, bound, order)
        for v in n.vars:
            if v not in order:
                order.append(v)
        m = {v: namer.inner(v) for v in order}
        factors = sorted((_rename(f, m) for f in factors), key=repr)
        body = factors[0] if len(factors) == 1 else NMul(tuple(factors))
        return NSum(tuple(m[v] for v in order), body)
    if isinstance(n, Agg):
        bound = set(n.vars)
        body = _canon(n.body, namer)
        arg = _canon(n.arg, namer) if n.arg is not None else None
        m = {v: namer.inner(v) for v in n.vars}
        return Agg(n.func, n.distinct, tuple(m[v] for v in n.vars), _rename(body, m), _rename(arg, m) if arg is not None else None)
    if isinstance(n, Scalar):
        body = _canon(n.body, namer)
        out = namer.inner(n.out)
        return Scalar(out, _rename(body, {n.out: out}))
    if isinstance(n, (NAdd, And, Or)):
        args = sorted((_canon(a, namer) for a in n.args), key=repr)
        return type(n)(tuple(args))
    if isinstance(n, Same):
        a, b = _canon(n.a, namer), _canon(n.b, namer)
        return Same(*sorted((a, b), key=repr))
    vals = [_canon(getattr(n, f.name), namer) for f in fields(n)]
    return type(n)(*vals)


def canon_term(t: Term) -> Term:
    """A top-level term with canonical variable names (``R#-k`` for the k-th variable over R)."""

    namer = _Namer()
    factors = [_canon(f, namer) for f in t.factors]
    bound = set(t.vars)
    factors.sort(key=lambda f: _masked(f, bound))
    order: list = []
    for f in factors:
        _first_vars(f, bound, order)
    for v in t.vars:
        if v not in order:
            order.append(v)
    counts: dict = {}
    m = {}
    for v in order:
        key = v.table if isinstance(v, TVar) else "$s"
        counts[key] = counts.get(key, 0) + 1
        m[v] = TVar(-counts[key], v.table) if isinstance(v, TVar) else SVar(-counts[key], None)
    factors = sorted((_rename(f, m) for f in factors), key=repr)
    new_vars = tuple(sorted((m[v] for v in t.vars), key=repr))
    return Term(new_vars, t.coef, tuple(factors))


def signature(t: Term) -> tuple:
    return tuple(sorted(repr(v) for v in t.vars))


# ---------------------------------------------------------------------------
# Lambda lifting
# ---------------------------------------------------------------------------


def lift(n):
    """``(key, lifted, args)``: ``n`` with its outside references replaced by parameters.

    Maximal values (and conditions) that mention variables free in ``n`` and none bound
    inside it become parameters ``P1..Pk`` (scalar variables with negative ids), in
    canonical order; ``args`` are the replaced values (conditions as BOOL values). Two
    terms with equal ``key`` are the same function of their arguments.
    """

    outside = free_vars(n)
    params: dict = {}
    lifted = _lift(n, outside, frozenset(), params, root=True)
    canon_form = canon(lifted)
    # Order parameters by first occurrence in the canonical form.
    order: list = []
    _first_vars(canon_form, set(params.values()), order)
    for p in params.values():
        if p not in order:
            order.append(p)
    rename = {p: SVar(-(i + 1), "param") for i, p in enumerate(order)}
    final = _rename(canon_form, rename)
    by_param = {p: v for v, p in params.items()}
    args = tuple(by_param[p] for p in order)
    return repr(final), final, args


def _lift(n, outside, inner_bound, params, root=False):
    if isinstance(n, tuple):
        return tuple(_lift(c, outside, inner_bound, params) for c in n)
    if not isinstance(n, Node) or isinstance(n, (TVar, SVar)):
        return n
    from .ir import bound_vars

    is_value = _is_value(n)
    is_formula = _is_formula(n)
    if (is_value or is_formula) and not root:
        fv = free_vars(n)
        if fv and not (fv & inner_bound) and fv <= outside and not _mentions_bound_tuple(n, inner_bound):
            key = n if is_value else BoolV(n, Not(n))
            p = params.get(key)
            if p is None:
                from .ir import fresh_id

                p = SVar(fresh_id(), "param")
                params[key] = p
            return Ref(p) if is_value else Truth(Ref(p))
    if isinstance(n, NRel) and n.tup in outside:
        raise Unsupported("a multiplicity of an outer row inside an inner term")
    b = bound_vars(n)
    if b:
        inner_bound = inner_bound | frozenset(b)
    vals = []
    for f in fields(n):
        val = getattr(n, f.name)
        if f.name in ("vars", "out") and isinstance(n, (NSum, Agg, Scalar)):
            vals.append(val)
        else:
            vals.append(_lift(val, outside, inner_bound, params))
    return type(n)(*vals)


def _mentions_bound_tuple(n, bound) -> bool:
    for x in _walk(n):
        if isinstance(x, (TVar, SVar)) and x in bound:
            return True
    return False


def _walk(n):
    from .ir import walk

    stack = [n]
    while stack:
        cur = stack.pop()
        if isinstance(cur, tuple):
            stack.extend(cur)
            continue
        if isinstance(cur, (TVar, SVar)):
            yield cur
            continue
        if isinstance(cur, Node):
            for f in fields(cur):
                stack.append(getattr(cur, f.name))


_VALUE_TYPES = None
_FORMULA_TYPES = None


def _is_value(n) -> bool:
    from .ir import Arith, Col, Fn, Ite, Lit, Ref as _Ref

    return isinstance(n, (Col, _Ref, Fn, Arith, Ite, BoolV, Agg, Scalar)) and not isinstance(n, Lit)


def _is_formula(n) -> bool:
    from .ir import Cmp, Exists as _Exists, Gt, IsNull

    return isinstance(n, (InRel, IsNull, Truth, Same, Cmp, _Exists, Gt, And, Or, Not))
