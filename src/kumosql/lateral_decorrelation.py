"""Decorrelate a grouped ``CROSS JOIN LATERAL`` by pulling its equality correlations up to the join.

``o CROSS JOIN LATERAL (SELECT MAX(e.sal) AS m FROM emp AS e WHERE e.mgr = o.empno GROUP BY e.deptno) AS d``
is ``o INNER JOIN (SELECT MAX(e.sal) AS m, e.mgr AS k FROM emp AS e GROUP BY e.deptno, e.mgr) AS d ON o.empno = d.k``.

The lateral body may nest single-source selects (filter, project, ``GROUP BY``, ``DISTINCT``,
``HAVING``) whose only outer references are ``WHERE`` conjuncts ``outer.c = local.k``. Each such
conjunct is removed and ``local.k`` is passed up as an extra output, so the join tests it instead.
Write ``B(o)`` for the correlated body of outer row ``o`` and ``B'`` for the rewritten one; by
induction over the levels, ``B'`` restricted to the rows whose passed-up ``k = o.c`` is ``B(o)``
with ``k`` appended:

* filter and project commute with a test on a passed-up column;
* ``GROUP BY G``: for one outer row the correlated filter keeps the rows with ``k = o.c``. When
  ``k`` and ``o.c`` are both integers (or both dates, or both booleans) that test is exact equality,
  so ``k`` holds the one value ``o.c`` throughout those rows, and grouping them by ``G`` gives exactly
  the groups by ``(G, k)`` of all rows whose ``k`` is ``o.c``, with the same rows in each: every
  aggregate, ``HAVING`` and hidden ``GROUP BY`` key agrees. ``DISTINCT`` (``GROUP BY`` every output)
  is the same argument. Over other types ``=`` can convert ``k`` lossily (``BIGINT`` 2**53 and
  2**53 + 1 both equal ``DOUBLE`` 2**53), and grouping by ``k`` would split what the filter kept
  together, so a correlation whose types are not both known to be such is not grouped by;
* no rows meet ``k = o.c`` gives no groups on both sides (a plain ``GROUP BY`` over no rows is
  empty), and a NULL ``k`` or ``o.c`` passes neither test;
* an inner join keeps an outer row once per row of ``B'`` with ``k = o.c``, which is what
  ``CROSS JOIN LATERAL`` does, duplicates of ``o`` included, dropping outer rows whose body is empty.

Not rewritten: ``LEFT JOIN LATERAL`` (keeps empty outer rows); a global aggregate, ``GROUP BY ()``,
``ROLLUP``/``CUBE``/``GROUPING SETS`` (each returns a row on no input, the COUNT bug), and a select
without ``GROUP BY`` holding a function sqlglot does not know (it may be an aggregate);
``LIMIT``/``ORDER``/windows (they read the whole row set); any outer reference other than such an
equality in ``WHERE`` (in the select list, ``GROUP BY`` or ``HAVING``, under ``OR``, a non-equality);
joins inside the body; non-deterministic functions; and an outer query that could see the new
columns (a star, a ``NATURAL``/``USING`` join after the lateral, the lateral's alias read as a row).
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import extended_grouping
from .cast_rules import expression_type

_BLOCKING = (
    "limit", "offset", "fetch", "order", "qualify", "windows", "with_", "with", "laterals", "joins", "into", "locks", "sample", "prewhere", "connect", "match",
    "pivots", "kind", "exclude", "distribute", "sort", "cluster", "settings", "format", "options", "for_", "operation_modifiers",
)  # kind: SELECT AS STRUCT / AS VALUE would fold a new output into its one value
_VOLATILE = {"RAND", "RANDOM", "UUID", "NEWID", "GEN_RANDOM_UUID", "GENERATE_UUID", "NEXTVAL", "CURRVAL", "SETSEED", "UUIDV4", "UUIDV7"}
# a correlation k = o.c over these kinds is exact equality, so the rows it keeps share one value of k
_EXACT_KINDS = {"int", "date", "bool"}
# function forms that can aggregate: in a select without GROUP BY they make it a global aggregate
_MAYBE_AGGREGATE = (exp.AggFunc, exp.Anonymous, exp.List, exp.IgnoreNulls, exp.RespectNulls, exp.WithinGroup, exp.Filter)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _and_all(parts: list[exp.Expression]) -> exp.Expression:
    out = None
    for part in parts:
        part = exp.Paren(this=part) if isinstance(part, exp.Or) else part
        out = part if out is None else exp.And(this=out, expression=part)
    return out


def _plain_alias(node: exp.Expression) -> str | None:
    alias = node.args.get("alias")
    if alias is None or alias.args.get("columns"):
        return None if alias is not None else (node.name.lower() if isinstance(node, exp.Table) else None)
    return node.alias.lower() or None


def _fresh(select: exp.Select) -> str:
    taken = {e.alias_or_name.lower() for e in select.expressions}
    index = 0
    while f"kumosql_lat{index}" in taken:
        index += 1
    return f"kumosql_lat{index}"


def _has_star(select: exp.Select) -> bool:
    return any(isinstance(e.unalias(), exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions)


class _Context:
    """What one decorrelation reads: the outer select, its FROM names, the schema's columns and types."""

    def __init__(self, outer_select: exp.Select, outer: set[str], columns: dict[str, set[str]], types: dict, dialect: str):
        self.outer_select, self.outer, self.columns, self.types, self.dialect = outer_select, outer, columns, types, dialect

    def exact(self, outer_column: exp.Column, local: exp.Column, select: exp.Select) -> bool:
        """``outer_column = local`` is exact equality: both are integers, both dates or both booleans."""

        if not self.types:
            return False
        a = expression_type(outer_column, self.outer_select, self.types, dialect=self.dialect)
        b = expression_type(local, select, self.types, dialect=self.dialect)
        return a is not None and b is not None and a[0] == b[0] and a[0] in _EXACT_KINDS


def _pull(select: exp.Expression, context: _Context) -> list[tuple[exp.Column, str, bool]] | None:
    """Make ``select`` uncorrelated in place; return each (outer column, output name, exact) it must equal."""

    outer = context.outer
    if not isinstance(select, exp.Select) or any(select.args.get(k) for k in _BLOCKING):
        return None
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    group = select.args.get("group")
    if group is not None and (
        extended_grouping(group) or group.args.get("all") or not group.expressions or any(isinstance(g, (exp.Tuple, exp.Paren)) for g in group.expressions)
    ):
        return None  # GROUP BY () / ROLLUP return a row on no input; GROUP BY ALL would group by the new column itself
    if _has_star(select):
        return None
    names = [e.alias_or_name.lower() for e in select.expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ is not None else None
    local = _plain_alias(source) if isinstance(source, (exp.Subquery, exp.Table)) else None
    if local is None or local in outer:
        return None
    passed: list[tuple[exp.Column, exp.Expression, bool]] = []
    if isinstance(source, exp.Subquery):
        inherited = _pull(source.this, context)
        if inherited is None:
            return None
        passed += [(o, exp.column(name, table=source.alias), exact) for o, name, exact in inherited]
    elif not isinstance(source.this, exp.Identifier) or source.args.get("joins") or source.args.get("laterals") or source.args.get("pivots"):
        return None
    # An unqualified column is the source's own only when the schema says the base table has it.
    known = context.columns.get(".".join(p.name for p in source.parts).lower(), set()) if isinstance(source, exp.Table) else set()

    def is_local(column: exp.Column) -> bool:
        return column.table.lower() == local or not column.table and column.name.lower() in known

    # The select's own clauses: no nested scopes, only columns of the source or qualified ones of the outer query.
    where = select.args.get("where")
    own = [select.args.get(k) for k in ("where", "group", "having", "distinct")] + list(select.expressions)
    for clause in own:
        if clause is None:
            continue
        for node in clause.walk():
            if isinstance(node, (exp.Subquery, exp.Select, exp.Exists, exp.Window, exp.Lateral, exp.Rand, exp.Uuid)):
                return None
            if isinstance(node, exp.Anonymous) and node.name.upper() in _VOLATILE:
                return None
            if isinstance(node, exp.Column):
                if isinstance(node.this, exp.Star) or node.args.get("db") or node.args.get("catalog"):
                    return None  # e.* reads a whole row, a.b.c may be a struct field: not read here
                table = node.table.lower()
                if not is_local(node) and table not in outer:
                    return None
                if table in outer and not (where is not None and node.find_ancestor(exp.Where) is where):
                    return None  # an outer reference in the select list, GROUP BY or HAVING
    kept = []
    for part in _conjuncts(where.this if where is not None else None):
        if not any(c.table.lower() in outer for c in part.find_all(exp.Column)):
            kept.append(part)
            continue
        if not isinstance(part, exp.EQ):
            return None
        sides = [(part.left, part.right), (part.right, part.left)]
        match = next(((o, k) for o, k in sides if isinstance(o, exp.Column) and o.table.lower() in outer and isinstance(k, exp.Column) and is_local(k)), None)
        if match is None:
            return None
        passed.append((match[0], match[1], context.exact(match[0], match[1], select)))
    if not passed:
        return []
    if group is None and (select.args.get("having") is not None or any(isinstance(n, _MAYBE_AGGREGATE) for clause in own if clause is not None for n in clause.walk())):
        return None  # a global aggregate returns a row on empty input
    if (group is not None or distinct is not None) and not all(exact for _, _, exact in passed):
        return None  # grouping by a k that = may convert lossily could split a correlated group
    select.set("where", exp.Where(this=_and_all(kept)) if kept else None)
    out = []
    for o, value, exact in passed:
        name = _fresh(select)
        select.append("expressions", exp.alias_(value.copy(), name))
        if group is not None and value.sql() not in {g.sql() for g in group.expressions}:
            group.append("expressions", value.copy())
        out.append((o, name, exact))
    return out


def decorrelate_grouped_lateral(
    select: exp.Select,
    schema: dict[str, list[str]] | None = None,
    types: dict[str, dict[str, str]] | None = None,
    dialect: str = "bigquery",
    ctes: frozenset[str] = frozenset(),
) -> exp.Expression | None:
    """``x CROSS JOIN LATERAL (..) AS d`` with only equality correlations is an inner join (module docstring)."""

    joins = select.args.get("joins") or []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or not joins or _has_star(select):
        return None
    columns = {t.lower(): {c.lower() for c in cs} for t, cs in (schema or {}).items() if t.lower() not in ctes}
    types = {t.lower(): {c.lower(): v for c, v in cs.items()} for t, cs in (types or {}).items() if t.lower() not in ctes}
    for index, join in enumerate(joins):
        lateral = join.this
        if not isinstance(lateral, exp.Lateral) or lateral.args.get("view") or lateral.args.get("outer") or lateral.args.get("ordinality") or not isinstance(lateral.this, exp.Subquery):
            continue
        if lateral.args.get("cross_apply") is False:
            continue  # OUTER APPLY keeps an outer row whose body is empty, as LEFT JOIN LATERAL does
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER", "CROSS") or any(join.args.get(k) for k in ("using", "method", "match_condition", "expressions", "pivots")):
            continue
        on = join.args.get("on")
        if (join.args.get("kind") or "").upper() == "INNER" and on is None:
            continue
        alias = _plain_alias(lateral)
        if alias is None:
            continue
        # the new outputs must stay unseen: no NATURAL or USING join after it, no read of the alias as a row
        if any(j.args.get("using") or j.args.get("method") for j in joins[index + 1 :]):
            continue
        inside = {id(n) for n in lateral.walk()}
        if any(id(c) not in inside and not c.table and c.name.lower() == alias for c in select.find_all(exp.Column)):
            continue
        outer = set()
        for item in [from_.this] + [j.this for j in joins[:index]]:
            name = (item.alias_or_name or "").lower()
            if not name:
                break
            outer.add(name)
        else:
            if alias in outer:
                continue
            body = lateral.this.this.copy()
            passed = _pull(body, _Context(select, outer, columns, types, dialect))
            if not passed:
                continue
            conditions = [exp.EQ(this=o.copy(), expression=exp.column(name, table=lateral.alias)) for o, name, _ in passed]
            if on is not None and not (isinstance(on, exp.Boolean) and on.this):
                conditions = _conjuncts(on.copy()) + conditions
            copy = select.copy()
            target = copy.args["joins"][index]
            target.set("this", exp.Subquery(this=body, alias=lateral.args["alias"].copy()))
            target.set("kind", "INNER")
            target.set("on", _and_all(conditions))
            return copy
    return None


def decorrelate_laterals(tree: exp.Expression, schema: dict[str, list[str]] | None = None, types: dict[str, dict[str, str]] | None = None, dialect: str = "bigquery") -> exp.Expression:
    """:func:`decorrelate_grouped_lateral` on every select, innermost first, before other rules reshape the body."""

    ctes = frozenset(cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE))
    for select in list(tree.find_all(exp.Select))[::-1]:
        rewritten = decorrelate_grouped_lateral(select, schema, types, dialect, ctes)
        if rewritten is None:
            continue
        if select is tree:
            tree = rewritten
        else:
            select.replace(rewritten)
    return tree
