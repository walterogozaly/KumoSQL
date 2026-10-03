"""What a query's output is guaranteed to look like, without running it.

``infer_properties(sql, constraints, schema)`` answers three questions about a
SELECT, each as a proven fact or as "not known" (never as a guess):

* which output columns are never NULL (a NOT NULL column that is read through
  inner joins, a ``COUNT``, a column the WHERE clause tests with ``IS NOT NULL``
  or a comparison, a ``COALESCE`` with a constant; but not a column of the
  nullable side of an outer join, nor ``SUM`` over a global aggregate that may
  see no rows);
* which sets of output columns are unique (``GROUP BY`` makes its keys unique,
  ``DISTINCT`` makes the whole row unique, a join keeps a side's key when the
  other side is matched on its own key and otherwise only the pair of keys is
  unique);
* how many rows it returns at most (a global aggregate returns exactly one row,
  ``LIMIT 1`` and a lookup by a full key return at most one), which is what a
  scalar subquery needs to be safe.

Uniqueness means no two rows are identical, with NULL equal to NULL (the way
``GROUP BY`` and ``DISTINCT`` compare). A fact that rests on a declared NOT NULL
column or key says so in ``assumptions``; facts that follow from the query alone
carry none. Anything the analysis does not understand is left as unknown, so
"not known" is the safe answer for a case it cannot decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

from .ast_utils import EXCEPT_KEY, conjuncts as _conjuncts, is_call
from .smt_equivalence import TableConstraints

# A fact's provenance: the declared facts it rests on (empty = follows from the query).
Provenance = frozenset

_NULL_PROPAGATING = {
    "ABS", "UPPER", "LOWER", "LENGTH", "TRIM", "LTRIM", "RTRIM", "ROUND", "FLOOR", "CEIL", "CEILING",
    "SQRT", "CONCAT", "REVERSE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "SUBSTR",
    "SUBSTRING", "LEFT", "RIGHT", "REPLACE", "SIGN", "MOD", "POWER", "LN", "LOG", "EXP",
}
_ALWAYS_NON_NULL = {"ROW_NUMBER", "RANK", "DENSE_RANK", "COUNTIF", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_DATETIME", "CURRENT_TIME"}
_NULL_ON_EMPTY_AGGREGATES = (exp.Sum, exp.Min, exp.Max, exp.Avg)
# Marks an internal column name that no reference can resolve to: a repeated output name
# (``name`` + _HIDDEN + position) or a VALUES column nobody named.
_HIDDEN = "\x00"
_MAX_GROUPING_SETS = 4096
_STAR_SOURCE = "output_properties_star_source"


@dataclass(frozen=True)
class ColumnFact:
    name: str
    non_null: bool
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniqueKey:
    columns: tuple[str, ...]  # empty: the query returns at most one row
    assumptions: tuple[str, ...] = ()
    positions: tuple[int, ...] = ()  # the same columns by output position (names can repeat)


@dataclass(frozen=True)
class OutputProperties:
    """Proven facts about one query's output; absence of a fact means "not known"."""

    columns: tuple[ColumnFact, ...] = ()
    keys: tuple[UniqueKey, ...] = ()
    exactly_one_row: bool = False
    unsupported: str = ""

    def column(self, name: str) -> ColumnFact | None:
        """The fact for an output name; None when no column, or several, carry that name."""

        found = [column for column in self.columns if column.name == name.lower()]
        return found[0] if len(found) == 1 else None

    def non_null(self, name: str) -> bool:
        column = self.column(name)
        return bool(column and column.non_null)

    def is_unique(self, *names: str) -> bool:
        """True when the given output columns (or a subset of them) are proven unique."""

        wanted = {n.lower() for n in names}
        return any(set(key.columns) <= wanted for key in self.keys)

    @property
    def at_most_one_row(self) -> bool:
        return any(not key.columns for key in self.keys)

    @property
    def scalar_subquery(self) -> str:
        """``exactly_one``, ``at_most_one`` or ``unknown`` (it may return several rows)."""

        if self.exactly_one_row:
            return "exactly_one"
        return "at_most_one" if self.at_most_one_row else "unknown"

    def assumptions_for(self, *facts: str) -> tuple[str, ...]:
        """Declared facts a conclusion rests on, e.g. ``assumptions_for("id")`` for the key and NOT NULL behind ``id``."""

        found: dict[str, None] = {}
        for name in facts:
            column = self.column(name)
            for item in (column.assumptions if column else ()):
                found[item] = None
        return tuple(found)

    def to_json(self) -> dict:
        return {
            "columns": [{"name": c.name, "non_null": c.non_null, "assumptions": list(c.assumptions)} for c in self.columns],
            "unique_keys": [{"columns": list(k.columns), "assumptions": list(k.assumptions)} for k in self.keys],
            "exactly_one_row": self.exactly_one_row,
            "scalar_subquery": self.scalar_subquery,
            "unsupported": self.unsupported,
        }


class _Unsupported(Exception):
    pass


@dataclass
class _Rel:
    """A relation during analysis: columns keyed by ``alias.column`` (all lower case)."""

    cols: dict[str, tuple[bool, Provenance]] = field(default_factory=dict)  # qualified -> (non_null, provenance)
    keys: list[tuple[frozenset, Provenance]] = field(default_factory=list)  # sets of qualified columns
    exactly_one: bool = False
    order: list[str] = field(default_factory=list)  # qualified names in declared order
    revert: dict = field(default_factory=dict)  # outer-join side column -> (declared non-null, provenance, side id)
    same: list[set] = field(default_factory=list)  # columns known equal in every row (inner join / WHERE equalities)

    def unite(self, a: str, b: str) -> None:
        merged = {a, b}
        rest = []
        for group in self.same:
            if group & merged:
                merged |= group
            else:
                rest.append(group)
        self.same = [*rest, merged]

    def equal_to(self, q: str) -> set:
        for group in self.same:
            if q in group:
                return set(group)
        return {q}


def _fact(kind: str, table: str, columns: Iterable[str]) -> str:
    return f"{table}.{next(iter(columns))} is NOT NULL" if kind == "not_null" else f"({', '.join(columns)}) is unique in {table}"


class _Analyzer:
    def __init__(self, constraints: Mapping[str, TableConstraints], schema: Mapping[str, list[str]], dialect: str) -> None:
        self.constraints = {k.lower().strip("`"): v for k, v in (constraints or {}).items()}
        self.schema = {k.lower().strip("`"): [c.lower() for c in v] for k, v in (schema or {}).items()}
        self.dialect = dialect
        self.ctes: dict[str, exp.Expression] = {}

    # ----- lookups

    def _lookup(self, table: exp.Table) -> tuple[list[str], frozenset, tuple]:
        parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]
        candidates = [".".join(parts[i:]) for i in range(len(parts))]
        for name in candidates:
            if name in self.schema or name in self.constraints:
                columns = self.schema.get(name)
                facts = self.constraints.get(name, TableConstraints())
                if columns is None:
                    raise _Unsupported(f"columns of {name} are not known")
                return columns, frozenset(facts.not_null), tuple(facts.keys)
        raise _Unsupported(f"table {'.'.join(parts)} is not in the schema")

    # ----- relations

    def source(self, node: exp.Expression) -> tuple[str, _Rel]:
        alias = node.alias.lower() if node.alias else ""
        if isinstance(node, exp.Table):
            name = node.name.lower()
            if not node.args.get("db") and name in self.ctes:
                cte = self.ctes[name]
                rel = _requalify(self.query_rel(cte.this), alias or name, _alias_columns(cte))
                return alias or name, _requalify(rel, alias or name, _alias_columns(node))
            columns, not_null, keys = self._lookup(node)
            alias = alias or node.name.lower()
            rel = _Rel()
            display = ".".join(p.name.lower() for p in (node.args.get("db"), node.this) if p is not None and p.name)
            for column in columns:
                q = f"{alias}.{column}"
                rel.cols[q] = (column in not_null, frozenset({f"{display}.{column} is NOT NULL"}) if column in not_null else frozenset())
                rel.order.append(q)
            for key in keys:
                key = tuple(c.lower() for c in key)
                if all(c in columns for c in key):
                    rel.keys.append((frozenset(f"{alias}.{c}" for c in key), frozenset({f"({', '.join(key)}) is unique in {display}"})))
            return alias, _requalify(rel, alias, _alias_columns(node))
        if isinstance(node, exp.Values):
            alias = alias or f"{_HIDDEN}values{id(node)}"
            return alias, _requalify(self.values_rel(node), alias, _alias_columns(node))
        if isinstance(node, exp.Subquery):
            if not alias:
                if isinstance(node.this, (exp.Table, exp.Subquery)) and node.this.args.get("joins"):
                    return "", self._join_group(node.this)
                alias = f"{_HIDDEN}subquery{id(node)}"  # its columns are read unqualified
            return alias, _requalify(self.query_rel(node.this), alias, _alias_columns(node))
        if isinstance(node, exp.Lateral) and not node.args.get("view") and isinstance(node.this, exp.Subquery) and alias:
            # Correlated: the inner query's references to earlier FROM items read as outer values
            # (constants per outer row), so its facts hold per row of the left side.
            return alias, _requalify(self.query_rel(node.this.this), alias, _alias_columns(node))
        raise _Unsupported(f"source {type(node).__name__}")

    def _join_group(self, node: exp.Expression) -> _Rel:
        """``(a JOIN b ON ...)`` in FROM: the joined relation, its columns keeping their own qualifiers."""

        node = node.copy()
        joins = node.args.pop("joins")
        _, current = self.source(node)
        for join in joins:
            current = self._join(current, {}, join, None)
        return current

    def values_rel(self, node: exp.Values) -> _Rel:
        """``VALUES (...), (...)``: literal rows, so NULLs, row count and repeats are visible."""

        rows = [list(row.expressions) if isinstance(row, exp.Tuple) else [row] for row in node.expressions]
        width = len(rows[0]) if rows else 0
        if not rows or not width or any(len(row) != width for row in rows):
            raise _Unsupported("VALUES rows of different widths")
        out = _Rel(exactly_one=len(rows) == 1)
        empty = _Rel()
        values = []
        for index in range(width):
            q = f".{_HIDDEN}{index}"
            cells = [row[index] for row in rows]
            non_null = all(self._non_null(cell, empty)[0] for cell in cells)
            out.cols[q] = (non_null, frozenset())
            out.order.append(q)
            values.append([_literal_value(cell) for cell in cells])
            if len(rows) > 1 and _all_distinct(values[-1]):
                out.keys.append((frozenset({q}), frozenset()))
        if len(rows) == 1:
            out.keys.append((frozenset(), frozenset()))
        elif _all_distinct([tuple(v[i] for v in values) if all(v[i] is not None for v in values) else None for i in range(len(rows))]):
            out.keys.append((frozenset(out.order), frozenset()))
        out.keys = _prune_keys(out.keys)
        return out

    def query_rel(self, node: exp.Expression) -> _Rel:
        """The output relation of a query, columns qualified by the empty alias."""

        while isinstance(node, exp.Subquery):
            node = node.this
        with_ = node.args.get("with_") or node.args.get("with")
        saved = dict(self.ctes)
        try:
            if with_ is not None:
                if with_.args.get("recursive"):
                    raise _Unsupported("recursive WITH")
                for cte in with_.expressions:
                    self.ctes[cte.alias.lower()] = cte
            if isinstance(node, exp.Select):
                return self.select_rel(node)
            if isinstance(node, exp.Values):
                return self.values_rel(node)
            if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
                return self.set_rel(node)
            raise _Unsupported(type(node).__name__)
        finally:
            self.ctes = saved

    def set_rel(self, node) -> _Rel:
        if node.args.get("by_name") or node.args.get("on"):
            raise _Unsupported("BY NAME set operation")  # columns pair by name, not position
        left, right = self.query_rel(node.left), self.query_rel(node.right)
        if len(left.order) != len(right.order):
            raise _Unsupported("set operation with different widths")
        distinct = node.args.get("distinct", True)
        out = _Rel()
        for lq, rq in zip(left.order, right.order):
            ln, lp = left.cols[lq]
            rn, rp = right.cols[rq]
            if isinstance(node, exp.Except):
                non_null, prov = ln, lp
            elif isinstance(node, exp.Intersect):
                non_null, prov = (ln, lp) if ln else (rn, rp) if rn else (False, frozenset())
            else:
                non_null, prov = ln and rn, lp | rp
            out.cols[lq] = (non_null, prov if non_null else frozenset())
            out.order.append(lq)
        if distinct:
            out.keys.append((frozenset(out.order), frozenset()))
        elif isinstance(node, exp.Except):
            out.keys = [(frozenset(f".{k.split('.', 1)[1]}" for k in key), prov) for key, prov in left.keys if all(k in left.order for k in key)]
        return out

    def select_rel(self, select: exp.Select) -> _Rel:
        from_ = select.args.get("from_") or select.args.get("from")
        scope: dict[str, _Rel] = {}
        if from_ is None:
            rel = _Rel(exactly_one=True)
            rel.keys.append((frozenset(), frozenset()))
            current = rel
        else:
            alias, current = self.source(from_.this)
            scope[alias] = current
            for join in select.args.get("joins") or []:
                current = self._join(current, scope, join, select.args.get("where").this if select.args.get("where") else None)
        current = self._filter(current, select.args.get("where"), scope)
        return self._project(select, current, scope)

    # ----- joins and filters

    def _join(self, left: _Rel, scope: dict, join: exp.Join, where: exp.Expression | None = None) -> _Rel:
        if join.args.get("using"):
            raise _Unsupported("JOIN USING")
        alias, right = self.source(join.this)
        side = (join.args.get("side") or "").upper()
        on = join.args.get("on")
        known = {**{q: None for q in left.cols}, **{q: None for q in right.cols}}
        left_bound: set[str] = set()
        right_bound: set[str] = set()
        conds = list(_conjuncts(on)) if on is not None else []
        if on is None and side == "" and where is not None:
            conds += list(_conjuncts(where))  # FROM a, b WHERE a.x = b.y is an inner join on the equality
        for cond in conds:
            pair = _eq_pair(cond)
            if pair is None:
                continue
            for x, y in (pair, pair[::-1]):
                qx = self._resolve(x, known)
                if qx is None:
                    continue
                qy = self._resolve(y, known)
                if qy is not None and ((qx in right.cols) != (qy in right.cols)) or qy is None and self._free_of(y, known):
                    (right_bound if qx in right.cols else left_bound).add(qx)
        out = _Rel(order=left.order + right.order, same=[set(g) for g in left.same + right.same])
        out.revert = {**left.revert, **right.revert}
        for rel, nullable in ((left, side in ("RIGHT", "FULL")), (right, side in ("LEFT", "FULL"))):
            for q in rel.order:
                non_null, prov = rel.cols[q]
                out.cols[q] = (non_null and not nullable, prov if non_null and not nullable else frozenset())
                if nullable:
                    out.revert[q] = (*(out.revert[q][:2] if q in out.revert else (non_null, prov)), id(rel) if q not in out.revert else out.revert[q][2])
        if side == "":
            if on is not None:
                self._mark_non_null(out, on)
                for cond in _conjuncts(on):
                    pair = _eq_pair(cond)
                    if pair and (a := self._resolve(pair[0], known)) and (b := self._resolve(pair[1], known)):
                        out.unite(a, b)
        else:
            out.same = [set(g) for g in left.same] if side == "LEFT" else [set(g) for g in right.same] if side == "RIGHT" else []
        keys: list[tuple[frozenset, Provenance]] = []
        if _covers_key(right.keys, right_bound) and side in ("", "INNER", "LEFT"):
            keys += [(k, p | _prov_of(right.keys, right_bound)) for k, p in left.keys]
        if _covers_key(left.keys, left_bound) and side in ("", "INNER", "RIGHT"):
            keys += [(k, p | _prov_of(left.keys, left_bound)) for k, p in right.keys]
        for kl, pl in left.keys:
            for kr, pr in right.keys:
                if side != "FULL":
                    keys.append((kl | kr, pl | pr))
                    continue
                # A row padded on the right and one padded on the left are equal when both keys are
                # NULL (or empty); a key column never NULL on its own side tells them apart.
                witness = [rel.cols[q] for rel, key in ((left, kl), (right, kr)) for q in sorted(key) if rel.cols[q][0]]
                if witness:
                    keys.append((kl | kr, pl | pr | witness[0][1]))
        out.keys = _prune_keys(keys)
        scope[alias] = right
        return out

    def _resolve(self, node, known):
        """Qualified name of a column reference, or None (outside this scope / ambiguous)."""

        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            return None
        if node.meta.get(_STAR_SOURCE) in known:
            return node.meta[_STAR_SOURCE]
        name = node.name.lower()
        table = node.table.lower()
        if table:
            q = f"{table}.{name}"
            if q not in known or any(k.startswith(q + _HIDDEN) for k in known):
                return None  # a repeated output name is ambiguous
            return q
        found = [q for q in known if _base(q.split(".", 1)[1]) == name]
        return found[0] if len(found) == 1 else None

    def _filter(self, rel: _Rel, where: exp.Where | None, scope: dict) -> _Rel:
        if where is None:
            return rel
        out = _Rel(cols=dict(rel.cols), keys=list(rel.keys), order=list(rel.order), same=[set(g) for g in rel.same], revert=dict(rel.revert))
        self._mark_non_null(out, where.this)
        bound: set[str] = set()
        for cond in _conjuncts(where.this):
            pair = _eq_pair(cond)
            if pair is None:
                continue
            qa, qb = self._resolve(pair[0], out.cols), self._resolve(pair[1], out.cols)
            if qa and qb:
                out.unite(qa, qb)
            for x, y, qx, qy in ((pair[0], pair[1], qa, qb), (pair[1], pair[0], qb, qa)):
                if qx is not None and qy is None and self._free_of(y, out.cols):
                    bound.add(qx)
        # A key whose remaining columns are all bound to constants identifies at most one row.
        keys = list(out.keys)
        for key, prov in rel.keys:
            closed = set(bound)
            for q in bound:
                closed |= out.equal_to(q)
            reduced = key - closed
            if reduced != key:
                keys.append((reduced, prov))
        out.keys = _prune_keys(keys)
        return out

    def _free_of(self, node: exp.Expression, known) -> bool:
        """The expression reads nothing from this query's own relations (a literal, parameter or outer column)."""

        for column in node.find_all(exp.Column):
            if self._resolve(column, known) is not None:
                return False
            if not column.table and any(q.split(".", 1)[1] == column.name.lower() for q in known):
                return False
        return not node.find(exp.Subquery) and not node.find(exp.Window)

    def _mark_non_null(self, rel: _Rel, cond: exp.Expression) -> None:
        rejected = self._null_rejected(cond, rel)
        for name in rejected:
            if name in rel.cols and not rel.cols[name][0]:
                rel.cols[name] = (True, frozenset())
        # A match required on the nullable side of an outer join makes it an inner join for those rows.
        for side in {rel.revert[n][2] for n in rejected if n in rel.revert}:
            for q, (non_null, prov, group) in rel.revert.items():
                if group == side and non_null:
                    rel.cols[q] = (True, prov)

    def _null_rejected(self, cond: exp.Expression, rel: _Rel) -> set:
        """Columns that must be non-NULL for ``cond`` to be TRUE."""

        if isinstance(cond, exp.Paren):
            return self._null_rejected(cond.this, rel)
        if isinstance(cond, exp.And):
            return self._null_rejected(cond.left, rel) | self._null_rejected(cond.right, rel)
        if isinstance(cond, exp.Or):
            return self._null_rejected(cond.left, rel) & self._null_rejected(cond.right, rel)
        if isinstance(cond, exp.Not):
            inner = cond.this.this if isinstance(cond.this, exp.Paren) else cond.this
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                return self._strict_columns(inner.this, rel)
            return set()
        if isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)):
            return self._strict_columns(cond.left, rel) | self._strict_columns(cond.right, rel)
        if isinstance(cond, exp.Between):
            return self._strict_columns(cond.this, rel) | self._strict_columns(cond.args["low"], rel) | self._strict_columns(cond.args["high"], rel)
        if isinstance(cond, exp.In) and not cond.args.get("query") and not cond.args.get("unnest"):
            return self._strict_columns(cond.this, rel)
        return set()

    def _is_null_tests(self, cond: exp.Expression, rel: _Rel) -> set:
        """Columns a false ``cond`` proves non-NULL: ``col IS NULL`` (alone or in an OR)."""

        if isinstance(cond, exp.Paren):
            return self._is_null_tests(cond.this, rel)
        if isinstance(cond, exp.Or):
            return self._is_null_tests(cond.left, rel) | self._is_null_tests(cond.right, rel)
        if isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null):
            q = self._resolve(cond.this.this if isinstance(cond.this, exp.Paren) else cond.this, rel.cols)
            return {q} if q else set()
        return set()

    def _strict_columns(self, node: exp.Expression, rel: _Rel) -> set:
        """Columns whose NULL would make ``node`` NULL (a bare column or arithmetic on columns)."""

        if isinstance(node, exp.Paren):
            return self._strict_columns(node.this, rel)
        if isinstance(node, exp.Column):
            q = self._resolve(node, rel.cols)
            return {q} if q else set()
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return self._strict_columns(node.left, rel) | self._strict_columns(node.right, rel)
        if isinstance(node, exp.Neg):
            return self._strict_columns(node.this, rel)
        if isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast):
            return self._strict_columns(node.this, rel)
        return set()

    # ----- projection

    def _canon(self, node: exp.Expression, rel: _Rel) -> str:
        """Identity of an expression for matching GROUP BY items with select items."""

        if isinstance(node, exp.Paren):
            node = node.this
        return self._resolve(node, rel.cols) or node.sql(dialect=self.dialect).lower()

    def _project(self, select: exp.Select, rel: _Rel, scope: dict) -> _Rel:
        group = select.args.get("group")
        sets = self._grouping_sets(group, rel) if group is not None else None
        grouped = bool(group and group.expressions) if sets is None else True
        has_agg = any(select_has_aggregate(e) for e in select.expressions) or bool(select.args.get("having"))
        global_agg = has_agg and not grouped
        unrolled = rel
        if sets is not None:
            if any(isinstance(g, exp.Literal) for g in self._group_items(group)):
                raise _Unsupported("grouping sets by position")
            rel = self._rolled_up(rel, group, sets)
        if any(set_returning_item(item) for item in select.expressions):
            raise _Unsupported("set-returning function in the select list")  # several rows per input row
        projected: list[tuple[str, exp.Expression]] = []
        for item in select.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                qualifier = item.table.lower() if isinstance(item, exp.Column) else ""
                star = item if isinstance(item, exp.Star) else item.this
                if star.args.get("rename") or star.args.get("ilike"):
                    raise _Unsupported("SELECT * RENAME / ILIKE")
                dropped = star.args.get(EXCEPT_KEY) or []
                replaced = star.args.get("replace") or []
                if any(not isinstance(c, exp.Column) or c.table for c in dropped) or any(not isinstance(a, exp.Alias) for a in replaced):
                    raise _Unsupported("SELECT * EXCEPT / REPLACE of this shape")
                dropped = {c.name.lower() for c in dropped}
                replaced = {a.alias.lower(): a.this for a in replaced}
                for q in rel.order:
                    table, name = q.split(".", 1)
                    if not qualifier or table == qualifier:
                        if _base(name) in dropped:
                            continue  # * EXCEPT (name)
                        if _base(name) in replaced:
                            projected.append((name, replaced[_base(name)]))  # * REPLACE (expr AS name)
                            continue
                        column = exp.column(name, table=table)
                        column.meta[_STAR_SOURCE] = q  # the column itself, even when its name repeats
                        projected.append((name, column))
                continue
            name = item.alias_or_name.lower() if item.alias_or_name else f"f{len(projected)}_"  # BigQuery's name for it
            projected.append((name, item.this if isinstance(item, exp.Alias) else item))
        # A repeated output name keeps its position but can no longer be referenced by name.
        seen_names: set[str] = set()
        for index, (name, expr) in enumerate(projected):
            base = _base(name)
            projected[index] = (f"{base}{_HIDDEN}{index}" if base in seen_names or not base else base, expr)
            seen_names.add(base)
        out = _Rel()
        for name, expr in projected:
            non_null, prov = self._non_null(expr, rel, possibly_empty=global_agg or bool(sets and any(not s for s in sets)))
            out.cols[f".{name}"] = (non_null, prov if non_null else frozenset())
            out.order.append(f".{name}")
        keys: list[tuple[frozenset, Provenance]] = []
        by_canon: dict[str, str] = {}
        for name, expr in projected:
            by_canon.setdefault(self._canon(expr, rel), f".{name}")
        if sets is not None:
            # Rows of different grouping sets differ in which grouping columns are NULL when no
            # grouped value is NULL in the input; within one set the set's columns are unique.
            members = [by_canon.get(c) for c in sorted({c for s in sets for c in s})]
            exprs = {self._canon(g, unrolled): g for g in self._group_items(group)}
            if all(members) and len(set(sets)) == len(sets) and all(self._non_null(e, unrolled)[0] for e in exprs.values()):
                keys.append((frozenset(members), frozenset().union(*(self._non_null(e, unrolled)[1] for e in exprs.values()))))
        elif grouped:
            members = []
            for g in group.expressions:
                if isinstance(g, exp.Literal) and not g.is_string and g.this.isdigit() and 1 <= int(g.this) <= len(projected):
                    members.append(f".{projected[int(g.this) - 1][0]}")
                    continue
                alias_hit = [n for n, e in projected if isinstance(g, exp.Column) and not g.table and _base(n) == g.name.lower() and self._resolve(g, rel.cols) is None]
                alias_hit = alias_hit if len(alias_hit) == 1 else []
                members.append(by_canon.get(self._canon(g, rel)) or (f".{alias_hit[0]}" if alias_hit else None))
            if all(members):
                keys.append((frozenset(members), frozenset()))
        elif global_agg:
            keys.append((frozenset(), frozenset()))
            out.exactly_one = not select.args.get("having")
        else:
            by_col: dict[str, str] = {}
            for name, expr in projected:
                q = self._resolve(expr, rel.cols)
                if q:
                    by_col.setdefault(q, f".{name}")
            for key, prov in rel.keys:
                chosen = []
                for c in key:
                    hit = next((by_col[e] for e in sorted(rel.equal_to(c)) if e in by_col), None)
                    if hit is None:
                        break
                    chosen.append(hit)
                else:
                    keys.append((frozenset(chosen), prov))
            out.exactly_one = rel.exactly_one
        if not grouped and not global_agg:
            for name, expr in projected:
                if isinstance(expr, exp.Window) and isinstance(expr.this, exp.RowNumber) and not expr.args.get("partition_by"):
                    keys.append((frozenset({f".{name}"}), frozenset()))  # 1..n, no repeats
        distinct = select.args.get("distinct")
        if isinstance(distinct, exp.Distinct) and not distinct.args.get("on"):
            keys.append((frozenset(out.order), frozenset()))
        limit = select.args.get("limit")
        if limit is not None:
            value = limit.expression
            count = int(value.this) if isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit() else None
            if count is not None and count <= 1:
                keys.append((frozenset(), frozenset()))
            if count is None or count < 1:
                out.exactly_one = False
        if select.args.get("offset") or select.args.get("qualify"):
            out.exactly_one = False
        out.keys = _prune_keys(keys)
        return out

    def _group_items(self, group: exp.Group) -> list:
        items = []
        for node in [*group.expressions, *(group.args.get("grouping_sets") or []), *(group.args.get("rollup") or []), *(group.args.get("cube") or [])]:
            if isinstance(node, (exp.Rollup, exp.Cube, exp.GroupingSets)):
                for element in node.expressions:
                    items += element.expressions if isinstance(element, exp.Tuple) else [element.this if isinstance(element, exp.Paren) else element]
            else:
                items.append(node)
        return items

    def _grouping_sets(self, group: exp.Group, rel: _Rel) -> list[frozenset] | None:
        """The grouping sets of ``GROUP BY`` (as sets of canonical expressions), or None for a plain one."""

        if group.args.get("totals") or group.args.get("all"):
            raise _Unsupported("GROUP BY ALL / WITH TOTALS")
        plain = [g for g in group.expressions if not isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets))]
        factors = [n for n in [*group.expressions, *(group.args.get("grouping_sets") or []), *(group.args.get("rollup") or []), *(group.args.get("cube") or [])] if isinstance(n, (exp.Rollup, exp.Cube, exp.GroupingSets))]
        if not factors:
            return None
        canon = lambda node: self._canon(node.this if isinstance(node, exp.Paren) else node, rel)  # noqa: E731
        sets = [frozenset(canon(g) for g in plain)]
        for factor in factors:
            elements = [list(e.expressions) if isinstance(e, exp.Tuple) else [e] for e in factor.expressions]
            if any(isinstance(x, (exp.Rollup, exp.Cube, exp.GroupingSets)) for e in elements for x in e):
                raise _Unsupported("nested grouping sets")
            if isinstance(factor, exp.Rollup) and not elements:  # MySQL WITH ROLLUP: over the plain list
                if len(factors) > 1:
                    raise _Unsupported("WITH ROLLUP combined with grouping sets")
                elements = [[g] for g in plain]
                sets = [frozenset()]
            if isinstance(factor, exp.Rollup):
                options = [[x for e in elements[:i] for x in e] for i in range(len(elements) + 1)]
            elif isinstance(factor, exp.Cube):
                if len(elements) > 12:
                    raise _Unsupported("too many grouping sets")
                options = [[x for i, e in enumerate(elements) if mask >> i & 1 for x in e] for mask in range(1 << len(elements))]
            else:
                options = elements
            sets = [s | frozenset(canon(x) for x in option) for s in sets for option in options]
            if len(sets) > _MAX_GROUPING_SETS:
                raise _Unsupported("too many grouping sets")
        return sets

    def _rolled_up(self, rel: _Rel, group: exp.Group, sets: list[frozenset]) -> _Rel:
        """The grouped input as the select list sees it: a column grouped in some sets but not all is NULL in the others."""

        out = _Rel(cols=dict(rel.cols), keys=list(rel.keys), order=list(rel.order), revert=dict(rel.revert))
        common = frozenset.intersection(*sets)
        partial: set[str] = set()
        for item in self._group_items(group):
            if self._canon(item, rel) in common:
                continue
            columns = list(item.find_all(exp.Column))
            resolved = {self._resolve(c, rel.cols) for c in columns}
            if not columns or None in resolved:
                partial = set(rel.cols)  # an output alias or position: give up on every column
                break
            partial |= resolved
        for q in partial:
            out.cols[q] = (False, frozenset())
        return out

    def _non_null(self, node: exp.Expression, rel: _Rel, possibly_empty: bool = False) -> tuple[bool, Provenance]:
        """Whether an expression is never NULL, and the declared facts that say so."""

        none = (False, frozenset())

        def combine(*args) -> tuple[bool, Provenance]:
            parts = [go(a) for a in args if a is not None]
            return (True, frozenset().union(*(p for _, p in parts))) if parts and all(ok for ok, _ in parts) else none

        def all_args(n: exp.Expression) -> tuple[bool, Provenance]:
            # Every argument, not only this/expressions: SUBSTR's start, REPLACE's replacement and
            # ROUND's decimals make the result NULL too.
            args = [v for value in n.args.values() for v in (value if isinstance(value, list) else [value]) if isinstance(v, exp.Expression)]
            return combine(*args) if args else none

        def under(tested: set, node: exp.Expression) -> tuple[bool, Provenance]:
            """``node`` where each tested column is known not to be NULL (an earlier ``col IS NULL`` was false)."""

            saved = {q: rel.cols[q] for q in tested if q in rel.cols}
            for q in saved:
                rel.cols[q] = (True, rel.cols[q][1])
            try:
                return go(node)
            finally:
                rel.cols.update(saved)

        def go(n: exp.Expression) -> tuple[bool, Provenance]:
            if isinstance(n, (exp.Paren, exp.Alias)):
                return go(n.this)
            if isinstance(n, exp.Null):
                return none
            if isinstance(n, (exp.Literal, exp.Boolean, exp.Var, exp.DataType)):
                return True, frozenset()
            if isinstance(n, exp.Column):
                q = self._resolve(n, rel.cols)
                return rel.cols[q] if q else none
            if isinstance(n, exp.Count):
                return True, frozenset()
            if isinstance(n, _NULL_ON_EMPTY_AGGREGATES):
                return none if possibly_empty else all_args(n)
            if isinstance(n, exp.Window):
                return (True, frozenset()) if isinstance(n.this, (exp.RowNumber, exp.Count)) or is_call(n.this, "Rank") or is_call(n.this, "DenseRank") else none
            if isinstance(n, (exp.Is, exp.Exists)) or is_call(n, "Grouping"):
                return True, frozenset()
            if isinstance(n, exp.Coalesce):
                best = none
                for arg in [n.this, *n.expressions]:
                    ok, prov = go(arg)
                    if ok and (not best[0] or len(prov) < len(best[1])):
                        best = (True, prov)
                return best
            if isinstance(n, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.DPipe, exp.Like, exp.Neg, exp.Not)):
                return combine(n.args.get("this"), n.args.get("expression"))
            if isinstance(n, exp.Cast):
                return none if isinstance(n, exp.TryCast) else go(n.this)
            if isinstance(n, exp.Case):
                if n.args.get("default") is None or n.this is not None:
                    return none
                parts, seen_null = [], set()
                for branch in n.args["ifs"]:
                    parts.append(under(seen_null, branch.args["true"]))
                    seen_null |= self._is_null_tests(branch.this, rel)
                parts.append(under(seen_null, n.args["default"]))
                return (True, frozenset().union(*(p for _, p in parts))) if all(ok for ok, _ in parts) else none
            if isinstance(n, exp.If):
                if n.args.get("false") is None:
                    return none
                tested = self._is_null_tests(n.this, rel)
                return (lambda a, b: (True, a[1] | b[1]) if a[0] and b[0] else none)(go(n.args["true"]), under(tested, n.args["false"]))
            if isinstance(n, exp.Subquery):
                try:
                    inner = self.query_rel(n.this)
                except _Unsupported:
                    return none
                return inner.cols[inner.order[0]] if len(inner.order) == 1 and inner.exactly_one else none
            if isinstance(n, (exp.Anonymous, exp.Func)):
                name = (n.name if isinstance(n, exp.Anonymous) else n.sql_name()).upper()
                if name in _NULL_PROPAGATING:
                    return all_args(n)
                if name in _ALWAYS_NON_NULL:
                    return True, frozenset()
            return none

        return go(node)


def _requalify(rel: _Rel, alias: str, names: list[str] | None = None) -> _Rel:
    """``rel`` under a FROM alias, its columns renamed by ``AS alias(a, b, ...)`` when given."""

    out = _Rel(exactly_one=rel.exactly_one)
    if names and len(names) != len(rel.order):
        raise _Unsupported("column alias list of a different width")
    mapping = {q: f"{alias}.{names[i] if names else q.split('.', 1)[1]}" for i, q in enumerate(rel.order)}
    if len(set(mapping.values())) != len(mapping):
        raise _Unsupported("repeated column alias")
    out.order = [mapping[q] for q in rel.order]
    out.cols = {mapping[q]: v for q, v in rel.cols.items()}
    out.keys = [(frozenset(mapping[c] for c in key), prov) for key, prov in rel.keys]
    out.same = [{mapping[c] for c in g if c in mapping} for g in rel.same]
    return out


def _base(name: str) -> str:
    """An output name without the marker that keeps repeated names apart."""

    return name.split(_HIDDEN, 1)[0]


def _alias_columns(node: exp.Expression) -> list[str]:
    alias = node.args.get("alias")
    return [c.name.lower() for c in alias.columns] if isinstance(alias, exp.TableAlias) and alias.columns else []


def _literal_value(node: exp.Expression):
    """A comparable value for a plain literal, or None when equality with other cells is not obvious."""

    from decimal import Decimal, InvalidOperation

    negate = False
    while isinstance(node, (exp.Paren, exp.Neg)):
        negate ^= isinstance(node, exp.Neg)
        node = node.this
    if isinstance(node, exp.Literal) and not node.is_string:
        if not re.fullmatch(r"\d{1,15}(\.\d{0,15})?", node.this) or len(node.this.replace(".", "")) > 15:
            return None  # long or exponent literals may round to the same float
        try:
            value = Decimal(node.this)
        except InvalidOperation:
            return None
        return ("number", -value if negate else value) if value.is_finite() else None
    if negate:
        return None
    if isinstance(node, exp.Literal) and node.this.isascii() and node.this.isalnum():
        # case and trailing spaces are ignored by some collations, so compare without them
        return ("string", node.this.casefold())
    if isinstance(node, exp.Boolean):
        return ("boolean", bool(node.this))
    return None


def _all_distinct(values: list) -> bool:
    """Every value is a known literal of one kind and no two are equal."""

    if any(v is None for v in values):
        return False
    kinds = {tuple(c[0] for c in v) if isinstance(v[0], tuple) else v[0] for v in values}
    return len(kinds) == 1 and len(set(values)) == len(values)


def _eq_pair(cond: exp.Expression):
    return (cond.left, cond.right) if isinstance(cond, exp.EQ) else None


def _is_outer_or_const(node: exp.Expression) -> bool:
    return not node.find(exp.Subquery) and not node.find(exp.Window)


def _covers_key(keys: list, bound: set) -> bool:
    return any(key <= bound for key, _ in keys)


def _prov_of(keys: list, bound: set) -> Provenance:
    for key, prov in keys:
        if key <= bound:
            return prov
    return frozenset()


def _prune_keys(keys: list) -> list:
    """Drop keys that contain another key (they say less), keep the cheapest provenance of equal keys."""

    best: dict[frozenset, Provenance] = {}
    for key, prov in keys:
        if key not in best or len(prov) < len(best[key]):
            best[key] = prov
    kept = [(k, p) for k, p in best.items() if not any(o < k for o in best)]
    return kept


def select_has_aggregate(node: exp.Expression) -> bool:
    """Whether the select item aggregates its own rows (not inside a window or a subquery)."""

    for agg in node.find_all(exp.AggFunc):
        if agg is node:
            return True
        owner = agg.parent
        while owner is not None:
            if isinstance(owner, (exp.Window, exp.Subquery)):
                break
            if owner is node:
                return True
            owner = owner.parent
    return False


def set_returning_item(node: exp.Expression) -> bool:
    """Whether a select item can return several rows per input row (DuckDB's ``SELECT UNNEST(list)``,
    Postgres's ``SELECT generate_series(...)``); not inside a subquery, nor ``x IN UNNEST(array)``."""

    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Query):
            continue
        if isinstance(current, (exp.UDTF, exp.ExplodingGenerateSeries)):
            return True
        unnest = current.args.get("unnest") if isinstance(current, exp.In) else None
        stack.extend(child for child in current.iter_expressions() if child is not unnest)
    return False


def infer_properties(
    sql: str,
    constraints: Mapping[str, TableConstraints] | None = None,
    schema: Mapping[str, list[str]] | None = None,
    dialect: str = "bigquery",
) -> OutputProperties:
    """Proven output properties of ``sql``; unsupported shapes return ``OutputProperties(unsupported=...)``."""

    analyzer = _Analyzer(constraints or {}, schema or {}, dialect)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        rel = analyzer.query_rel(tree)
    except (_Unsupported, sqlglot.errors.SqlglotError) as error:
        return OutputProperties(unsupported=str(error))
    position = {q: i for i, q in enumerate(rel.order)}
    shown = {q: _base(q.split(".", 1)[1]) or f"f{i}_" for q, i in position.items()}
    columns = tuple(ColumnFact(shown[q], rel.cols[q][0], tuple(sorted(rel.cols[q][1]))) for q in rel.order)
    keys = tuple(
        UniqueKey(tuple(sorted(shown[c] for c in key)), tuple(sorted(prov)), tuple(sorted(position[c] for c in key)))
        for key, prov in sorted(rel.keys, key=lambda kp: (len(kp[0]), sorted(position[c] for c in kp[0])))
    )
    return OutputProperties(columns=columns, keys=keys, exactly_one_row=rel.exactly_one)
