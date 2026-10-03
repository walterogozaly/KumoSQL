"""Split a duplicate-blind select into a union of simpler selects.

Two identities of set semantics, used where the outer ``SELECT DISTINCT`` makes duplicates invisible:

* A derived table whose join key is a searched ``CASE`` (``CASE WHEN u1 = 1 THEN u2 WHEN u2 = 1
  THEN u1 END AS k``) is the ``UNION ALL`` of one filtered copy per ``WHEN`` arm (each row takes the
  first arm whose condition is TRUE). A row whose key is NULL never passes the join ``d.k = x``, so the
  ``ELSE NULL`` arm is dropped. A later arm also skips rows an earlier arm took (``NOT (c IS TRUE)``),
  except when both conditions force both results to the same value: then the row adds a duplicate of
  a row the earlier arm already has, which the outer DISTINCT cannot see.
* ``SELECT DISTINCT ... FROM (a UNION b) AS d JOIN t ...`` is the ``UNION`` of the select with ``d``
  replaced by each branch in turn: inner joins and filters distribute over the branches, and under
  DISTINCT it does not matter whether the derived union removed duplicates.

The SMT prover compares unions of sets branch by branch (splitting a branch on a disjunction when
needed), so queries written as ``OR`` / ``IN (... UNION ...)`` / ``JOIN (... UNION ...)`` / a ``CASE``
key reach comparable shapes.
"""

from __future__ import annotations

from fractions import Fraction

from sqlglot import exp

MAX_BRANCHES = 8

_EXTRAS = ("group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals")


def split_distinct_select(select: exp.Select) -> exp.Expression | None:
    """The first of the rewrites above that applies to ``select``, or None."""

    split = _split_in_over_union(select)
    if split is not None:
        return split
    if _returns_set_of_union(select):
        return _distribute_union_source(select)
    if not _duplicate_blind(select):
        return None
    if select.args.get("order") and select.parent is None:
        select = select.copy()
        select.set("order", None)  # a top-level ORDER BY without LIMIT does not change the rows
    return _split_case_key(select) or _split_case_comparison(select) or _distribute_union_source(select)


def _duplicate_blind(select: exp.Select) -> bool:
    distinct = select.args.get("distinct")
    if distinct is None or distinct.args.get("on"):
        return False
    extras = [k for k in _EXTRAS if select.args.get(k)]
    if extras and not (extras == ["order"] and select.parent is None):
        return False
    if select.find_ancestor(exp.In, exp.Exists) is not None:
        return False
    for item in select.expressions:
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select)) for n in item.walk()):
            return False
    return _inner_relations(select) is not None


def _inner_relations(select: exp.Select) -> list[exp.Expression] | None:
    """FROM and JOIN relations when every join is inner or cross."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    relations = [from_.this]
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") not in (None, "", "INNER", "CROSS"):
            return None
        if join.args.get("using") or join.args.get("method") or join.args.get("global_"):
            return None
        relations.append(join.this)
    return relations


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _filters(select: exp.Select) -> list[exp.Expression]:
    """Top-level conjuncts of WHERE and of every (inner) ON clause."""

    where = select.args.get("where")
    found = _conjuncts(where.this if where is not None else None)
    for join in select.args.get("joins") or []:
        found += _conjuncts(join.args.get("on"))
    return found


def _plain_body(node: exp.Expression, allow_distinct: bool = False) -> exp.Select | None:
    """A derived table's select that only projects and filters its FROM clause (``allow_distinct``: DISTINCT too)."""

    if not isinstance(node, exp.Select) or any(node.args.get(k) for k in _EXTRAS):
        return None
    distinct = node.args.get("distinct")
    if distinct is not None and (not allow_distinct or distinct.args.get("on")):
        return None
    if _inner_relations(node) is None:
        return None
    if any(isinstance(n, (exp.AggFunc, exp.Window)) for item in node.expressions for n in item.walk()):
        return None
    return node


# --- CASE join keys ------------------------------------------------------------------------


def _split_case_key(select: exp.Select) -> exp.Expression | None:
    for relation in _inner_relations(select) or []:
        if not isinstance(relation, exp.Subquery) or not relation.alias:
            continue
        # Under the outer DISTINCT, the derived table's own DISTINCT changes nothing.
        body = _plain_body(relation.this, allow_distinct=True)
        if body is None:
            continue
        for position, item in enumerate(body.expressions):
            arms = _guarded_arms(_unparen(item.this)) if isinstance(item, exp.Alias) else None
            if arms is None or not _null_rejected(select, relation.alias, item.alias):
                continue
            branches = []
            for condition, result, guards in arms:
                branch = body.copy()
                branch.set("distinct", None)
                branch.expressions[position].set("this", result.copy())
                for test in [condition.copy(), *guards]:
                    branch = branch.where(exp.Paren(this=test) if isinstance(test, exp.Or) else test, copy=False)
                branches.append(branch)
            union: exp.Expression = branches[0]
            for branch in branches[1:]:
                union = exp.Union(this=union, expression=branch, distinct=False)
            copy = select.copy()
            for subquery in copy.find_all(exp.Subquery):
                if subquery.alias == relation.alias and subquery.this.sql() == relation.this.sql():
                    subquery.set("this", union)
                    return copy
    return None


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _guarded_arms(case: exp.Expression) -> list[tuple[exp.Expression, exp.Expression, list[exp.Expression]]] | None:
    """``(condition, result, guards)`` per arm of a searched CASE with no ELSE (or ELSE NULL)."""

    if not isinstance(case, exp.Case) or case.this is not None:
        return None
    default = case.args.get("default")
    if default is not None and not isinstance(default, exp.Null):
        return None
    arms = [(arm.this, arm.args.get("true")) for arm in case.args.get("ifs") or []]
    if not arms or len(arms) > MAX_BRANCHES or any(c is None or r is None for c, r in arms):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Select, exp.AggFunc, exp.Window)) for c, r in arms for n in (*c.walk(), *r.walk())):
        return None
    guarded = []
    for index, (condition, result) in enumerate(arms):
        guards = [
            exp.Not(this=exp.Paren(this=exp.Is(this=exp.Paren(this=earlier.copy()), expression=exp.true())))
            for earlier, earlier_result in arms[:index]
            if not _same_when_both(earlier, earlier_result, condition, result)
        ]
        guarded.append((condition, result, guards))
    return guarded


def _split_case_comparison(select: exp.Select) -> exp.Expression | None:
    """A filter ``CASE WHEN c1 THEN e1 WHEN c2 THEN e2 END = x`` is ``(c1 AND e1 = x) OR (c2 AND e2 = x)``,
    each arm guarded by "no earlier arm taken" unless that arm gives the same value (see above).

    Only TRUE passes a filter: the comparison is TRUE exactly when the arm a row takes has a result
    equal to ``x``; with no ELSE the result is NULL, never equal.
    """

    for test in _filters(select):
        if not isinstance(test, exp.EQ):
            continue
        for side, other in ((test.this, test.expression), (test.expression, test.this)):
            arms = _guarded_arms(side)
            if arms is None or any(isinstance(n, (exp.Case, exp.Subquery, exp.Select)) for n in other.walk()):
                continue
            disjuncts = []
            for condition, result, guards in arms:
                parts = [exp.Paren(this=condition.copy())] + guards + [exp.EQ(this=result.copy(), expression=other.copy())]
                disjunct = parts[0]
                for part in parts[1:]:
                    disjunct = exp.And(this=disjunct, expression=part)
                disjuncts.append(disjunct)
            replacement = disjuncts[0]
            for disjunct in disjuncts[1:]:
                replacement = exp.Or(this=replacement, expression=disjunct)
            copy = select.copy()
            target = next(n for n in copy.find_all(exp.EQ) if n.sql() == test.sql())
            target.replace(exp.Paren(this=replacement))
            return copy
    return None


def _null_rejected(select: exp.Select, alias: str, column: str) -> bool:
    """Some top-level conjunct ``alias.column = x`` drops every row whose column is NULL."""

    for test in _filters(select):
        if isinstance(test, exp.EQ):
            for side in (test.this, test.expression):
                if isinstance(side, exp.Column) and side.table.lower() == alias.lower() and side.name.lower() == column.lower():
                    return True
    return False


def _pins(condition: exp.Expression) -> dict[str, str] | None:
    """``column -> literal`` for the ``column = literal`` conjuncts of a condition."""

    pins: dict[str, str] = {}
    for test in _conjuncts(condition):
        if not isinstance(test, exp.EQ):
            continue
        sides = (test.this, test.expression)
        for column, literal in (sides, sides[::-1]):
            if isinstance(column, exp.Column) and isinstance(literal, exp.Literal):
                key, value = column.sql().lower(), _literal_key(literal)
                if pins.get(key, value) != value:
                    return None  # contradictory: no row takes both arms
                pins[key] = value
    return pins


def _literal_key(literal: exp.Literal) -> str:
    if literal.is_string:
        return "s:" + literal.this
    try:
        return "n:" + str(Fraction(literal.this))
    except (ValueError, ZeroDivisionError):
        return "n:" + literal.this


def _resolve(value: exp.Expression, pins: dict[str, str]) -> str | None:
    if isinstance(value, exp.Literal):
        return _literal_key(value)
    if isinstance(value, exp.Column):
        return pins.get(value.sql().lower(), "c:" + value.sql().lower())
    return None


def _same_when_both(first: exp.Expression, first_result: exp.Expression, second: exp.Expression, second_result: exp.Expression) -> bool:
    """When both conditions are TRUE, the two results are provably the same value.

    Both conditions pin columns to literals (``c = 1``); a result that is a pinned column
    becomes that literal. Equal resolved forms are the same value on every such row.
    """

    one, two = _pins(first), _pins(second)
    if one is None or two is None:
        return False
    pins = dict(one)
    for key, value in two.items():
        if pins.get(key, value) != value:
            return False
        pins[key] = value
    left, right = _resolve(first_result, pins), _resolve(second_result, pins)
    return left is not None and left == right


# --- DISTINCT over a derived union -----------------------------------------------------------


def _union_branches(node: exp.Expression) -> list[exp.Select] | None:
    """Branches of a UNION / UNION ALL tree (no ORDER BY, LIMIT or WITH anywhere)."""

    if isinstance(node, exp.Subquery) and not node.alias and not any(node.args.get(k) for k in ("order", "limit", "offset")):
        return _union_branches(node.this)
    if type(node) is exp.Union:
        if any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "with", "by_name", "side", "kind", "on")):
            return None  # BY NAME (and OUTER / CORRESPONDING) unions pair columns by name, not position
        left, right = _union_branches(node.this), _union_branches(node.expression)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, exp.Select) and not any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "with")):
        return [node]
    return None


def _aligned(branches: list[exp.Select]) -> list[exp.Select] | None:
    """Each branch with its columns named like the first branch's (a union takes the first branch's names)."""

    names = [item.alias_or_name for item in branches[0].expressions]
    if "" in names or len({n.lower() for n in names}) != len(names):
        return None
    aligned = []
    for branch in branches:
        if len(branch.expressions) != len(names) or any(isinstance(i, exp.Star) or isinstance(getattr(i, "this", None), exp.Star) for i in branch.expressions):
            return None
        copy = branch.copy()
        copy.set(
            "expressions",
            [
                item if item.alias_or_name == name else exp.alias_((item.this if isinstance(item, exp.Alias) else item).copy(), name)
                for item, name in zip(copy.expressions, names)
            ],
        )
        aligned.append(copy)
    return aligned


def _distribute_union_source(select: exp.Select) -> exp.Expression | None:
    total = 1
    for relation in _inner_relations(select) or []:
        if isinstance(relation, exp.Subquery) and type(relation.this) is exp.Union:
            total *= len(_union_branches(relation.this) or [1])
    if total > 2 * MAX_BRANCHES:
        return None  # the copies would multiply past what is worth comparing
    for relation in _inner_relations(select) or []:
        if not isinstance(relation, exp.Subquery) or not relation.alias or type(relation.this) is not exp.Union:
            continue
        branches = _union_branches(relation.this)
        if branches is None or len(branches) < 2 or len(branches) > MAX_BRANCHES:
            continue
        branches = _aligned(branches)
        if branches is None:
            continue
        copies = []
        for branch in branches:
            copy = select.copy()
            copy.set("distinct", None)
            for subquery in copy.find_all(exp.Subquery):
                if subquery.alias == relation.alias and subquery.this.sql() == relation.this.sql():
                    subquery.set("this", branch)
                    break
            else:
                return None
            copies.append(copy)
        result: exp.Expression = copies[0]
        for copy in copies[1:]:
            result = exp.Union(this=result, expression=copy, distinct=True)
        return result
    return None


def _returns_set_of_union(select: exp.Select) -> bool:
    """``SELECT a, b FROM (x UNION y) AS d WHERE p`` listing every column of the union once:
    filtering a set and permuting its columns keeps it a set, so this is DISTINCT already."""

    if select.args.get("distinct") or select.args.get("joins") or any(select.args.get(k) for k in _EXTRAS):
        return False
    if select.args.get("where") is None:
        return False  # without a filter it is the union itself, which other rules unwrap
    relations = _inner_relations(select)
    if not relations or len(relations) != 1:
        return False
    source = relations[0]
    if not isinstance(source, exp.Subquery) or not source.alias or type(source.this) is not exp.Union or not source.this.args.get("distinct"):
        return False
    if select.find_ancestor(exp.In, exp.Exists) is not None:
        return False
    branches = _union_branches(source.this)
    if not branches:
        return False
    names = [item.alias_or_name.lower() for item in branches[0].expressions]
    wanted = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
            return False
        if column.table and column.table.lower() != source.alias.lower():
            return False
        wanted.append(column.name.lower())
    return "" not in names and sorted(wanted) == sorted(names) and len(set(names)) == len(names)


# --- IN over a union ----------------------------------------------------------------------


def _in_branches(query: exp.Expression, filtering: bool = False) -> list[exp.Select] | None:
    """The selects whose values make up an IN subquery's set, through a derived union it filters.

    With ``filtering`` (the test is a top-level filter conjunct, where only TRUE counts), a subquery
    that selects a searched CASE with no ELSE is also one select per arm: its NULLs can only turn a
    FALSE into an unknown, and a filter drops both.
    """

    branches = _union_branches(query)
    if branches is None:
        return None
    if len(branches) > 1:
        return branches
    select = branches[0]
    body = _plain_body(select, allow_distinct=True) if filtering else None
    if body is not None and len(body.expressions) == 1:
        item = body.expressions[0]
        arms = _guarded_arms(_unparen(item.this if isinstance(item, exp.Alias) else item))
        if arms is not None:
            copies = []
            for condition, result, guards in arms:
                copy = body.copy()
                copy.set("distinct", None)
                copy.set("expressions", [exp.alias_(result.copy(), item.alias) if item.alias else result.copy()])
                for test in [condition.copy(), *guards]:
                    copy = copy.where(exp.Paren(this=test) if isinstance(test, exp.Or) else test, copy=False)
                copies.append(copy)
            return copies
    relations = _inner_relations(select)
    if (
        relations is None
        or len(relations) != 1
        or select.args.get("joins")
        or (select.args.get("distinct") is not None and select.args["distinct"].args.get("on"))
        or any(select.args.get(k) for k in _EXTRAS)
        or any(isinstance(n, (exp.AggFunc, exp.Window)) for item in select.expressions for n in item.walk())
    ):
        return None
    source = relations[0]
    if not isinstance(source, exp.Subquery) or not source.alias or type(source.this) is not exp.Union:
        return None
    inner = _union_branches(source.this)
    if inner is None or len(inner) < 2:
        return None
    inner = _aligned(inner)
    if inner is None:
        return None
    copies = []
    for branch in inner:
        copy = select.copy()
        copy.set("distinct", None)
        (copy.args.get("from_") or copy.args.get("from")).this.set("this", branch)
        copies.append(copy)
    return copies


def _split_in_over_union(select: exp.Select) -> exp.Expression | None:
    """``x IN (SELECT a FROM p UNION SELECT b FROM q)`` is ``x IN (SELECT a FROM p) OR x IN (SELECT b FROM q)``,
    also when the subquery projects and filters a derived union (``SELECT c FROM (p UNION q) AS d WHERE w``).

    A match in either part is a match in the whole and an unknown stays unknown, so the three-valued
    result agrees (and ``NOT IN`` is its negation). Unlike the pass before normalization, this one
    also sees unions that earlier rewrites expose. ``EXISTS`` over a filtered derived union is likewise
    an ``OR`` of one ``EXISTS`` per branch.
    """

    for node in select.find_all(exp.In):
        if node.find_ancestor(exp.Select) is not select or node.args.get("expressions") or node.args.get("unnest"):
            continue
        query = node.args.get("query")
        if not isinstance(query, exp.Subquery) or any(isinstance(n, (exp.Subquery, exp.Select)) for n in node.this.walk()):
            continue
        branches = _in_branches(query.this, filtering=any(node is test for test in _filters(select)))
        if branches is None or len(branches) < 2 or len(branches) > MAX_BRANCHES:
            continue
        if any(len(b.expressions) != 1 for b in branches):
            continue
        copy = select.copy()
        target = next(n for n in copy.find_all(exp.In) if n.sql() == node.sql())
        tests = [exp.In(this=target.this.copy(), query=exp.Subquery(this=b.copy())) for b in branches]
        result = tests[0]
        for test in tests[1:]:
            result = exp.Or(this=result, expression=test)
        target.replace(exp.Paren(this=result))
        return copy
    # EXISTS over a derived union holds when it holds over some branch.
    for node in select.find_all(exp.Exists):
        if node.find_ancestor(exp.Select) is not select:
            continue
        body = node.this.this if isinstance(node.this, exp.Subquery) else node.this
        if not isinstance(body, exp.Select):
            continue
        branches = _in_branches(body)
        if branches is None or len(branches) < 2 or len(branches) > MAX_BRANCHES:
            continue
        copy = select.copy()
        target = next(n for n in copy.find_all(exp.Exists) if n.sql() == node.sql())
        result = exp.Exists(this=branches[0].copy())
        for branch in branches[1:]:
            result = exp.Or(this=result, expression=exp.Exists(this=branch.copy()))
        target.replace(exp.Paren(this=result))
        return copy
    return None
