"""Uncorrelated scalar subqueries as shared values.

``x = (SELECT MAX(r) FROM t)`` reads one value that does not depend on the outer row. When both
queries contain subqueries that return the same value (the same text, or a proof that the
two subqueries are equivalent) they are replaced by one placeholder function call, so the
solver treats them as the same unknown value and the rest of the comparison goes on.

A subquery is replaced only when every column in it is bound inside the subquery itself;
a column that might come from the outer query (or that cannot be resolved without a schema)
leaves it alone, because the same text could then mean different values on each side.
The placeholder stands for the single value the subquery returns (NULL when it returns no
row), so a proof assumes each such subquery returns at most one row.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

ASSUMPTION = "uncorrelated scalar subqueries return at most one row"
MAX_PROOFS = 8
PLACEHOLDER = "kumosql_scalar"


def _select_names(select: exp.Expression) -> list[str] | None:
    while isinstance(select, exp.Subquery):
        select = select.this
    if not isinstance(select, exp.Select):
        return None
    names = []
    for item in select.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        name = item.alias_or_name
        if not name:
            return None
        names.append(name.lower())
    return names


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_") or select.args.get("from")
    found = [from_.this] if from_ is not None else []
    found += [join.this for join in select.args.get("joins") or []]
    return found


def _table_key(table: exp.Table) -> str:
    return ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None).lower()


def _has_column(source: exp.Expression, name: str, schema: dict[str, list[str]] | None) -> bool | None:
    """True/False when the source's columns are known, ``None`` when they are not."""

    if isinstance(source, exp.Table):
        known = (schema or {}).get(_table_key(source))
        return None if known is None else name in known
    if isinstance(source, exp.Subquery):
        names = _select_names(source.this)
        return None if names is None else name in names
    return None


def is_uncorrelated(subquery: exp.Subquery, schema: dict[str, list[str]] | None) -> bool:
    """Whether every column reference in ``subquery`` is bound by a source inside it."""

    schema = {key.lower(): [c.lower() for c in cols] for key, cols in (schema or {}).items()}
    root = subquery.this
    if not isinstance(root, exp.Select):
        return False
    if any(root.find(kind) for kind in (exp.Window, exp.Lateral, exp.Unnest, exp.CTE)):
        return False
    for column in root.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            return False
        scopes = []
        node = column.parent
        while node is not None and node is not subquery:
            if isinstance(node, exp.Select):
                scopes.append(node)
            node = node.parent
        if not scopes:
            return False
        qualifier = column.table.lower()
        name = column.name.lower()
        bound = False
        for select in scopes:
            sources = _sources(select)
            if qualifier:
                if any((s.alias_or_name or "").lower() == qualifier for s in sources):
                    bound = True
                    break
                continue
            answers = [_has_column(s, name, schema) for s in sources]
            if any(a is True for a in answers):
                bound = True
                break
            if any(a is None for a in answers):
                return False  # the name may come from a source whose columns are unknown, or from outside
            if name in [(i.alias or "").lower() for i in select.expressions]:
                bound = True  # an output alias used in HAVING / ORDER BY
                break
        if not bound:
            return False
    return True


def _candidates(tree: exp.Expression, schema) -> list[exp.Subquery]:
    found = []
    for node in tree.find_all(exp.Subquery):
        parent = node.parent
        if isinstance(parent, (exp.From, exp.Join, exp.In, exp.Exists, exp.CTE, exp.Union, exp.Subquery, exp.Table, exp.TableAlias)):
            continue
        inner = node.this
        if not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
            continue
        if inner.args.get("limit") or inner.args.get("offset"):
            continue
        if is_uncorrelated(node, schema):
            found.append(node)
    return found


def unify(left_sql: str, right_sql: str, *, dialect: str = "bigquery", schema=None, prove=None, single_row=None, report=None) -> tuple[str, str, bool]:
    """Replace shared uncorrelated scalar subqueries by placeholders in both queries.

    ``prove(a, b)`` is called to compare two differently written subqueries (it returns
    whether they are provably equivalent). Returns ``(left, right, replaced_any)``.

    ``single_row(sql)`` may say whether a subquery provably returns at most one row (see
    ``output_properties``); when given with ``report``, ``report["unproven"]`` counts the replaced
    subqueries, nested scalar ones included, that it could not vouch for. A proof then needs
    ``ASSUMPTION`` only if that count is not zero.
    """

    left_tree = sqlglot.parse_one(left_sql, read=dialect)
    right_tree = sqlglot.parse_one(right_sql, read=dialect)
    left_nodes, right_nodes = _candidates(left_tree, schema), _candidates(right_tree, schema)
    if not left_nodes and not right_nodes:
        return left_sql, right_sql, False
    classes: list[str] = []  # representative SQL of each class
    proofs = 0

    def class_of(node: exp.Subquery) -> int:
        nonlocal proofs
        text = node.this.sql(dialect="bigquery")
        for index, representative in enumerate(classes):
            if representative == text:
                return index
        if prove is not None:
            for index, representative in enumerate(classes):
                if proofs >= MAX_PROOFS:
                    break
                proofs += 1
                if prove(representative, text):
                    return index
        classes.append(text)
        return len(classes) - 1

    replaced = False
    for tree, nodes in ((left_tree, left_nodes), (right_tree, right_nodes)):
        # Outermost first: a subquery inside another one is replaced with its parent.
        for node in nodes:
            if node.find_ancestor(exp.Subquery) in nodes:
                continue
            if single_row is not None and report is not None:
                scalars = [node] + [n for n in node.find_all(exp.Subquery) if n is not node and not isinstance(n.parent, (exp.From, exp.Join, exp.In, exp.Exists, exp.CTE, exp.Union, exp.Subquery, exp.Table, exp.TableAlias))]
                report["unproven"] = report.get("unproven", 0) + sum(1 for n in scalars if not single_row(n.this.sql(dialect=dialect)))
            index = class_of(node)
            node.replace(exp.Anonymous(this=PLACEHOLDER, expressions=[exp.Literal.number(index)]))
            replaced = True
    return left_tree.sql(dialect=dialect), right_tree.sql(dialect=dialect), replaced
