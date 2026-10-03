"""Read a LEFT JOIN as an inner join when the data or the query rules out its null-extended rows.

Three rewrites, each tried by :func:`join_rewrites`:

* **Foreign key.** ``child LEFT JOIN parent ON child.fk = parent.pk`` is an inner join when
  ``child(fk)`` is declared to reference ``parent(pk)`` and ``child.fk`` is NOT NULL: every child
  row has a match, so the outer join adds no row. The child must not itself be null-extended
  by an earlier join (a NULL ``fk`` from there matches nothing).
* **HAVING.** In a grouped select, a LEFT JOIN to ``t`` is inner when every aggregate of the
  select ignores rows whose ``t`` columns are NULL (``COUNT(t.c)``, ``SUM(t.c + 1)``, ``MAX(t.c)``,
  ...) and HAVING rejects a group in which those aggregates see no row (``COUNT(t.c) > 0``,
  ``SUM(t.c) > 10``). A null-extended row then never changes a surviving group's values, and a
  group made only of such rows is dropped, so removing them changes nothing.
* **Nested join group.** ``c LEFT JOIN (a JOIN b ON p) ON q`` is read as a derived table
  ``c LEFT JOIN (SELECT a.x AS a__x, ... FROM a JOIN b ON p) AS g ON q'``, the only form of a
  mirrored three-way RIGHT JOIN, which the SMT encoding reads. Needs the members' columns.

A LEFT JOIN that a WHERE filter makes inner is :mod:`kumosql.null_rejecting_joins`.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts
from .null_rejecting_joins import _OPAQUE, _sources, _strict_tables, rejected_tables

_EMPTY_NULL = (exp.Sum, exp.Min, exp.Max, exp.Avg)  # NULL over no row; COUNT is 0


def _lower(mapping) -> dict:
    return {str(k).lower(): v for k, v in (mapping or {}).items()}


def _table_name(table: exp.Table) -> str:
    return ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None).lower()


def _same_table(spelled: str, declared: str) -> bool:
    a, b = spelled.lower().split("."), declared.lower().split(".")
    short = min(len(a), len(b))
    return a[-short:] == b[-short:]


def _lookup(mapping: dict, name: str):
    if name in mapping:
        return mapping[name]
    matches = [v for k, v in mapping.items() if _same_table(name, k)]
    return matches[0] if len(matches) == 1 else None


def _side(join: exp.Join) -> str:
    return (join.args.get("side") or "").upper()


def _fk_left_join_to_inner(select: exp.Select, not_null, foreign_keys) -> exp.Expression | None:
    if not foreign_keys:
        return None
    sources = _sources(select)
    if sources is None:
        return None
    foreign_keys, not_null = _lower(foreign_keys), _lower(not_null)
    by_alias = {(s.alias_or_name or "").lower(): (s, j) for s, j in sources}
    for index, (parent, join) in enumerate(sources):
        if join is None or _side(join) != "LEFT" or not isinstance(parent, exp.Table):
            continue
        p_alias = parent.alias_or_name.lower()
        pairs: dict[str, str] = {}  # child column -> parent column
        child_alias = None
        for part in conjuncts(join.args["on"]):
            if not isinstance(part, exp.EQ) or not all(isinstance(x, exp.Column) and x.table for x in (part.this, part.expression)):
                break
            mine = [x for x in (part.this, part.expression) if x.table.lower() == p_alias]
            other = [x for x in (part.this, part.expression) if x.table.lower() != p_alias]
            if len(mine) != 1 or len(other) != 1 or child_alias not in (None, other[0].table.lower()):
                break
            child_alias = other[0].table.lower()
            pairs[other[0].name.lower()] = mine[0].name.lower()
        else:
            child, child_join = by_alias.get(child_alias or "", (None, None))
            if not pairs or not isinstance(child, exp.Table) or not any(s is child for s, _ in sources[:index]):
                continue
            if child_join is not None and _side(child_join):
                continue  # a null-extended child row has NULL keys and no match
            child_name = _table_name(child)
            declared = {c.lower() for c in (_lookup(not_null, child_name) or ())}
            covered = any(
                _same_table(_table_name(parent), fk_parent) and {c.lower(): p.lower() for c, p in zip(cols, parent_cols)} == pairs
                for cols, fk_parent, parent_cols in (_lookup(foreign_keys, child_name) or [])
            )
            if covered and set(pairs) <= declared:
                copy = select.copy()
                copy_join = copy.args["joins"][index - 1]
                copy_join.set("side", None)
                copy_join.set("kind", None)
                return copy
    return None


def _ignores_null_rows(aggregate: exp.Expression, table: str) -> bool:
    """``aggregate`` skips every row whose ``table`` columns are all NULL."""

    if not isinstance(aggregate, (exp.Count, *_EMPTY_NULL)) or any(k not in ("this", "big_int") for k, v in aggregate.args.items() if v):
        return False
    argument = aggregate.this
    if isinstance(argument, exp.Distinct):
        if len(argument.expressions) != 1 or argument.args.get("on"):
            return False
        argument = argument.expressions[0]
    return argument is not None and table in _strict_tables(argument)


def _compare_literals(condition: exp.Expression) -> bool | None:
    """The value of a comparison between two number literals, or None."""

    if not isinstance(condition, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    sides = (condition.this, condition.expression)
    if not all(isinstance(s, exp.Literal) and not s.is_string for s in sides):
        return None
    try:
        left, right = (float(s.this) for s in sides)
    except ValueError:
        return None
    return {exp.EQ: left == right, exp.NEQ: left != right, exp.GT: left > right, exp.GTE: left >= right, exp.LT: left < right, exp.LTE: left <= right}[type(condition)]


def _never_true(condition: exp.Expression) -> bool:
    """``condition`` (aggregates already replaced by their value over no row) is never TRUE."""

    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return _never_true(condition.this) or _never_true(condition.expression)
    if isinstance(condition, exp.Or):
        return _never_true(condition.this) and _never_true(condition.expression)
    if isinstance(condition, exp.Not):
        inner = condition.this.this if isinstance(condition.this, exp.Paren) else condition.this
        if _compare_literals(inner) is True:
            return True
    value = _compare_literals(condition)
    if value is not None:
        return not value
    return _OPAQUE in rejected_tables(condition)


def _having_left_join_to_inner(select: exp.Select) -> exp.Expression | None:
    having = select.args.get("having")
    if having is None or select.args.get("qualify") or select.args.get("windows"):
        return None
    sources = _sources(select)
    if sources is None or any(_side(j) not in ("", "LEFT") for _, j in sources if j is not None):
        return None
    own = [n for n in select.find_all(exp.AggFunc, exp.Window) if n.find_ancestor(exp.Select) is select]
    if not own or any(isinstance(n, exp.Window) or n.find_ancestor(exp.Window) for n in own):
        return None
    for index, (source, join) in enumerate(sources):
        if join is None or _side(join) != "LEFT":
            continue
        table = (source.alias_or_name or "").lower()
        if not all(_ignores_null_rows(a, table) for a in own if not a.find_ancestor(exp.AggFunc)):
            continue
        empty = having.this.copy()
        for aggregate in [n for n in empty.find_all(exp.AggFunc) if n.find_ancestor(exp.Select) is None and not n.find_ancestor(exp.AggFunc)]:
            aggregate.replace(exp.Literal.number(0) if isinstance(aggregate, exp.Count) else exp.column("v", table=_OPAQUE))
        if not any(_never_true(part) for part in conjuncts(empty)):
            continue
        copy = select.copy()
        copy_join = copy.args["joins"][index - 1]
        copy_join.set("side", None)
        copy_join.set("kind", None)
        return copy
    return None


def _group(source: exp.Expression) -> exp.Table | None:
    """The base table carrying the joins of an unaliased parenthesized join group, or None."""

    if isinstance(source, exp.Subquery) and not source.alias and isinstance(source.this, exp.Table) and source.this.args.get("joins"):
        return source.this
    return None


def _nested_join_to_derived(select: exp.Select, schema) -> exp.Expression | None:
    if not schema or select.args.get("laterals"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    holders = [from_] + list(select.args.get("joins") or [])
    found = [(i, _group(h.this)) for i, h in enumerate(holders) if _group(h.this) is not None]
    if not found:
        return None
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}
    index, group = found[0]
    members = [group] + [j.this for j in group.args["joins"]]
    if not all(isinstance(m, exp.Table) and m.alias_or_name for m in members):
        return None
    member_aliases = [m.alias_or_name.lower() for m in members]
    if len(set(member_aliases)) != len(member_aliases):
        return None
    columns = {a: _lookup(schema, _table_name(m)) for a, m in zip(member_aliases, members)}
    if any(c is None for c in columns.values()):
        return None
    for join in group.args["joins"]:
        if join.args.get("using") or join.args.get("method"):
            return None
        if any(not c.table or c.table.lower() not in member_aliases for c in join.find_all(exp.Column)):
            return None
    other = [(h.this.alias_or_name or "").lower() for i, h in enumerate(holders) if i != index]
    if "" in other or set(other) & set(member_aliases):
        return None
    for nested in select.find_all(exp.Select):
        if nested is not select and any((t.alias_or_name or "").lower() in member_aliases for t in nested.find_all(exp.Table, exp.Subquery)):
            return None
    taken = {(t.alias_or_name or "").lower() for t in select.find_all(exp.Table, exp.Subquery)}
    alias = next(f"_g{n}" for n in range(len(taken) + 1) if f"_g{n}" not in taken)

    copy = select.copy()
    copy_holders = [copy.args.get("from_") or copy.args.get("from")] + list(copy.args.get("joins") or [])
    copy_group = _group(copy_holders[index].this)
    outside = [c for c in copy.find_all(exp.Column) if not _within(c, copy_group)]
    if any(isinstance(c.this, exp.Star) or not c.table for c in outside):
        return None  # a bare column, even in a subquery, might be a member's
    if any(not isinstance(star.parent, exp.Count) and not _within(star, copy_group) for star in copy.find_all(exp.Star)):
        return None
    used: dict[tuple[str, str], str] = {}
    for column in outside:
        owner = column.table.lower()
        if owner not in member_aliases:
            continue
        name = column.name.lower()
        if isinstance(column.this, exp.Star) or name not in columns[owner]:
            return None
        used.setdefault((owner, name), f"{owner}__{name}")
    if not used:
        used[(member_aliases[0], columns[member_aliases[0]][0])] = f"{member_aliases[0]}__{columns[member_aliases[0]][0]}"
    output_names = [item.output_name for item in copy.expressions]
    for column in outside:
        key = (column.table.lower(), column.name.lower())
        if key in used:
            column.replace(exp.column(used[key], table=alias))
    inner = exp.Select(expressions=[exp.alias_(exp.column(c, table=t), out) for (t, c), out in used.items()])
    inner.set("from_", exp.From(this=_strip_joins(copy_group)))
    inner.set("joins", [j.copy() for j in copy_group.args["joins"]])
    copy_holders[index].set("this", exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    copy.set("expressions", [exp.alias_(item, name) if name and item.output_name != name else item for item, name in zip(copy.expressions, output_names)])
    return copy


def _within(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _strip_joins(table: exp.Table) -> exp.Table:
    copy = table.copy()
    copy.set("joins", None)
    return copy


def join_rewrites(select: exp.Select, schema, not_null, foreign_keys) -> exp.Expression | None:
    """``select`` with one LEFT JOIN read as inner, or a nested join group made a derived table; or None."""

    return (
        _fk_left_join_to_inner(select, not_null, foreign_keys)
        or _having_left_join_to_inner(select)
        or _nested_join_to_derived(select, schema)
    )
