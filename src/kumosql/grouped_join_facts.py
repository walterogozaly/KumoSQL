"""Propagate integer key filters from a derived source across inner equalities.

A grouped source filtered on a projected grouping key guarantees that filter
for every output row. An inner equality to a column of the same integer type
therefore guarantees the same filter on that column. Adding it changes no row
or multiplicity, including when the source is empty or contains NULL keys.
"""
from __future__ import annotations

from sqlglot import exp
from .ast_utils import select_sources, visible_ctes
from .having_rules import _conjuncts

_INTEGER = {"INT", "INTEGER", "BIGINT", "INT64", "INT32", "SMALLINT", "INT16", "TINYINT", "INT8"}
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _column_id(column):
    if not isinstance(column, exp.Column) or not column.table or column.args.get("db") or column.args.get("catalog") or isinstance(column.this, exp.Star):
        return None
    return column.table.lower(), column.name.lower()


def _sources(select):
    sources = select_sources(select)
    if any(source.args.get("pivots") for source in sources):
        return None  # renamed aggregate outputs cannot inherit physical column types
    if any(source.args.get("alias") and source.args["alias"].args.get("columns") for source in sources):
        return None  # output positions can rename a FLOAT column to an integer column's name
    names = [source.alias_or_name.lower() for source in sources]
    if not all(names) or len(set(names)) != len(names):
        return None
    return dict(zip(names, sources))


def _type(column, select, types, depth=0):
    key = _column_id(column)
    sources = _sources(select)
    if key is None or sources is None or depth > 12 or key[0] not in sources:
        return None
    source = sources[key[0]]
    if isinstance(source, exp.Table):
        if not source.args.get("db") and not source.args.get("catalog") and source.name.lower() in visible_ctes(source):
            return None
        name = ".".join(part.name for part in source.parts).lower()
        kind = str(types.get(name, {}).get(key[1], "")).upper()
        return kind if kind in _INTEGER else None
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        if source.args.get("alias") and source.args["alias"].args.get("columns"):
            return None
        found = [item.unalias() for item in source.this.expressions if item.alias_or_name.lower() == key[1]]
        if len(found) == 1 and isinstance(found[0], exp.Column):
            return _type(found[0], source.this, types, depth + 1)
    return None


def _facts(source):
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return []
    if source.args.get("alias") and source.args["alias"].args.get("columns"):
        return []
    inner = source.this
    where, group = inner.args.get("where"), inner.args.get("group")
    if where is None or any(inner.args.get(k) for k in ("with", "with_", "qualify", "windows")):
        return []
    if group is not None and (any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals", "all")) or any(isinstance(k, (exp.Rollup, exp.Cube, exp.GroupingSets)) for k in group.expressions)):
        return []
    if group is None and any(a.find_ancestor(exp.Select) is inner for a in inner.find_all(exp.AggFunc)):
        return []  # a global aggregate may emit a row even when its WHERE is false
    names = [item.alias_or_name.lower() for item in inner.expressions]
    if not all(names) or len(set(names)) != len(names):
        return []
    outputs = {item.unalias().sql(): item.alias_or_name for item in inner.expressions if isinstance(item.unalias(), exp.Column)}
    grouped = {key.sql() for key in group.expressions} if group is not None else None
    facts = []
    for predicate in _conjuncts(where.this):
        if not isinstance(predicate, _COMPARISONS):
            continue
        cols = list(predicate.find_all(exp.Column))
        literals = list(predicate.find_all(exp.Literal))
        if len(cols) != 1 or len(literals) != 1 or literals[0].is_string:
            continue
        column = cols[0]
        if column.sql() not in outputs or (grouped is not None and column.sql() not in grouped):
            continue
        if not ((predicate.this is column and predicate.expression is literals[0]) or (predicate.expression is column and predicate.this is literals[0])):
            continue  # no arithmetic, casts, volatile functions or nested scopes
        rewritten = predicate.copy()
        replacement = exp.column(outputs[column.sql()], table=source.alias)
        for col in list(rewritten.find_all(exp.Column)):
            col.replace(replacement.copy())
        facts.append((replacement, rewritten))
    return facts


def propagate_grouped_join_facts(select: exp.Select, types: dict) -> exp.Expression | None:
    sources = _sources(select)
    joins = select.args.get("joins") or []
    if sources is None or not joins or not types or select.args.get("laterals"):
        return None
    if any(join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method") or isinstance(join.this, exp.Lateral) for join in joins):
        return None  # no propagation across NULL padding or semi/anti join boundaries
    where = select.args.get("where")
    predicates = _conjuncts(where.this) if where is not None else []
    predicates += [p for join in joins if join.args.get("on") is not None for p in _conjuncts(join.args["on"])]
    graph = {}
    columns = {}
    for predicate in predicates:
        if not isinstance(predicate, exp.EQ):
            continue
        a, b = _column_id(predicate.this), _column_id(predicate.expression)
        if a is None or b is None or a[0] not in sources or b[0] not in sources:
            continue
        kind = _type(predicate.this, select, types)
        if kind is None or kind != _type(predicate.expression, select, types):
            continue  # equality with coercion need not transfer an ordering predicate
        graph.setdefault(a, set()).add(b); graph.setdefault(b, set()).add(a)
        columns[a], columns[b] = predicate.this, predicate.expression
    facts = [fact for source in sources.values() for fact in _facts(source)]
    existing = {p.sql() for p in predicates} | {p.sql() for _, p in facts}
    added = []
    for column, predicate in facts:
        key = _column_id(column)
        if key not in graph:
            continue
        todo, seen = list(graph[key]), {key}
        while todo:
            target = todo.pop()
            if target in seen:
                continue
            seen.add(target); todo.extend(graph.get(target, ()))
            if target[0] == key[0]:
                continue
            rewritten = predicate.copy()
            for col in list(rewritten.find_all(exp.Column)):
                col.replace(columns[target].copy())
            if rewritten.sql() not in existing:
                added.append(rewritten); existing.add(rewritten.sql())
    if not added:
        return None
    result = select.copy()
    result.set("where", exp.Where(this=exp.and_(*([where.this.copy()] if where is not None else []), *added)))
    return result
