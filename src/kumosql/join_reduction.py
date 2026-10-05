"""Outer joins that are inner (or one-sided) because every row finds a partner, and joins that fold away.

Rules for the cases an optimizer reads off the schema or off constants, one entry
(:func:`join_reduction_rules`) in the normalizer's rule list:

* ``twin_outer_join``: an outer join of a table with itself, or of two derived tables that are
  copies of it, whose ON clause holds for a row paired with itself. ``a LEFT JOIN a AS b ON a.k = b.k``
  with ``k`` NOT NULL pairs every row of ``a`` with at least itself, so no row is padded and the join is
  inner. When only one side is the whole table the other side's rows all find their twin, so a FULL
  join loses the padding on that side (``a FULL JOIN (SELECT .. FROM a WHERE p) AS b ON a.k = b.k`` is
  ``a LEFT JOIN ..``). A key is not needed: the twin is a match whatever else matches. A derived side
  may project columns, filter, and join the table with itself on a NOT NULL key (which returns the table
  once). ON conjuncts that hold of the twin pair and read one side only (``a.x = a.x`` on a NOT NULL
  column) are dropped.
* ``lookup_self_join``: ``x LEFT JOIN t AS u ON v.k <=> u.k`` where ``v`` is an earlier alias of the same
  table ``t`` and ``k`` covers a NOT NULL key. The match is ``v``'s own row, or none when ``v`` is itself
  null-extended (its key is NULL and ``u``'s is not), so ``u`` carries ``v``'s values and the join goes.
* ``reject_in_inner_on``: an inner join's ON clause is a filter, so a conjunct that is never true on NULLs
  makes an earlier LEFT join inner (also inside a derived table the ON reads), as a WHERE clause does in
  ``null_rejecting_joins``.
* ``constant_outer_join``: a LEFT, RIGHT or FULL join ON TRUE against a side that always holds a row
  pads nothing; a LEFT/RIGHT join whose ON is false for the constants of one-row sources never matches,
  so its dropped side is the empty relation.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .ast_utils import FROM_KEY, conjuncts
from .join_rewrites import _lookup, _table_name
from .null_rejecting_joins import _plain_derived, _through_derived, rejected_tables, _convert

_BANNED_DERIVED = ("group", "having", "distinct", "limit", "offset", "qualify", "windows", "with_", "with", "order", "laterals", "kind", "into", "locks", "sample", "settings")


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")


def _side(join: exp.Join) -> str:
    return (join.args.get("side") or "").upper()


def _name(source: exp.Expression) -> str:
    if isinstance(source, exp.Table):
        return (source.alias_or_name or "").lower()
    if isinstance(source, exp.Subquery) and source.alias:
        return source.alias.lower()
    return ""


def _is_true(node: exp.Expression) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(node, exp.Boolean) and bool(node.this)


class Facts:
    """NOT NULL columns and keys of the declared tables, by lower-cased name."""

    def __init__(self, not_null, keys):
        self.not_null = {str(t).lower(): {str(c).lower() for c in cols} for t, cols in (not_null or {}).items()}
        self.keys = {str(t).lower(): [tuple(str(c).lower() for c in key) for key in ks] for t, ks in (keys or {}).items()}

    def non_null(self, table: str, column: str) -> bool:
        return column in (_lookup(self.not_null, table) or ())

    def key_covered(self, table: str, columns) -> bool:
        """Whether ``columns`` include every column of some key of ``table`` and all of them are NOT NULL."""

        have = set(columns)
        return any(key and set(key) <= have and all(self.non_null(table, c) for c in key) for key in (_lookup(self.keys, table) or ()))


# --- copies of one table -------------------------------------------------------------------


@dataclass
class _View:
    """A relation whose every row is a row of ``table`` (a projection of one), by output column name.

    ``columns`` maps an output name to the table column it carries; None means every column of the table
    under its own name (the table itself). ``full`` means each row of the table appears at least once.
    """

    table: str
    columns: dict[str, str] | None
    full: bool

    def column(self, name: str) -> str | None:
        return name if self.columns is None else self.columns.get(name)


def _ref(node: exp.Expression) -> exp.Column | None:
    """The qualified plain column an output expression is, or None."""

    node = node.this if isinstance(node, exp.Alias) else node
    return node if isinstance(node, exp.Column) and node.table and not isinstance(node.this, exp.Star) else None


def _view(source: exp.Expression, facts: Facts, depth: int = 0) -> _View | None:
    if depth > 4:
        return None
    if isinstance(source, exp.Table):
        if source.args.get("joins") or source.args.get("pivots") or source.args.get("laterals") or not isinstance(source.this, exp.Identifier):
            return None
        if source.args.get("version") or source.args.get("sample") or source.args.get("hints"):
            return None
        return _View(_table_name(source), None, True)
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in _BANNED_DERIVED) or any(inner.find_all(exp.AggFunc, exp.Window)):
        return None
    if any(n.find_ancestor(exp.Select) is not inner for n in inner.expressions):
        return None
    from_ = _from(inner)
    if from_ is None:
        return None
    sources = [from_.this] + [j.this for j in inner.args.get("joins") or []]
    views: dict[str, _View] = {}
    for item in sources:
        name, view = _name(item), _view(item, facts, depth + 1)
        if not name or view is None or name in views:
            return None
        views[name] = view
    if len({v.table for v in views.values()}) != 1:
        return None
    table = next(iter(views.values())).table

    def resolve(column: exp.Column) -> str | None:
        view = views.get(column.table.lower())
        return None if view is None else view.column(column.name.lower())

    for join in inner.args.get("joins") or []:
        if _side(join) or (join.args.get("kind") or "").upper() not in ("", "INNER") or join.args.get("using") or join.args.get("method"):
            return None
        on = join.args.get("on")
        if on is None:
            return None
        # the joined copy meets the row of an earlier copy it equals on a whole NOT NULL key
        linked: set[str] = set()
        for part in conjuncts(on):
            if _is_true(part):
                continue
            sides = _equated(part)
            if sides is None:
                return None
            a, b = sides
            ca, cb = resolve(a), resolve(b)
            if ca is None or ca != cb:
                return None
            if a.table.lower() != b.table.lower() and _name(join.this) in (a.table.lower(), b.table.lower()):
                linked.add(ca)
            elif not (facts.non_null(table, ca) or isinstance(part, exp.NullSafeEQ)):
                return None
        if not facts.key_covered(table, linked):
            return None
    where = inner.args.get("where")
    full = where is None and all(v.full for v in views.values())
    outputs: dict[str, str] = {}
    for item in inner.expressions:
        column = _ref(item)
        mapped = None if column is None else resolve(column)
        name = item.alias_or_name.lower()
        if mapped is None or not name or name in outputs:
            return None
        outputs[name] = mapped
    return _View(table, outputs, full)


def _equated(part: exp.Expression) -> tuple[exp.Column, exp.Column] | None:
    if isinstance(part, (exp.EQ, exp.NullSafeEQ)) and isinstance(part.this, exp.Column) and isinstance(part.expression, exp.Column):
        a, b = part.this, part.expression
        if a.table and b.table and not isinstance(a.this, exp.Star) and not isinstance(b.this, exp.Star):
            return a, b
    return None


def _twin_true(part: exp.Expression, views: dict[str, _View], facts: Facts, table: str) -> bool:
    """Whether ``part`` is TRUE on a row paired with itself (``views`` maps the two sides' names)."""

    while isinstance(part, exp.Paren):
        part = part.this
    if _is_true(part):
        return True
    if isinstance(part, (exp.EQ, exp.NullSafeEQ, exp.GTE, exp.LTE)) and isinstance(part.this, exp.Column) and isinstance(part.expression, exp.Column):
        a, b = part.this, part.expression
        va, vb = views.get(a.table.lower()), views.get(b.table.lower())
        if va is None or vb is None or isinstance(a.this, exp.Star) or isinstance(b.this, exp.Star):
            return False
        ca, cb = va.column(a.name.lower()), vb.column(b.name.lower())
        if ca is None or ca != cb:
            return False
        return isinstance(part, exp.NullSafeEQ) or facts.non_null(table, ca)
    return False


def twin_outer_join(select: exp.Select, facts: Facts) -> exp.Expression | None:
    from_, joins = _from(select), select.args.get("joins") or []
    if from_ is None or not joins or select.args.get("laterals"):
        return None
    join = joins[0]
    side = _side(join)
    if side not in ("LEFT", "RIGHT", "FULL") or join.args.get("kind") or join.args.get("using") or join.args.get("method") or join.args.get("on") is None:
        return None
    left_name, right_name = _name(from_.this), _name(join.this)
    left, right = _view(from_.this, facts), _view(join.this, facts)
    if not left_name or not right_name or left_name == right_name or left is None or right is None or left.table != right.table:
        return None
    views = {left_name: left, right_name: right}
    parts = conjuncts(join.args["on"])
    if not all(_twin_true(p, views, facts, left.table) for p in parts):
        return None
    left_matched, right_matched = right.full, left.full  # a side's rows find their twin in a whole other side
    if side == "LEFT":
        new = "" if left_matched else None
    elif side == "RIGHT":
        new = "" if right_matched else None
    else:
        new = "" if left_matched and right_matched else "RIGHT" if left_matched else "LEFT" if right_matched else None
    kept, seen = [], set()
    for part in parts:
        pair = _equated(part)
        one_side = _is_true(part) or (pair is not None and pair[0].table.lower() == pair[1].table.lower() and pair[0].name.lower() == pair[1].name.lower())
        key = part.sql()
        if one_side or key in seen:
            continue
        seen.add(key)
        kept.append(part)
    if new is None and len(kept) == len(parts):
        return None
    copy = select.copy()
    target = copy.args["joins"][0]
    if new is not None:
        target.set("side", new or None)
        target.set("kind", None)
    target.set("on", exp.and_(*[p.copy() for p in kept]) if kept else exp.true())
    return copy if copy.sql() != select.sql() else None


# --- a second lookup of the same row -------------------------------------------------------


def lookup_self_join(select: exp.Select, facts: Facts) -> exp.Expression | None:
    from_, joins = _from(select), select.args.get("joins") or []
    if from_ is None or not joins or select.args.get("laterals") or any(j.args.get("using") or j.args.get("method") for j in joins):
        return None
    if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)):
        return None
    sources = [from_.this] + [j.this for j in joins]
    for index, join in enumerate(joins, start=1):
        if _side(join) != "LEFT" or join.args.get("kind") or join.args.get("on") is None:
            continue
        u = join.this
        if not isinstance(u, exp.Table) or _view(u, facts) is None:
            continue
        u_name, table = _name(u), _table_name(u)
        pairs: set[str] = set()
        v_name = None
        for part in conjuncts(join.args["on"]):
            if _is_true(part):
                continue
            sides = _equated(part)
            if sides is None:
                break
            mine = [c for c in sides if c.table.lower() == u_name]
            other = [c for c in sides if c.table.lower() != u_name]
            if len(mine) != 1 or len(other) != 1 or mine[0].name.lower() != other[0].name.lower() or v_name not in (None, other[0].table.lower()):
                break
            v_name = other[0].table.lower()
            pairs.add(mine[0].name.lower())
        else:
            earlier = [s for s in sources[:index] if _name(s) == v_name]
            if not pairs or len(earlier) != 1 or not isinstance(earlier[0], exp.Table) or _table_name(earlier[0]) != table or not facts.key_covered(table, pairs):
                continue
            if sum(1 for s in sources if _name(s) == u_name) != 1:
                continue
            copy = select.copy()
            removed = copy.args["joins"][index - 1]
            reads = [c for c in copy.find_all(exp.Column) if c.table.lower() == u_name and not _within(c, removed)]
            if any(c.find_ancestor(exp.Select) is not copy for c in reads):
                continue  # read inside a subquery, which may bind the name itself
            for c in reads:
                c.set("table", exp.to_identifier(v_name) if not isinstance(earlier[0].args.get("alias"), exp.TableAlias) else earlier[0].args["alias"].this.copy())
            remaining = [j for j in copy.args["joins"] if j is not removed]
            copy.set("joins", remaining or None)
            return copy
    return None


def _within(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


# --- an inner join's ON rejects the padding of an earlier LEFT join -------------------------


def _strengthen_derived(source: exp.Expression, on: exp.Expression) -> bool:
    if not isinstance(source, exp.Subquery) or not source.alias or not _plain_derived(source.this):
        return False
    rejected = _through_derived(on, source.alias.lower(), source.this, False)
    return bool(rejected) and _convert(source.this, rejected)


def reject_in_inner_on(select: exp.Select) -> exp.Expression | None:
    from_, joins = _from(select), select.args.get("joins") or []
    if from_ is None or not joins or select.args.get("laterals"):
        return None
    if any(j.args.get("using") or j.args.get("method") for j in joins):
        return None
    sources = [from_.this] + [j.this for j in joins]
    names = [_name(s) for s in sources]
    if "" in names or len(set(names)) != len(names):
        return None
    sides = [_side(j) for j in joins]
    copy = select.copy()
    copy_sources = [_from(copy).this] + [j.this for j in copy.args["joins"]]
    changed = False
    for k, join in enumerate(copy.args["joins"]):
        if sides[k] or (join.args.get("kind") or "").upper() not in ("", "INNER") or join.args.get("on") is None:
            continue
        if any(side not in ("", "LEFT") for side in sides[:k]):
            continue
        on = join.args["on"]
        rejected = rejected_tables(on)
        for j in range(k):
            if sides[j] == "LEFT" and names[j + 1] in rejected and not copy.args["joins"][j].args.get("kind"):
                copy.args["joins"][j].set("side", None)
                sides[j] = ""
                changed = True
        for i in range(k + 2):
            changed |= _strengthen_derived(copy_sources[i], on)
    return copy if changed else None


# --- computed columns of a derived source read in a filter ---------------------------------------


def inline_derived_computed_columns(select: exp.Select) -> exp.Expression | None:
    """``d.c`` in a WHERE or ON of an all-inner select reads ``f(d.x)`` when the derived table ``d`` computes ``c`` as ``f(x)``.

    ``x`` must be an output of ``d`` under a name of its own, so that the expression can be written outside it. The
    derived table keeps its column; once nothing reads it the pruning rules drop it, and two spellings
    of one filter (a cast kept inside ``d`` or written in the join) meet.
    """

    from_, joins = _from(select), select.args.get("joins") or []
    if from_ is None or not joins or select.args.get("laterals"):
        return None
    for join in joins:
        if _side(join) or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method"):
            return None
    if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)):
        return None
    copy = select.copy()
    sources = [_from(copy).this] + [j.this for j in copy.args["joins"]]
    names = [_name(s) for s in sources]
    if "" in names or len(set(names)) != len(names):
        return None
    holders: list[tuple[exp.Expression, str]] = []
    if copy.args.get("where") is not None:
        holders.append((copy.args["where"], "this"))
    holders += [(j, "on") for j in copy.args["joins"] if j.args.get("on") is not None]
    changed = False
    for source in sources:
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select) or not _plain_derived(source.this):
            continue
        inner, alias = source.this, source.alias.lower()
        outputs: dict[str, exp.Expression] = {}
        for item in inner.expressions:
            name = item.alias_or_name.lower()
            if not name or name in outputs:
                outputs = {}
                break
            outputs[name] = item.this if isinstance(item, exp.Alias) else item
        exposed = {value.sql(): name for name, value in outputs.items() if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star)}
        for holder, key in holders:
            for column in list(holder.args[key].find_all(exp.Column)):
                if column.table.lower() != alias or column.find_ancestor(exp.Select) is not copy:
                    continue
                value = outputs.get(column.name.lower())
                if value is None or isinstance(value, exp.Column) or not _inlinable(value, exposed):
                    continue
                rewritten = value.copy()
                for inner_column in list(rewritten.find_all(exp.Column)):
                    inner_column.replace(exp.column(exposed[inner_column.sql()], table=alias))
                column.replace(exp.Paren(this=rewritten) if isinstance(rewritten, exp.Binary) else rewritten)
                changed = True
    return copy if changed else None


def _inlinable(value: exp.Expression, exposed: dict[str, str]) -> bool:
    columns = list(value.find_all(exp.Column))
    if not columns or any(c.sql() not in exposed for c in columns):
        return False
    return not any(isinstance(n, (exp.Subquery, exp.Select, exp.Window, exp.AggFunc, exp.Rand, exp.Anonymous, exp.Star)) for n in value.walk())


# --- constants ------------------------------------------------------------------------------


def _always_a_row(node: exp.Expression | None) -> bool:
    """A relation built from constants that holds at least one row."""

    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    if type(node) is exp.Union:
        return _always_a_row(node.left) or _always_a_row(node.right)
    if not isinstance(node, exp.Select):
        return False
    if any(node.args.get(k) for k in ("group", "having", "limit", "offset", "qualify", "windows")):
        return False
    if node.args.get("where") is not None and not _is_true(node.args["where"].this):
        return False
    distinct = node.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    from_ = _from(node)
    if from_ is None:
        return True
    return not node.args.get("joins") and _always_a_row(from_.this)


def _constants(source: exp.Expression) -> dict[str, exp.Expression] | None:
    """The literal each output of a one-row, FROM-less derived table holds."""

    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if _from(inner) is not None or any(inner.args.get(k) for k in ("where", "group", "having", "limit", "offset", "distinct", "qualify", "joins")):
        return None
    out = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Literal) or value.is_string or not item.alias_or_name:
            return None
        out[item.alias_or_name.lower()] = value
    return out


def _never_matches(on: exp.Expression, left: exp.Expression, right: exp.Expression) -> bool:
    from .empty_rules import _false

    constants = {}
    for source in (left, right):
        found = _constants(source)
        if found is not None:
            constants[_name(source)] = found
    holder = exp.Paren(this=on.copy())
    for column in list(holder.find_all(exp.Column)):
        value = constants.get(column.table.lower(), {}).get(column.name.lower()) if column.table else None
        if value is not None:
            column.replace(value.copy())
    return _false(holder)


def constant_outer_join(select: exp.Select) -> exp.Expression | None:
    from .empty_rules import _global_aggregate

    from_, joins = _from(select), select.args.get("joins") or []
    if from_ is None or len(joins) != 1 or select.args.get("laterals"):
        return None
    join = joins[0]
    side = _side(join)
    if side not in ("LEFT", "RIGHT", "FULL") or join.args.get("kind") or join.args.get("using") or join.args.get("method") or join.args.get("on") is None:
        return None
    left, right = from_.this, join.this
    if _is_true(join.args["on"]):
        left_row, right_row = _always_a_row(left), _always_a_row(right)
        new = {"LEFT": "" if right_row else None, "RIGHT": "" if left_row else None}.get(side)
        if side == "FULL":
            new = "" if left_row and right_row else "RIGHT" if right_row else "LEFT" if left_row else None
        if new is None:
            return None
        copy = select.copy()
        copy.args["joins"][0].set("side", new or None)
        return copy
    if side in ("LEFT", "RIGHT") and _never_matches(join.args["on"], left, right):
        dropped = right if side == "LEFT" else left
        if not isinstance(dropped, exp.Subquery) or not isinstance(dropped.this, exp.Select) or _global_aggregate(dropped.this):
            return None
        copy = select.copy()
        target = (copy.args["joins"][0].this if side == "LEFT" else _from(copy).this).this
        where = target.args.get("where")
        target.set("where", exp.Where(this=exp.false() if where is None else exp.and_(where.this.copy(), exp.false())))
        return copy
    return None


def join_reduction_rules(select: exp.Select, not_null=None, keys=None) -> exp.Expression | None:
    """The rules of this module, as one entry of the normalizer's rule list."""

    facts = Facts(not_null, keys)
    return (
        twin_outer_join(select, facts)
        or lookup_self_join(select, facts)
        or reject_in_inner_on(select)
        or inline_derived_computed_columns(select)
        or constant_outer_join(select)
    )
