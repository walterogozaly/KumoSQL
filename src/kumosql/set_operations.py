"""Rewrite BigQuery ``BY NAME`` / ``CORRESPONDING`` set operations into positional ones.

``SELECT a, b FROM x UNION ALL BY NAME SELECT b, a FROM y`` matches columns by name, but
column tracing, pruning and most rewrites assume position. :func:`positionalize` projects
every branch to the output column list, in order, with ``NULL AS col`` for a column a
branch does not have, so everything downstream can keep reading set operations by position.

It works on a qualified query (stars already expanded). When a branch's columns are not
known (an unexpanded star, duplicate names) or the combination is an error in BigQuery,
it leaves that operation untouched and reports it, so callers treat the columns as unknown
instead of tracing them to the wrong place.
"""

from __future__ import annotations

from sqlglot import exp


def is_by_name(node: exp.Expression) -> bool:
    return isinstance(node, exp.SetOperation) and bool(node.args.get("by_name") or node.args.get("on"))


def positionalize(query: exp.Expression) -> tuple[exp.Expression, list[str]]:
    """The query with by-name set operations made positional, plus a problem per one left alone."""

    problems: list[str] = []
    counter = [0]

    def visit(node: exp.Expression) -> exp.Expression:
        # Children first: ``(A BY NAME B) BY NAME C`` matches C against the output of the inner one.
        for child in list(node.args.values()):
            for item in child if isinstance(child, list) else [child]:
                if isinstance(item, exp.Expression):
                    visit(item)
        if is_by_name(node):
            problem = _rewrite(node, counter)
            if problem:
                problems.append(problem)
        return node

    return visit(query), problems


def _names(branch: exp.Expression) -> list[str] | None:
    while isinstance(branch, exp.Subquery):
        branch = branch.this
    names = [name.lower() for name in branch.named_selects]
    if not names or "*" in names or len(set(names)) != len(names):
        return None
    if isinstance(branch, exp.Select) and len(branch.expressions) != len(names):
        return None
    return names


def _branches(node: exp.Expression) -> list[exp.Expression]:
    return [node.this, node.expression]


def _rewrite(node: exp.SetOperation, counter: list[int]) -> str | None:
    left, right = _branches(node)
    left_names, right_names = _names(left), _names(right)
    if left_names is None or right_names is None:
        return "columns of a BY NAME branch are not known"
    on = node.args.get("on")
    if on:
        output = [c.name.lower() for c in on if isinstance(c, exp.Column)]
        if len(output) != len(on) or any(n not in left_names or n not in right_names for n in output):
            return "CORRESPONDING BY names a column a branch does not have"
    else:
        side = (node.args.get("side") or "").upper()
        kind = (node.args.get("kind") or "").upper()
        if side == "FULL":
            output = left_names + [n for n in right_names if n not in left_names]
        elif side == "LEFT":
            output = list(left_names)
        elif kind == "INNER":
            output = [n for n in left_names if n in right_names]
        else:
            if set(left_names) != set(right_names):
                return "BY NAME branches do not have the same columns"
            output = list(left_names)
    if not output:
        return "BY NAME branches have no column in common"
    node.set("this", _project(left, left_names, output, counter))
    node.set("expression", _project(right, right_names, output, counter))
    for key in ("by_name", "side", "kind", "on"):
        node.set(key, None)
    return None


def _project(branch: exp.Expression, names: list[str], output: list[str], counter: list[int]) -> exp.Expression:
    inner = branch.this if isinstance(branch, exp.Subquery) and isinstance(branch.this, exp.Select) else branch
    if (
        isinstance(inner, exp.Select)
        and all(not isinstance(e, exp.Star) for e in inner.expressions)
        and not _ordinal_keys(inner)
        and not (any(name not in output for name in names) and _reads_dropped_columns(inner))
    ):
        by_name = {name: e for name, e in zip(names, inner.expressions)}
        if names == output:
            return branch
        # Same SELECT, reordered; a column the branch lacks is a constant NULL.
        # Window/aggregate/ORDER BY/LIMIT stay valid because only the select list changes
        # (an ``ORDER BY 1`` or ``GROUP BY 1`` would not, and neither would dropping a column that
        # DISTINCT, an alias in ORDER BY / QUALIFY / HAVING or a LIMIT depends on: such a branch is wrapped below).
        projections = []
        for name in output:
            item = by_name.get(name)
            if item is None:
                projections.append(exp.alias_(exp.Null(), name, quoted=False))
            else:
                projections.append(item)
        inner.set("expressions", projections)
        return branch
    # A set-operation, starred or ordinal-keyed branch: select from it by name.
    counter[0] += 1
    alias = f"_by_name_{counter[0]}"
    wrapped = exp.Subquery(this=branch, alias=exp.TableAlias(this=exp.to_identifier(alias)))
    projections = []
    for name in output:
        if name in names:
            projections.append(exp.column(name, table=alias))
        else:
            projections.append(exp.alias_(exp.Null(), name, quoted=False))
    return exp.Subquery(this=exp.select(*projections).from_(wrapped))


def _reads_dropped_columns(select: exp.Select) -> bool:
    """Whether a column of the select list may be dropped without changing which rows the select returns.

    ``SELECT DISTINCT a, b`` keeps one row per pair, so dropping ``b`` from the list afterwards is not
    ``SELECT DISTINCT a``; a name in ORDER BY, QUALIFY, HAVING or GROUP BY may be one of the aliases dropped.
    """

    return any(select.args.get(k) for k in ("distinct", "group", "having", "qualify", "order", "limit", "offset", "windows"))


def _ordinal_keys(select: exp.Select) -> bool:
    """Whether the SELECT's ORDER BY or GROUP BY names an output column by its position."""

    keys = []
    order, group = select.args.get("order"), select.args.get("group")
    if order is not None:
        keys += [o.this if isinstance(o, exp.Ordered) else o for o in order.expressions]
    if group is not None:
        keys += list(group.expressions)
    return any(isinstance(k, exp.Literal) and not k.is_string for k in keys)


def positional_sql_pair(left_sql: str, right_sql: str, dialect: str = "bigquery") -> tuple[str, str, str | None]:
    """Both queries with by-name set operations made positional, or the reason one cannot be.

    Queries without such operations come back unchanged, so provers call this on every pair.
    """

    import sqlglot

    from .ast_utils import UnmodeledConstruct, faithful_sql

    out = []
    for sql in (left_sql, right_sql):
        if "name" not in sql.lower() and "corresponding" not in sql.lower():
            out.append(sql)
            continue
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            out.append(sql)
            continue
        if not any(is_by_name(node) for node in tree.find_all(exp.SetOperation)):
            out.append(sql)
            continue
        tree, problems = positionalize(tree)
        if problems:
            return left_sql, right_sql, problems[0]
        try:
            out.append(faithful_sql(tree, dialect))
        except UnmodeledConstruct as error:
            return left_sql, right_sql, str(error)
    return out[0], out[1], None
