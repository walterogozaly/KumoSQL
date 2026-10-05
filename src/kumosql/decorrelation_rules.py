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

from .ast_utils import FROM_KEY, declared_key, extended_grouping, same_table

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


def _bound_by(select: exp.Select) -> set[str]:
    """The relation aliases ``select`` itself declares in its FROM and joins (not those of nested selects)."""

    clauses = [select.args.get("from_") or select.args.get("from")] + list(select.args.get("joins") or [])
    return {
        (n.alias_or_name or "").lower()
        for clause in clauses
        if clause is not None
        for n in clause.walk()
        if isinstance(n, (exp.Table, exp.Subquery, exp.Lateral, exp.Unnest)) and n.alias_or_name and n.find_ancestor(exp.Select) is select
    }


def _bound_within(column: exp.Column, root: exp.Expression) -> bool:
    """Whether ``column``'s qualifier names a relation of a select that encloses it inside ``root``.

    Each select only sees its own relations and those of the selects around it, so an alias declared
    in a sibling or deeper subquery does not bind the column.
    """

    name = column.table.lower()
    node = column
    while node is not None:
        if isinstance(node, exp.Select) and name in _bound_by(node):
            return True
        if node is root:
            return False
        node = node.parent
    return False


def _free_qualifiers(node: exp.Expression) -> set[str]:
    return {c.table.lower() for c in node.find_all(exp.Column) if c.table and not _bound_within(c, node)}


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


def _named(value: exp.Expression, name: str) -> exp.Alias:
    # exp.alias_ would set a subquery's own alias, which prints the same but parses back as an Alias
    return exp.Alias(this=value, alias=exp.to_identifier(name))


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
            value = _named(value, column.name)
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
        select.set(FROM_KEY, exp.From(this=new_sources[0]))
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
    if group is None or extended_grouping(group):
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
            column.replace(_named(value, column.name) if column.parent is select and column.arg_key == "expressions" else value)
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


def decorrelation_step(select: exp.Select, not_null: dict[str, frozenset[str]] | None = None, schema: dict[str, list[str]] | None = None, dialect: str | None = None) -> exp.Expression | None:
    """The rules above, in order, for ``algebraic_equivalence.normalize``."""

    for rule in (null_comparison_filter, merge_correlated_derived, existence_joins, push_filter_to_lateral, distinct_lateral_to_in, one_row_joins, single_value_scalar, lambda sel: self_witnessed_exists(sel, not_null, dialect), lambda sel: self_domain_join(sel, not_null, dialect), lambda sel: lift_membership_tests(sel, schema), lambda sel: drop_implied_membership(sel, dialect), extreme_of_top_rows):
        rewritten = rule(select)
        if rewritten is not None:
            return rewritten
    if any(
        isinstance(n.args.get("query"), exp.Subquery) and isinstance(n.args["query"].this, exp.Select) and n.args["query"].this.args.get("having") is not None
        for n in select.find_all(exp.In)
        if n.find_ancestor(exp.Select) is select
    ):
        # a grouped IN body that star or CTE inlining exposed after the first pass
        from .algebraic_equivalence import _grouped_in_to_derived

        before = select.sql()
        copy = _grouped_in_to_derived(select.copy())
        if copy.sql() != before:
            return copy
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
        if any(body.args.get(k) for k in ("distinct", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with")) or extended_grouping(group):
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


def _one_row_body(body: exp.Expression) -> bool:
    """A select with an aggregate and no GROUP BY, HAVING or LIMIT returns exactly one row."""

    if not isinstance(body, exp.Select) or any(body.args.get(k) for k in ("group", "having", "limit", "offset", "fetch", "qualify", "windows", "with_", "with", "order")):
        return False
    if any(isinstance(n, exp.Window) for n in body.walk()) or not body.expressions:
        return False
    own = [n for n in body.find_all(exp.AggFunc) if n.find_ancestor(exp.Select) is body]
    is_single_value = [n for n in body.find_all(exp.Anonymous) if n.name.upper() == "SINGLE_VALUE" and n.find_ancestor(exp.Select) is body]
    return bool(own or is_single_value)


def one_row_joins(select: exp.Select) -> exp.Expression | None:
    """``x LEFT JOIN (SELECT MAX(v) AS m FROM t) AS d ON TRUE`` reads ``d.m`` as the scalar subquery ``(SELECT MAX(v) FROM t)``.

    A global aggregate that reads nothing from the query returns exactly one row, so joining it on
    TRUE (inner or LEFT) keeps every row once and adds the same values to each.
    """

    for join in [j for j in select.find_all(exp.Join) if j.find_ancestor(exp.Select) is select]:
        item = join.this
        if not isinstance(item, exp.Subquery) or not item.alias or not _one_row_body(item.this):
            continue
        side, kind = (join.args.get("side") or "").upper(), (join.args.get("kind") or "").upper()
        on = join.args.get("on")
        if side not in ("", "LEFT") or kind not in ("", "INNER", "CROSS", "OUTER") or join.args.get("using") or join.args.get("method"):
            continue
        if on is not None and not (isinstance(on, exp.Boolean) and on.this):
            continue
        body = item.this
        if _free_qualifiers(body):
            continue
        alias = item.alias.lower()
        outside = [n for n in select.walk() if n is not item and not _under(n, item)]
        if any(isinstance(n, (exp.Table, exp.Subquery, exp.Lateral)) and (n.alias_or_name or "").lower() == alias for n in outside):
            continue
        names = [e.alias_or_name.lower() for e in body.expressions]
        if "" in names or len(set(names)) != len(names) or any(isinstance(e.unalias(), exp.Star) for e in body.expressions):
            continue
        values = dict(zip(names, (e.unalias() for e in body.expressions)))
        uses = [n for n in outside if isinstance(n, exp.Column) and n.table.lower() == alias]
        if any(c.name.lower() not in values for c in uses):
            continue
        # an inner join filtered on the aggregate's own columns (COUNT(*) >= 0) is read better as a join
        where = select.args.get("where")
        if side != "LEFT" and where is not None and any(_under(c, where) for c in uses):
            continue
        holder = join.parent
        if holder is None or join.arg_key != "joins":
            continue
        probes = {}
        for name, value in values.items():
            probe = body.copy()
            probe.set("expressions", [value.copy()])
            probes[name] = probe
        # the body's one row comes from its aggregates: (SELECT 7 FROM u) alone has a row per row of u
        if not all(_one_row_body(probes[c.name.lower()]) for c in uses):
            continue
        for column in uses:
            value = exp.Subquery(this=probes[column.name.lower()].copy())
            column.replace(_named(value, column.name) if column.parent is select and column.arg_key == "expressions" else value)
        holder.set("joins", [j for j in holder.args["joins"] if j is not join] or None)
        wrapper = holder.parent
        # (t AS a) once its last nested join is gone is t AS a
        if isinstance(holder, exp.Table) and not holder.args.get("joins") and isinstance(wrapper, exp.Subquery) and not wrapper.alias:
            wrapper.replace(holder)
        return select
    return None


def single_value_scalar(select: exp.Select) -> exp.Expression | None:
    """``(SELECT SINGLE_VALUE(x) FROM t)`` is ``(SELECT x FROM t)``: NULL on no rows, the value on one, an error on more.

    Redundant parentheses around a subquery are dropped too.
    """

    changed = False
    for node in list(select.find_all(exp.Subquery)):
        # ((SELECT ..)) is (SELECT ..), unless a layer carries a tail: ((a UNION b) LIMIT 1) printed as one layer
        # reads as (a UNION b) LIMIT 1, which no longer sits inside the surrounding IN or scalar parentheses.
        if (
            isinstance(node.this, exp.Subquery)
            and not node.alias
            and not node.this.alias
            and not any(layer.args.get(k) for layer in (node, node.this) for k in ("order", "limit", "offset", "with", "with_"))
            and node.find_ancestor(exp.Select) is select
        ):
            node.replace(node.this)
            changed = True
    for node in list(select.find_all(exp.Subquery)):
        body = node.this
        if node.find_ancestor(exp.Select) is not select or isinstance(node.parent, (exp.From, exp.Join, exp.In, exp.Lateral)) or node.alias:
            continue
        if not isinstance(body, exp.Select) or len(body.expressions) != 1 or any(body.args.get(k) for k in ("group", "having", "limit", "offset", "qualify", "windows", "distinct", "order")):
            continue
        value = body.expressions[0].unalias()
        if not (isinstance(value, exp.Anonymous) and value.name.upper() == "SINGLE_VALUE" and len(value.expressions) == 1):
            continue
        if any(n.find_ancestor(exp.Select) is body for n in body.find_all(exp.AggFunc)):
            continue
        body.expressions[0].replace(value.expressions[0].copy())
        changed = True
    return select if changed else None


def self_witnessed_exists(select: exp.Select, not_null: dict[str, frozenset[str]] | None, dialect: str | None = None) -> exp.Expression | None:
    """``FROM t AS x WHERE EXISTS (SELECT .. FROM t AS y WHERE y.c = x.c ..)`` is TRUE: the row ``x`` itself is a witness.

    Every conjunct of the subquery's filter must equate a column of ``y`` with the same column of
    ``x`` (``=`` on a NOT NULL column, or ``<=>``), and ``x`` must be a real row of ``t`` (not the
    NULL-extended side of an outer join).
    """

    where = select.args.get("where")
    if where is None:
        return None
    joins = select.args.get("joins") or []
    if any(j.args.get("side") in ("RIGHT", "FULL") or (j.args.get("kind") or "").upper() in ("SEMI", "ANTI") for j in joins):
        return None
    sources = _sources(select)
    real = {}
    for position, source in enumerate(sources):
        if not isinstance(source, exp.Table) or source.args.get("joins") or not source.alias_or_name:
            continue
        if position > 0 and (joins[position - 1].args.get("side") or any(j.args.get("side") for j in joins[:position - 1])):
            continue
        real[source.alias_or_name.lower()] = source
    lowered = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}
    for test in list(where.find_all(exp.Exists)):
        if test.find_ancestor(exp.Select) is not select:
            continue
        body = test.this
        if not isinstance(body, exp.Select) or any(body.args.get(k) for k in _CLAUSES) or body.args.get("joins"):
            continue
        if any(isinstance(n, (exp.AggFunc, exp.Window)) for e in body.expressions for n in e.walk()):
            continue
        inner = _sources(body)
        if len(inner) != 1 or not isinstance(inner[0], exp.Table) or not inner[0].alias_or_name:
            continue
        table, alias = inner[0], inner[0].alias_or_name.lower()
        parts = _conjuncts(body.args["where"].this) if body.args.get("where") is not None else []
        outer = None
        ok = bool(parts)
        for part in parts:
            if not isinstance(part, (exp.EQ, exp.NullSafeEQ)) or not all(isinstance(o, exp.Column) for o in (part.left, part.right)):
                ok = False
                break
            mine = [o for o in (part.left, part.right) if o.table.lower() == alias]
            other = [o for o in (part.left, part.right) if o.table.lower() != alias]
            if len(mine) != 1 or len(other) != 1 or mine[0].name.lower() != other[0].name.lower():
                ok = False
                break
            if outer is None:
                outer = other[0].table.lower()
            if other[0].table.lower() != outer:
                ok = False
                break
            if isinstance(part, exp.EQ) and mine[0].name.lower() not in lowered.get(declared_key(table), set()):
                ok = False
                break
        source = real.get(outer or "")
        if not ok or source is None or not same_table(source, table, dialect):
            continue
        if alias in real and alias != outer:
            continue
        test.replace(exp.true())
        return select
    return None


def _closed(node: exp.Expression, schema: dict[str, list[str]] | None) -> bool:
    """Whether every column under ``node`` is bound inside it (qualified by an alias it declares, or a
    bare name of the one table it reads, by ``schema``)."""

    for select in node.find_all(exp.Select):
        tables = [s for s in _sources(select)]
        for column in select.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not select:
                continue
            if column.table:
                if not _bound_within(column, node):
                    return False
                continue
            if len(tables) != 1 or not isinstance(tables[0], exp.Table):
                return False
            known = {k.lower(): [c.lower() for c in v] for k, v in (schema or {}).items()}.get(declared_key(tables[0]))
            if known is None or column.name.lower() not in known:
                return False
    return True


def lift_membership_tests(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    """``x IN (SELECT c FROM t WHERE p AND c IN q AND NOT c IN r)`` is ``x IN (SELECT c FROM t WHERE p) AND x IN q AND NOT x IN r``.

    The inner tests read only the column that ``IN`` compares, so for a row with ``c = x`` they are
    the same tests on ``x`` (a non-NULL ``x``; a NULL ``x`` fails both forms). Only an ``IN`` that is a
    conjunct of the ``WHERE`` is rewritten, where FALSE and NULL both drop the row; the lifted tests
    may read no column of the query (uncorrelated subqueries and constants).
    """

    where = select.args.get("where")
    if where is None:
        return None
    parts = _conjuncts(where.this)
    for index, part in enumerate(parts):
        if not isinstance(part, exp.In) or not isinstance(part.args.get("query"), exp.Subquery):
            continue
        body = part.args["query"].this
        if not isinstance(body, exp.Select) or len(body.expressions) != 1 or body.args.get("where") is None:
            continue
        if any(body.args.get(k) for k in _CLAUSES if k != "distinct") or isinstance(body.args.get("distinct"), exp.Distinct) and body.args["distinct"].args.get("on"):
            continue
        sources = _sources(body)
        if len(sources) != 1 or not isinstance(sources[0], exp.Table):
            continue
        alias = sources[0].alias_or_name.lower()
        output = body.expressions[0].unalias()
        while isinstance(output, exp.Paren):
            output = output.this
        if not isinstance(output, exp.Column) or (output.table and output.table.lower() != alias):
            continue
        name = output.name.lower()

        def is_output(column: exp.Column) -> bool:
            return column.name.lower() == name and (not column.table or column.table.lower() == alias)

        lifted, kept = [], []
        for inner in _conjuncts(body.args["where"].this):
            own = [c for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is body]
            nested = [n for n in inner.walk() if isinstance(n, exp.Subquery)]
            liftable = (
                own
                and all(is_output(c) for c in own)
                and not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Rand, exp.Anonymous)) for n in inner.walk())
                and all(_closed(n.this, schema) for n in nested)
            )
            (lifted if liftable else kept).append(inner)
        if not lifted or any(_under(n, lifted_part) for lifted_part in lifted for n in [part.this] if False):
            continue
        probe = part.this
        if any(isinstance(n, (exp.Subquery, exp.AggFunc, exp.Window, exp.Rand, exp.Anonymous)) for n in probe.walk()):
            continue
        tests = []
        for inner in lifted:
            inner = inner.copy()
            for column in [inner] if isinstance(inner, exp.Column) else list(inner.find_all(exp.Column)):
                if column.find_ancestor(exp.Select) is None and is_output(column):
                    value = probe.copy()
                    if column is inner:
                        inner = value
                    else:
                        column.replace(value)
            tests.append(inner)
        new_body = body.copy()
        rest = _and_all([k.copy() for k in kept])
        new_body.set("where", exp.Where(this=rest) if rest is not None else None)
        membership = exp.In(this=probe.copy(), query=exp.Subquery(this=new_body))
        new_parts = parts[:index] + [membership] + tests + parts[index + 1:]
        select.set("where", exp.Where(this=_and_all(new_parts)))
        return select
    return None


def _membership_source(part: exp.Expression) -> tuple[str, exp.Table, str, bool] | None:
    """``x IN (SELECT [DISTINCT] c FROM t [WHERE p])`` as (x, table, column, filtered)."""

    if not isinstance(part, exp.In) or not isinstance(part.args.get("query"), exp.Subquery):
        return None
    body = part.args["query"].this
    if not isinstance(body, exp.Select) or len(body.expressions) != 1 or any(body.args.get(k) for k in _CLAUSES if k != "distinct"):
        return None
    if isinstance(body.args.get("distinct"), exp.Distinct) and body.args["distinct"].args.get("on"):
        return None
    sources = _sources(body)
    if len(sources) != 1 or not isinstance(sources[0], exp.Table) or sources[0].args.get("joins"):
        return None
    output = body.expressions[0].unalias()
    while isinstance(output, exp.Paren):
        output = output.this
    alias = sources[0].alias_or_name.lower()
    if not isinstance(output, exp.Column) or (output.table and output.table.lower() != alias):
        return None
    return part.this.sql(), sources[0], output.name.lower(), body.args.get("where") is not None


def drop_implied_membership(select: exp.Select, dialect: str | None = None) -> exp.Expression | None:
    """``x IN (SELECT c FROM t) AND x IN (SELECT c FROM t WHERE p)``: the second test implies the first, which is dropped."""

    where = select.args.get("where")
    if where is None:
        return None
    parts = _conjuncts(where.this)
    shapes = [_membership_source(p) for p in parts]
    filtered = [shape for shape in shapes if shape is not None and shape[3]]
    for index, shape in enumerate(shapes):
        if shape is not None and not shape[3] and any(
            f[0] == shape[0] and f[2] == shape[2] and same_table(f[1], shape[1], dialect) for f in filtered
        ):
            select.set("where", exp.Where(this=_and_all(parts[:index] + parts[index + 1:])))
            return select
    return None


def extreme_of_top_rows(select: exp.Select) -> exp.Expression | None:
    """``SELECT MAX(d.x) FROM (q ORDER BY x DESC LIMIT n) AS d`` is ``MAX`` over all of ``q``.

    The top ``n >= 1`` rows by ``x`` descending, NULLs last, hold the largest ``x`` whenever ``q`` has a
    non-NULL one, and otherwise both sides are NULL (``MIN`` with ascending order alike).
    """

    if select.args.get("joins") or any(select.args.get(k) for k in ("where", "group", "having", "order", "limit", "offset", "qualify", "windows")):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    order, limit = inner.args.get("order"), inner.args.get("limit")
    if order is None or limit is None or inner.args.get("offset") or not order.expressions or isinstance(inner.args.get("distinct"), exp.Distinct) and inner.args["distinct"].args.get("on"):
        return None
    count = limit.expression if isinstance(limit, exp.Limit) else None
    if not (isinstance(count, exp.Literal) and count.is_int and int(count.this) >= 1) or limit.args.get("offset"):
        return None
    first = order.expressions[0]
    key = first.this
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if isinstance(key, exp.Literal) and key.is_int and 1 <= int(key.this) <= len(names):
        name = names[int(key.this) - 1]
    elif isinstance(key, exp.Column):
        # the ordered value is an output, named by its alias or spelled as its expression
        matches = [n for n, e in zip(names, inner.expressions) if e.unalias().sql() == key.sql() or (not key.table and n == key.name.lower())]
        if len(set(matches)) != 1:
            return None
        name = matches[0]
    else:
        return None
    if first.args.get("nulls_first"):
        return None
    wanted = exp.Max if first.args.get("desc") else exp.Min
    calls = [n for n in select.find_all(exp.AggFunc) if n.find_ancestor(exp.Select) is select]
    if not calls or any(not isinstance(c, wanted) or not (isinstance(c.this, exp.Column) and c.this.name.lower() == name and c.this.table.lower() in ("", source.alias.lower())) for c in calls):
        return None
    # every column the select reads sits inside one of those calls
    if any(c.find_ancestor(exp.AggFunc) is None for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select):
        return None
    inner.set("order", None)
    inner.set("limit", None)
    return select


def _real_sources(select: exp.Select) -> dict[str, exp.Expression]:
    """The FROM items of ``select`` whose rows are real rows (not NULL-extended by an outer join)."""

    joins = select.args.get("joins") or []
    if any(j.args.get("side") in ("RIGHT", "FULL") for j in joins):
        return {}
    real = {}
    for position, source in enumerate(_sources(select)):
        if isinstance(source, (exp.Table, exp.Subquery)) and source.alias_or_name and not source.args.get("joins"):
            if position == 0 or not joins[position - 1].args.get("side"):
                real[source.alias_or_name.lower()] = source
    return real


def _base_column(select: exp.Select, column: exp.Column, depth: int = 0) -> tuple[exp.Table, str] | None:
    """The base table column whose values ``column`` carries: it names a real row of a table, or a
    plain column output of a derived table that does (through any number of projections)."""

    if depth > 8 or not column.table:
        return None
    source = _real_sources(select).get(column.table.lower())
    if isinstance(source, exp.Table):
        return source, column.name.lower()
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    group = inner.args.get("group")
    if group is not None and extended_grouping(group):
        return None
    for item in inner.expressions:
        if item.alias_or_name.lower() == column.name.lower():
            value = item.unalias()
            return _base_column(inner, value, depth + 1) if isinstance(value, exp.Column) else None
    return None


def self_domain_join(select: exp.Select, not_null: dict[str, frozenset[str]] | None, dialect: str | None = None) -> exp.Expression | None:
    """``FROM t AS x JOIN (SELECT y.c AS k FROM t AS y GROUP BY y.c) AS d ON x.c = d.k`` is ``FROM t AS x``, reading ``d.k`` as ``x.c``.

    The derived table holds each value of ``t.c`` once, and the row ``x`` of ``t`` carries one of
    them, so every row of ``x`` meets exactly one row of ``d`` (``=`` needs ``c`` NOT NULL; ``<=>``
    matches NULL too). This is the domain join a top-down decorrelator adds.
    """

    joins = select.args.get("joins") or []
    if any(j.args.get("side") in ("RIGHT", "FULL") or (j.args.get("kind") or "").upper() in ("SEMI", "ANTI") for j in joins):
        return None
    lowered = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}
    sources = _sources(select)
    for join in joins:
        item = join.this
        if join.args.get("side") or join.args.get("using") or join.args.get("method") or not isinstance(item, exp.Subquery) or not item.alias:
            continue
        body = item.this
        if not isinstance(body, exp.Select) or body.args.get("where") is not None or body.args.get("joins"):
            continue
        if any(body.args.get(k) for k in _CLAUSES if k not in ("group", "distinct")) or body.args.get("having"):
            continue
        inner = _sources(body)
        if len(inner) != 1 or not isinstance(inner[0], exp.Table) or not inner[0].alias_or_name:
            continue
        table = inner[0]
        outputs = {}
        for e in body.expressions:
            value = e.unalias()
            if not isinstance(value, exp.Column) or (value.table and value.table.lower() != table.alias_or_name.lower()):
                outputs = None
                break
            outputs[e.alias_or_name.lower()] = value.name.lower()
        if not outputs or len(set(outputs.values())) != len(outputs):
            continue
        group = body.args.get("group")
        distinct = body.args.get("distinct")
        if group is not None:
            keys = []
            for k in group.expressions:
                if not isinstance(k, exp.Column):
                    keys = None
                    break
                keys.append(k.name.lower())
            if keys is None or set(keys) != set(outputs.values()) or extended_grouping(group):
                continue
        elif not (isinstance(distinct, exp.Distinct) and not distinct.args.get("on")):
            continue
        alias = item.alias.lower()
        parts = _conjuncts(join.args.get("on"))
        pinned, outer = {}, None
        ok = bool(parts)
        for part in parts:
            if not isinstance(part, (exp.EQ, exp.NullSafeEQ)) or not all(isinstance(o, exp.Column) for o in (part.left, part.right)):
                ok = False
                break
            mine = [o for o in (part.left, part.right) if o.table.lower() == alias]
            other = [o for o in (part.left, part.right) if o.table.lower() != alias]
            if len(mine) != 1 or len(other) != 1 or mine[0].name.lower() not in outputs:
                ok = False
                break
            base = _base_column(select, other[0])
            if base is None or base[1] != outputs[mine[0].name.lower()] or not same_table(base[0], table, dialect):
                ok = False
                break
            outer = outer or other[0].table.lower()
            if other[0].table.lower() != outer:
                ok = False
                break
            if isinstance(part, exp.EQ) and base[1] not in lowered.get(declared_key(table), set()):
                ok = False
                break
            pinned[mine[0].name.lower()] = other[0]
        if not ok or outer is None or set(pinned) != set(outputs):
            continue
        reader = next((s for s in sources if (s.alias_or_name or "").lower() == outer), None)
        if reader is None or sources.index(reader) > sources.index(item):
            continue
        outside = [n for n in select.walk() if n is not item and not _under(n, item) and not _under(n, join)]
        if any(isinstance(n, (exp.Table, exp.Subquery, exp.Lateral)) and (n.alias_or_name or "").lower() == alias for n in outside):
            continue
        for column in [n for n in outside if isinstance(n, exp.Column) and n.table.lower() == alias]:
            value = pinned[column.name.lower()].copy()
            column.replace(_named(value, column.name) if column.parent is select and column.arg_key == "expressions" else value)
        select.set("joins", [j for j in joins if j is not join] or None)
        return select
    return None
