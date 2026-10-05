"""Sum-of-products normal form for multiplicity terms.

``normalize(term)`` returns a list of :class:`Term`, each ``coef · Σ vars. f1·f2·..`` whose
factors are relation multiplicities ``R(x)``, indicators ``[φ]`` of atomic formulas,
numeric values ``val(v)`` and the opaque ``min`` / monus of two terms. The list stands for
the sum of its terms. Every step is an identity of the multiplicity algebra:

* products distribute over sums and nested sums are flattened (``(Σx.a)·(Σy.b) = Σx,y. a·b``
  with ``x``, ``y`` distinct);
* a scalar variable fixed by an equality is substituted: ``Σs. [s ≡ e]·f(s) = f(e)`` and
  ``Σs. [s = e]·f(s) = [e IS NOT NULL]·f(e)`` (``s`` not free in ``e``);
* a tuple variable over a table with a declared key, whose key columns are fixed by
  equalities, is the one row with that key: ``Σy. R(y)·[y.k = e]·f(y) = [ι ∈ R]·f(ι)``
  where ``ι`` is the row of ``R`` with key ``e`` (``R(ι)`` is 0 or 1 because of the key);
* ``[a ∧ b] = [a]·[b]``, ``[FALSE]`` deletes the term, ``[TRUE]`` is 1;
* inside ``∃``: ``∃(Σ terms) = ∨ ∃(term)``, a term without variables is the conjunction of
  its factors being positive, and conditions that do not mention the summed variables
  move out of the ``∃``.

Context: a tuple variable bound by an enclosing sum is known to be in its table where an
inner term is evaluated (the enclosing product is 0 otherwise), so its NOT NULL columns
are not NULL there.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .ir import (
    FALSE,
    ONE,
    TRUE,
    ZERO,
    Agg,
    And,
    Arith,
    BoolV,
    Cmp,
    Col,
    Exists,
    FConst,
    Fn,
    Gt,
    InRel,
    IsNull,
    Iota,
    Ite,
    Lit,
    NAdd,
    NConst,
    NInd,
    NMin,
    NMonus,
    NMul,
    NRel,
    NSum,
    NVal,
    Not,
    Or,
    Ref,
    Same,
    Scalar,
    SVar,
    Truth,
    TVar,
    conj,
    disj,
    free_vars,
    fresh_id,
    neg,
    replace,
    subst,
    walk,
)
from .translate import Catalog, Unsupported


@dataclass(frozen=True)
class Term:
    vars: tuple
    coef: Fraction
    factors: tuple

    def __repr__(self) -> str:
        head = f"{self.coef}·" if self.coef != 1 else ""
        body = "·".join(map(repr, self.factors)) or "1"
        return f"{head}Σ{list(self.vars)}({body})" if self.vars else f"{head}{body}"


class Ctx:
    """What normalization may assume: declared constraints and the tuples known to be in their table."""

    def __init__(self, catalog: Catalog, exact: bool, known: frozenset = frozenset(), cache: dict | None = None):
        self.catalog = catalog
        self.exact = exact
        self.known = known
        self.cache = cache if cache is not None else {}

    def with_known(self, more) -> "Ctx":
        more = frozenset(more)
        if more <= self.known:
            return self
        return Ctx(self.catalog, self.exact, self.known | more, self.cache)


MAX_TERMS = 4000


# ---------------------------------------------------------------------------
# Numeric terms
# ---------------------------------------------------------------------------


def normalize(n, ctx: Ctx) -> list:
    terms = _norm(n, ctx)
    out = []
    for t in terms:
        out.extend(simplify_term(t, ctx))
    return merge_terms(out)


def _norm(n, ctx) -> list:
    if isinstance(n, NConst):
        return [Term((), n.value, ())] if n.value != 0 else []
    if isinstance(n, NRel):
        return [Term((), Fraction(1), (n,))]
    if isinstance(n, NInd):
        f = n.f
        if f == TRUE:
            return [Term((), Fraction(1), ())]
        if f == FALSE:
            return []
        return [Term((), Fraction(1), tuple(NInd(c) for c in _conjuncts(f)))]
    if isinstance(n, NVal):
        return expand_val(n.v, ctx)
    if isinstance(n, NAdd):
        out = []
        for a in n.args:
            out.extend(_norm(a, ctx))
        return out
    if isinstance(n, NMul):
        acc = [Term((), Fraction(1), ())]
        for a in n.args:
            part = _norm(a, ctx)
            if not part:
                return []
            acc = [product(x, y) for x in acc for y in part]
            if len(acc) > MAX_TERMS:
                raise Unsupported("normal form too large")
        return acc
    if isinstance(n, NSum):
        return [Term(tuple(n.vars) + t.vars, t.coef, t.factors) for t in _norm(n.body, ctx)]
    if isinstance(n, (NMin, NMonus)):
        return [Term((), Fraction(1), (n,))]
    raise Unsupported(f"numeric term {type(n).__name__}")


def product(x: Term, y: Term) -> Term:
    """``x · y``, renaming ``y``'s summed variables if ``x`` sums over the same ones."""

    if set(x.vars) & set(y.vars):
        from .ir import _freshen, fresh_like

        m = {v: fresh_like(v) for v in y.vars}
        y = Term(tuple(m[v] for v in y.vars), y.coef, tuple(_freshen(f, m) for f in y.factors))
    return Term(x.vars + y.vars, x.coef * y.coef, x.factors + y.factors)


def expandable(v, ctx: Ctx) -> bool:
    """Whether ``expand_val`` rewrites ``val(v)`` into something other than itself."""

    if isinstance(v, Lit):
        return True
    if isinstance(v, Ite):
        return True
    if ctx.exact and isinstance(v, Arith) and v.op in "+-*":
        return True
    return ctx.exact and isinstance(v, Agg) and not v.distinct and v.func in ("COUNT", "SUM", "COUNTIF")


def expand_val(v, ctx: Ctx) -> list:
    """The numeric value of ``v`` (0 when NULL) as a sum of products."""

    if isinstance(v, Lit):
        if v.value is None:
            return []
        if isinstance(v.value, Fraction):
            return [Term((), v.value, ())] if v.value != 0 else []
        raise Unsupported("numeric value of a non-number")
    if ctx.exact and isinstance(v, Arith) and v.op in "+-*":
        a, b = expand_val(v.a, ctx), expand_val(v.b, ctx)
        if v.op == "*":
            return [product(x, y) for x in a for y in b]
        nn_a, nn_b = NInd(neg(IsNull(v.a))), NInd(neg(IsNull(v.b)))
        sign = 1 if v.op == "+" else -1
        return [Term(x.vars, x.coef, x.factors + (nn_b,)) for x in a] + [Term(y.vars, sign * y.coef, y.factors + (nn_a,)) for y in b]
    if isinstance(v, Ite):
        c = v.cond
        return [Term(t.vars, t.coef, t.factors + tuple(NInd(x) for x in _conjuncts(c))) for t in expand_val(v.a, ctx)] + [
            Term(t.vars, t.coef, t.factors + (NInd(neg(c)),)) for t in expand_val(v.b, ctx)
        ]
    if ctx.exact and isinstance(v, Agg) and not v.distinct and v.func in ("COUNT", "SUM", "COUNTIF"):
        if v.func == "COUNT":
            weight = [Term((), Fraction(1), ())] if v.arg is None else [Term((), Fraction(1), (NInd(neg(IsNull(v.arg))),))]
        elif v.func == "COUNTIF":
            weight = [Term((), Fraction(1), (NInd(v.arg.t),))]
        else:
            weight = expand_val(v.arg, ctx)
        from .ir import rename_bound

        if v.vars:  # fresh names, so the same aggregate can be expanded twice in one product
            from .ir import _freshen, fresh_like

            m = {x: fresh_like(x) for x in v.vars}
            v = Agg(v.func, v.distinct, tuple(m[x] for x in v.vars), _freshen(v.body, m), _freshen(v.arg, m) if v.arg is not None else None)
            weight = [Term(w.vars, w.coef, tuple(_freshen(f, m) for f in w.factors)) for w in weight]
        body = _norm(rename_bound(v.body), ctx)
        return [product(Term(tuple(v.vars) + b.vars, b.coef, b.factors), w) for b in body for w in weight]
    return [Term((), Fraction(1), (NVal(v),))]


def merge_terms(terms: list) -> list:
    """Add up terms that are the same up to their coefficient."""

    out: dict = {}
    order = []
    for t in terms:
        key = (t.vars, t.factors)
        if key in out:
            out[key] += t.coef
        else:
            out[key] = t.coef
            order.append(key)
    return [Term(k[0], out[k], k[1]) for k in order if out[k] != 0]


def rebuild(terms: list):
    """A numeric term equal to the sum of ``terms``."""

    from .ir import nadd, nmul, nsum

    parts = []
    for t in terms:
        body = nmul(*((NConst(t.coef),) if t.coef != 1 else ()), *t.factors)
        parts.append(nsum(t.vars, body))
    return nadd(*parts)


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------


def _conjuncts(f) -> list:
    if isinstance(f, And):
        out = []
        for a in f.args:
            out.extend(_conjuncts(a))
        return out
    if f == TRUE:
        return []
    return [f]


def simplify_term(t: Term, ctx: Ctx) -> list:
    """The term after substitution, key elimination and formula simplification (``[]`` if 0)."""

    for _ in range(64):
        rels = [f for f in t.factors if isinstance(f, NRel)]
        known = {f.tup for f in rels}
        conjs = []
        for f in t.factors:
            if isinstance(f, NInd):
                conjs.extend(_conjuncts(f.f))
        members = {c.tup for c in conjs if isinstance(c, InRel)}
        inner = ctx.with_known(known | members)
        # 1. simplify the conditions (a membership condition is not used to simplify itself)
        new_conjs = []
        for c in conjs:
            if isinstance(c, InRel) and c.tup not in known and c.tup not in ctx.known:
                s = simplify_formula(c, ctx.with_known(known | (members - {c.tup})))
            else:
                s = simplify_formula(c, inner)
            if s == FALSE:
                return []
            new_conjs.extend(_conjuncts(s))
        new_conjs = _dedup(new_conjs)
        others = []
        for f in t.factors:
            if isinstance(f, NInd):
                continue
            if isinstance(f, NVal):
                v = simplify_value(f.v, inner)
                if isinstance(v, Lit):
                    if v.value is None or v.value == 0:
                        return []
                    if isinstance(v.value, Fraction):
                        t = Term(t.vars, t.coef * v.value, t.factors)
                        continue
                if expandable(v, ctx) and not isinstance(v, Lit):
                    rest = Term(t.vars, t.coef, tuple(g for g in t.factors if g is not f))
                    out = []
                    for e in expand_val(v, ctx):
                        for u in simplify_term(product(rest, e), ctx):
                            out.append(u)
                    return out
                others.append(NVal(v))
            elif isinstance(f, (NMin, NMonus)):
                others.append(type(f)(rebuild(normalize(f.a, inner)), rebuild(normalize(f.b, inner))))
            else:
                others.append(f)
        factors = tuple(others) + tuple(NInd(c) for c in new_conjs)
        t = Term(t.vars, t.coef, factors)
        # 2. eliminate a scalar variable fixed by an equality
        changed = _eliminate_scalar(t)
        if changed is not None:
            t = changed
            continue
        # 3. a tuple variable whose key is fixed is the row with that key
        changed = _eliminate_keyed(t, ctx)
        if changed is not None:
            t = changed
            continue
        # 4. a sum over the key values of a keyed table is a sum over its rows
        changed = _unkey_scalars(t, ctx)
        if changed is not None:
            t = changed
            continue
        break
    for v in t.vars:
        if isinstance(v, SVar) and not any(v in free_vars(f) for f in t.factors):
            raise Unsupported("an unconstrained scalar variable (an infinite sum)")
    return [Term(_ordered_vars(t), t.coef, t.factors)]


def _ordered_vars(t: Term) -> tuple:
    used = set()
    for f in t.factors:
        free_vars(f, used)
    return tuple(v for v in t.vars if v in used or isinstance(v, TVar))


def _dedup(items: list) -> list:
    seen = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _eliminate_scalar(t: Term) -> Term | None:
    bound = set(t.vars)
    for v in t.vars:
        if not isinstance(v, SVar):
            continue
        for f in t.factors:
            if not isinstance(f, NInd):
                continue
            c = f.f
            target = None
            extra = None
            if isinstance(c, Same):
                if c.a == Ref(v) and v not in free_vars(c.b):
                    target = c.b
                elif c.b == Ref(v) and v not in free_vars(c.a):
                    target = c.a
            elif isinstance(c, IsNull) and c.a == Ref(v):
                target = Lit(None, v.kind)
            elif isinstance(c, Cmp) and c.op == "=":
                if c.a == Ref(v) and v not in free_vars(c.b):
                    target, extra = c.b, NInd(neg(IsNull(c.b)))
                elif c.b == Ref(v) and v not in free_vars(c.a):
                    target, extra = c.a, NInd(neg(IsNull(c.a)))
            if target is None:
                continue
            rest = [g for g in t.factors if g is not f]
            if extra is not None:
                rest.append(extra)
            new = tuple(subst(g, {v: target}) for g in rest)
            return Term(tuple(x for x in t.vars if x != v), t.coef, new)
    return None


def _unkey_scalars(t: Term, ctx: Ctx) -> Term | None:
    """``Σk. [ι(k) ∈ R]·f(k, ι(k)) = Σy. R(y)·f(y.k, y)``: the inverse of key elimination.

    Summing over the values of a key of ``R`` that name a row of ``R`` is summing over the rows of ``R``
    (a key is unique and not NULL, so ``y ↦ y.k`` is a bijection from the rows onto those values and
    ``R(y)`` is 0 or 1). It makes a bag of key values agree with the bag of the rows it was read from.
    """

    scalars = {v for v in t.vars if isinstance(v, SVar)}
    if not scalars:
        return None
    for f in t.factors:
        if not (isinstance(f, NInd) and isinstance(f.f, InRel) and isinstance(f.f.tup, Iota)):
            continue
        iota = f.f.tup
        if not all(isinstance(v, Ref) and v.var in scalars for v in iota.values):
            continue
        keys = [v.var for v in iota.values]
        if len(set(keys)) != len(keys):
            continue
        info = ctx.catalog.info(iota.table)
        y = TVar(fresh_id(), iota.table)
        mapping = {w: Col(y, k, info.kinds.get(k)) for w, k in zip(keys, iota.key)}
        rest = [replace(g, {iota: y}) for g in t.factors if g is not f]
        new = tuple(subst(g, mapping) for g in rest) + (NRel(y),)
        return Term(tuple(v for v in t.vars if v not in mapping) + (y,), t.coef, new)
    return None


def _equalities(conjs: list) -> dict:
    """Union-find classes of values equal under the conditions (``=`` TRUE or ``≡``)."""

    parent: dict = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    for c in conjs:
        if isinstance(c, (Same,)) or (isinstance(c, Cmp) and c.op == "="):
            ra, rb = find(c.a), find(c.b)
            if ra != rb:
                parent[ra] = rb
    classes: dict = {}
    for a in list(parent):
        classes.setdefault(find(a), []).append(a)
    for root in list(classes):
        if root not in classes[root]:
            classes[root].append(root)
    out = {}
    for members in classes.values():
        for m in members:
            out[m] = members
    return out


def _eliminate_keyed(t: Term, ctx: Ctx) -> Term | None:
    tvars = [v for v in t.vars if isinstance(v, TVar)]
    if not tvars:
        return None
    conjs = [f.f for f in t.factors if isinstance(f, NInd)]
    eq = None
    for y in tvars:
        info = ctx.catalog.info(y.table)
        if not info.keys:
            continue
        rel = [f for f in t.factors if isinstance(f, NRel) and f.tup == y]
        if len(rel) != 1:
            continue
        if eq is None:
            eq = _equalities(conjs)
        for key in info.keys:
            values = []
            for k in key:
                col = Col(y, k, info.kinds.get(k))
                choice = _pick_value(eq.get(col, []), y)
                if choice is None:
                    break
                values.append(choice)
            else:
                iota = Iota(y.table, tuple(key), tuple(values))
                ctx.catalog.used_constraints = True
                rest = [f for f in t.factors if f is not rel[0]]
                new = tuple(subst(f, {y: iota}) for f in rest) + (NInd(InRel(iota)),)
                return Term(tuple(v for v in t.vars if v != y), t.coef, new)
    return None


def _pick_value(members: list, y: TVar):
    """The best value equal to ``y``'s key column that does not mention ``y``."""

    best = None
    best_rank = None
    for m in members:
        if y in free_vars(m) or _mentions_tuple(m, y):
            continue
        rank = 0 if isinstance(m, Lit) and m.value is not None else 1 if isinstance(m, (Ref, Col)) else 2
        key = (rank, repr(m))
        if best_rank is None or key < best_rank:
            best, best_rank = m, key
    return best


def _mentions_tuple(n, y) -> bool:
    for x in walk(n):
        if isinstance(x, (Col, NRel, InRel)) and _tuple_mentions(x.tup, y):
            return True
    return False


def _tuple_mentions(tup, y) -> bool:
    if tup == y:
        return True
    if isinstance(tup, Iota):
        return any(_mentions_tuple(v, y) for v in tup.values)
    return False


# ---------------------------------------------------------------------------
# Formulas and values
# ---------------------------------------------------------------------------


def simplify_formula(f, ctx: Ctx):
    if isinstance(f, FConst):
        return f
    if isinstance(f, And):
        return conj(*[simplify_formula(a, ctx) for a in f.args])
    if isinstance(f, Or):
        return disj(*[simplify_formula(a, ctx) for a in f.args])
    if isinstance(f, Not):
        return neg(simplify_formula(f.a, ctx))
    if isinstance(f, IsNull):
        v = simplify_value(f.a, ctx)
        return _is_null(v, ctx)
    if isinstance(f, Cmp):
        a, b = simplify_value(f.a, ctx), simplify_value(f.b, ctx)
        if isinstance(a, Lit) and isinstance(b, Lit):
            if a.value is None or b.value is None:
                return FALSE
            folded = _fold_cmp(f.op, a.value, b.value)
            if folded is not None:
                return folded
        if (isinstance(a, Lit) and a.value is None) or (isinstance(b, Lit) and b.value is None):
            return FALSE
        if a == b:
            return neg(_is_null(a, ctx)) if f.op in ("=", "<=", ">=") else FALSE
        return Cmp(f.op, a, b)
    if isinstance(f, Same):
        a, b = simplify_value(f.a, ctx), simplify_value(f.b, ctx)
        if a == b:
            return TRUE
        if isinstance(a, Lit) and isinstance(b, Lit):
            if a.value is None or b.value is None:
                return TRUE if a.value is None and b.value is None else FALSE
            folded = _fold_cmp("=", a.value, b.value)
            if folded is not None:
                return folded
        if isinstance(a, Lit) and a.value is None:
            return _is_null(b, ctx)
        if isinstance(b, Lit) and b.value is None:
            return _is_null(a, ctx)
        if _never_null(a, ctx) or _never_null(b, ctx):
            return Cmp("=", a, b)
        return Same(a, b)
    if isinstance(f, Truth):
        v = simplify_value(f.a, ctx)
        if isinstance(v, BoolV):
            return v.t
        if isinstance(v, Lit):
            return TRUE if v.value is True else FALSE
        if isinstance(v, Ite):
            return disj(conj(v.cond, simplify_formula(Truth(v.a), ctx)), conj(neg(v.cond), simplify_formula(Truth(v.b), ctx)))
        return Truth(v)
    if isinstance(f, Exists):
        return exists_formula(f.term, ctx)
    if isinstance(f, InRel):
        tup = simplify_tuple(f.tup, ctx)
        if tup in ctx.known:
            return TRUE
        implied = _foreign_key_member(tup, ctx)
        if implied is not None:
            return simplify_formula(implied, ctx)
        return InRel(tup)
    if isinstance(f, Gt):
        a = rebuild(normalize(f.a, ctx))
        b = rebuild(normalize(f.b, ctx))
        if b == ZERO:
            return exists_formula(a, ctx)
        if a == ZERO:
            return FALSE
        return Gt(a, b)
    raise Unsupported(f"formula {type(f).__name__}")


def _foreign_key_member(tup, ctx: Ctx):
    """``ι ∈ D`` for the row of ``D`` with key ``e.f``, where ``e`` is a known row of a table whose
    foreign key ``f`` references that key: the row exists exactly when ``e.f`` is not NULL."""

    if not isinstance(tup, Iota):
        return None
    owners = set()
    for v in tup.values:
        if not isinstance(v, Col) or not isinstance(v.tup, (TVar, Iota)) or v.tup not in ctx.known:
            return None
        owners.add(v.tup)
    if len(owners) != 1:
        return None
    (owner,) = owners
    columns = tuple(v.name for v in tup.values)
    for cols, parent, pcols in ctx.catalog.info(owner.table).foreign:
        if parent != tup.table:
            continue
        pairs = dict(zip(pcols, cols))
        if set(pcols) == set(tup.key) and all(pairs[k] == c for k, c in zip(tup.key, columns)):
            ctx.catalog.used_constraints = True
            return conj(*[neg(IsNull(v)) for v in tup.values])
    return None


def _fold_cmp(op, a, b):
    if isinstance(a, str) and isinstance(b, str) and op not in ("=", "<>"):
        return None
    if type(a) is not type(b) and not (isinstance(a, Fraction) and isinstance(b, Fraction)):
        return None
    try:
        res = {"=": a == b, "<>": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    except TypeError:
        return None
    return TRUE if res else FALSE


def _never_null(v, ctx: Ctx) -> bool:
    if isinstance(v, Lit):
        return v.value is not None
    if isinstance(v, Col):
        tup = v.tup
        if isinstance(tup, TVar) and tup in ctx.known:
            return v.name in ctx.catalog.info(tup.table).not_null
        if isinstance(tup, Iota):
            if v.name in tup.key:
                return False  # the key value itself may be NULL when no row matches
            return tup in ctx.known and v.name in ctx.catalog.info(tup.table).not_null
        return False
    if isinstance(v, Agg):
        return v.func in ("COUNT", "COUNTIF")
    if isinstance(v, BoolV):
        return False
    if isinstance(v, Arith):
        return _never_null(v.a, ctx) and _never_null(v.b, ctx) and v.op != "/"
    return False


def _is_null(v, ctx: Ctx):
    if isinstance(v, Lit):
        return TRUE if v.value is None else FALSE
    if _never_null(v, ctx):
        return FALSE
    if isinstance(v, Arith) and v.op in "+-*":
        return disj(_is_null(v.a, ctx), _is_null(v.b, ctx))
    if isinstance(v, BoolV):
        return conj(neg(v.t), neg(v.f))
    if isinstance(v, Ite):
        return disj(conj(v.cond, _is_null(v.a, ctx)), conj(neg(v.cond), _is_null(v.b, ctx)))
    return IsNull(v)


def simplify_tuple(tup, ctx: Ctx):
    if isinstance(tup, Iota):
        return Iota(tup.table, tup.key, tuple(simplify_value(v, ctx) for v in tup.values))
    return tup


def simplify_value(v, ctx: Ctx):
    if isinstance(v, (Lit, Ref)):
        return v
    if isinstance(v, Col):
        tup = simplify_tuple(v.tup, ctx)
        if isinstance(tup, Iota) and v.name in tup.key:
            return tup.values[tup.key.index(v.name)]
        return Col(tup, v.name, v.kind) if tup is not v.tup else v
    if isinstance(v, Fn):
        args = tuple(simplify_value(a, ctx) for a in v.args)
        if v.strict and any(isinstance(a, Lit) and a.value is None for a in args):
            return Lit(None, v.kind)
        return Fn(v.name, args, v.strict, v.kind)
    if isinstance(v, Arith):
        a, b = simplify_value(v.a, ctx), simplify_value(v.b, ctx)
        if (isinstance(a, Lit) and a.value is None) or (isinstance(b, Lit) and b.value is None):
            return Lit(None, v.kind)
        if isinstance(a, Lit) and isinstance(b, Lit) and isinstance(a.value, Fraction) and isinstance(b.value, Fraction):
            if v.op == "+":
                return Lit(a.value + b.value, v.kind)
            if v.op == "-":
                return Lit(a.value - b.value, v.kind)
            if v.op == "*":
                return Lit(a.value * b.value, v.kind)
            if v.op == "/" and b.value != 0:
                return Lit(a.value / b.value, "num")
        return Arith(v.op, a, b, v.kind)
    if isinstance(v, Ite):
        c = simplify_formula(v.cond, ctx)
        if c == TRUE:
            return simplify_value(v.a, ctx)
        if c == FALSE:
            return simplify_value(v.b, ctx)
        a, b = simplify_value(v.a, ctx), simplify_value(v.b, ctx)
        if a == b:
            return a
        return Ite(c, a, b)
    if isinstance(v, BoolV):
        t, f = simplify_formula(v.t, ctx), simplify_formula(v.f, ctx)
        if t == TRUE:
            return Lit(True, "bool")
        if f == TRUE:
            return Lit(False, "bool")
        if t == FALSE and f == FALSE:
            return Lit(None, "bool")
        return BoolV(t, f)
    if isinstance(v, Agg):
        return _canonical_agg(v, ctx)
    if isinstance(v, Scalar):
        terms = normalize(v.body, ctx)
        if not terms:
            return Lit(None, v.out.kind)
        return Scalar(v.out, rebuild(terms))
    raise Unsupported(f"value {type(v).__name__}")


def _empty_aggregate(v: Agg):
    if v.func in ("COUNT", "COUNTIF"):
        return Lit(Fraction(0), "int")
    return Lit(None, None)


def _canonical_agg(v: Agg, ctx: Ctx):
    """The aggregate over its bag of argument values.

    ``F(arg | Σ x. body)`` becomes ``F(w | Σ w. V(w))`` with ``V(w) = Σ x. body·[w ≡ arg]``,
    the multiplicity of each argument value ``w``. Normalizing ``V`` keeps the argument in
    step with the substitutions made in the body. ``COUNT(*)`` keeps the bag of rows.
    """

    from .ir import fresh_id, nmul

    key = ("agg", v, ctx.known)
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    if v.arg is None:
        terms = normalize(NSum(tuple(v.vars), v.body) if v.vars else v.body, ctx)
        result = _empty_aggregate(v) if not terms else Agg(v.func, v.distinct, (), rebuild(terms), None)
    else:
        w = SVar(fresh_id(), None)
        body = nmul(v.body, NInd(Same(Ref(w), v.arg)))
        terms = normalize(NSum(tuple(v.vars), body) if v.vars else body, ctx)
        result = _empty_aggregate(v) if not terms else Agg(v.func, v.distinct, (w,), rebuild(terms), Ref(w))
    ctx.cache[key] = result
    return result


def _mul(a, b):
    from .ir import nmul

    return nmul(a, b)


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def exists_formula(term, ctx: Ctx):
    """``term > 0`` for a multiplicity term, as a formula."""

    key = ("exists", term, ctx.known)
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    terms = normalize(term, ctx)
    parts = []
    for t in terms:
        if t.coef < 0:
            raise Unsupported("existence of a signed term")
        parts.append(_exists_term(t, ctx))
    result = disj(*parts)
    ctx.cache[key] = result
    if isinstance(result, Exists):
        ctx.cache[("exists", result.term, ctx.known)] = result
    return result


def _exists_term(t: Term, ctx: Ctx):
    positive = []
    for f in t.factors:
        if isinstance(f, NRel):
            positive.append(InRel(f.tup))
        elif isinstance(f, NInd):
            positive.append(f.f)
        elif isinstance(f, NMin):
            positive.append(conj(exists_formula(f.a, ctx), exists_formula(f.b, ctx)))
        elif isinstance(f, NMonus):
            positive.append(Gt(f.a, f.b))
        else:
            raise Unsupported("existence of a weighted term")
    if not t.vars:
        return conj(*positive)
    bound = set(t.vars)
    outside, inside, rels = [], [], []
    for p in positive:
        if free_vars(p) & bound or any(_mentions_tuple(p, v) for v in bound if isinstance(v, TVar)):
            inside.append(p)
        else:
            outside.append(p)
    body_factors = tuple(NRel(f.tup) for f in t.factors if isinstance(f, NRel) and (f.tup in bound or _tuple_bound(f.tup, bound)))
    inner_conds = [p for p in inside if not (isinstance(p, InRel) and any(NRel(p.tup) == r for r in body_factors))]
    from .ir import nmul, nsum

    inner = nsum(t.vars, nmul(*body_factors, *[NInd(c) for c in inner_conds]))
    return conj(*outside, Exists(inner))


def _tuple_bound(tup, bound) -> bool:
    return isinstance(tup, Iota) and any(_mentions_tuple(v, b) for v in tup.values for b in bound if isinstance(b, TVar)) or (
        isinstance(tup, Iota) and bool(free_vars(tup.values) & bound)
    )
