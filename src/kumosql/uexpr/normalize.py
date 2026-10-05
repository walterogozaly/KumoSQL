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
import itertools

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
    value_kind,
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

    def __init__(self, catalog: Catalog, exact: bool, known: frozenset = frozenset(), cache: dict | None = None, facts: tuple = ()):
        self.catalog = catalog
        self.exact = exact
        self.known = known
        self.cache = cache if cache is not None else {}
        self.facts = facts  # conditions on scalar variables that hold wherever an inner term is evaluated
        self.has_truths = any(isinstance(k, tuple) and k[0] == "true" for k in known)

    def with_known(self, more) -> "Ctx":
        more = frozenset(more)
        if more <= self.known:
            return self
        return Ctx(self.catalog, self.exact, self.known | more, self.cache, self.facts)

    def with_facts(self, more) -> "Ctx":
        more = tuple(f for f in more if f not in self.facts)
        if not more:
            return self
        return Ctx(self.catalog, self.exact, self.known, self.cache, self.facts + more)


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

    passes = 0
    for _ in range(64):
        rels = [f for f in t.factors if isinstance(f, NRel)]
        known = {f.tup for f in rels}
        conjs = []
        for f in t.factors:
            if isinstance(f, NInd):
                conjs.extend(_conjuncts(f.f))
        members = {c.tup for c in conjs if isinstance(c, InRel)}
        nonnull = {_NN(c.a.a) for c in conjs if isinstance(c, Not) and isinstance(c.a, IsNull)}
        truths = {
            _TRUE(c) for c in conjs if isinstance(c, (Exists, Or, Gt)) or (isinstance(c, Not) and not isinstance(c.a, IsNull))
        }
        seen = ctx.with_facts(c for c in conjs if _scalar_fact(c))
        inner = seen.with_known(known | members | nonnull | truths)
        # 1. simplify the conditions (a membership, non-NULL or other condition that nested
        # subterms may assume is not used to simplify itself)
        new_conjs = []
        for c in conjs:
            if isinstance(c, InRel) and c.tup not in known and c.tup not in ctx.known:
                s = simplify_formula(c, seen.with_known(known | (members - {c.tup}) | nonnull | truths))
            elif isinstance(c, Not) and isinstance(c.a, IsNull):
                s = simplify_formula(c, seen.with_known(known | members | (nonnull - {_NN(c.a.a)}) | truths))
            elif _TRUE(c) in truths:
                s = simplify_formula(c, seen.with_known(known | members | nonnull | (truths - {_TRUE(c)})))
            else:
                s = simplify_formula(c, inner)
            if s == FALSE:
                return []
            new_conjs.extend(_conjuncts(s))
        new_conjs = _prune_non_null(_dedup(new_conjs + _propagated_facts(new_conjs, t, inner)))
        propagated = _propagate_equalities(new_conjs, set(t.vars))
        if propagated is not None:
            new_conjs = propagated
        pulled = _pull_outer_equalities(new_conjs, set(t.vars))
        if pulled is not None:
            new_conjs = pulled
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
        # 4. a scalar restricted to a few distinct literals is that many terms
        split = _split_literal_domain(t)
        if split is not None:
            return [u for part in split for u in simplify_term(part, ctx)]
        # 5. a sum over the key values of a keyed table is a sum over its rows
        changed = _unkey_scalars(t, ctx)
        if changed is not None:
            t = changed
            continue
        # 6. an existence test that the term's own rows witness is redundant
        changed = _drop_implied_exists(t, ctx)
        if changed is not None:
            t = changed
            continue
        # 7. conditions simplified against each other: once more, until they settle
        if passes < 3 and not _settled(new_conjs, conjs):
            passes += 1
            continue
        break
    for v in t.vars:
        if isinstance(v, SVar) and not any(v in free_vars(f) for f in t.factors):
            raise Unsupported("an unconstrained scalar variable (an infinite sum)")
    return [Term(_ordered_vars(t), t.coef, t.factors)]


def _scalar_fact(c) -> bool:
    """A comparison of one scalar variable with constants (no tuple variable, no inner term)."""

    if not isinstance(c, (Cmp, IsNull, Not)):
        return False
    fv = free_vars(c)
    if len(fv) != 1 or not isinstance(next(iter(fv)), SVar):
        return False
    return not any(isinstance(x, (Exists, Agg, Scalar, Gt)) for x in walk(c))


def _propagated_facts(conjs: list, t: Term, ctx: Ctx) -> list:
    """Conditions on an outside value ``o`` rewritten onto the inside value ``e`` it equals.

    An inner term inside ``[o > c]·...`` sees ``[o = e]·[o > c]``, which implies ``[e > c]``; stating it
    inside lets ``Σx.[o = x.k]·R(x)`` under ``[o > c]`` and ``Σx.[x.k > c]·[o = x.k]·R(x)`` agree.
    """

    if not ctx.facts:
        return []
    bound = set(t.vars)
    out = []
    for c in conjs:
        if isinstance(c, Same):
            pairs = [(c.a, c.b), (c.b, c.a)]
        elif isinstance(c, Cmp) and c.op == "=":
            pairs = [(c.a, c.b), (c.b, c.a)]
        else:
            continue
        for o, e in pairs:
            if not isinstance(o, Ref) or o.var in bound or o.var in free_vars(e) or not (free_vars(e) & bound):
                continue
            for f in ctx.facts:
                if free_vars(f) == {o.var}:
                    g = simplify_formula(subst(f, {o.var: e}), ctx)
                    if g != TRUE:
                        out.extend(_conjuncts(g))
    return out


def _cond_key(c):
    """A formula up to the order of the two sides of ``=`` / ``≡``."""

    if isinstance(c, (Same,)) or (isinstance(c, Cmp) and c.op == "="):
        return (type(c).__name__, getattr(c, "op", None), frozenset((c.a, c.b)))
    return c


def _witnessed(ex, conds: set, rows: set, ctx: Ctx) -> bool:
    """Whether the existence test ``ex`` holds wherever a term with rows ``rows`` and conditions ``conds`` is non-zero.

    ``ex`` is ``∃ Σy. R(y)·φ1(y)··φk(y)`` over rows only. If some choice of rows ``x`` of the
    term (of the same tables) makes every ``φi(x)`` one of the term's own conditions, then wherever
    the term is non-zero its rows ``x`` are in their tables and satisfy those conditions, so they
    witness ``ex``. ``rows`` are the tuple variables the term sums over and the rows ``ι`` it
    requires to exist (``ι ∈ R`` is one of its conditions)."""

    body = ex.term
    if not isinstance(body, NSum) or not all(isinstance(v, TVar) for v in body.vars):
        return False
    factors = body.body.args if isinstance(body.body, NMul) else (body.body,)
    rel_vars = {f.tup for f in factors if isinstance(f, NRel)}
    if set(body.vars) != rel_vars or len(rel_vars) != len(body.vars):
        return False
    needed = []
    for f in factors:
        if isinstance(f, NRel):
            continue
        if not isinstance(f, NInd):
            return False
        needed.extend(_conjuncts(f.f))
    pools = [[x for x in sorted(rows, key=repr) if x.table == y.table] for y in body.vars]
    if any(not p for p in pools) or len(body.vars) > 4:
        return False
    inner = ctx.with_known(rows)
    count = 0
    for choice in itertools.product(*pools):
        count += 1
        if count > 64:
            return False
        m = dict(zip(body.vars, choice))
        for c in needed:
            c = simplify_formula(subst(c, m), inner)
            if c == TRUE or (isinstance(c, InRel) and c.tup in rows):
                continue
            if _cond_key(c) not in conds:
                break
        else:
            return True
    return False


def _drop_implied_exists(t: Term, ctx: Ctx) -> Term | None:
    """``Σx. R(x)·φ(x)·[∃y. R(y)·φ(y)]·f = Σx. R(x)·φ(x)·f``: the existence test is witnessed by ``x`` itself."""

    rows = {f.tup for f in t.factors if isinstance(f, NRel) and f.tup in t.vars and isinstance(f.tup, TVar)}
    for f in t.factors:
        if isinstance(f, NInd):
            rows.update(c.tup for c in _conjuncts(f.f) if isinstance(c, InRel) and isinstance(c.tup, Iota))
    if not rows:
        return None
    for f in t.factors:
        if not isinstance(f, NInd):
            continue
        c = f.f
        tests = [c] if isinstance(c, Exists) else [a for a in c.args if isinstance(a, Exists)] if isinstance(c, Or) else []
        if not tests:
            continue
        conds = {_cond_key(x) for g in t.factors if isinstance(g, NInd) and g is not f for x in _conjuncts(g.f)}
        if any(_witnessed(e, conds, rows, ctx) for e in tests):
            return Term(t.vars, t.coef, tuple(g for g in t.factors if g is not f))
    return None


def _settled(new: list, old: list) -> bool:
    """The conditions did not change (an aggregate's bound variables are renamed on every pass, so
    compare up to those names)."""

    if set(new) == set(old):
        return True
    from .canon import canon

    return {canon(c) for c in new} == {canon(c) for c in old}


def _ordered_vars(t: Term) -> tuple:
    used = set()
    for f in t.factors:
        free_vars(f, used)
    return tuple(v for v in t.vars if v in used or isinstance(v, TVar))


def _alpha_equal(x, y) -> bool:
    """Equal up to the names of bound variables (an aggregate is renamed each time it is normalized)."""

    if x == y:
        return True
    from .canon import canon

    return canon(x) == canon(y)


def _prune_non_null(conjs: list) -> list:
    """Drop ``¬a IS NULL`` when another condition is a comparison with ``a`` (it is TRUE only for non-NULL operands)."""

    compared = set()
    for c in conjs:
        if isinstance(c, Cmp):
            compared.add(c.a)
            compared.add(c.b)
    return [c for c in conjs if not (isinstance(c, Not) and isinstance(c.a, IsNull) and c.a.a in compared)]


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


def _split_literal_domain(t: Term) -> list | None:
    """``Σs. [s = a ∨ s = b]·f(s) = f(a) + f(b)`` for distinct literals ``a``, ``b`` (the disjuncts exclude each other)."""

    for f in t.factors:
        if not (isinstance(f, NInd) and isinstance(f.f, Or)):
            continue
        var, lits = None, []
        for a in f.f.args:
            if not (isinstance(a, Cmp) and a.op == "=" and isinstance(a.a, Ref) and isinstance(a.b, Lit) and a.b.value is not None):
                break
            if var is None:
                var = a.a.var
            if a.a.var != var:
                break
            lits.append(a.b)
        else:
            if var in t.vars and isinstance(var, SVar) and len({(type(x.value), x.value) for x in lits}) == len(lits) and len(lits) <= 16:
                rest = tuple(g for g in t.factors if g is not f)
                return [
                    Term(tuple(v for v in t.vars if v != var), t.coef, tuple(subst(g, {var: lit}) for g in rest)) for lit in lits
                ]
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
    # eliminate in table-name order, so that two joins of the same rows through different keys
    # (``e.empno = d.deptno`` with both sides keyed) reach one form whichever side was written first
    tvars = sorted((v for v in t.vars if isinstance(v, TVar)), key=lambda v: v.table)
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
    result = _simplify_formula(f, ctx)
    if ctx.has_truths:
        if isinstance(result, And):
            return conj(*[a for a in result.args if not _is_known(a, ctx)])
        if _is_known(result, ctx):
            return TRUE
    return result


def _is_known(f, ctx: Ctx) -> bool:
    if isinstance(f, Not) and isinstance(f.a, IsNull):
        return _NN(f.a.a) in ctx.known
    return isinstance(f, (Exists, Or, Gt, Not)) and _TRUE(f) in ctx.known


def _simplify_formula(f, ctx: Ctx):
    if isinstance(f, FConst):
        return f
    if isinstance(f, And):
        return conj(*[simplify_formula(a, ctx) for a in f.args])
    if isinstance(f, Or):
        return disj(*[simplify_formula(a, ctx) for a in f.args])
    if isinstance(f, Not):
        inner = simplify_formula(f.a, ctx)
        if isinstance(inner, Cmp) and _never_null(inner.a, ctx) and _never_null(inner.b, ctx):
            return Cmp(_NEGATED_CMP[inner.op], inner.a, inner.b)
        return neg(inner)
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


def _TRUE(f) -> tuple:
    """The marker in ``Ctx.known`` for a condition that holds wherever the enclosing product is non-zero
    (up to the names of its bound variables)."""

    from .canon import canon

    return ("true", canon(f))


def _propagate_equalities(conjs: list, bound: set):
    """The conditions with each bound column that equals an outside value replaced by that value
    (``[s = y.a]·[∃z (z.b = y.a)]`` becomes ``[s = y.a]·[∃z (z.b = s)]``); ``None`` if nothing changes."""

    eq = _equalities(conjs)
    if not eq:
        return None
    mapping = {}
    seen = set()
    for members in eq.values():
        if id(members) in seen:
            continue
        seen.add(id(members))
        outer = [m for m in members if not _bound_in(m, bound)]
        if not outer:
            continue
        outer.sort(key=lambda m: (0 if isinstance(m, Lit) and m.value is not None else 1 if isinstance(m, Ref) else 2, repr(m)))
        if isinstance(outer[0], Lit) and outer[0].value is None:
            continue
        for m in members:
            if isinstance(m, Col) and _bound_in(m, bound):
                mapping[m] = outer[0]
    if not mapping:
        return None
    from .ir import replace

    out = []
    for c in conjs:
        if (isinstance(c, Same) or (isinstance(c, Cmp) and c.op == "=")) and c.a in eq and c.b in eq and eq[c.a] is eq[c.b]:
            out.append(c)
        else:
            out.append(replace(c, mapping))
    return None if out == conjs else _dedup(out)


def _pull_outer_equalities(conjs: list, bound: set):
    """Equalities between outside values that a term's conditions force, taken out of the sum.

    ``Σy. [s = y.a]·[y.a = 10]·f(y) = [s = 10]·Σy. [y.a = 10]·f(y)``: when an equality class of the
    conditions holds two or more values that do not mention the summed variables, the conditions say those
    values are equal, whatever the rows. Done for such classes only (see ``_pull_equalities``)."""

    parent: dict = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    links = [c for c in conjs if isinstance(c, Same) or (isinstance(c, Cmp) and c.op == "=")]
    for c in links:
        ra, rb = find(c.a), find(c.b)
        if ra != rb:
            parent[ra] = rb
    outside: dict = {}
    inside_roots = set()
    for c in links:
        for a in (c.a, c.b):
            if _bound_in(a, bound):
                inside_roots.add(find(a))
            else:
                outside.setdefault(find(a), set()).add(a)
    roots = {r for r, m in outside.items() if len(m) >= 2 and r in inside_roots}
    if not roots:
        return None
    chosen = [c for c in links if find(c.a) in roots]
    extra, rewritten = _pull_equalities(chosen, bound)
    new = _dedup([c for c in conjs if c not in chosen] + extra + rewritten)
    return None if set(new) == set(conjs) else new


_NEGATED_CMP = {"=": "<>", "<>": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def _NN(v) -> tuple:
    """The marker in ``Ctx.known`` for a value that an enclosing condition makes non-NULL."""

    return ("non-null", v)


def _never_null(v, ctx: Ctx) -> bool:
    if _NN(v) in ctx.known:
        return True
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


def _fold_string_fn(name: str, args: tuple):
    """The literal an ASCII string function gives on literal arguments, or ``None``.

    Only ``UPPER``, ``LOWER``, ``CONCAT`` and ``SUBSTRING`` at a positive start, where engines agree.
    """

    def text(a):
        return isinstance(a, Lit) and isinstance(a.value, str) and a.kind == "str" and a.value.isascii()

    def whole(a):
        return isinstance(a, Lit) and isinstance(a.value, Fraction) and a.value.denominator == 1

    if name in ("UPPER", "LOWER") and len(args) == 1 and text(args[0]):
        return Lit(args[0].value.upper() if name == "UPPER" else args[0].value.lower(), "str")
    if name == "CONCAT" and args and all(text(a) for a in args):
        return Lit("".join(a.value for a in args), "str")
    if name == "SUBSTRING" and len(args) in (2, 3) and text(args[0]) and all(whole(a) for a in args[1:]):
        start = int(args[1].value)
        length = int(args[2].value) if len(args) == 3 else None
        if start >= 1 and (length is None or length >= 0):
            return Lit(args[0].value[start - 1 :] if length is None else args[0].value[start - 1 : start - 1 + length], "str")
    return None


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
        folded = _fold_string_fn(v.name, args)
        if folded is not None:
            return folded
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
        if isinstance(b, Lit) and b.value is None and isinstance(c, Not) and isinstance(c.a, IsNull) and _alpha_equal(c.a.a, a):
            return a  # CASE WHEN a IS NOT NULL THEN a END is a
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


_DISTINCT_AS_SQUASH = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})


def _canonical_agg(v: Agg, ctx: Ctx):
    """The aggregate over its bag of argument values.

    ``F(arg | Σ x. body)`` becomes ``F(w | Σ w. V(w))`` with ``V(w) = Σ x. body·[w ≡ arg]``,
    the multiplicity of each argument value ``w``. Normalizing ``V`` keeps the argument in
    step with the substitutions made in the body. ``COUNT(*)`` keeps the bag of rows.
    """

    from .ir import fresh_id, nmul

    key = ("agg", v, ctx.known, ctx.facts)
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    if v.arg is None:
        terms = normalize(NSum(tuple(v.vars), v.body) if v.vars else v.body, ctx)
        result = _empty_aggregate(v) if not terms else Agg(v.func, v.distinct, (), rebuild(terms), None)
    else:
        if v.distinct and v.func in ("MIN", "MAX"):
            v = Agg(v.func, False, v.vars, v.body, v.arg)  # only the set of values matters to them
        w = SVar(fresh_id(), None)
        body = nmul(v.body, NInd(Same(Ref(w), v.arg)))
        terms = normalize(NSum(tuple(v.vars), body) if v.vars else body, ctx)
        if not terms:
            result = _empty_aggregate(v)
        elif v.distinct and v.func in _DISTINCT_AS_SQUASH:
            # F(DISTINCT w | V(w)) = F(w | [∃ V(w)]): each value counted once, which is the squashed bag
            squashed = exists_formula(rebuild(terms), ctx)
            result = Agg(v.func, False, (w,), rebuild(normalize(NInd(squashed), ctx)), Ref(w))
        else:
            result = Agg(v.func, v.distinct, (w,), rebuild(terms), Ref(w))
        if terms and isinstance(result, Agg):
            single = _one_valued_aggregate(v, terms, w, ctx)
            if single is not None:
                result = single
            else:
                merged = _extremum_of_extrema(v, terms, w)
                if merged is not None:
                    result = _canonical_agg(merged, ctx)
    ctx.cache[key] = result
    return result


def _one_valued_aggregate(v: Agg, terms: list, w: SVar, ctx: Ctx):
    """``MIN``/``MAX``/``SUM(DISTINCT)`` over a bag whose only possible value is one expression ``e``.

    ``terms`` is the bag as ``V(w)``, the multiplicity of each argument value ``w``. If every term
    forces ``w ≡ e`` for the same ``e`` that mentions neither ``w`` nor the term's own variables
    (through its ``=`` / ``≡`` conditions), the set of non-NULL values is ``{e}`` when some term
    holds at ``w = e`` with ``e`` non-NULL, and empty otherwise. Only the set matters to ``MIN``,
    ``MAX`` and ``SUM(DISTINCT)``, and each of them is ``e`` on ``{e}`` and NULL on the empty set,
    so the aggregate is ``CASE WHEN ∃ ... THEN e END``. ``SUM`` of the bag itself (without
    ``DISTINCT``) is the same only when no value has multiplicity above one; ``AVG`` is left alone.
    """

    from .canon import canon
    from .ir import nsum, nmul

    # SUM without DISTINCT also depends on the multiplicities: only a bag that holds each value at most once
    # (one term without variables, made of conditions) is the set.
    plain_sum = v.func == "SUM" and not v.distinct
    if not (v.func in ("MIN", "MAX") or v.func == "SUM"):
        return None
    if plain_sum and not (len(terms) == 1 and not terms[0].vars and terms[0].coef == 1 and all(isinstance(f, NInd) for f in terms[0].factors)):
        return None
    chosen = None
    key = None
    parts = []
    for t in terms:
        if t.coef <= 0 or not all(isinstance(f, (NRel, NInd)) for f in t.factors):
            return None
        conjs = [x for f in t.factors if isinstance(f, NInd) for x in _conjuncts(f.f)]
        bound = set(t.vars)
        members = _equalities(conjs).get(Ref(w), [])
        best = None
        for m in members:
            if m == Ref(w) or w in free_vars(m) or (free_vars(m) & bound) or any(_mentions_tuple(m, y) for y in bound if isinstance(y, TVar)):
                continue
            rank = (0 if isinstance(m, Lit) and m.value is not None else 1 if isinstance(m, (Ref, Col)) else 2, repr(m))
            if best is None or rank < best[0]:
                best = (rank, m)
        if best is None:
            return None
        e = best[1]
        ck = repr(canon(e))
        if key is None:
            chosen, key = e, ck
        elif ck != key:
            return None
        body = nmul(*[subst(f, {w: chosen}) if not isinstance(f, NRel) else f for f in t.factors], NInd(neg(IsNull(chosen))))
        parts.append(nsum(t.vars, body))
    if v.func == "SUM" and not any(value_kind(x) in ("int", "num") for x in (v.arg, chosen)):
        return None
    cond = exists_formula(_sum_of(parts), ctx)
    return simplify_value(Ite(cond, chosen, Lit(None, value_kind(chosen))), ctx)


def _extremum_of_extrema(v: Agg, terms: list, w: SVar):
    """``MIN`` over per-group ``MIN`` values is the ``MIN`` over the union of the groups (likewise ``MAX``).

    The bag is ``Σ g. Q(g)·[w ≡ MIN(w' | W_g(w'))]`` for scalar variables ``g`` (the groups). Every
    value of every group ``W_g`` is at least that group's minimum, and the minimum is itself a value of the
    group, so the minimum of the group minima is the minimum over all the values of the groups that satisfy
    ``Q``; and the group minima are NULL only for groups without a non-NULL value. Only the set of values
    matters to ``MIN``, so the result is ``MIN(w' | Σ g. Q(g)·W_g(w'))``, a bag whose multiplicities are
    not the aggregates' (the inner multiplicities are summed with ``Q``)."""

    from .ir import nadd, nsum, nmul, rename_bound, fresh_id

    if v.func not in ("MIN", "MAX"):
        return None
    w2 = SVar(fresh_id(), None)
    parts = []
    for t in terms:
        if t.coef <= 0 or any(isinstance(x, TVar) for x in t.vars) or not all(isinstance(f, NInd) for f in t.factors):
            return None
        conjs = [x for f in t.factors for x in _conjuncts(f.f)]
        found = None
        for c in conjs:
            if isinstance(c, (Same,)) or (isinstance(c, Cmp) and c.op == "="):
                for a, b in ((c.a, c.b), (c.b, c.a)):
                    if a == Ref(w) and isinstance(b, Agg) and b.func == v.func and len(b.vars) == 1 and b.arg == Ref(b.vars[0]):
                        found = (c, b)
                        break
            if found:
                break
        if found is None:
            return None
        c, inner = found
        rest = [x for x in conjs if x is not c]
        if w in free_vars(inner) or any(w in free_vars(x) for x in rest):
            return None
        fresh = rename_bound(inner)
        body = subst(fresh.body, {fresh.vars[0]: Ref(w2)})
        parts.append(nsum(t.vars, nmul(*[NInd(x) for x in rest], body)))
    return Agg(v.func, False, (w2,), nadd(*parts), Ref(w2))


def _sum_of(parts):
    from .ir import nadd

    return nadd(*parts)


def _mul(a, b):
    from .ir import nmul

    return nmul(a, b)


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def exists_formula(term, ctx: Ctx):
    """``term > 0`` for a multiplicity term, as a formula."""

    key = ("exists", term, ctx.known, ctx.facts)
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    terms = normalize(term, ctx)
    parts = []
    for t in terms:
        if t.coef < 0:
            raise Unsupported("existence of a signed term")
        for u in _flatten_exists(t, ctx):
            parts.append(_exists_term(u, ctx))
    result = disj(*parts)
    ctx.cache[key] = result
    if isinstance(result, Exists):
        ctx.cache[("exists", result.term, ctx.known, ctx.facts)] = result
    return result


def _flatten_exists(t: Term, ctx: Ctx, depth: int = 0) -> list:
    """The terms equal to ``t`` as far as existence goes, with a nested ``∃`` merged into it.

    ``∃x (A·[∃y B])`` holds exactly when ``∃x,y (A·B)`` does, so inside an existence test a nested
    ``∃`` over a single product is just more summed variables (and may now eliminate some).
    """

    if depth < 8:
        for f in t.factors:
            if not (isinstance(f, NInd) and isinstance(f.f, Exists)):
                continue
            sub = normalize(f.f.term, ctx)
            if len(sub) != 1 or sub[0].coef < 0 or not all(isinstance(g, (NRel, NInd)) for g in sub[0].factors):
                continue
            rest = Term(t.vars, t.coef, tuple(g for g in t.factors if g is not f))
            out = []
            for u in simplify_term(product(rest, sub[0]), ctx):
                out.extend(_flatten_exists(u, ctx, depth + 1))
            return out
    return [t]


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
    from .ir import nmul, nsum

    extra, inside = _pull_equalities(inside, bound)
    inner_conds = [p for p in inside if not (isinstance(p, InRel) and any(NRel(p.tup) == r for r in body_factors))]
    items = [*body_factors, *[NInd(c) for c in inner_conds]]
    atoms = [Exists(nsum(vs, nmul(*its))) for vs, its in _components(t.vars, items, bound)]
    return conj(*outside, *[c for c in extra if c not in outside], *atoms)


def _bound_in(n, bound: set) -> set:
    found = free_vars(n) & bound
    return found | {b for b in bound if isinstance(b, TVar) and _mentions_tuple(n, b)}


def _components(vars_: tuple, items: list, bound: set) -> list:
    """The summed variables and factors of a product, split into groups that share no variable
    (``∃x,y A(x)·B(y)`` is ``∃x A(x) ∧ ∃y B(y)``). One group when the product does not split."""

    parent = {v: v for v in vars_}

    def find(a):
        while parent[a] != a:
            a = parent[a]
        return a

    used = []
    for item in items:
        mine = [v for v in vars_ if v in _bound_in(item.f if isinstance(item, NInd) else item, bound)]
        used.append(mine)
        for v in mine[1:]:
            ra, rb = find(mine[0]), find(v)
            if ra != rb:
                parent[ra] = rb
    groups: dict = {}
    for v in vars_:
        groups.setdefault(find(v), ([], []))[0].append(v)
    for item, mine in zip(items, used):
        if mine:
            groups[find(mine[0])][1].append(item)
    if len(groups) == 1:
        return [(tuple(vars_), list(items))]
    return [(tuple(vs), its) for vs, its in groups.values()]


def _pull_equalities(inside: list, bound: set) -> tuple:
    """Equalities between outside values that the conditions inside an ``∃`` force, and the inside
    conditions with each equality class that reaches outside re-expressed against one outside member.

    ``∃y [x = y.a][y.a = 10]`` implies ``x = 10``. Values equal under the inside conjuncts (``=`` TRUE
    or ``≡``) form union-find classes. A class with any ``=`` link is all non-NULL and equal, otherwise
    all ``≡``; so two outside members of it are equal (and non-NULL), and the inside links of the class
    are the same as one link from each inside member to an outside member.
    """

    parent: dict = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    links = [c for c in inside if isinstance(c, Same) or (isinstance(c, Cmp) and c.op == "=")]
    for c in links:
        ra, rb = find(c.a), find(c.b)
        if ra != rb:
            parent[ra] = rb
    strict = {find(c.a) for c in links if isinstance(c, Cmp)}
    classes: dict = {}
    for c in links:
        for a in (c.a, c.b):
            members = classes.setdefault(find(a), [])
            if a not in members:
                members.append(a)

    def is_outside(v) -> bool:
        return not _bound_in(v, bound)

    extra = []
    rewritten = set()
    stars = []
    for root, members in classes.items():
        outer = [m for m in members if is_outside(m)]
        if not outer:
            continue
        outer.sort(key=lambda m: (0 if isinstance(m, Lit) else 1 if isinstance(m, Ref) else 2, repr(m)))
        rep = outer[0]
        rewritten.add(root)
        make = (lambda m, r: Cmp("=", m, r)) if root in strict else (lambda m, r: Same(m, r))
        for m in members:
            if m is not rep and m != rep:
                (extra if is_outside(m) else stars).append(make(m, rep))
        if root in strict and not (isinstance(rep, Lit)):
            extra.append(neg(IsNull(rep)))
    if not rewritten:
        return [], inside
    kept = [c for c in inside if not (c in links and find(c.a) in rewritten)]
    return extra, kept + stars


def _tuple_bound(tup, bound) -> bool:
    return isinstance(tup, Iota) and any(_mentions_tuple(v, b) for v in tup.values for b in bound if isinstance(b, TVar)) or (
        isinstance(tup, Iota) and bool(free_vars(tup.values) & bound)
    )
