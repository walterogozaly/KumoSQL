"""Extra rewrites that bring two spellings of a query closer before the prover runs.

Every rule here keeps the query's result bag the same on every database (NULLs
included); none relies on keys or NOT NULL facts. They are applied by
``canonicalize`` until nothing changes, and ``prove_with_canonical_rules`` only
uses them when the prover cannot decide the original pair:

* ``ORDER BY`` without ``LIMIT`` is dropped (results are compared as bags).
* ``SELECT DISTINCT`` over a ``GROUP BY`` whose keys are all selected is a plain
  grouped select: each group already gives one distinct row.
* ``HAVING COUNT(*) > 0`` (or ``>= 1``) under a ``GROUP BY`` is always true.
* A select that reads only one derived table and only projects or filters its
  columns is merged into it: the outer filter joins the inner ``WHERE`` (plain
  inner select) or ``HAVING`` (grouped inner select, one row per group).
* ``(a, b) IN (SELECT a2, agg FROM t GROUP BY a2)`` in a top-level ``WHERE``
  conjunct is an inner join with that grouped table: the table holds at most one
  row per ``a2``, so each outer row matches at most once.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .ast_utils import conjuncts as _conjuncts, select_sources as _sources

ORDERED = (exp.Limit, exp.Fetch, exp.Offset)


def _unalias(node: exp.Expression) -> exp.Expression:
    return node.this if isinstance(node, exp.Alias) else node


def _output_name(node: exp.Expression) -> str:
    return node.alias_or_name.lower()


def _own(select: exp.Select, kinds) -> bool:
    """Whether ``select`` itself (not a subquery inside it) uses one of ``kinds``."""

    parts = list(select.expressions) + [select.args.get(k) for k in ("having", "qualify", "order")]
    return any(
        node.find_ancestor(exp.Select) is select for part in parts if part is not None for node in part.find_all(*kinds)
    )


def _has_window_or_limit(select: exp.Select) -> bool:
    return bool(select.args.get("limit") or select.args.get("offset") or _own(select, (exp.Window,)))


def _is_aggregate(select: exp.Select) -> bool:
    return _own(select, (exp.AggFunc,))


def _single_source_name(select: exp.Select) -> str | None:
    sources = _sources(select)
    if len(sources) != 1 or select.args.get("joins"):
        return None
    return sources[0].alias_or_name.lower()


def _strip_qualifier(node: exp.Expression, name: str | None) -> str:
    copy = node.copy()
    for column in copy.find_all(exp.Column):
        if name is not None and column.table.lower() == name:
            column.set("table", None)
    return copy.sql().lower()


def drop_order_without_limit(tree: exp.Expression) -> bool:
    changed = False
    for select in list(tree.find_all(exp.Select)):
        if select.args.get("order") is not None and not select.args.get("limit") and not select.args.get("offset"):
            select.set("order", None)
            changed = True
    for union in list(tree.find_all(exp.Union)):
        if union.args.get("order") is not None and not union.args.get("limit") and not union.args.get("offset"):
            union.set("order", None)
            changed = True
    return changed


def drop_distinct_over_group_keys(tree: exp.Expression) -> bool:
    changed = False
    for select in list(tree.find_all(exp.Select)):
        group = select.args.get("group")
        if not select.args.get("distinct") or group is None or not group.expressions or _own(select, (exp.Window,)):
            continue
        if select.args["distinct"].args.get("on"):
            continue
        name = _single_source_name(select)
        projected = {_strip_qualifier(_unalias(e), name) for e in select.expressions}
        if all(_strip_qualifier(g, name) in projected for g in group.expressions):
            select.set("distinct", None)
            changed = True
    return changed


def _always_nonempty_count(node: exp.Expression) -> bool:
    """``COUNT(*) > 0``, ``COUNT(1) >= 1`` and the mirrored forms."""

    def is_count_star(e):
        return isinstance(e, exp.Count) and (
            isinstance(e.this, exp.Star) or (isinstance(e.this, exp.Literal) and not e.this.is_string)
        ) and not e.args.get("distinct") and not isinstance(e.this, exp.Distinct)

    def number(e):
        return float(e.name) if isinstance(e, exp.Literal) and not e.is_string else None

    if isinstance(node, (exp.GT, exp.GTE)) and is_count_star(node.this):
        n = number(node.expression)
        return n is not None and (n < 1 if isinstance(node, exp.GT) else n <= 1)
    if isinstance(node, (exp.LT, exp.LTE)) and is_count_star(node.expression):
        n = number(node.this)
        return n is not None and (n < 1 if isinstance(node, exp.LT) else n <= 1)
    return False


def drop_trivial_having(tree: exp.Expression) -> bool:
    changed = False
    for select in list(tree.find_all(exp.Select)):
        having = select.args.get("having")
        group = select.args.get("group")
        if having is None or group is None or not group.expressions:
            continue
        parts = _conjuncts(having.this)
        kept = [p for p in parts if not _always_nonempty_count(p)]
        if len(kept) != len(parts):
            select.set("having", exp.Having(this=exp.and_(*kept)) if kept else None)
            changed = True
    return changed


def _substitute(node: exp.Expression, alias: str, outputs: dict[str, exp.Expression]) -> exp.Expression | None:
    """``node`` with each column of ``alias`` replaced by the inner expression; ``None`` if one is missing."""

    missing = []

    def replace(n):
        if isinstance(n, exp.Column) and (n.table.lower() == alias or not n.table):
            inner = outputs.get(n.name.lower())
            if inner is None:
                missing.append(n.name)
                return n
            return inner.copy()
        return n

    result = node.copy().transform(replace)
    return None if missing else result


def merge_projection_over_derived(tree: exp.Expression) -> bool:
    """``SELECT f(T.c) FROM (inner) T WHERE p(T.c)`` becomes ``inner`` selecting ``f`` with ``p`` added."""

    for outer in list(tree.find_all(exp.Select)):
        sources = _sources(outer)
        if len(sources) != 1 or outer.args.get("joins") or not isinstance(sources[0], exp.Subquery):
            continue
        inner = sources[0].this
        if not isinstance(inner, exp.Select) or _has_window_or_limit(inner) or _has_window_or_limit(outer):
            continue
        if outer.args.get("group") or outer.args.get("having") or _is_aggregate(outer) or outer.args.get("order"):
            continue
        if inner.args.get("order") or inner.args.get("with"):
            continue
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in inner.expressions):
            continue
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in outer.expressions):
            continue
        # Subqueries in the outer select may refer to its columns by bare name; keep it simple and skip them.
        where = outer.args.get("where")
        if any(e.find(exp.Subquery, exp.Exists) for e in outer.expressions + ([where] if where is not None else [])):
            continue
        inner_distinct = inner.args.get("distinct") is not None
        outer_distinct = outer.args.get("distinct") is not None
        if inner_distinct and not outer_distinct:
            continue
        if any(d.args.get("on") for d in (inner.args.get("distinct"), outer.args.get("distinct")) if d is not None):
            continue
        names = [_output_name(e) for e in inner.expressions]
        if len(set(names)) != len(names):
            continue
        outputs = {n: _unalias(e) for n, e in zip(names, inner.expressions)}
        alias = sources[0].alias_or_name.lower()
        projections = []
        for e in outer.expressions:
            new = _substitute(_unalias(e), alias, outputs)
            if new is None:
                break
            projections.append(exp.alias_(new, _output_name(e)) if e.alias_or_name else new)
        else:
            condition = _substitute(where.this, alias, outputs) if where is not None else None
            if where is not None and condition is None:
                continue
            grouped = inner.args.get("group") is not None or _is_aggregate(inner)
            if condition is not None and grouped and inner.args.get("group") is None:
                continue  # HAVING over a global aggregate: leave it
            merged = inner.copy()
            merged.set("expressions", projections)
            if condition is not None:
                key = "having" if grouped else "where"
                old = merged.args.get(key)
                both = exp.and_(old.this, condition) if old is not None else condition
                merged.set(key, exp.Having(this=both) if grouped else exp.Where(this=both))
            merged.set("distinct", exp.Distinct() if outer_distinct else None)
            if outer is tree:
                return merged  # type: ignore[return-value]
            outer.replace(merged)
            return True
    return False


def in_grouped_to_join(tree: exp.Expression, schema: dict[str, list[str]] | None = None) -> bool:
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None:
            continue
        parts = _conjuncts(where.this)
        for index, part in enumerate(parts):
            if not isinstance(part, exp.In) or not isinstance(part.args.get("query"), exp.Subquery):
                continue
            sub = part.args["query"].this
            if not isinstance(sub, exp.Select) or _has_window_or_limit(sub):
                continue
            group = sub.args.get("group")
            if group is None or not group.expressions or sub.args.get("distinct"):
                continue
            left = part.this.expressions if isinstance(part.this, exp.Tuple) else [part.this]
            if len(left) != len(sub.expressions):
                continue
            name = _single_source_name(sub)
            outputs = [_strip_qualifier(_unalias(e), name) for e in sub.expressions]
            if not all(_strip_qualifier(g, name) in outputs for g in group.expressions):
                continue
            if schema is None or sub.find(exp.Subquery, exp.Exists) or not all(isinstance(s, exp.Table) for s in _sources(sub)):
                continue
            aliases = {s.alias_or_name.lower() for s in _sources(sub)}
            columns = {c.lower() for s in _sources(sub) for c in schema.get(s.name.lower(), [])}
            if any(c.table.lower() not in aliases if c.table else c.name.lower() not in columns for c in sub.find_all(exp.Column)):
                continue  # may be correlated
            taken = {t.alias_or_name.lower() for t in tree.find_all(exp.Subquery, exp.Table)}
            alias = next(f"kq_in{n}" for n in range(len(taken) + 1) if f"kq_in{n}" not in taken)
            derived = sub.copy()
            derived.set("expressions", [exp.alias_(_unalias(e).copy(), f"c{i}") for i, e in enumerate(sub.expressions)])
            condition = exp.and_(*[exp.EQ(this=l.copy(), expression=exp.column(f"c{i}", table=alias)) for i, l in enumerate(left)])
            rest = parts[:index] + parts[index + 1 :]
            select.set("where", exp.Where(this=exp.and_(*rest)) if rest else None)
            select.append("joins", exp.Join(this=exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias))), on=condition, kind="INNER"))
            return True
    return False


RULES = (drop_order_without_limit, drop_distinct_over_group_keys, drop_trivial_having)


def canonicalize(sql: str, dialect: str = "bigquery", schema: dict[str, list[str]] | None = None) -> str:
    """``sql`` with the rules above applied until none changes it."""

    tree = sqlglot.parse_one(sql, read=dialect)
    for _ in range(20):
        changed = False
        for rule in RULES:
            changed = rule(tree) or changed
        changed = in_grouped_to_join(tree, schema) or changed
        merged = merge_projection_over_derived(tree)
        if isinstance(merged, exp.Expression):
            tree, changed = merged, True
        elif merged:
            changed = True
        if not changed:
            break
    return tree.sql(dialect=dialect)
