"""Deciding equality of normalized multiplicity terms with z3.

Two normalized sums ``Σ_i A_i`` and ``Σ_j B_j`` are compared signature by signature:
terms over the same multiset of tables (after canonical renaming) form a group, and a
group holds when its terms agree at every point. For a group whose terms all carry the
multiplicity monomial ``R1(x1)·..·Rk(xk)``,

    Σ_x m(x)·P(x) = Σ_x m(x)·Q(x)   whenever   ∀x. m(x) > 0 ⇒ Σ_π P(πx) = Σ_π Q(πx)

where ``π`` ranges over the permutations of same-table variables (the sum over ``x`` is
invariant under renaming, and so is ``m``). What remains is linear arithmetic over 0/1
indicators and numeric values, which z3 decides; the multiplicities themselves cancel.

Inner terms (existence tests, counts and sums, aggregates, scalar subqueries) are
abstracted: each is lifted to a closed lambda over the values it reads from outside
(``canon.lift``) and becomes an uninterpreted function of those values, so equal
arguments give equal results. Lambdas proven equal by a recursive check share their
results. An existence test ``∃y. φ(y)`` also gets instance axioms: a fresh witness when
it holds (``∃y.φ ⇒ φ(sk)``) and ``φ(t) ⇒ ∃y.φ`` for the rows ``t`` the problem names.
Declared keys, NOT NULL columns and foreign keys are axioms over the rows a problem names.
"""

from __future__ import annotations

from fractions import Fraction
import itertools
import time

from .canon import canon_term, lift, signature
from .ir import (
    FALSE,
    TRUE,
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
    NConst,
    NInd,
    NMin,
    NMonus,
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
    fresh_id,
    free_vars,
    neg,
    nmul,
    nsum,
    subst,
    value_kind,
)
from .normalize import Ctx, Term, exists_formula, normalize, rebuild, simplify_formula, simplify_value
from .translate import Unsupported

try:  # pragma: no cover - import guard
    import z3
except ImportError:  # pragma: no cover
    z3 = None


class Timeout(Exception):
    pass


import os as _os

_DEBUG = bool(_os.environ.get("UEXPR_DEBUG"))


_SORTS = {}


def _uval():
    if "uval" not in _SORTS:
        d = z3.Datatype("UexprValue")
        d.declare("Null")
        d.declare("Num", ("num", z3.RealSort()))
        d.declare("Str", ("str", z3.StringSort()))
        d.declare("Bool", ("bool", z3.BoolSort()))
        _SORTS["uval"] = d.create()
        _SORTS["rest"] = z3.DeclareSort("UexprRest")
        _SORTS["rank"] = z3.Function("uexpr_rank", z3.StringSort(), z3.RealSort())
    return _SORTS["uval"]


MAX_WITNESS_MAPS = 64
MAX_INSTANCES = 400
MAX_PERMUTATIONS = 24
MAX_DEPTH = 6


class Prover:
    """Recursive equality checks over one proof, with caches shared by its sub-problems."""

    def __init__(self, ctx: Ctx, *, timeout_ms: int = 5000, budget_s: float | None = None):
        if z3 is None:
            raise Unsupported("z3 is not installed")
        _uval()
        self.ctx = ctx
        self.catalog = ctx.catalog
        self.timeout_ms = timeout_ms
        self.deadline = time.monotonic() + (budget_s if budget_s is not None else max(timeout_ms / 1000.0 * 4, 2.0))
        self.classes: dict = {}  # (kind, key) -> class id
        self.parent: dict = {}  # union-find over class ids
        self.memo: dict = {}
        self.in_progress: set = set()
        self.lambdas: dict = {}  # class id -> (kind, lifted)
        self.checks = 0

    # ---- lambda classes ---------------------------------------------------

    def class_of(self, kind: str, key: str) -> int:
        k = (kind, key)
        cid = self.classes.get(k)
        if cid is None:
            cid = len(self.classes) + 1
            self.classes[k] = cid
            self.parent[cid] = cid
        return self.find(cid)

    def find(self, c: int) -> int:
        while self.parent[c] != c:
            self.parent[c] = self.parent[self.parent[c]]
            c = self.parent[c]
        return c

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise Timeout()

    # ---- bag and set equality ---------------------------------------------

    def bag_equal(self, a_terms: list, b_terms: list, depth: int = 0) -> bool:
        """``Σ a_terms = Σ b_terms`` for every database and every value of the free variables."""

        self.check_time()
        if depth > MAX_DEPTH:
            return False
        ca = [canon_term(t) for t in a_terms]
        cb = [canon_term(t) for t in b_terms]
        key = ("bag", tuple(sorted(map(repr, ca))), tuple(sorted(map(repr, cb))))
        if key in self.memo:
            return self.memo[key]
        if key in self.in_progress:
            return False
        self.in_progress.add(key)
        try:
            groups: dict = {}
            for t in ca:
                groups.setdefault(signature(t), ([], []))[0].append(t)
            for t in cb:
                groups.setdefault(signature(t), ([], []))[1].append(t)
            result = True
            for sig, (ta, tb) in sorted(groups.items()):
                if sorted(map(repr, ta)) == sorted(map(repr, tb)):
                    continue
                if not self.group_equal(ta, tb, depth):
                    result = False
                    break
        finally:
            self.in_progress.discard(key)
        self.memo[key] = result
        return result

    def group_equal(self, ta: list, tb: list, depth: int) -> bool:
        tvars = sorted({v for t in ta + tb for v in t.vars}, key=repr)
        groups: dict = {}
        for v in tvars:
            groups.setdefault(v.table if isinstance(v, TVar) else "$s", []).append(v)
        total = 1
        for vs in groups.values():
            for i in range(2, len(vs) + 1):
                total *= i
        if self._group_check(ta, tb, tvars, [{}], depth):
            return True
        if 1 < total <= MAX_PERMUTATIONS:
            perms = [{}]
            for vs in groups.values():
                if len(vs) < 2:
                    continue
                perms = [dict(p, **dict(zip(vs, perm))) for p in perms for perm in itertools.permutations(vs)]
            return self._group_check(ta, tb, tvars, perms, depth)
        return False

    def _group_check(self, ta, tb, tvars, perms, depth) -> bool:
        problem = Problem(self, depth)
        for v in tvars:
            if isinstance(v, TVar):
                problem.assume(problem.in_rel(v))
        lhs, rhs = [], []
        for perm in perms:
            for t in ta:
                lhs.append(problem.term(_rename_term(t, perm)))
            for t in tb:
                rhs.append(problem.term(_rename_term(t, perm)))
        left = z3.Sum(*lhs) if lhs else z3.RealVal(0)
        right = z3.Sum(*rhs) if rhs else z3.RealVal(0)
        result = problem.prove(left == right)
        if _DEBUG:
            print(f"{'  ' * depth}group {len(perms)} perms -> {result}")
            for t in ta:
                print(f"{'  ' * depth}  A {t}")
            for t in tb:
                print(f"{'  ' * depth}  B {t}")
        return result

    def set_equal_formulas(self, fa, fb, depth: int = 0) -> bool:
        """Two formulas over the same free variables are equivalent."""

        self.check_time()
        problem = Problem(self, depth)
        a = problem.form(fa)
        b = problem.form(fb)
        result = problem.prove(a == b)
        if _DEBUG:
            print(f"{'  ' * depth}formulas -> {result}\n{'  ' * depth}  A {fa}\n{'  ' * depth}  B {fb}")
        return result

    # ---- lambda equivalence -------------------------------------------------

    def lambda_equal(self, kind: str, la, lb, depth: int) -> bool:
        key = ("lambda", kind, repr(la), repr(lb))
        if key in self.memo:
            return self.memo[key]
        if key in self.in_progress or depth > MAX_DEPTH:
            return False
        self.in_progress.add(key)
        try:
            result = self._lambda_equal(kind, la, lb, depth)
        except Unsupported:
            result = False
        finally:
            self.in_progress.discard(key)
        self.memo[key] = result
        self.memo[("lambda", kind, repr(lb), repr(la))] = result
        return result

    def _lambda_equal(self, kind, la, lb, depth) -> bool:
        ctx = self.ctx
        if kind == "ex":
            return self.set_equal_formulas(la, lb, depth + 1)
        if kind == "ns":
            return self.bag_equal(normalize(la, ctx), normalize(lb, ctx), depth + 1)
        if kind == "agg":
            if la.func != lb.func or la.distinct != lb.distinct or (la.arg is None) != (lb.arg is None):
                return False
            if la.arg is None:
                return self.bag_equal(normalize(la.body, ctx), normalize(lb.body, ctx), depth + 1)
            w = SVar(fresh_id(), None)
            va = subst(la.body, {la.vars[0]: Ref(w)})
            vb = subst(lb.body, {lb.vars[0]: Ref(w)})
            if la.distinct or la.func in ("MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR"):
                fa = exists_formula(nmul(va, NInd(neg(IsNull(Ref(w))))), ctx)
                fb = exists_formula(nmul(vb, NInd(neg(IsNull(Ref(w))))), ctx)
                return self.set_equal_formulas(fa, fb, depth + 1)
            return self.bag_equal(normalize(va, ctx), normalize(vb, ctx), depth + 1)
        if kind == "scalar":
            o = SVar(fresh_id(), None)
            ba = subst(la.body, {la.out: Ref(o)})
            bb = subst(lb.body, {lb.out: Ref(o)})
            return self.bag_equal(normalize(ba, ctx), normalize(bb, ctx), depth + 1)
        return False


def _rename_term(t: Term, perm: dict) -> Term:
    if not perm:
        return t
    m = {k: v for k, v in perm.items()}
    return Term(tuple(m.get(v, v) for v in t.vars), t.coef, tuple(_rename_vars(f, m) for f in t.factors))


def _rename_vars(n, m):
    from .canon import _rename

    return _rename(n, m)


class Problem:
    """One SMT query: the encoding of values, conditions and terms, with its axioms."""

    def __init__(self, prover: Prover, depth: int):
        self.prover = prover
        self.depth = depth
        self.ctx = prover.ctx
        self.catalog = prover.catalog
        self.V = _uval()
        self.facts: list = []
        self.consts: dict = {}
        self.tuples: dict = {}  # table -> {tuple term: None}, insertion ordered
        self.atoms: list = []  # (kind, class id, lifted, args, z3 term, origin)
        self.atom_index: dict = {}
        self.pending_ex: list = []  # existence atoms waiting for witness axioms
        self.skolemized: set = set()
        self.instances = 0
        self.functions: dict = {}

    # ---- plumbing ----------------------------------------------------------

    def assume(self, f) -> None:
        self.facts.append(f)

    def fn(self, name: str, domain: list, rng):
        key = (name, len(domain))
        f = self.functions.get(key)
        if f is None:
            f = z3.Function(name, *domain, rng)
            self.functions[key] = f
        return f

    def const(self, var, suffix: str = ""):
        key = (var, suffix)
        c = self.consts.get(key)
        if c is None:
            if isinstance(var, TVar):
                name = f"t{var.id}_{var.table}.{suffix}"
            else:
                name = f"s{var.id}_{var.kind}"
            sort = _SORTS["rest"] if suffix == "$rest" else self.V
            c = z3.Const(f"{name}#{len(self.consts)}", sort)
            self.consts[key] = c
            if isinstance(var, SVar) and var.kind == "param":
                pass
        return c

    # ---- tuples --------------------------------------------------------------

    def columns(self, table: str) -> list:
        return self.catalog.info(table).all_columns()

    def register(self, tup) -> None:
        bucket = self.tuples.setdefault(tup.table, {})
        if tup not in bucket:
            bucket[tup] = None

    def col(self, tup, name: str):
        self.register(tup)
        if isinstance(tup, TVar):
            return self.const(tup, name)
        if isinstance(tup, Iota):
            if name in tup.key:
                return self.val(tup.values[tup.key.index(name)])
            f = self.fn(f"iota!{tup.table}!{','.join(tup.key)}!{name}", [self.V] * len(tup.key), self.V)
            return f(*[self.canon_val(v) for v in tup.values])
        raise Unsupported(f"tuple {tup!r}")

    def rest(self, tup):
        if isinstance(tup, TVar):
            return self.const(tup, "$rest")
        f = self.fn(f"iota!{tup.table}!{','.join(tup.key)}!$rest", [self.V] * len(tup.key), _SORTS["rest"])
        return f(*[self.canon_val(v) for v in tup.values])

    def row(self, tup) -> list:
        cols = [self.col(tup, c) for c in self.columns(tup.table)]
        if not self.catalog.info(tup.table).declared:
            cols.append(self.rest(tup))
        return cols

    def in_rel(self, tup):
        self.register(tup)
        info = self.catalog.info(tup.table)
        domain = [self.V] * len(info.all_columns()) + ([_SORTS["rest"]] if not info.declared else [])
        f = self.fn(f"in!{tup.table}", domain, z3.BoolSort())
        return f(*self.row(tup))

    def canon_val(self, v):
        return self.val(v)

    # ---- values ---------------------------------------------------------------

    def val(self, v):
        V = self.V
        if isinstance(v, Col):
            return self.col(v.tup, v.name)
        if isinstance(v, Ref):
            return self.const(v.var)
        if isinstance(v, Lit):
            if v.value is None:
                return V.Null
            if isinstance(v.value, bool):
                return V.Bool(z3.BoolVal(v.value))
            if isinstance(v.value, Fraction):
                return V.Num(z3.RealVal(v.value))
            if isinstance(v.value, str):
                return V.Str(z3.StringVal(v.value))
            raise Unsupported("literal")
        if isinstance(v, Fn):
            args = [self.val(a) for a in v.args]
            f = self.fn(f"fn!{v.name}", [V] * len(args), V)
            result = f(*args) if args else z3.Const(f"fn!{v.name}", V)
            if v.strict and args:
                return z3.If(z3.Or(*[V.is_Null(a) for a in args]), V.Null, result)
            return result
        if isinstance(v, Arith):
            a, b = self.val(v.a), self.val(v.b)
            nulls = z3.Or(V.is_Null(a), V.is_Null(b))
            x, y = V.num(a), V.num(b)
            if v.op == "+":
                r = V.Num(x + y)
            elif v.op == "-":
                r = V.Num(x - y)
            elif v.op == "*":
                r = V.Num(x * y)
            elif v.op == "/":
                div = self.fn("arith!div0", [V], V)
                if self.catalog.dialect == "mysql":
                    zero = V.Null
                else:
                    zero = div(a)
                return z3.If(nulls, V.Null, z3.If(y == 0, zero, V.Num(x / y)))
            else:
                raise Unsupported(f"operator {v.op}")
            return z3.If(nulls, V.Null, r)
        if isinstance(v, Ite):
            return z3.If(self.form(v.cond), self.val(v.a), self.val(v.b))
        if isinstance(v, BoolV):
            return z3.If(self.form(v.t), V.Bool(z3.BoolVal(True)), z3.If(self.form(v.f), V.Bool(z3.BoolVal(False)), V.Null))
        if isinstance(v, Agg):
            return self.agg(v)
        if isinstance(v, Scalar):
            return self.scalar(v)
        raise Unsupported(f"value {type(v).__name__}")

    def num(self, v):
        """Payload of a value known to be numeric."""

        return self.V.num(self.val(v))

    # ---- formulas -------------------------------------------------------------

    def form(self, f):
        V = self.V
        if isinstance(f, FConst):
            return z3.BoolVal(f.value)
        if isinstance(f, And):
            return z3.And(*[self.form(a) for a in f.args])
        if isinstance(f, Or):
            return z3.Or(*[self.form(a) for a in f.args])
        if isinstance(f, Not):
            return z3.Not(self.form(f.a))
        if isinstance(f, IsNull):
            return V.is_Null(self.val(f.a))
        if isinstance(f, Same):
            return self.val(f.a) == self.val(f.b)
        if isinstance(f, Truth):
            if isinstance(f.a, BoolV):
                return self.form(f.a.t)
            return self.val(f.a) == V.Bool(z3.BoolVal(True))
        if isinstance(f, Cmp):
            return self.cmp(f)
        if isinstance(f, InRel):
            return self.in_rel(f.tup)
        if isinstance(f, Exists):
            return self.exists(f)
        if isinstance(f, Gt):
            return self.numsum(f.a) > self.numsum(f.b)
        raise Unsupported(f"formula {type(f).__name__}")

    def cmp(self, f: Cmp):
        V = self.V
        a, b = self.val(f.a), self.val(f.b)
        nn = z3.And(z3.Not(V.is_Null(a)), z3.Not(V.is_Null(b)))
        if f.op == "=":
            return z3.And(nn, a == b)
        if f.op == "<>":
            return z3.And(nn, a != b)
        ka, kb = value_kind(f.a), value_kind(f.b)
        kinds = {k for k in (ka, kb) if k is not None}
        num_lt = self._order(f.op, V.num(a), V.num(b))
        str_lt = self._order(f.op, _SORTS["rank"](V.str(a)), _SORTS["rank"](V.str(b)))
        num_case = z3.And(V.is_Num(a), V.is_Num(b), num_lt)
        str_case = z3.And(V.is_Str(a), V.is_Str(b), str_lt)
        if kinds and kinds <= {"int", "num", "date", "time"}:
            return z3.And(nn, num_case)
        if kinds == {"str"}:
            if f.op in ("<=", ">="):
                strict = self._order(f.op[0], _SORTS["rank"](V.str(a)), _SORTS["rank"](V.str(b)))
                return z3.And(nn, V.is_Str(a), V.is_Str(b), z3.Or(strict, a == b))
            return z3.And(nn, str_case)
        bool_lt = self._order(f.op, z3.If(V.bool(a), 1, 0), z3.If(V.bool(b), 1, 0))
        bool_case = z3.And(V.is_Bool(a), V.is_Bool(b), bool_lt)
        if f.op in ("<=", ">="):
            strict_str = self._order(f.op[0], _SORTS["rank"](V.str(a)), _SORTS["rank"](V.str(b)))
            str_case = z3.And(V.is_Str(a), V.is_Str(b), z3.Or(strict_str, a == b))
        return z3.And(nn, z3.Or(num_case, str_case, bool_case))

    @staticmethod
    def _order(op, x, y):
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]

    # ---- numeric terms --------------------------------------------------------

    def term(self, t: Term):
        """``coef · Π factors`` of a group term, with its relation factors factored out."""

        parts = []
        bound = set(t.vars)
        for f in t.factors:
            if isinstance(f, NRel):
                if f.tup not in bound:
                    raise Unsupported("a multiplicity of a row that is not summed over")
                continue
            parts.append(self.factor(f))
        product = z3.RealVal(t.coef)
        for p in parts:
            product = product * p
        return product

    def factor(self, f):
        if isinstance(f, NInd):
            return z3.If(self.form(f.f), z3.RealVal(1), z3.RealVal(0))
        if isinstance(f, NVal):
            x = self.val(f.v)
            return z3.If(self.V.is_Num(x), self.V.num(x), z3.RealVal(0))
        if isinstance(f, NMin):
            a, b = self.numsum(f.a), self.numsum(f.b)
            return z3.If(a < b, a, b)
        if isinstance(f, NMonus):
            a, b = self.numsum(f.a), self.numsum(f.b)
            return z3.If(a > b, a - b, z3.RealVal(0))
        if isinstance(f, NConst):
            return z3.RealVal(f.value)
        raise Unsupported(f"factor {type(f).__name__}")

    def numsum(self, n):
        """A numeric term with summations, as a real number (sums over rows become atoms)."""

        terms = normalize(n, self.ctx)
        total = []
        groups: dict = {}
        for t in terms:
            if not t.vars:
                total.append(self._closed_term(t))
            else:
                outer, rest = _split_outer(t)
                ct = canon_term(rest)
                groups.setdefault((signature(ct), tuple(sorted(map(repr, outer)))), ([], outer))[0].append(rest)
        for sig, (members, outer) in sorted(groups.items(), key=lambda kv: kv[0]):
            atom = self.sum_atom(members)
            for f in outer:
                atom = atom * self.factor(f)
            total.append(atom)
        return z3.Sum(*total) if len(total) > 1 else (total[0] if total else z3.RealVal(0))

    def _closed_term(self, t: Term):
        product = z3.RealVal(t.coef)
        for f in t.factors:
            if isinstance(f, NRel):
                raise Unsupported("a multiplicity of a free row")
            product = product * self.factor(f)
        return product

    def sum_atom(self, members: list):
        """One atom for a group of terms with the same tables: ``Σ`` over its rows."""

        body = rebuild(members)
        key, lifted, args = lift(body)
        cid = self.prover.class_of("ns", key)
        self.prover.lambdas.setdefault(cid, ("ns", lifted))
        f = self.fn(f"ns!{cid}", [self.V] * len(args), z3.RealSort())
        term = f(*[self.val(a) for a in args])
        if self._new_atom("ns", cid, lifted, args, term):
            nonneg = all(t.coef > 0 and not any(isinstance(x, (NVal, NMonus)) for x in t.factors) for t in members)
            support = []
            for t in members:
                conds = [x for x in t.factors if not isinstance(x, NVal)]
                support.append(Exists(nsum(t.vars, nmul(*conds))))
            exists = self.form(simplify_formula(_disj(support), self.ctx))
            if nonneg:
                self.assume(term >= 0)
                self.assume(exists == (term > 0))
                if all(t.coef.denominator == 1 for t in members):
                    self.assume(z3.IsInt(term))
            else:
                self.assume(z3.Implies(z3.Not(exists), term == 0))
        return term

    # ---- atoms ----------------------------------------------------------------

    def _new_atom(self, kind, cid, lifted, args, term) -> bool:
        k = (kind, cid, tuple(map(repr, args)))
        if k in self.atom_index:
            return False
        self.instances += 1
        if self.instances > MAX_INSTANCES:
            raise Unsupported("too many atom instances")
        self.atom_index[k] = term
        self.atoms.append((kind, cid, lifted, args, term))
        return True

    def exists(self, f: Exists):
        if not isinstance(f.term, NSum):
            g = simplify_formula(f, self.ctx)
            if g == f:
                raise Unsupported("existence of an unnormalized term")
            return self.form(g)
        key, lifted, args = lift(f)
        cid = self.prover.class_of("ex", key)
        self.prover.lambdas.setdefault(cid, ("ex", lifted))
        fn = self.fn(f"ex!{cid}", [self.V] * len(args), z3.BoolSort())
        term = fn(*[self.val(a) for a in args])
        if self._new_atom("ex", cid, lifted, args, term):
            self.pending_ex.append((cid, lifted, args, term))
        return term

    def agg(self, v: Agg):
        V = self.V
        v = simplify_value(v, self.ctx) if not _canonical_agg(v) else v
        if not isinstance(v, Agg):
            return self.val(v)
        w = v.vars[0] if v.vars else None
        if v.arg is None:
            count = self.numsum(v.body)
            if v.func == "COUNT" and not v.distinct:
                return V.Num(count)
            raise Unsupported(f"{v.func}(*)")
        nn_bag = nsum((w,), nmul(v.body, NInd(neg(IsNull(Ref(w))))))
        if v.func == "COUNT" and not v.distinct:
            return V.Num(self.numsum(nn_bag))
        if v.func == "COUNTIF" and not v.distinct:
            return V.Num(self.numsum(nsum((w,), nmul(v.body, NInd(Truth(Ref(w)))))))
        nonempty = self.form(simplify_formula(Exists(nn_bag), self.ctx))
        if self.ctx.exact and not v.distinct and v.func in ("SUM", "AVG"):
            s = self.numsum(nsum((w,), nmul(v.body, NVal(Ref(w)))))
            if v.func == "SUM":
                return z3.If(nonempty, V.Num(s), V.Null)
            c = self.numsum(nn_bag)
            return z3.If(nonempty, V.Num(s / c), V.Null)
        key, lifted, args = lift(v)
        cid = self.prover.class_of("agg", key)
        self.prover.lambdas.setdefault(cid, ("agg", lifted))
        if v.func == "COUNT":
            fn = self.fn(f"agg!{cid}", [V] * len(args), z3.RealSort())
            term = fn(*[self.val(a) for a in args])
            if self._new_atom("agg", cid, lifted, args, term):
                self.assume(term >= 0)
                self.assume(z3.IsInt(term))
                self.assume((term > 0) == nonempty)
            return V.Num(term)
        fn = self.fn(f"agg!{cid}", [V] * len(args), V)
        term = fn(*[self.val(a) for a in args])
        if self._new_atom("agg", cid, lifted, args, term):
            self.assume(z3.Not(V.is_Null(term)))
            kind = value_kind(v.arg)
            if v.func in ("SUM", "AVG"):
                self.assume(V.is_Num(term))
        return z3.If(nonempty, term, V.Null)

    def scalar(self, v: Scalar):
        V = self.V
        v = simplify_value(v, self.ctx)
        if not isinstance(v, Scalar):
            return self.val(v)
        terms = normalize(v.body, self.ctx)
        if len(terms) == 1 and not terms[0].vars and terms[0].coef == 1 and all(isinstance(f, NInd) for f in terms[0].factors):
            conds = [f.f for f in terms[0].factors]
            for c in conds:
                target = None
                if isinstance(c, Same) and c.a == Ref(v.out) and v.out not in free_vars(c.b):
                    target = c.b
                elif isinstance(c, Same) and c.b == Ref(v.out) and v.out not in free_vars(c.a):
                    target = c.a
                elif isinstance(c, Cmp) and c.op == "=" and c.a == Ref(v.out) and v.out not in free_vars(c.b):
                    target = c.b
                elif isinstance(c, Cmp) and c.op == "=" and c.b == Ref(v.out) and v.out not in free_vars(c.a):
                    target = c.a
                if target is not None:
                    rest = conj(*[d for d in conds if d is not c])
                    if v.out not in free_vars(rest):
                        return z3.If(self.form(rest), self.val(target), V.Null)
        key, lifted, args = lift(v)
        cid = self.prover.class_of("scalar", key)
        self.prover.lambdas.setdefault(cid, ("scalar", lifted))
        fn = self.fn(f"scalar!{cid}", [V] * len(args), V)
        term = fn(*[self.val(a) for a in args])
        self._new_atom("scalar", cid, lifted, args, term)
        return term

    # ---- finishing --------------------------------------------------------------

    def _instantiate(self, lifted: Exists, args, mapping: dict):
        """The condition of an existence lambda at given arguments and rows."""

        params = {SVar(-(i + 1), "param"): a for i, a in enumerate(args)}
        body = lifted.term
        cond_parts = []
        factors = body.body.args if hasattr(body.body, "args") and type(body.body).__name__ == "NMul" else (body.body,)
        for f in factors:
            if isinstance(f, NRel):
                cond_parts.append(InRel(f.tup))
            elif isinstance(f, NInd):
                cond_parts.append(f.f)
            else:
                raise Unsupported("existence over a weighted term")
        cond = conj(*cond_parts)
        cond = subst(cond, {**params, **mapping})
        return cond

    def _skolemize(self, cid, lifted, args, term) -> None:
        mapping = {}
        for v in lifted.term.vars:
            mapping[v] = TVar(fresh_id(), v.table) if isinstance(v, TVar) else Ref(SVar(fresh_id(), None))
        cond = self._instantiate(lifted, args, mapping)
        self.assume(z3.Implies(term, self.form(cond)))

    def _witnesses(self, cid, lifted, args, term) -> None:
        tvars = [v for v in lifted.term.vars if isinstance(v, TVar)]
        if any(isinstance(v, SVar) for v in lifted.term.vars):
            return
        pools = [list(self.tuples.get(v.table, {})) for v in tvars]
        if any(not p for p in pools):
            return
        count = 0
        for combo in itertools.product(*pools):
            count += 1
            if count > MAX_WITNESS_MAPS:
                break
            cond = self._instantiate(lifted, args, dict(zip(tvars, combo)))
            self.assume(z3.Implies(self.form(cond), term))

    def _tuple_axioms(self) -> None:
        V = self.V
        done = set()
        for table, bucket in list(self.tuples.items()):
            info = self.catalog.info(table)
            tups = list(bucket)
            for u in tups:
                if (u, "typing") in done:
                    continue
                done.add((u, "typing"))
                member = self.in_rel(u)
                facts = []
                for c in info.all_columns():
                    x = self.col(u, c)
                    if c in info.not_null:
                        facts.append(z3.Not(V.is_Null(x)))
                    kind = info.kinds.get(c)
                    if kind in ("int", "date", "time"):
                        facts.append(z3.Or(V.is_Null(x), z3.And(V.is_Num(x), z3.IsInt(V.num(x)))))
                    elif kind == "num":
                        facts.append(z3.Or(V.is_Null(x), V.is_Num(x)))
                    elif kind == "str":
                        facts.append(z3.Or(V.is_Null(x), V.is_Str(x)))
                    elif kind == "bool":
                        facts.append(z3.Or(V.is_Null(x), V.is_Bool(x)))
                if facts:
                    self.assume(z3.Implies(member, z3.And(*facts)))
                # foreign keys: the referenced row exists
                for cols, parent, pcols in info.foreign:
                    pinfo = self.catalog.info(parent)
                    if tuple(sorted(pcols)) not in {tuple(sorted(k)) for k in pinfo.keys}:
                        continue
                    key = next(k for k in pinfo.keys if sorted(k) == sorted(pcols))
                    order = dict(zip(pcols, cols))
                    values = tuple(Col(u, order[k], info.kinds.get(order[k])) for k in key)
                    iota = Iota(parent, tuple(key), values)
                    nn = z3.And(*[z3.Not(V.is_Null(self.col(u, order[k]))) for k in key])
                    self.assume(z3.Implies(z3.And(member, nn), self.in_rel(iota)))
            # keys: rows that agree on a key are the same row
            tups = list(self.tuples.get(table, {}))
            for key in info.keys:
                for i, u in enumerate(tups):
                    for w in tups[i + 1 :]:
                        if (u, w, key) in done:
                            continue
                        done.add((u, w, key))
                        same_key = z3.And(*[z3.And(z3.Not(V.is_Null(self.col(u, k))), self.col(u, k) == self.col(w, k)) for k in key])
                        same_row = z3.And(*[a == b for a, b in zip(self.row(u), self.row(w))])
                        self.assume(z3.Implies(z3.And(self.in_rel(u), self.in_rel(w), same_key), same_row))
                    if isinstance(u, TVar):
                        # the row with u's key is u
                        iota = Iota(table, tuple(key), tuple(Col(u, k, info.kinds.get(k)) for k in key))
                        if iota in self.tuples.get(table, {}):
                            continue

    def _cross_atoms(self) -> None:
        """Atoms of different lambdas proven equal share their values."""

        by_kind: dict = {}
        for kind, cid, lifted, args, term in self.atoms:
            by_kind.setdefault((kind, len(args)), []).append((cid, lifted, args, term))
        for (kind, arity), items in by_kind.items():
            seen = {}
            for cid, lifted, args, term in items:
                seen.setdefault(self.prover.find(cid), (lifted, []))[1].append((args, term))
            classes = list(seen.items())
            for i in range(len(classes)):
                for j in range(i + 1, len(classes)):
                    (ca, (la, ia)), (cb, (lb, ib)) = classes[i], classes[j]
                    if self.prover.find(ca) == self.prover.find(cb):
                        equal = True
                    else:
                        if not _compatible(kind, la, lb):
                            continue
                        equal = self.prover.lambda_equal(kind, la, lb, self.depth + 1)
                        if equal:
                            self.prover.union(ca, cb)
                    if equal:
                        for args_a, ta in ia:
                            for args_b, tb in ib:
                                same_args = z3.And(*[self.val(x) == self.val(y) for x, y in zip(args_a, args_b)]) if args_a else z3.BoolVal(True)
                                self.assume(z3.Implies(same_args, ta == tb))

    def finish(self) -> None:
        rounds = 0
        while self.pending_ex and rounds < 3:
            rounds += 1
            batch, self.pending_ex = self.pending_ex, []
            for item in batch:
                cid, lifted, args, term = item
                if (cid, tuple(map(repr, args))) in self.skolemized:
                    continue
                self.skolemized.add((cid, tuple(map(repr, args))))
                self._skolemize(*item)
            for kind, cid, lifted, args, term in list(self.atoms):
                if kind == "ex":
                    self._witnesses(cid, lifted, args, term)
        self._cross_atoms()
        # witness axioms for atoms found while comparing lambdas or in the last round
        for kind, cid, lifted, args, term in list(self.atoms):
            if kind == "ex":
                self._witnesses(cid, lifted, args, term)
        self._tuple_axioms()

    def prove(self, goal) -> bool:
        self.prover.check_time()
        self.finish()
        solver = z3.Solver()
        remaining = max(1, int((self.prover.deadline - time.monotonic()) * 1000))
        solver.set("timeout", min(self.prover.timeout_ms, remaining))
        for f in self.facts:
            solver.add(f)
        solver.add(z3.Not(goal))
        self.prover.checks += 1
        result = solver.check()
        return result == z3.unsat


def _split_outer(t: Term) -> tuple:
    """``(outer, rest)``: the conditions and values of ``t`` that do not read its summed variables.

    ``Σx. c·f(x) = c·Σx. f(x)`` when ``c`` does not mention ``x``, so ``val(e)·Σx.f(x)`` is one product of
    an outside value and the same atom the sum without ``val(e)`` gives.
    """

    bound = set(t.vars)
    outer, kept = [], []
    for f in t.factors:
        if isinstance(f, (NInd, NVal)) and not (free_vars(f) & bound):
            outer.append(f)
        else:
            kept.append(f)
    if not outer:
        return (), t
    return tuple(outer), Term(t.vars, t.coef, tuple(kept))


def _canonical_agg(v: Agg) -> bool:
    return (v.arg is None and not v.vars) or (len(v.vars) == 1 and v.arg == Ref(v.vars[0]))


def _disj(parts):
    from .ir import disj

    return disj(*parts)


def _compatible(kind, la, lb) -> bool:
    if kind == "agg":
        return la.func == lb.func and la.distinct == lb.distinct
    if kind == "ns":
        return True
    return True
