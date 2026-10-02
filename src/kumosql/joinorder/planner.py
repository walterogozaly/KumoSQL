"""Join ordering: choose a join tree that minimises estimated cost.

``optimize`` runs DPccp (Moerkotte and Neumann, VLDB 2006), dynamic programming
over connected sub-graph / complement pairs, so it never considers cross
products. It finds the cheapest bushy tree under a cost model fed by any
cardinality function, which lets the benchmarks swap estimators (or the true
sizes) while keeping the optimizer fixed. Very large graphs fall back to greedy
operator ordering (GOO, Fegaras 1998).

Cost models:

* ``cout``: sum of intermediate result sizes (Cluet and Moerkotte), the usual
  model in join-ordering studies such as Leis et al., "How Good Are Query
  Optimizers, Really?" (VLDB 2015);
* ``hash``: a hash-join model, output plus both inputs plus extra weight on the
  build (right) side, which also decides which input is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import sqlglot
from sqlglot import exp

from .query import JoinQuery

CardFn = Callable[[frozenset[str]], float]


@dataclass
class Plan:
    aliases: frozenset[str]
    card: float
    cost: float
    left: Optional["Plan"] = None
    right: Optional["Plan"] = None

    @property
    def is_leaf(self) -> bool:
        return self.left is None

    def __str__(self) -> str:
        if self.is_leaf:
            return next(iter(self.aliases))
        return f"({self.left} ⋈ {self.right})"

    def joins(self) -> list["Plan"]:
        if self.is_leaf:
            return []
        return self.left.joins() + self.right.joins() + [self]


def _swap(out: float, left: Plan, right: Plan) -> bool:
    """Swap so the right input, which a hash join builds, is the smaller one."""
    return left.card < right.card


def _join_cost(model: str, out: float, left: Plan, right: Plan) -> tuple[float, bool]:
    """Cost of joining two sub-plans; second value says whether to swap sides."""
    if model == "cout":
        return out, _swap(out, left, right)
    # hash join: probe the larger side, build the smaller (right) side.
    small, big = sorted((left.card, right.card))
    return out + big + 2.0 * small, _swap(out, left, right)


def optimize(query: JoinQuery, card: CardFn, model: str = "cout",
             max_pairs: int = 2_000_000) -> Plan:
    # DPccp's enumeration order is only valid with nodes numbered breadth first.
    names: list[str] = []
    for root in sorted(query.tables):
        if root in names:
            continue
        names.append(root)
        k = len(names) - 1
        while k < len(names):
            for nxt in sorted(query.neighbours(names[k])):
                if nxt not in names:
                    names.append(nxt)
            k += 1
    n = len(names)
    index = {a: i for i, a in enumerate(names)}
    nbr = [0] * n
    for e in query.edges:
        i, j = index[e.left], index[e.right]
        if i != j:
            nbr[i] |= 1 << j
            nbr[j] |= 1 << i

    def to_set(mask: int) -> frozenset[str]:
        return frozenset(names[i] for i in range(n) if mask >> i & 1)

    cards: dict[int, float] = {}

    def card_of(mask: int) -> float:
        c = cards.get(mask)
        if c is None:
            c = cards[mask] = max(float(card(to_set(mask))), 0.0)
        return c

    best: dict[int, Plan] = {}
    for i in range(n):
        m = 1 << i
        best[m] = Plan(frozenset([names[i]]), card_of(m), 0.0)
    if n == 1:
        return best[1]
    full = (1 << n) - 1

    def neighbourhood(mask: int) -> int:
        out = 0
        m = mask
        while m:
            low = m & -m
            out |= nbr[low.bit_length() - 1]
            m ^= low
        return out & ~mask

    def subsets(mask: int):
        # non-empty subsets in increasing order, as DPccp requires
        sub = (0 - mask) & mask
        while sub:
            yield sub
            sub = (sub - mask) & mask

    pairs = 0

    def emit(s1: int, s2: int) -> None:
        nonlocal pairs
        pairs += 1
        if pairs > max_pairs:
            raise _TooBig
        p1, p2 = best[s1], best[s2]
        s = s1 | s2
        out = card_of(s)
        step, swap = _join_cost(model, out, p1, p2)
        cost = p1.cost + p2.cost + step
        cur = best.get(s)
        if cur is None or cost < cur.cost:
            left, right = (p2, p1) if swap else (p1, p2)
            best[s] = Plan(left.aliases | right.aliases, out, cost, left, right)

    def csg_rec(s: int, x: int, out: list[int]) -> None:
        nb = neighbourhood(s) & ~x
        if not nb:
            return
        for sub in subsets(nb):
            out.append(s | sub)
        for sub in subsets(nb):
            csg_rec(s | sub, x | nb, out)

    def cmp_of(s1: int) -> list[int]:
        low = (s1 & -s1).bit_length() - 1
        x = ((1 << (low + 1)) - 1) | s1
        nb = neighbourhood(s1) & ~x
        out: list[int] = []
        for i in range(n - 1, -1, -1):
            if nb >> i & 1:
                v = 1 << i
                out.append(v)
                csg_rec(v, x | (nb & ((1 << (i + 1)) - 1)), out)
        return out

    try:
        for i in range(n - 1, -1, -1):
            v = 1 << i
            csgs = [v]
            csg_rec(v, (1 << (i + 1)) - 1, csgs)
            for s1 in csgs:
                for s2 in cmp_of(s1):
                    emit(s1, s2)
    except _TooBig:
        return greedy(query, card, model)
    if full not in best:
        raise ValueError("join graph is not connected")
    return best[full]


class _TooBig(Exception):
    pass


def greedy(query: JoinQuery, card: CardFn, model: str = "cout") -> Plan:
    """Greedy operator ordering: repeatedly join the pair with the smallest result."""
    plans = [Plan(frozenset([a]), max(card(frozenset([a])), 0.0), 0.0) for a in sorted(query.tables)]
    while len(plans) > 1:
        best_pair = None
        for i in range(len(plans)):
            for j in range(i + 1, len(plans)):
                if not query.edges_between(plans[i].aliases, plans[j].aliases):
                    continue
                out = max(card(plans[i].aliases | plans[j].aliases), 0.0)
                step, swap = _join_cost(model, out, plans[i], plans[j])
                if best_pair is None or (out, step) < best_pair[0]:
                    best_pair = ((out, step), i, j, swap)
        if best_pair is None:
            raise ValueError("join graph is not connected")
        (out, step), i, j, swap = best_pair
        a, b = plans[i], plans[j]
        left, right = (b, a) if swap else (a, b)
        merged = Plan(a.aliases | b.aliases, out, a.cost + b.cost + step, left, right)
        plans = [p for k, p in enumerate(plans) if k not in (i, j)] + [merged]
    return plans[0]


def plan_cost(plan: Plan, card: CardFn, model: str = "cout") -> float:
    """Cost of a fixed plan under another cardinality function (e.g. true sizes)."""
    if plan.is_leaf:
        return 0.0
    left = Plan(plan.left.aliases, card(plan.left.aliases), 0.0)
    right = Plan(plan.right.aliases, card(plan.right.aliases), 0.0)
    step, _ = _join_cost(model, card(plan.aliases), left, right)
    return plan_cost(plan.left, card, model) + plan_cost(plan.right, card, model) + step


def to_sql(query: JoinQuery, plan: Plan, dialect: str = "duckdb") -> str:
    """The query with its FROM clause rewritten as the plan's explicit join tree."""
    used: set[int] = set()

    def build(p: Plan) -> exp.Expression:
        if p.is_leaf:
            alias = next(iter(p.aliases))
            return exp.Table(this=exp.to_identifier(query.tables[alias]),
                             alias=exp.TableAlias(this=exp.to_identifier(alias)))
        left, right = build(p.left), build(p.right)
        conds: list[exp.Expression] = []
        for e in query.edges_between(p.left.aliases, p.right.aliases):
            conds.append(exp.EQ(this=exp.column(e.left_col, e.left), expression=exp.column(e.right_col, e.right)))
        for k, (refs, node) in enumerate(query.residual):
            if k not in used and refs <= p.aliases:
                used.add(k)
                conds.append(node.copy())
        on = exp.and_(*conds) if conds else exp.true()
        if not p.left.is_leaf:
            left = exp.Paren(this=left)
        if not p.right.is_leaf:
            right = exp.Paren(this=right)
        return _JoinTree(this=left, expression=right, on=on)

    tree = build(plan)
    where = [f.copy() for fl in query.filters.values() for f in fl]
    select = ", ".join(s.sql(dialect=dialect, identify=True) for s in query.select) or "COUNT(*)"
    sql = f"SELECT {select} FROM {_render(tree, dialect)}"
    if where:
        sql += " WHERE " + exp.and_(*where).sql(dialect=dialect, identify=True)
    for key in ("group", "having", "order", "limit"):
        node = query.tail.get(key)
        if node is not None:
            sql += " " + node.sql(dialect=dialect, identify=True)
    return sql


class _JoinTree(exp.Expression):
    arg_types = {"this": True, "expression": True, "on": True}


def _render(node: exp.Expression, dialect: str) -> str:
    if isinstance(node, exp.Paren):
        return "(" + _render(node.this, dialect) + ")"
    if isinstance(node, _JoinTree):
        return (f"{_render(node.this, dialect)} JOIN {_render(node.expression, dialect)} "
                f"ON {node.args['on'].sql(dialect=dialect, identify=True)}")
    return node.sql(dialect=dialect, identify=True)
