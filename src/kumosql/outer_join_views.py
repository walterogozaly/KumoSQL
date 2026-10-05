"""Outer-join view matching: which rows of an outer-join view a query needs, and how to select them.

After Larson and Zhou, "View matching for outer-join views" (VLDB 2005). A select-project-join block
whose FROM chains inner, LEFT, RIGHT and FULL joins returns a *minimum union of terms*: each term is a
set of source tables, and its rows are the combinations of rows of those tables that satisfy the term's
join predicates, NULL-extended on every other table, minus the combinations that extend to a row of a
larger term (the *net* rows). ``a LEFT JOIN b ON p`` has the terms ``{a, b}`` (rows matching ``p``) and
``{a}`` (rows of ``a`` matching nothing); a FULL join adds ``{b}``.

A query term can be read from a view when the view has the same term, the view's predicates for it are
among the query's (the query keeps a subset of those rows: the extra conjuncts are the *residual*), and
the term's net rows are decided the same way in both: every larger term of either block exists in both
with the same extension condition (the predicates it adds). The rows of one term are told apart in the
view by a *presence column* of each table: an output that is never NULL where the table is present
(declared NOT NULL, or rejected by a predicate of every term with the table) and NULL where it is
NULL-extended. The query is then the view filtered by ``OR`` over its terms of (the term's presence
tests AND its residual), which collapses to the plain residual when the terms coincide.

The proposer only proposes: ``model_reuse`` hands every filter to the prover, which must prove the
replacement equal to the query before it is returned.

Leaves are base tables or single-table derived tables that only rename columns and filter
(``(SELECT x AS y FROM t WHERE f) AS d``); a derived filter belongs to its leaf, so on a NULL-supplying
side it behaves like an ON condition and on a preserved side like a WHERE condition. WHERE conjuncts are
applied to each term after the join: one that cannot be TRUE on a NULL-extended table removes the terms
without that table, ``IS NULL`` on a missing table's column is TRUE there, and any other conjunct over a
missing table makes the block unreadable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable, Mapping

from sqlglot import exp

from .null_rejecting_joins import rejected_tables

KINDS = ("inner", "left", "right", "full")


class Unreadable(Exception):
    """The block uses a shape this module does not read."""


@dataclass
class Leaf:
    alias: str
    table: str
    filters: list[exp.Expression] = field(default_factory=list)


@dataclass
class Shape:
    """``leaves[0]`` joined in turn to each later leaf by ``steps[i] = (kind, ON conjuncts)``, then ``where``."""

    leaves: list[Leaf]
    steps: list[tuple[str, list[exp.Expression]]]
    where: list[exp.Expression]

    @property
    def aliases(self) -> list[str]:
        return [leaf.alias for leaf in self.leaves]

    @property
    def is_outer(self) -> bool:
        return any(kind != "inner" for kind, _ in self.steps)

    def renamed(self, mapping: Mapping[str, str]) -> "Shape":
        """The same shape with table aliases renamed (``mapping``: old alias -> new alias)."""

        def rename(node: exp.Expression) -> exp.Expression:
            return node.copy().transform(lambda n: exp.column(n.name, table=mapping[n.table]) if isinstance(n, exp.Column) and n.table in mapping else n)

        return Shape(
            [Leaf(mapping.get(leaf.alias, leaf.alias), leaf.table, [rename(f) for f in leaf.filters]) for leaf in self.leaves],
            [(kind, [rename(c) for c in on]) for kind, on in self.steps],
            [rename(w) for w in self.where],
        )


@dataclass
class Term:
    tables: frozenset
    join: list[exp.Expression]  # leaf filters and ON conjuncts its rows satisfy (decides membership and subsumption)
    where: list[exp.Expression]  # WHERE conjuncts left after NULL-extending the missing tables


def tables_of(node: exp.Expression) -> set[str]:
    return {c.table for c in node.find_all(exp.Column) if c.table}


def _split(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, (exp.Where, exp.Paren)):
        return _split(node.this)
    if isinstance(node, exp.And):
        return _split(node.left) + _split(node.right)
    return [node]


def _kind(join: exp.Join) -> str:
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    if join.args.get("method") or kind in ("SEMI", "ANTI", "NATURAL", "ASOF") or join.args.get("using"):
        raise Unreadable("special join")
    if side in ("LEFT", "RIGHT", "FULL"):
        return side.lower()
    if kind in ("", "INNER", "CROSS", "OUTER"):
        return "inner"
    raise Unreadable(f"{kind} join")


def _leaf(relation: exp.Expression) -> tuple[Leaf, dict[str, str]]:
    """A base table, or a single-table derived table that only renames columns and filters.

    Returns the leaf and, for a derived table, its output names mapped to base column names."""

    if isinstance(relation, exp.Table) and not isinstance(relation.this, exp.Func):
        return Leaf(relation.alias_or_name, relation.name), {}
    if not isinstance(relation, exp.Subquery) or not relation.alias:
        raise Unreadable("derived table or table function")
    inner = relation.this
    if not isinstance(inner, exp.Select) or inner.args.get("joins") or inner.args.get("laterals"):
        raise Unreadable("derived table over a join")
    if any(inner.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "windows", "with", "order")):
        raise Unreadable("derived table that groups, deduplicates or limits")
    source = inner.args.get("from_") or inner.args.get("from")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table) or isinstance(table.this, exp.Func):
        raise Unreadable("derived table over a derived table")
    alias = relation.alias
    renames: dict[str, str] = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Column) or isinstance(value.this, exp.Star):
            raise Unreadable("derived table computes a column")
        name = item.alias_or_name
        if name in renames:
            raise Unreadable("derived table repeats an output name")
        renames[name] = value.name
    inner_alias = table.alias_or_name
    filters = []
    for conjunct in _split(inner.args.get("where")):
        if any(isinstance(n, (exp.Subquery, exp.Exists, exp.AggFunc, exp.Window)) for n in conjunct.walk()):
            raise Unreadable("derived filter with a subquery")
        copy = conjunct.copy().transform(lambda n: exp.column(n.name, table=alias) if isinstance(n, exp.Column) and n.table in (inner_alias, "") else n)
        if tables_of(copy) - {alias}:
            raise Unreadable("derived filter reads another table")
        filters.append(copy)
    return Leaf(alias, table.name, filters), renames


def read_shape(select: exp.Select) -> tuple[Shape, exp.Select]:
    """The join shape of a prepared SELECT, and the SELECT with derived leaves' column names resolved to
    base column names (``d.y`` read as ``d.x`` for ``(SELECT x AS y ..) AS d``)."""

    source = select.args.get("from_") or select.args.get("from")
    if source is None:
        raise Unreadable("no FROM")
    joins = list(select.args.get("joins") or [])
    leaves: list[Leaf] = []
    steps: list[tuple[str, list[exp.Expression]]] = []
    renames: dict[str, dict[str, str]] = {}
    for index, relation in enumerate([source.this] + [j.this for j in joins]):
        leaf, names = _leaf(relation)
        if leaf.alias in renames or any(other.alias == leaf.alias for other in leaves):
            raise Unreadable("an alias is used twice")
        leaves.append(leaf)
        renames[leaf.alias] = names
        if index:
            join = joins[index - 1]
            steps.append((_kind(join), _split(join.args.get("on"))))
    # a later join that NULL-extends an earlier leaf would turn a preserved-side filter into a wrong WHERE;
    # filters stay with their leaf, so that is handled by the terms. Derived columns read as base columns.

    def resolve(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.table in renames and renames[node.table]:
            base = renames[node.table].get(node.name)
            if base is None:
                raise Unreadable(f"unknown column {node.sql()}")
            return exp.column(base, table=node.table)
        return node

    def keep_name(item: exp.Expression) -> exp.Expression:
        """A bare derived column read as its base column keeps the name it had in the output."""

        done = item.transform(resolve)
        if isinstance(item, exp.Column) and isinstance(done, exp.Column) and done.name != item.name:
            return exp.alias_(done, item.name)
        return done

    resolved = select.copy()
    for key in ("expressions", "where", "group", "having", "order"):
        value = resolved.args.get(key)
        if key == "expressions" and isinstance(value, list):
            resolved.set(key, [keep_name(v) for v in value])
        elif isinstance(value, list):
            resolved.set(key, [v.transform(resolve) for v in value])
        elif value is not None:
            resolved.set(key, value.transform(resolve))
    steps = [(kind, [c.transform(resolve) for c in on]) for kind, on in steps]
    where = _split(resolved.args.get("where"))
    for _, on in steps:
        for conjunct in on:
            if any(isinstance(n, (exp.Subquery, exp.Exists, exp.AggFunc, exp.Window)) for n in conjunct.walk()):
                raise Unreadable("ON condition with a subquery")
    return Shape(leaves, steps, where), resolved


# ---------------------------------------------------------------------------------------------------
# terms


def _null_out(node: exp.Expression, missing: set[str]) -> exp.Expression:
    """``node`` with every column of a NULL-extended table replaced by NULL, then folded where the
    answer no longer depends on them: ``NULL IS NULL`` is TRUE, comparisons and arithmetic with NULL
    are NULL, ``COALESCE`` drops NULL arguments, AND/OR/NOT follow three-valued logic."""

    def fold(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Column) and n.table in missing:
            return exp.Null()
        return n

    def simplify(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Paren) and isinstance(n.this, (exp.Null, exp.Boolean)):
            return n.this
        if isinstance(n, exp.Is) and isinstance(n.expression, exp.Null):
            if isinstance(n.this, exp.Null):
                return exp.true()
            return n
        if isinstance(n, exp.Not):
            inner = n.this.this if isinstance(n.this, exp.Paren) else n.this
            if isinstance(inner, exp.Boolean):
                return exp.Boolean(this=not inner.this)
            if isinstance(inner, exp.Null):
                return exp.Null()
            return n
        if isinstance(n, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Like)):
            if isinstance(n.this, exp.Null) or isinstance(n.expression, exp.Null):
                return exp.Null()
            return n
        if isinstance(n, exp.Coalesce):
            args = [a for a in [n.this, *n.expressions] if not isinstance(a, exp.Null)]
            if not args:
                return exp.Null()
            if len(args) == 1:
                return args[0]
            return exp.Coalesce(this=args[0], expressions=args[1:])
        if isinstance(n, exp.And):
            a, b = n.this, n.expression
            if _is_false(a) or _is_false(b):
                return exp.false()
            if _is_true(a):
                return b
            if _is_true(b):
                return a
            return n
        if isinstance(n, exp.Or):
            a, b = n.this, n.expression
            if _is_true(a) or _is_true(b):
                return exp.true()
            if _is_false(a):
                return b
            if _is_false(b):
                return a
            if isinstance(a, exp.Null) and isinstance(b, exp.Null):
                return exp.Null()
            return n
        return n

    def bottom_up(n: exp.Expression) -> exp.Expression:
        for key, value in list(n.args.items()):
            if isinstance(value, exp.Expression):
                n.set(key, bottom_up(value))
            elif isinstance(value, list):
                n.set(key, [bottom_up(v) if isinstance(v, exp.Expression) else v for v in value])
        return simplify(n)

    return bottom_up(node.copy().transform(fold))


def _is_true(n: exp.Expression) -> bool:
    return isinstance(n, exp.Boolean) and n.this is True


def _is_false(n: exp.Expression) -> bool:
    return isinstance(n, exp.Boolean) and n.this is False


def _restrict(conjuncts: list[exp.Expression], present: frozenset) -> list[exp.Expression] | None:
    """The conjuncts as they act on rows where only ``present`` tables are not NULL-extended; None when
    one of them can never be TRUE there (the term has no rows)."""

    kept = []
    for conjunct in conjuncts:
        missing = tables_of(conjunct) - present
        if not missing:
            kept.append(conjunct)
            continue
        if rejected_tables(conjunct) & missing:
            return None
        folded = _null_out(conjunct, missing)
        if _is_true(folded):
            continue
        if _is_false(folded) or isinstance(folded, exp.Null):
            return None
        if tables_of(folded) & missing:
            raise Unreadable(f"a condition on a NULL-extended table: {conjunct.sql()}")
        kept.append(folded)
    return kept


def terms(shape: Shape) -> dict[frozenset, Term]:
    """The terms of the block's join (before WHERE: ``where`` empty) and of its result (WHERE applied)."""

    first = shape.leaves[0]
    current: dict[frozenset, list[exp.Expression]] = {frozenset([first.alias]): list(first.filters)}
    seen = {first.alias}
    for leaf, (kind, on) in zip(shape.leaves[1:], shape.steps):
        seen.add(leaf.alias)
        for conjunct in on:
            if not (tables_of(conjunct) <= seen):
                raise Unreadable("an ON condition reads a table joined later")
        alone = {frozenset([leaf.alias]): list(leaf.filters)}
        inner: dict[frozenset, list[exp.Expression]] = {}
        for present, predicates in current.items():
            together = present | {leaf.alias}
            restricted = _restrict(on, together)
            if restricted is None:
                continue
            inner[together] = predicates + list(leaf.filters) + restricted
        if kind == "inner":
            current = inner
        elif kind == "left":
            current = {**inner, **current}
        elif kind == "right":
            current = {**inner, **alone}
        else:
            current = {**inner, **current, **alone}
    found: dict[frozenset, Term] = {}
    for present, predicates in current.items():
        where = _restrict(shape.where, present)
        found[present] = Term(present, predicates, where if where is not None else [])
        if where is None:
            found[present].where = None  # type: ignore[assignment]
    return found


def result_terms(found: Mapping[frozenset, Term]) -> dict[frozenset, Term]:
    """Terms that can return rows once WHERE is applied."""

    return {s: t for s, t in found.items() if t.where is not None}


# ---------------------------------------------------------------------------------------------------
# matching


@dataclass
class Compensation:
    """What to apply to the view: ``predicate`` (over base columns, in the query's aliases) selects exactly
    the query's rows; ``common`` lists conjuncts every selected view row satisfies (for reading one
    column as another); ``terms`` are the selected terms."""

    predicate: list[exp.Expression]
    common: list[exp.Expression]
    terms: list[frozenset]


def _keys(conjuncts, key: Callable[[exp.Expression], str]) -> dict[str, exp.Expression]:
    out: dict[str, exp.Expression] = {}
    for c in conjuncts:
        out.setdefault(key(c), c)
    return out


def presence_column(alias: str, view_terms: Mapping[frozenset, Term], outputs: set[str], not_null: Mapping[str, set[str]], table: str, key) -> str | None:
    """A column of ``alias`` that the view outputs and that is never NULL in a view row where ``alias`` is present."""

    declared = sorted(c for c in not_null.get(table, set()) if c in outputs)
    if declared:
        return declared[0]
    candidates = sorted(outputs)
    with_alias = [t for s, t in view_terms.items() if alias in s]
    for column in candidates:
        if all(any(_rejects_column(c, alias, column) for c in t.join + (t.where or [])) for t in with_alias):
            return column
    return None


def _rejects_column(conjunct: exp.Expression, alias: str, column: str) -> bool:
    """Whether ``conjunct`` cannot be TRUE when ``alias.column`` is NULL."""

    probe = conjunct.copy().transform(lambda n: exp.column("__kumosql_probe__", table="__kumosql_probe__") if isinstance(n, exp.Column) and n.table == alias and n.name == column else n)
    return "__kumosql_probe__" in rejected_tables(probe)


def compensate(
    query: Shape,
    view: Shape,
    *,
    key: Callable[[exp.Expression], str],
    view_outputs: Mapping[str, set[str]],
    not_null: Mapping[str, set[str]],
) -> Compensation | None:
    """How to read the query's rows from the view, both in the query's aliases (the view already renamed),
    or None when a query term cannot be read."""

    if sorted(query.aliases) != sorted(view.aliases):
        return None
    tables = {leaf.alias: leaf.table for leaf in view.leaves}
    q_all, v_all = terms(query), terms(view)
    q_terms, v_terms = result_terms(q_all), result_terms(v_all)
    if not q_terms:
        return None
    residuals: dict[frozenset, list[exp.Expression]] = {}
    for present, q_term in q_terms.items():
        v_term = v_terms.get(present)
        if v_term is None:
            return None
        have = _keys(q_term.join + q_term.where, key)
        need = _keys(v_term.join + v_term.where, key)
        if not set(need) <= set(have):
            return None
        # the term's net rows: every larger term exists in both, with the same extension condition
        q_join, v_join = set(_keys(q_all[present].join, key)), set(_keys(v_all[present].join, key))
        for larger in set(q_all) | set(v_all):
            if not (present < larger):
                continue
            if larger not in q_all or larger not in v_all:
                return None
            if set(_keys(q_all[larger].join, key)) - q_join != set(_keys(v_all[larger].join, key)) - v_join:
                return None
        residuals[present] = [c for k, c in have.items() if k not in need]
    # select the query's terms among the view's
    wanted = set(q_terms)
    varying = sorted({a for s in v_terms for a in view.aliases if any(a not in other for other in v_terms)})
    same_residual = len({tuple(sorted(key(c) for c in r)) for r in residuals.values()}) == 1
    predicate: list[exp.Expression] = []
    if wanted == set(v_terms) and same_residual:
        predicate = list(next(iter(residuals.values())))
    else:
        presence = {}
        for alias in varying:
            column = presence_column(alias, v_terms, view_outputs.get(alias, set()), not_null, tables[alias], key)
            if column is not None:
                presence[alias] = exp.column(column, table=alias)
        if same_residual and _residual_implies(next(iter(residuals.values())), [set(view.aliases) - set(u) for u in v_terms if u not in wanted]):
            test = exp.true()  # the residual alone is not TRUE on the other terms' rows
        else:
            test = _selector(wanted, set(v_terms), varying, presence)
        if test is None:
            return None
        if same_residual:
            predicate = list(next(iter(residuals.values()))) + ([test] if test is not None and not _is_true(test) else [])
        else:
            # terms that share a residual are selected together (one presence test can cover several)
            groups: dict[tuple, list[frozenset]] = {}
            for present in sorted(wanted, key=sorted):
                groups.setdefault(tuple(sorted(key(c) for c in residuals[present])), []).append(present)
            disjuncts = []
            for members in groups.values():
                own = _selector(set(members), set(v_terms), varying, presence)
                if own is None:
                    return None
                parts = ([own] if not _is_true(own) else []) + residuals[members[0]]
                disjuncts.append(_and(parts) if parts else exp.true())
            predicate = [_or(disjuncts)]
    common_keys = set.intersection(*(set(_keys(v_all[s].join + (v_all[s].where or []), key)) for s in wanted))
    common = [c for k, c in _keys([c for s in wanted for c in v_all[s].join + (v_all[s].where or [])], key).items() if k in common_keys]
    return Compensation(predicate, common, sorted(wanted, key=sorted))


def _residual_implies(residual: list[exp.Expression], missing_per_term: list[set]) -> bool:
    """Whether, on the rows of each unwanted term (``missing`` = its NULL-extended tables), some conjunct
    of the residual cannot be TRUE."""

    return bool(residual) and all(any(rejected_tables(c) & missing for c in residual) for missing in missing_per_term)


def _presence_test(alias: str, present: bool, presence: Mapping[str, exp.Expression]) -> exp.Expression | None:
    column = presence.get(alias)
    if column is None:
        return None
    test = exp.Is(this=column.copy(), expression=exp.Null())
    return exp.Not(this=test) if present else test


def _selector(wanted: set, available: set, varying: list[str], presence: Mapping[str, exp.Expression]) -> exp.Expression | None:
    """The smallest conjunction of presence tests true on exactly the ``wanted`` terms among ``available``
    (TRUE when they are all of them), or an OR of each wanted term's full test; None when a needed table
    has no presence column."""

    if wanted == available:
        return exp.true()
    usable = [a for a in varying if a in presence]
    for size in range(1, len(usable) + 1):
        for chosen in _subsets(usable, size):
            for signs in product((True, False), repeat=size):
                hit = {s for s in available if all((a in s) == sign for a, sign in zip(chosen, signs))}
                if hit == wanted:
                    return _and([_presence_test(a, sign, presence) for a, sign in zip(chosen, signs)])
    parts = []
    for s in sorted(wanted, key=sorted):
        tests = []
        for a in varying:
            if a not in presence:
                if any((a in other) != (a in s) for other in available if other != s):
                    return None
                continue
            tests.append(_presence_test(a, a in s, presence))
        parts.append(_and(tests) if tests else exp.true())
    return _or(parts)


def _subsets(items: list[str], size: int):
    if size == 0:
        yield []
        return
    for index, item in enumerate(items):
        for rest in _subsets(items[index + 1 :], size - 1):
            yield [item, *rest]


def _and(parts: list[exp.Expression]) -> exp.Expression:
    result = None
    for part in parts:
        part = exp.Paren(this=part.copy()) if isinstance(part, exp.Or) else part.copy()
        result = part if result is None else exp.And(this=result, expression=part)
    return result if result is not None else exp.true()


def _or(parts: list[exp.Expression]) -> exp.Expression:
    result = None
    for part in parts:
        part = exp.Paren(this=part.copy()) if isinstance(part, exp.And) else part.copy()
        result = part if result is None else exp.Or(this=result, expression=part)
    return exp.Paren(this=result) if result is not None else exp.false()
