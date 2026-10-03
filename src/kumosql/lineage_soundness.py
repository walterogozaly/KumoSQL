"""Where sqlglot's column tracing is not exact, make the pipeline analysis say so or trace it right.

The pipeline analysis traces each output column with ``sqlglot.lineage``. A few shapes need help so
that lineage and impact never confidently miss a dependency:

* ``condition_columns``: the source columns that decide which rows a query returns (filters, join
  keys, grouping, ``QUALIFY``, ``DISTINCT``, predicate subqueries). A change to one of them can change
  every downstream model, not only the columns computed from it.
* ``lineage_view``: a copy of a qualified query rewritten for value lineage only. A projected
  ``EXISTS`` (or a scalar subquery that projects no column, like ``COUNT(*)``) depends on the columns
  that decide whether its subquery returns rows; a key of ``JOIN ... USING`` takes its value from one
  side (left for inner and left joins, right for right joins); a field of a ``STRUCT`` built in a CTE
  comes from that field's expression only.
* ``masked_reads``: the columns a masked Dataform expression (``${when(incremental(), ...)}``) names,
  and the tables whose columns are unknown so the masked SQL cannot be resolved.

Reads (consumption) are never narrowed here: these only refine value lineage or add conditions.
"""

from __future__ import annotations

import re
from typing import Callable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

UNTRACED = "__kumosql_untraced__"
_FIELD = "__kumosql_field_{}__"
_USING = "kumosql_using"
_PSEUDO_COLUMNS = {"_table_suffix", "_partitiontime", "_partitiondate", "_file_name"}


def _set_operation_scopes(scope: Scope) -> list[Scope]:
    return list(getattr(scope, "set_operation_scopes", None) or getattr(scope, "union_scopes", None) or ())


def _nearest_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Select, exp.SetOperation)):
        parent = parent.parent
    return parent


def in_select_list(node: exp.Expression, select: exp.Expression) -> bool:
    """True when ``node`` sits in the select list of ``select`` (not in its FROM, WHERE, GROUP BY, ...)."""

    child = node
    while child.parent is not None and child.parent is not select:
        child = child.parent
    return child.parent is select and child.arg_key == "expressions"


def _in_aggregate(node: exp.Expression, select: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not select:
        if isinstance(parent, exp.AggFunc):
            return True
        parent = parent.parent
    return False


def _row_deciding_select(scope: Scope) -> bool:
    """A select whose projected values decide its rows: DISTINCT, or a branch of a set operation that compares rows."""

    select = scope.expression
    if not isinstance(select, exp.Select):
        return False
    if select.args.get("distinct"):
        return True
    parent = select.parent
    while isinstance(parent, exp.Subquery):
        parent = parent.parent
    if isinstance(parent, (exp.Intersect, exp.Except)):
        return True
    return isinstance(parent, exp.Union) and bool(parent.args.get("distinct"))


def _projected_subquery(scope: Scope) -> bool:
    """A subquery in its parent's select list (a scalar subquery or a projected EXISTS), not a predicate."""

    if scope.parent is None:
        return False
    outer = scope.parent.expression
    return isinstance(outer, exp.Select) and in_select_list(scope.expression, outer)


def condition_columns(query: exp.Expression, owner: Callable[[exp.Table], str]) -> set | None:
    """Source columns that decide which rows ``query`` returns, or ``None`` when that cannot be worked out.

    ``owner`` names the table a column belongs to. ``None`` means callers must treat every column the
    query reads as one that can change its rows.
    """

    from .pipeline_types import ColumnRef

    try:
        scopes = traverse_scope(query)
    except Exception:
        return None
    resolver = _Resolver(owner, ColumnRef)
    found: set = set()
    for scope in scopes:
        expression = scope.expression
        if scope.is_subquery and not _projected_subquery(scope):
            every = True  # a predicate subquery: everything it reads decides the outer rows
        else:
            every = _row_deciding_select(scope)
        grouped = isinstance(expression, exp.Select) and expression.args.get("group") is not None
        for column in scope.columns:
            if not every and in_select_list(column, expression):
                if not (grouped and not _in_aggregate(column, expression)):
                    continue  # a value: tracing follows it
            refs = resolver.column(scope, column)
            if refs is None:
                return None
            found |= refs
    return found


class _Resolver:
    """Resolve a column of a scope to the source table columns it is computed from."""

    def __init__(self, owner, column_ref) -> None:
        self.owner = owner
        self.ref = column_ref
        self.memo: dict[tuple[int, str], set | None] = {}
        self.depth = 0

    def column(self, scope: Scope, column: exp.Column) -> set | None:
        name = column.name
        if name.casefold() in _PSEUDO_COLUMNS:
            return set()
        if not column.table:
            select = scope.expression
            if isinstance(select, exp.Select) and not in_select_list(column, select):
                for projection in select.expressions:
                    if projection.alias_or_name.casefold() == name.casefold():
                        return self.projection(scope, projection)  # a select-list alias named in ORDER BY, QUALIFY, ...
            return None
        current: Scope | None = scope
        while current is not None and column.table not in current.sources:
            current = current.parent
        if current is None:
            return None
        source = current.sources[column.table]
        if isinstance(source, exp.Table):
            return {self.ref(self.owner(source), name)}
        if isinstance(source, Scope):
            return self.name(source, name)
        return None

    def name(self, scope: Scope, name: str) -> set | None:
        key = (id(scope), name.casefold())
        if key in self.memo:
            return self.memo[key]
        if self.depth > 50:
            return None
        self.memo[key] = None  # a cycle resolves to unknown
        self.depth += 1
        try:
            result = self._name(scope, name)
        finally:
            self.depth -= 1
        self.memo[key] = result
        return result

    def _name(self, scope: Scope, name: str) -> set | None:
        expression = scope.expression
        if isinstance(expression, exp.Select):
            matches = [p for p in expression.expressions if p.alias_or_name.casefold() == name.casefold()]
            if len(matches) != 1:
                return None
            return self.projection(scope, matches[0])
        if isinstance(expression, exp.SetOperation):
            names = [n.casefold() for n in expression.named_selects]
            if name.casefold() not in names:
                return None
            index = names.index(name.casefold())
            found: set = set()
            for branch in _set_operation_scopes(scope):
                branch_names = branch.expression.named_selects
                if index >= len(branch_names):
                    return None
                refs = self.name(branch, branch_names[index])
                if refs is None:
                    return None
                found |= refs
            return found
        # A table-valued source such as UNNEST: every column it reads.
        found = set()
        for column in scope.columns:
            refs = self.column(scope, column)
            if refs is None:
                return None
            found |= refs
        return found

    def projection(self, scope: Scope, projection: exp.Expression) -> set | None:
        own = {id(c) for c in scope.columns}
        found: set = set()
        for column in projection.find_all(exp.Column):
            if id(column) not in own:
                return None  # inside a nested subquery: not followed here
            refs = self.column(scope, column)
            if refs is None:
                return None
            found |= refs
        return found


def mark_using_joins(query: exp.Expression) -> None:
    """Before qualifying, note on each SELECT which side's value a ``USING`` key takes.

    Qualifying turns an unqualified ``USING`` key into ``COALESCE(left.k, right.k)``; GoogleSQL takes the
    left input's value for inner and left joins and the right input's for a right join (only a full join
    coalesces).
    """

    for select in query.find_all(exp.Select):
        sides: dict[str, list[str]] = {}
        for join in select.args.get("joins") or ():
            for key in join.args.get("using") or ():
                sides.setdefault(key.name.casefold(), []).append((join.side or "").upper())
        choice = {}
        for name, used in sides.items():
            if all(side in ("", "LEFT") for side in used):
                choice[name] = "left"
            elif used == ["RIGHT"]:
                choice[name] = "right"
        if choice:
            select.meta[_USING] = choice


def pin_single_source_columns(query: exp.Expression, columns_of: Callable[[exp.Table], list]) -> None:
    """Before qualifying, qualify bare columns of a SELECT that reads one table whose columns are unknown.

    With some schemas known, sqlglot treats a table it has no schema for as having no columns: a bare
    column then stays unqualified, and one that matches a select-list alias is replaced by the aliased
    expression (``SELECT a AS x, x + 1 AS y`` reads ``a``; ``WHERE x`` reads ``a``). GoogleSQL has no
    lateral aliases and no aliases in WHERE, so such a column belongs to the one table read. Aliases stay
    visible in GROUP BY, HAVING, QUALIFY and ORDER BY, so a bare name there that is an alias is left alone.
    In a subquery, a bare column that an outer table with known columns has is left to the qualifier.
    """

    try:
        scopes = traverse_scope(query)
    except Exception:
        return
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select) or select.args.get("joins"):
            continue
        tables = list(scope.sources.values())
        if len(tables) != 1 or not isinstance(tables[0], exp.Table) or columns_of(tables[0]):
            continue
        table = tables[0]
        if table.this is None or isinstance(table.this, exp.Func):
            continue
        aliases = {p.alias.casefold() for p in select.expressions if isinstance(p, exp.Alias)}
        for column in scope.columns:
            if column.table or column.name.startswith("__sqlx_token_"):
                continue
            visible = not (in_select_list(column, select) or _in_clause(column, select, "where"))
            if visible and column.name.casefold() in aliases:
                continue  # an alias reference in GROUP BY, HAVING, QUALIFY or ORDER BY
            if scope.is_subquery and _outer_has(scope, column.name, columns_of):
                continue  # may be a correlated reference
            column.set("table", exp.to_identifier(table.alias_or_name))


def _outer_has(scope: Scope, name: str, columns_of: Callable[[exp.Table], list]) -> bool:
    current = scope.parent
    while current is not None:
        for source in current.sources.values():
            if isinstance(source, exp.Table) and name.casefold() in {c.casefold() for c in columns_of(source)}:
                return True
            if isinstance(source, Scope) and name.casefold() in {n.casefold() for n in source.expression.named_selects}:
                return True
        current = current.parent
    return False


def _in_clause(node: exp.Expression, select: exp.Expression, clause: str) -> bool:
    child = node
    while child.parent is not None and child.parent is not select:
        child = child.parent
    return child.parent is select and child.arg_key == clause


def lineage_view(qualified: exp.Expression) -> exp.Expression | None:
    """A copy of ``qualified`` rewritten for value lineage, or ``None`` when nothing needs rewriting."""

    needs_using = any(select.meta.get(_USING) for select in qualified.find_all(exp.Select))
    needs_subquery = any(_projected_predicate(node) for node in qualified.find_all(exp.Exists, exp.Subquery))
    needs_field = any(isinstance(dot.this, exp.Column) and dot.this.table for dot in qualified.find_all(exp.Dot))
    if not (needs_using or needs_subquery or needs_field):
        return None
    view = qualified.copy()
    changed = False
    if needs_using:
        changed |= _choose_using_side(view)
    if needs_subquery:
        changed |= _expose_subquery_predicates(view)
    if needs_field:
        changed |= _slice_struct_fields(view)
    return view if changed else None


def _projected_predicate(node: exp.Expression) -> bool:
    """A projected EXISTS, or a projected scalar subquery whose select list names no column."""

    select = _nearest_select(node)
    if not isinstance(select, exp.Select) or not in_select_list(node, select):
        return False
    if isinstance(node, exp.Exists):
        return True
    inner = node.this
    return (
        isinstance(node, exp.Subquery)
        and node.arg_key != "from_"
        and isinstance(inner, exp.Select)
        and not any(p.find(exp.Column) for p in inner.expressions)
        and inner.find(exp.Column) is not None
    )


def _choose_using_side(view: exp.Expression) -> bool:
    changed = False
    for select in list(view.find_all(exp.Select)):
        choice = select.meta.get(_USING)
        if not choice:
            continue
        for projection in select.expressions:
            for node in list(projection.find_all(exp.Coalesce)):
                if _nearest_select(node) is not select:
                    continue
                args = [node.this, *node.expressions]
                if len(args) < 2 or not all(isinstance(a, exp.Column) and a.table for a in args):
                    continue
                names = {a.name.casefold() for a in args}
                tables = [a.table for a in args]
                if len(names) != 1 or len(set(tables)) != len(tables):
                    continue
                side = choice.get(next(iter(names)))
                if side is None:
                    continue
                pick = args[0] if side == "left" else args[-1]
                replacement = pick.copy()
                if node is projection:
                    replacement = exp.alias_(replacement, projection.alias_or_name, quoted=False)
                node.replace(replacement)
                changed = True
    return changed


def _expose_subquery_predicates(view: exp.Expression) -> bool:
    """Replace a projected EXISTS (or column-free scalar subquery) with the columns that decide it.

    ``EXISTS (SELECT 1 FROM o WHERE o.k = s.k)`` becomes ``STRUCT(s.k, (SELECT STRUCT(o.k) FROM o WHERE
    o.k = s.k))``: the outer, correlated columns are traced in the outer query and the subquery's own
    columns through its scalar projection. A subquery with nested subqueries or set operations is
    replaced by a column that cannot be traced, so its output is reported unknown.
    """

    try:
        scopes = {id(scope.expression): scope for scope in traverse_scope(view)}
    except Exception:
        return False
    changed = False
    for node in list(view.find_all(exp.Exists, exp.Subquery)):
        if not _projected_predicate(node):
            continue
        inner = node.this
        scope = scopes.get(id(inner))
        nested = scope is not None and (scope.subquery_scopes or _set_operation_scopes(scope))
        if scope is None or not isinstance(inner, exp.Select) or nested:
            node.replace(exp.column(UNTRACED))
            changed = True
            continue
        keep_projection = inner.args.get("having") is not None or inner.args.get("qualify") is not None
        own: list[exp.Expression] = []
        outer: list[exp.Expression] = []
        for column in scope.columns:
            if not keep_projection and in_select_list(column, inner):
                continue
            if column.table and column.table in scope.sources:
                own.append(column.copy())
            elif column.table:
                outer.append(column.copy())
            else:
                own = []
                outer = [exp.column(UNTRACED)]
                break
        if not own and not outer:
            continue  # reads no column: constant as far as columns go
        parts = list(outer)
        if own:
            body = inner.copy()
            body.set("expressions", [exp.alias_(exp.Struct(expressions=own), "kumosql_decides", quoted=False)])
            for arg in ("order", "limit", "offset"):
                body.set(arg, None)
            parts.append(exp.Subquery(this=body))
        node.replace(exp.Struct(expressions=parts))
        changed = True
    return changed


def _struct_field(value: exp.Expression, field: str) -> exp.Expression | None:
    if not isinstance(value, exp.Struct):
        return None
    for item in value.expressions:
        if isinstance(item, exp.PropertyEQ):
            name, expression = item.this.name, item.expression
        elif isinstance(item, exp.Alias):
            name, expression = item.alias, item.this
        elif isinstance(item, exp.Column):
            name, expression = item.name, item
        else:
            continue
        if name.casefold() == field.casefold():
            return expression
    return None


def _slice_struct_fields(view: exp.Expression) -> bool:
    """``c.rec.a`` over ``c AS (SELECT STRUCT(v AS a, w AS b) AS rec ...)`` traces to ``v`` only.

    The field's expression is added to the CTE (or derived table) as an extra projection, and the
    reference points at it. Physical STRUCT columns of a table keep root-column lineage.
    """

    try:
        scopes = traverse_scope(view)
    except Exception:
        return False
    changed = False
    counter = 0
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        for dot in list(select.find_all(exp.Dot)):
            column = dot.this
            if not (isinstance(column, exp.Column) and column.table and isinstance(dot.expression, exp.Identifier)):
                continue
            if _nearest_select(dot) is not select or not in_select_list(dot, select):
                continue
            source = scope.sources.get(column.table)
            if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                continue
            matches = [p for p in source.expression.expressions if p.alias_or_name.casefold() == column.name.casefold()]
            if len(matches) != 1:
                continue
            field = _struct_field(matches[0].unalias(), dot.expression.name)
            if field is None:
                continue
            name = _FIELD.format(counter)
            counter += 1
            source.expression.append("expressions", exp.alias_(field.copy(), name, quoted=False))
            dot.replace(exp.column(name, table=column.table))
            changed = True
    return changed


_JS_STRING = re.compile(r"`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def masked_sql_words(text: str) -> set[str]:
    """Identifier-like words in the string literals of a masked Dataform expression (the SQL it can produce)."""

    words: set[str] = set()
    for literal in _JS_STRING.findall(text):
        words.update(word.casefold() for word in _WORD.findall(literal[1:-1]))
    return words
