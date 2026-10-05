"""The multiplicity algebra: queries as sums of products of table multiplicities.

A query with ``n`` output columns denotes a function from ``n``-tuples of SQL values to
natural numbers, the number of times the tuple occurs in the result (U-semiring
semantics, Chu et al. 2018). This module defines the terms of that algebra:

* **Values** (``Value``): SQL values, NULL included: columns of tuple variables, scalar
  variables, literals, functions, ``CASE``, aggregates over a bag, scalar subqueries.
* **Formulas** (``Formula``): two-valued conditions over values. A SQL predicate is
  translated to the pair of formulas "is TRUE" and "is FALSE", so three-valued logic
  is exact. ``Exists(term)`` is ``term > 0`` (squash).
* **Numeric terms** (``Num``): multiplicities and weights. ``NRel(x)`` is the
  multiplicity of tuple ``x`` in its table, ``NInd(f)`` is 1 when ``f`` holds and 0
  otherwise, ``NVal(v)`` is the numeric value of ``v`` (0 when NULL), and ``NSum`` sums
  over all tuples of its variables (finitely many are non-zero).

Every node is an immutable dataclass compared by structure, so terms can key caches.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from fractions import Fraction
import contextlib
import contextvars
import itertools
import re
from typing import Iterable

_IDS: contextvars.ContextVar = contextvars.ContextVar("uexpr_ids", default=None)
_DEFAULT_IDS = itertools.count(1)


def fresh_id() -> int:
    ids = _IDS.get()
    return next(_DEFAULT_IDS if ids is None else ids)


@contextlib.contextmanager
def id_scope(start: int = 1):
    """Number the variables of one proof from ``start``, whatever the process has translated before."""

    token = _IDS.set(itertools.count(start))
    try:
        yield
    finally:
        _IDS.reset(token)


_NUMBER = re.compile(r"-?\d+")


def _padded(match) -> str:
    return str(int(match.group()) + 10**9).zfill(14)


def in_id_scope() -> bool:
    return _IDS.get() is not None


def rkey(x) -> str:
    """A sort key for a term: its ``repr`` with every integer padded, so ids and numbers order by value.

    Sorting by plain ``repr`` puts ``s10`` before ``s9``, so which proof was found depended on how many
    variables the process had numbered before. This key orders the same way whatever the numbering starts at.
    """

    return _NUMBER.sub(_padded, x if isinstance(x, str) else repr(x))


class Node:
    """Structural equality with a cached hash."""

    __slots__ = ()

    def __hash__(self) -> int:  # dataclass(eq=True, frozen=True) keeps an explicit __hash__
        try:
            return self.__dict__["_h"]
        except KeyError:
            h = hash((type(self).__name__,) + tuple(getattr(self, f.name) for f in fields(self)))
            object.__setattr__(self, "_h", h)
            return h

    def children(self) -> tuple:
        return tuple(getattr(self, f.name) for f in fields(self))


def node(cls):
    cls = dataclass(frozen=True, eq=True, repr=False)(cls)
    cls.__hash__ = Node.__hash__  # dataclass would otherwise hash the fields afresh on every call
    return cls


# ---------------------------------------------------------------------------
# Variables and tuple terms
# ---------------------------------------------------------------------------


@node
class TVar(Node):
    """A tuple variable ranging over the rows of a base table."""

    id: int
    table: str

    def __repr__(self) -> str:
        return f"{self.table}#{self.id}"


@node
class Iota(Node):
    """The one row of ``table`` whose key columns ``key`` hold ``values`` (a definite description).

    Only meaningful when such a row exists; ``InRel(table, iota)`` says whether it does.
    """

    table: str
    key: tuple
    values: tuple

    def __repr__(self) -> str:
        return f"ι{self.table}({', '.join(f'{k}={v!r}' for k, v in zip(self.key, self.values))})"


@node
class SVar(Node):
    """A scalar variable: one SQL value (NULL included) of the given kind."""

    id: int
    kind: str | None = None

    def __repr__(self) -> str:
        return f"s{self.id}"


Var = TVar | SVar
Tuple = TVar | Iota


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

# Kinds: "int" and "num" (numbers; int is a whole number), "str", "bool", "date" (a day number),
# "time" (seconds), None (unknown).
NUMERIC = ("int", "num", "date", "time")


def family(kind: str | None) -> str | None:
    if kind in ("int", "num"):
        return "num"
    return kind


@node
class Col(Node):
    tup: object  # TVar | Iota
    name: str
    kind: str | None = None

    def __repr__(self) -> str:
        return f"{self.tup!r}.{self.name}"


@node
class Ref(Node):
    var: SVar

    def __repr__(self) -> str:
        return repr(self.var)


@node
class Lit(Node):
    """A literal; ``value`` is ``None`` for NULL, a Fraction for numbers, str or bool."""

    value: object
    kind: str | None

    def __repr__(self) -> str:
        return "NULL" if self.value is None else repr(self.value)


NULL = Lit(None, None)


@node
class Fn(Node):
    """A function the prover does not interpret: equal arguments give equal results.

    ``strict``: the result is NULL whenever an argument is NULL.
    """

    name: str
    args: tuple
    strict: bool
    kind: str | None = None

    def __repr__(self) -> str:
        return f"{self.name}({', '.join(map(repr, self.args))})"


@node
class Arith(Node):
    """``a op b`` for op in + - * / (NULL if either side is NULL)."""

    op: str
    a: object
    b: object
    kind: str | None = None

    def __repr__(self) -> str:
        return f"({self.a!r} {self.op} {self.b!r})"


@node
class Ite(Node):
    cond: object  # Formula
    a: object
    b: object

    def __repr__(self) -> str:
        return f"ite({self.cond!r}, {self.a!r}, {self.b!r})"


@node
class BoolV(Node):
    """The BOOL value of a predicate: TRUE when ``t``, FALSE when ``f``, NULL otherwise."""

    t: object
    f: object

    def __repr__(self) -> str:
        return f"bool({self.t!r})"


@node
class Agg(Node):
    """An aggregate over the bag ``Σ vars. body`` of argument values.

    ``func`` is COUNT, SUM, AVG, MIN, MAX, COUNTIF (arg is a BoolV), LOGICAL_AND, LOGICAL_OR; ``arg`` is
    ``None`` for ``COUNT(*)``.
    """

    func: str
    distinct: bool
    vars: tuple
    body: object  # Num
    arg: object  # Value | None

    def __repr__(self) -> str:
        d = "DISTINCT " if self.distinct else ""
        return f"{self.func}({d}{self.arg!r} | Σ{list(self.vars)} {self.body!r})"


@node
class Scalar(Node):
    """A scalar subquery: the value ``out`` takes in the bag ``Σ vars. body`` (NULL when empty)."""

    out: SVar
    body: object  # Num with ``out`` free

    def __repr__(self) -> str:
        return f"scalar({self.out!r} | {self.body!r})"


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------


@node
class FConst(Node):
    value: bool

    def __repr__(self) -> str:
        return "⊤" if self.value else "⊥"


TRUE = FConst(True)
FALSE = FConst(False)


@node
class Cmp(Node):
    """``a op b`` is TRUE: both non-NULL and the comparison holds. op in = <> < <= > >=."""

    op: str
    a: object
    b: object

    def __repr__(self) -> str:
        return f"{self.a!r} {self.op} {self.b!r}"


@node
class IsNull(Node):
    a: object

    def __repr__(self) -> str:
        return f"{self.a!r} IS NULL"


@node
class Same(Node):
    """``a`` and ``b`` are the same value (NULL is the same as NULL)."""

    a: object
    b: object

    def __repr__(self) -> str:
        return f"{self.a!r} ≡ {self.b!r}"


@node
class Truth(Node):
    """The BOOL value ``a`` is TRUE."""

    a: object

    def __repr__(self) -> str:
        return f"istrue({self.a!r})"


@node
class And(Node):
    args: tuple

    def __repr__(self) -> str:
        return "(" + " ∧ ".join(map(repr, self.args)) + ")"


@node
class Or(Node):
    args: tuple

    def __repr__(self) -> str:
        return "(" + " ∨ ".join(map(repr, self.args)) + ")"


@node
class Not(Node):
    a: object

    def __repr__(self) -> str:
        return f"¬{self.a!r}"


@node
class Exists(Node):
    """``term > 0`` for a multiplicity term (the squash of the term)."""

    term: object

    def __repr__(self) -> str:
        return f"∃[{self.term!r}]"


@node
class InRel(Node):
    """The tuple occurs in its table."""

    tup: object

    def __repr__(self) -> str:
        return f"{self.tup!r}∈"


@node
class Gt(Node):
    """Multiplicity ``a`` is greater than multiplicity ``b``."""

    a: object
    b: object

    def __repr__(self) -> str:
        return f"[{self.a!r} > {self.b!r}]"


# ---------------------------------------------------------------------------
# Numeric terms
# ---------------------------------------------------------------------------


@node
class NConst(Node):
    value: Fraction

    def __repr__(self) -> str:
        return str(self.value)


ZERO = NConst(Fraction(0))
ONE = NConst(Fraction(1))


@node
class NRel(Node):
    tup: object

    def __repr__(self) -> str:
        return f"{self.tup.table}({self.tup!r})" if isinstance(self.tup, TVar) else f"R({self.tup!r})"


@node
class NInd(Node):
    f: object

    def __repr__(self) -> str:
        return f"[{self.f!r}]"


@node
class NVal(Node):
    v: object

    def __repr__(self) -> str:
        return f"val({self.v!r})"


@node
class NAdd(Node):
    args: tuple

    def __repr__(self) -> str:
        return "(" + " + ".join(map(repr, self.args)) + ")"


@node
class NMul(Node):
    args: tuple

    def __repr__(self) -> str:
        return "·".join(map(repr, self.args))


@node
class NSum(Node):
    vars: tuple
    body: object

    def __repr__(self) -> str:
        return f"Σ{list(self.vars)}({self.body!r})"


@node
class NMin(Node):
    a: object
    b: object

    def __repr__(self) -> str:
        return f"min({self.a!r}, {self.b!r})"


@node
class NMonus(Node):
    """``max(a - b, 0)``."""

    a: object
    b: object

    def __repr__(self) -> str:
        return f"({self.a!r} ∸ {self.b!r})"


def nmul(*args):
    flat = []
    for a in args:
        if isinstance(a, NMul):
            flat.extend(a.args)
        elif a == ONE:
            continue
        elif a == ZERO:
            return ZERO
        else:
            flat.append(a)
    if not flat:
        return ONE
    return flat[0] if len(flat) == 1 else NMul(tuple(flat))


def nadd(*args):
    flat = []
    for a in args:
        if isinstance(a, NAdd):
            flat.extend(a.args)
        elif a == ZERO:
            continue
        else:
            flat.append(a)
    if not flat:
        return ZERO
    return flat[0] if len(flat) == 1 else NAdd(tuple(flat))


def nsum(vars: Iterable, body):
    vars = tuple(vars)
    return NSum(vars, body) if vars else body


def ind(f):
    if f == TRUE:
        return ONE
    if f == FALSE:
        return ZERO
    return NInd(f)


def conj(*args):
    flat = []
    for a in args:
        if isinstance(a, And):
            flat.extend(a.args)
        elif a == TRUE:
            continue
        elif a == FALSE:
            return FALSE
        elif a not in flat:
            flat.append(a)
    if not flat:
        return TRUE
    return flat[0] if len(flat) == 1 else And(tuple(flat))


def disj(*args):
    flat = []
    for a in args:
        if isinstance(a, Or):
            flat.extend(a.args)
        elif a == FALSE:
            continue
        elif a == TRUE:
            return TRUE
        elif a not in flat:
            flat.append(a)
    if not flat:
        return FALSE
    return flat[0] if len(flat) == 1 else Or(tuple(flat))


def neg(f):
    if isinstance(f, FConst):
        return FConst(not f.value)
    if isinstance(f, Not):
        return f.a
    return Not(f)


# ---------------------------------------------------------------------------
# Traversal and substitution
# ---------------------------------------------------------------------------

_BINDERS = (NSum, Agg)


def bound_vars(n) -> tuple:
    if isinstance(n, (NSum, Agg)):
        return n.vars
    if isinstance(n, Scalar):
        return (n.out,)
    return ()


def free_vars(n, acc: set | None = None) -> set:
    """Variables (TVar, SVar) occurring free in a term, value or formula."""

    out = set() if acc is None else acc
    _free(n, frozenset(), out)
    return out


def _free(n, bound, out) -> None:
    if isinstance(n, TVar):
        if n not in bound:
            out.add(n)
        return
    if isinstance(n, SVar):
        if n not in bound:
            out.add(n)
        return
    if isinstance(n, tuple):
        for c in n:
            _free(c, bound, out)
        return
    if not isinstance(n, Node):
        return
    b = bound_vars(n)
    if b:
        bound = bound | frozenset(b)
    for f in fields(n):
        if isinstance(n, (NSum, Agg)) and f.name == "vars":
            continue
        if isinstance(n, Scalar) and f.name == "out":
            continue
        _free(getattr(n, f.name), bound, out)


def subst(n, mapping: dict):
    """Replace free variables: TVar -> tuple term, SVar -> Value (as a ``Ref`` site).

    ``mapping`` maps TVar to a tuple term (TVar or Iota) and SVar to a Value. Bound
    variables are never captured: binders keep their own (globally fresh) variables.
    """

    if not mapping:
        return n
    return _subst(n, mapping)


def _subst(n, m):
    if isinstance(n, TVar):
        return m.get(n, n)
    if isinstance(n, Ref):
        r = m.get(n.var)
        if r is None:
            return n
        return Ref(r) if isinstance(r, SVar) else r
    if isinstance(n, SVar):
        r = m.get(n)
        if r is None:
            return n
        if isinstance(r, Ref):
            return r.var
        if isinstance(r, SVar):
            return r
        raise ValueError("a scalar variable in binding position cannot be replaced by a value")
    if isinstance(n, tuple):
        return tuple(_subst(c, m) for c in n)
    if not isinstance(n, Node):
        return n
    b = bound_vars(n)
    if b and any(v in m for v in b):
        m = {k: v for k, v in m.items() if k not in b}
    values = []
    changed = False
    for f in fields(n):
        old = getattr(n, f.name)
        if isinstance(n, (NSum, Agg)) and f.name == "vars":
            values.append(old)
            continue
        if isinstance(n, Scalar) and f.name == "out":
            values.append(old)
            continue
        new = _subst(old, m)
        changed = changed or new is not old
        values.append(new)
    return type(n)(*values) if changed else n


def rename_bound(n, prefix_map: dict | None = None):
    """A copy of ``n`` whose bound variables are all fresh (so it can be used twice in one term)."""

    return _freshen(n, {})


def _freshen(n, m):
    if isinstance(n, (TVar, SVar)):
        return m.get(n, n)
    if isinstance(n, Ref):
        r = m.get(n.var)
        return n if r is None else Ref(r)
    if isinstance(n, tuple):
        return tuple(_freshen(c, m) for c in n)
    if not isinstance(n, Node):
        return n
    b = bound_vars(n)
    if b:
        m = dict(m)
        for v in b:
            m[v] = fresh_like(v)
    values = [_freshen(getattr(n, f.name), m) for f in fields(n)]
    return type(n)(*values)


def fresh_like(v):
    if isinstance(v, TVar):
        return TVar(fresh_id(), v.table)
    return SVar(fresh_id(), v.kind)


def freshen_free(n, vars: Iterable):
    """Rename the given free variables of ``n`` to fresh ones; returns ``(term, mapping)``."""

    m = {v: fresh_like(v) for v in vars}
    return _freshen(n, m), m


def walk(n):
    """Every node below ``n`` (pre-order), including inside binders."""

    stack = [n]
    while stack:
        cur = stack.pop()
        if isinstance(cur, tuple):
            stack.extend(cur)
            continue
        if not isinstance(cur, Node):
            continue
        yield cur
        for f in fields(cur):
            stack.append(getattr(cur, f.name))


def tuples_in(n) -> set:
    """Tuple terms (TVar, Iota) appearing as ``Col``/``NRel``/``InRel`` owners."""

    out = set()
    for x in walk(n):
        if isinstance(x, (Col, NRel, InRel)):
            out.add(x.tup)
    return out


def value_kind(v) -> str | None:
    if isinstance(v, Col):
        return v.kind
    if isinstance(v, Lit):
        return v.kind
    if isinstance(v, Ref):
        return v.var.kind
    if isinstance(v, (Fn, Arith)):
        return v.kind
    if isinstance(v, BoolV):
        return "bool"
    if isinstance(v, Ite):
        ka, kb = value_kind(v.a), value_kind(v.b)
        if ka == kb:
            return ka
        if ka is None or (isinstance(v.a, Lit) and v.a.value is None):
            return kb
        if kb is None or (isinstance(v.b, Lit) and v.b.value is None):
            return ka
        if family(ka) == family(kb) == "num":
            return "num"
        return None
    if isinstance(v, Agg):
        if v.func in ("COUNT", "COUNTIF"):
            return "int"
        if v.func == "AVG":
            return "num"
        if v.func in ("LOGICAL_AND", "LOGICAL_OR"):
            return "bool"
        return value_kind(v.arg) if v.arg is not None else None
    if isinstance(v, Scalar):
        return v.out.kind
    return None


def replace(n, mapping: dict):
    """Replace every sub-node equal to a key of ``mapping`` (values or formulas) by its image."""

    if not mapping:
        return n
    return _replace(n, mapping)


def _replace(n, m):
    if isinstance(n, tuple):
        return tuple(_replace(c, m) for c in n)
    if not isinstance(n, Node):
        return n
    r = m.get(n)
    if r is not None:
        return r
    values = []
    changed = False
    for f in fields(n):
        old = getattr(n, f.name)
        new = _replace(old, m)
        changed = changed or new is not old
        values.append(new)
    return type(n)(*values) if changed else n
