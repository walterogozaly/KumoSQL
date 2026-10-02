"""Rewrites that let correlated subqueries reach the shapes the prover already decorrelates.

* **Correlated derived table**: inside a subquery body, ``FROM (SELECT f(a) AS x FROM t WHERE
  t.k = o.k) AS d`` (the derived table reads ``o`` from an enclosing query) is ``FROM t`` with
  ``t.k = o.k`` in the reader's ``WHERE`` and ``d.x`` read as ``f(t.a)``. A filter-and-project
  derived table keeps one row per row of its sources that passes its filter, so the reader sees
  the same rows; moving the filter up is what lets IN, EXISTS and scalar-subquery
  decorrelation see the correlation. Uncorrelated derived tables are left to the prover, which
  flattens them itself.
"""

from __future__ import annotations

import itertools

from sqlglot import exp

_counter = itertools.count()
_CLAUSES = ("distinct", "group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals", "into", "locks", "sample", "prewhere", "connect", "match")


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _and_all(parts: list[exp.Expression]) -> exp.Expression | None:
    out = None
    for part in parts:
        part = exp.Paren(this=part) if isinstance(part, exp.Or) else part
        out = part if out is None else exp.And(this=out, expression=part)
    return out


def _declared(node: exp.Expression) -> set[str]:
    """Every relation alias declared anywhere under ``node``."""

    return {
        (n.alias_or_name or "").lower()
        for n in node.walk()
        if isinstance(n, (exp.Table, exp.Subquery, exp.Lateral, exp.Unnest)) and n.alias_or_name
    }


def _free_qualifiers(node: exp.Expression) -> set[str]:
    declared = _declared(node)
    return {c.table.lower() for c in node.find_all(exp.Column) if c.table and c.table.lower() not in declared}


def _plain_body(inner: exp.Select) -> bool:
    """A filter-and-project select over inner joins of named relations, with no subqueries."""

    if any(inner.args.get(k) for k in _CLAUSES):
        return False
    sources = _sources(inner)
    if not sources or any(not isinstance(s, (exp.Table, exp.Subquery)) or not s.alias_or_name for s in sources):
        return False
    if any(isinstance(s, exp.Table) and (s.args.get("pivots") or s.args.get("joins") or s.args.get("laterals")) for s in sources):
        return False
    if any(isinstance(s, exp.Subquery) and not isinstance(s.this, (exp.Select, exp.SetOperation)) for s in sources):
        return False
    for join in inner.args.get("joins") or []:
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method"):
            return False
    for item in inner.expressions:
        if not isinstance(item, (exp.Alias, exp.Column)) or isinstance(item.unalias(), exp.Star):
            return False
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Exists, exp.Star, exp.Rand, exp.Anonymous)) for n in item.walk()):
            return False
    for cond in [inner.args.get("where")] + [j.args.get("on") for j in inner.args.get("joins") or []]:
        if cond is not None and any(isinstance(n, (exp.Subquery, exp.Select, exp.Exists, exp.AggFunc, exp.Window, exp.Rand, exp.Anonymous)) for n in cond.walk()):
            return False
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if "" in names or len(set(names)) != len(names):
        return False
    aliases = [s.alias_or_name.lower() for s in sources]
    if len(set(aliases)) != len(aliases):
        return False
    own = [c for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is inner]
    # bare columns are ambiguous once the sources move up
    return all(c.table for c in own)


def merge_correlated_derived(select: exp.Select) -> exp.Expression | None:
    """Fold a correlated filter-and-project derived table into the select that reads it."""

    if any(isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in select.find_all(exp.Star)):
        return None
    joins = select.args.get("joins") or []
    if any(j.args.get("using") or j.args.get("method") for j in joins):
        return None
    sides = [(j.args.get("side") or "").upper() for j in joins]
    kinds = [(j.args.get("kind") or "").upper() for j in joins]
    if any(s in ("RIGHT", "FULL") for s in sides) or any(k not in ("", "INNER", "CROSS", "OUTER") for k in kinds):
        return None
    for position, source in enumerate(_sources(select)):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        # The derived table must sit where filtering it first equals filtering the joined rows.
        if position > 0 and (sides[position - 1] or any(sides)):
            continue
        if position == 0 and any(s not in ("", "LEFT") for s in sides):
            continue
        inner = source.this
        if not _plain_body(inner):
            continue
        free = _free_qualifiers(inner)
        # an uncorrelated one is merged only into a correlated reader (a subquery body), where the
        # prover does not flatten it
        if not free and not (isinstance(select.parent, (exp.Subquery, exp.Exists)) and _free_qualifiers(select)):
            continue
        alias = source.alias.lower()
        outside = [n for n in select.walk() if n is not source and not _under(n, source)]
        declared_here = {
            (n.alias_or_name or "").lower()
            for n in outside
            if isinstance(n, (exp.Table, exp.Subquery, exp.Lateral, exp.Unnest)) and n.alias_or_name
        }
        # an alias the derived table reads from outside must not be captured by the reader's own names
        if free & declared_here or alias in declared_here:
            continue
        columns = [n for n in outside if isinstance(n, exp.Column)]
        if any(not c.table and not isinstance(c.this, exp.Star) for c in columns):
            continue
        uses = [c for c in columns if c.table.lower() == alias]
        names = [e.alias_or_name.lower() for e in inner.expressions]
        values = {n: e.unalias() for n, e in zip(names, inner.expressions)}
        if any(c.name.lower() not in values for c in uses):
            continue
        return _merge(select, source, position, uses, values)
    return None


def _under(node: exp.Expression, root: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is root:
            return True
        parent = parent.parent
    return False


def _merge(select: exp.Select, source: exp.Subquery, position: int, uses: list[exp.Column], values: dict[str, exp.Expression]) -> exp.Select:
    inner = source.this
    number = next(_counter)
    mapping = {s.alias_or_name.lower(): f"kumosql_c{number}_{s.alias_or_name.lower()}" for s in _sources(inner)}

    def rename(node: exp.Expression) -> exp.Expression:
        node = node.copy()
        for column in [node] if isinstance(node, exp.Column) else list(node.find_all(exp.Column)):
            if column.table.lower() in mapping:
                column.set("table", exp.to_identifier(mapping[column.table.lower()]))
        return node

    for column in uses:
        value = rename(values[column.name.lower()])
        value = exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Not)) and not isinstance(value, exp.Column) else value
        if column.parent is select and column.arg_key == "expressions":
            value = exp.alias_(value, column.name)
        column.replace(value)
    new_sources = []
    for item in _sources(inner):
        item = item.copy()
        item.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[item.alias_or_name.lower()])))
        new_sources.append(item)
    rest = []
    for item, join in zip(new_sources[1:], inner.args.get("joins") or []):
        join = join.copy()
        join.set("this", item)
        if join.args.get("on") is not None:
            join.set("on", rename(join.args["on"]))
        rest.append(join)
    joins = list(select.args.get("joins") or [])
    parts = []
    if position == 0:
        select.set("from_", exp.From(this=new_sources[0]))
        select.set("joins", rest + joins or None)
    else:
        # every join is inner here, so the old ON (which may read any merged source) moves to WHERE
        parts += _conjuncts(joins[position - 1].args.get("on"))
        select.set("joins", joins[: position - 1] + [exp.Join(this=new_sources[0], kind="CROSS")] + rest + joins[position:] or None)
    where = inner.args.get("where")
    parts += _conjuncts(rename(where.this)) if where is not None else []
    if select.args.get("where") is not None:
        parts += _conjuncts(select.args["where"].this)
    combined = _and_all(parts)
    select.set("where", exp.Where(this=combined) if combined is not None else None)
    return select


def _constant(node: exp.Expression) -> bool:
    return all(isinstance(n, (exp.Literal, exp.Boolean, exp.Null, exp.Paren, exp.Neg, exp.Cast, exp.DataType, exp.DataTypeParam, exp.Var)) for n in node.walk())


def _existence_body(body: exp.Select) -> bool:
    """``SELECT TRUE AS c FROM .. WHERE p GROUP BY TRUE``: one constant row when ``FROM .. WHERE p`` has a row, else none."""

    group = body.args.get("group")
    if group is None or group.args.get("rollup") or group.args.get("cube") or group.args.get("grouping_sets"):
        return False
    if not group.expressions or not all(_constant(e) for e in group.expressions):
        return False
    if any(body.args.get(k) for k in ("distinct", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
        return False
    if not _sources(body):
        return False
    names = [e.alias_or_name.lower() for e in body.expressions]
    if "" in names or len(set(names)) != len(names):
        return False
    return all(_constant(e.unalias()) for e in body.expressions)


def existence_joins(select: exp.Select) -> exp.Expression | None:
    """Read joins that only test for rows: an inner join to a body that returns one constant row
    when its input has a row is a ``WHERE EXISTS``, and a ``LATERAL`` body that reads nothing from
    the query is a plain derived table.
    """

    joins = select.args.get("joins") or []
    if not joins:
        return None
    for index, join in enumerate(joins):
        item = join.this
        if isinstance(item, exp.Lateral) and not item.args.get("view") and isinstance(item.this, exp.Subquery) and item.alias:
            body = item.this.this
            if isinstance(body, exp.Select) and not _free_qualifiers(body):
                derived = exp.Subquery(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(item.alias)))
                join.set("this", derived)
                if join.args.get("on") is None and not join.args.get("side"):
                    join.set("kind", "CROSS")
                return select
        if any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or j.args.get("using") or j.args.get("method") for j in joins):
            return None
        if isinstance(item, exp.Lateral) and not item.args.get("view") and isinstance(item.this, exp.Subquery) and item.alias:
            body, alias = item.this.this, item.alias
        elif isinstance(item, exp.Subquery) and item.alias:
            body, alias = item.this, item.alias
        else:
            continue
        if not isinstance(body, exp.Select) or not _existence_body(body):
            continue
        values = {e.alias_or_name.lower(): e.unalias() for e in body.expressions}
        outside = [n for n in select.walk() if n is not item and not _under(n, item)]
        uses = [n for n in outside if isinstance(n, exp.Column) and n.table.lower() == alias.lower()]
        if any(c.name.lower() not in values for c in uses):
            continue
        if sum(1 for n in outside if isinstance(n, (exp.Table, exp.Subquery, exp.Lateral)) and (n.alias_or_name or "").lower() == alias.lower()) != 0:
            continue
        for column in uses:
            value = values[column.name.lower()].copy()
            column.replace(exp.alias_(value, column.name) if column.parent is select and column.arg_key == "expressions" else value)
        probe = body.copy()
        probe.set("group", None)
        probe.set("expressions", [exp.Literal.number(1)])
        parts = [p for p in _conjuncts(join.args.get("on")) if not (isinstance(p, exp.Boolean) and p.this)]
        parts.append(exp.Exists(this=probe))
        if select.args.get("where") is not None:
            parts = _conjuncts(select.args["where"].this) + parts
        select.set("joins", [j for j in joins if j is not join] or None)
        combined = _and_all(parts)
        select.set("where", exp.Where(this=combined))
        return select
    return None


def decorrelation_step(select: exp.Select) -> exp.Expression | None:
    """The rules above, in order, for ``algebraic_equivalence.normalize``."""

    for rule in (null_comparison_filter, merge_correlated_derived, existence_joins, push_filter_to_lateral, distinct_lateral_to_in):
        rewritten = rule(select)
        if rewritten is not None:
            return rewritten
    if any(isinstance(j.this, exp.Lateral) for j in select.args.get("joins") or []):
        from .algebraic_equivalence import _lateral_joins

        copy = select.copy()
        for join in copy.args.get("joins") or []:
            # CROSS JOIN LATERAL is INNER JOIN LATERAL .. ON TRUE
            if isinstance(join.this, exp.Lateral) and (join.args.get("kind") or "").upper() == "CROSS" and join.args.get("on") is None:
                join.set("kind", None)
        before = select.sql()
        _lateral_joins(copy)
        if copy.sql() != before and not any(isinstance(j.this, exp.Lateral) and not j.args.get("kind") and not j.args.get("side") and j.args.get("on") is None for j in copy.args.get("joins") or []):
            return copy
    return None


_NULL_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def null_comparison_filter(select: exp.Select) -> exp.Expression | None:
    """A ``WHERE`` conjunct that compares with a NULL literal (``x = NULL``) is never TRUE, so the filter is FALSE."""

    where = select.args.get("where")
    if where is None or isinstance(where.this, exp.Boolean):
        return None
    parts = _conjuncts(where.this)
    if not any(isinstance(p, _NULL_COMPARISONS) and any(isinstance(o, exp.Null) for o in (p.left, p.right)) for p in parts):
        return None
    select.set("where", exp.Where(this=exp.false()))
    return select


def _lateral_body(join: exp.Join) -> tuple[exp.Select, str] | None:
    item = join.this
    if not isinstance(item, exp.Lateral) or item.args.get("view") or not isinstance(item.this, exp.Subquery) or not item.alias:
        return None
    if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or join.args.get("using"):
        return None
    on = join.args.get("on")
    if on is not None and not (isinstance(on, exp.Boolean) and on.this):
        return None
    body = item.this.this
    return (body, item.alias) if isinstance(body, exp.Select) else None


def push_filter_to_lateral(select: exp.Select) -> exp.Expression | None:
    """``SELECT .. FROM (SELECT .. FROM x JOIN LATERAL (..) AS d) AS w WHERE p`` filters inside ``w``.

    ``w`` only projects (no grouping, DISTINCT, LIMIT or windows), so filtering its rows before or
    after the projection keeps the same rows; inside, ``p`` sits next to the lateral join it tests.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    where = select.args.get("where")
    if from_ is None or where is None or select.args.get("joins"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not any(_lateral_body(j) for j in inner.args.get("joins") or []):
        return None
    if any(inner.args.get(k) for k in _CLAUSES if k != "laterals"):
        return None
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    outputs = {}
    for name, item in zip(names, inner.expressions):
        value = item.unalias()
        if not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Exists, exp.Star, exp.Rand, exp.Anonymous)) for n in value.walk()):
            outputs[name] = value
    alias = source.alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        columns = list(part.find_all(exp.Column))
        movable = (
            columns
            and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Rand, exp.Anonymous)) for n in part.walk())
            and all(c.table.lower() == alias and c.name.lower() in outputs for c in columns)
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    pushed = []
    for part in moved:
        holder = exp.Select(expressions=[part.copy()])
        for column in list(holder.find_all(exp.Column)):
            value = outputs[column.name.lower()].copy()
            column.replace(exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Not)) else value)
        pushed.append(holder.expressions[0])
    existing = inner.args.get("where")
    inner.set("where", exp.Where(this=_and_all(_conjuncts(existing.this if existing is not None else None) + pushed)))
    select.set("where", exp.Where(this=_and_all(kept)) if kept else None)
    return select


def distinct_lateral_to_in(select: exp.Select) -> exp.Expression | None:
    """``FROM x JOIN LATERAL (SELECT e AS c FROM .. GROUP BY e) AS d WHERE v = d.c`` is ``WHERE v IN (SELECT e FROM ..)``.

    The lateral body returns each value of ``e`` once per outer row, so at most one of its rows
    equals ``v``: the join keeps each outer row once when ``v`` is among the values, which is
    what ``IN`` tests. ``d`` may be read nowhere else.
    """

    where = select.args.get("where")
    joins = select.args.get("joins") or []
    if where is None or any(j.args.get("side") or (j.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") for j in joins):
        return None
    for join in joins:
        found = _lateral_body(join)
        if found is None:
            continue
        body, alias = found
        group = body.args.get("group")
        if group is None or len(body.expressions) != 1 or len(group.expressions) != 1:
            continue
        if any(body.args.get(k) for k in ("distinct", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with")) or group.args.get("rollup") or group.args.get("cube") or group.args.get("grouping_sets"):
            continue
        value = body.expressions[0].unalias()
        if value != group.expressions[0] or any(isinstance(n, (exp.AggFunc, exp.Window)) for n in value.walk()):
            continue
        name = body.expressions[0].alias_or_name.lower()
        item = join.this
        outside = [n for n in select.walk() if n is not item and not _under(n, item)]
        uses = [n for n in outside if isinstance(n, exp.Column) and n.table.lower() == alias.lower()]
        parts = _conjuncts(where.this)
        tests = [
            p for p in parts
            if isinstance(p, exp.EQ)
            and any(isinstance(o, exp.Column) and o.table.lower() == alias.lower() and o.name.lower() == name for o in (p.left, p.right))
            and not all(isinstance(o, exp.Column) and o.table.lower() == alias.lower() for o in (p.left, p.right))
        ]
        if len(tests) != 1 or len(uses) != 1 or not _under(uses[0], tests[0]):
            continue
        test = tests[0]
        probe = test.right if uses[0] is test.left else test.left
        if any(c.table.lower() == alias.lower() for c in probe.find_all(exp.Column)) or any(isinstance(n, (exp.Subquery, exp.AggFunc, exp.Window)) for n in probe.walk()):
            continue
        query = body.copy()
        query.set("group", None)
        membership = exp.In(this=probe.copy(), query=exp.Subquery(this=query))
        rest = [membership if p is test else p for p in parts]
        select.set("joins", [j for j in joins if j is not join] or None)
        select.set("where", exp.Where(this=_and_all(rest)))
        return select
    return None
