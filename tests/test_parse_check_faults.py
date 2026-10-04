"""Fault injection: a parse check that misses a misread parse is worse than none.

Each probe query is read by sqlglot, then one fault is injected into sqlglot's tree and the tree is compared with
the independent reading as if sqlglot had produced it. Every injected fault must be a disagreement. The faults are
the ways a misreading parser changes a query without changing its tokens:

* ``rotate_left`` / ``rotate_right``: an operator regrouped over its neighbour (``a + (b * c)`` as ``(a + b) * c``);
* ``drop_not``: a ``NOT`` or a unary minus lost; ``invent_not`` / ``invent_neg``: one added;
* ``swap_class``: an operator replaced by another (``+`` for ``-``, ``AND`` for ``OR``, ``<`` for ``>``, ``|`` for ``&``);
* ``flip_desc``, ``flip_distinct``, ``flip_all``: ``DESC``, ``DISTINCT`` and ``ALL`` toggled;
* ``swap_operands``: the operands of a non-commutative operator exchanged.

Same-class AND/OR rotations are not injected: they are associative, so they are the same query.
"""

from __future__ import annotations

import pytest
from sqlglot import exp

from kumosql import parse_check as pc

# What a misreading parser could put in place of an operator.
SWAP = {exp.Add: exp.Sub, exp.Sub: exp.Add, exp.Mul: exp.Div, exp.Div: exp.Mul, exp.And: exp.Or, exp.Or: exp.And,
        exp.EQ: exp.NEQ, exp.NEQ: exp.EQ, exp.LT: exp.GT, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.GTE: exp.LTE,
        exp.BitwiseAnd: exp.BitwiseOr, exp.BitwiseOr: exp.BitwiseXor, exp.BitwiseXor: exp.BitwiseAnd, exp.DPipe: exp.Add, exp.Mod: exp.Mul}
NONCOMM = (exp.Sub, exp.Div, exp.LT, exp.GT, exp.LTE, exp.GTE, exp.Mod)


def nodes(trees):
    for tree in trees:
        yield from tree.walk()


def replace_in(trees, node, new):
    """``trees`` with ``node`` replaced by ``new`` (the root may be the node)."""

    out = []
    for tree in trees:
        if tree is node:
            out.append(new)
        else:
            if any(n is node for n in tree.walk()):
                node.replace(new)
            out.append(tree)
    return out


def sites(kind, trees):
    """The nodes of ``trees`` at which a fault of this ``kind`` can be injected."""

    found = []
    for n in nodes(trees):
        if kind == "rotate_right" and isinstance(n, exp.Binary) and isinstance(n.expression, exp.Binary):
            found.append(n)
        elif kind == "rotate_left" and isinstance(n, exp.Binary) and isinstance(n.this, exp.Binary) and not (type(n) is type(n.this) and isinstance(n, (exp.And, exp.Or))):
            found.append(n)
        elif kind == "drop_not" and isinstance(n, (exp.Not, exp.Neg)):
            found.append(n)
        elif kind == "swap_class" and type(n) in SWAP:
            found.append(n)
        elif kind == "flip_desc" and isinstance(n, exp.Ordered):
            found.append(n)
        elif kind == "flip_distinct" and isinstance(n, exp.Select):
            found.append(n)
        elif kind == "flip_all" and isinstance(n, exp.SetOperation):
            found.append(n)
        elif kind == "invent_not" and isinstance(n, (exp.EQ, exp.NEQ, exp.LT, exp.GT, exp.In, exp.Between, exp.Like, exp.Is, exp.Column)) and not isinstance(n.parent, (exp.Select, exp.Group)) and isinstance(n.parent, (exp.Where, exp.Having, exp.Qualify, exp.And, exp.Or, exp.Not, exp.Join, exp.If, exp.Paren)):
            found.append(n)
        elif kind == "invent_neg" and isinstance(n, (exp.Column, exp.Literal)) and isinstance(n.parent, (exp.Binary, exp.Select)):
            found.append(n)
        elif kind == "swap_operands" and isinstance(n, NONCOMM):
            found.append(n)
    return found


def apply(kind, index, trees):
    """``trees`` with a fault of this ``kind`` injected at its ``index``-th site."""

    node = sites(kind, trees)[index]
    if kind == "rotate_right":
        right = node.expression
        inner = type(node)(this=node.this, expression=right.this)
        return replace_in(trees, node, type(right)(this=inner, expression=right.expression))
    if kind == "rotate_left":
        left = node.this
        inner = type(node)(this=left.expression, expression=node.expression)
        return replace_in(trees, node, type(left)(this=left.this, expression=inner))
    if kind == "drop_not":
        return replace_in(trees, node, node.this)
    if kind == "swap_class":
        return replace_in(trees, node, SWAP[type(node)](this=node.this, expression=node.expression))
    if kind == "flip_desc":
        node.set("desc", not node.args.get("desc"))
        return trees
    if kind == "flip_distinct":
        node.set("distinct", None if node.args.get("distinct") else exp.Distinct())
        return trees
    if kind == "flip_all":
        node.set("distinct", not node.args.get("distinct"))
        return trees
    if kind == "invent_not":
        return replace_in(trees, node, exp.Not(this=node.copy()))
    if kind == "invent_neg":
        return replace_in(trees, node, exp.Neg(this=node.copy()))
    if kind == "swap_operands":
        return replace_in(trees, node, type(node)(this=node.expression, expression=node.this))
    raise KeyError(kind)


KINDS = ["rotate_right", "rotate_left", "drop_not", "swap_class", "flip_desc", "flip_distinct", "flip_all", "swap_operands", "invent_not", "invent_neg"]


PROBES = {
 "bigquery": [
  "SELECT a + b * c - d / e AS x FROM t WHERE a = 1 AND NOT b OR c <> 2 AND d NOT IN (1, 2) ORDER BY a DESC, b",
  "SELECT DISTINCT a, -b FROM t WHERE a BETWEEN 1 AND 5 OR b LIKE 'x%' OR c IS NOT NULL AND d > 1",
  "SELECT a FROM t GROUP BY a HAVING SUM(b) > 1 AND NOT MIN(c) < 2 OR MAX(d) >= 3 QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b DESC) = 1",
  "SELECT a FROM t UNION ALL SELECT b FROM u UNION ALL SELECT c FROM v",
  "SELECT a FROM t UNION DISTINCT SELECT b FROM u UNION DISTINCT SELECT c FROM v",
  "SELECT a FROM t EXCEPT DISTINCT SELECT d FROM w",
  "SELECT a FROM t INTERSECT ALL SELECT d FROM w",
  "SELECT t.a, CASE WHEN a > 1 AND b < 2 THEN a - b ELSE b - 3 END AS c FROM t JOIN u ON t.a = u.a AND t.b <= u.b OR u.c <> t.c",
  "SELECT a | b, a & b, a ^ b, a << 1, a >> 2 FROM t WHERE (a + b) * c > 3",
  "SELECT a FROM t WHERE NOT (a = 1 OR b = 2) AND c = 3 AND (d IS NULL OR e IS NOT NULL)",
 ],
 "mysql": [
  "SELECT a + b * c - d / e AS x FROM t WHERE a = 1 AND NOT b OR c <> 2 XOR d NOT IN (1, 2) ORDER BY a DESC, b",
  "SELECT DISTINCT a, -b FROM t WHERE a BETWEEN 1 AND 5 OR b LIKE 'x%' OR c IS NOT NULL AND d > 1",
  "SELECT a FROM t GROUP BY a HAVING SUM(b) > 1 AND NOT MIN(c) < 2 OR MAX(d) >= 3",
  "SELECT a FROM t UNION ALL SELECT b FROM u UNION SELECT c FROM v",
 ],
 "postgres": [
  "SELECT a + b * c - d / e AS x FROM t WHERE a = 1 AND NOT b OR c <> 2 AND d NOT IN (1, 2) ORDER BY a DESC, b",
  "SELECT DISTINCT a, -b FROM t WHERE a BETWEEN 1 AND 5 OR b LIKE 'x%' OR c IS NOT NULL AND d > 1",
  "SELECT a FROM t UNION ALL SELECT b FROM u UNION SELECT c FROM v EXCEPT SELECT d FROM w",
 ],
}

PROBES["duckdb"] = PROBES["postgres"]



CASES = [(dialect, sql) for dialect, queries in PROBES.items() for sql in queries]


@pytest.mark.parametrize("dialect, sql", CASES, ids=[f"{d}-{i}" for i, (d, _) in enumerate(CASES)])
def test_every_injected_fault_is_caught(dialect, sql):
    assert pc._check(sql, dialect).status == "agree"  # the probe itself is read the same by both
    injected = 0
    for kind in KINDS:
        counter = []
        pc._check(sql, dialect, lambda trees: (counter.append(len(sites(kind, trees))), trees)[1])
        for index in range(counter[0]):
            result = pc._check(sql, dialect, lambda trees: apply(kind, index, trees))
            assert result.status == "disagree", f"{kind} #{index} was not caught in: {sql}"
            injected += 1
    assert injected > 0


def test_the_probes_inject_every_kind_of_fault():
    seen = set()
    for dialect, sql in CASES:
        for kind in KINDS:
            counter = []
            pc._check(sql, dialect, lambda trees: (counter.append(len(sites(kind, trees))), trees)[1])
            if counter and counter[0]:
                seen.add(kind)
    assert seen == set(KINDS)
