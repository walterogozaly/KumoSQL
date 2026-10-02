"""Shared sqlglot helpers used by rewrite rules and the equivalence prover."""

from __future__ import annotations

from contextlib import contextmanager
import logging

import sqlglot
from sqlglot import ErrorLevel, exp


@contextmanager
def quiet_parser():
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_statements(sql: str, *, recover: bool = False) -> list[exp.Expression]:
    """Parse BigQuery SQL, raising unless ``recover`` asks for recovery mode."""

    error_level = ErrorLevel.IGNORE if recover else ErrorLevel.RAISE
    with quiet_parser():
        parsed = sqlglot.parse(sql, read="bigquery", error_level=error_level)
    return [statement for statement in parsed if statement is not None]


def render_statement(statement: exp.Expression) -> str:
    """Render a transformed statement in the project's output style."""

    return statement.sql(dialect="bigquery", pretty=True, pad=4, identify=False)


def canonical_negation(tree: exp.Expression) -> exp.Expression:
    """Spell ``x IS NOT NULL``, ``x NOT LIKE y`` and ``x NOT ILIKE y`` as ``NOT (...)``.

    Some dialects (PostgreSQL in current sqlglot releases) parse these as ``Is``, ``Like`` or
    ``ILike`` carrying ``negate=True``; code that reads only the node type would take them for the
    positive test. One spelling everywhere keeps the provers from reading ``IS NOT NULL`` as ``IS NULL``.
    """

    for node in list(tree.find_all(exp.Is, exp.Like, exp.ILike)):
        if node.args.get("negate"):
            positive = node.copy()
            positive.set("negate", None)
            if node is tree:
                return exp.Not(this=positive)
            node.replace(exp.Not(this=positive))
    return tree


def identifier_name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    value = getattr(node, "name", None)
    if value:
        return value
    value = getattr(node, "this", None)
    return value if isinstance(value, str) else None


def with_arg_key(node: exp.Expression) -> str:
    """Return the sqlglot WITH argument name across supported versions."""

    args = getattr(node, "args", {})
    arg_types = getattr(node, "arg_types", {})
    if "with_" in args or "with_" in arg_types:
        return "with_"
    if "with" in args or "with" in arg_types:
        return "with"
    # Current sqlglot uses ``with_``. This fallback keeps construction
    # compatible with expression implementations that expose neither key until
    # the first value is assigned.
    return "with_"


def top_level_query(statement: exp.Expression) -> exp.Expression | None:
    if isinstance(statement, (exp.Create, exp.Insert)):
        candidate = statement.expression
        while isinstance(candidate, exp.Subquery):
            candidate = candidate.this
        return candidate
    if isinstance(statement, (exp.Select, exp.Union, exp.Update, exp.Delete, exp.Merge)):
        return statement
    return None


def with_clause(query: exp.Expression) -> exp.With | None:
    return query.args.get(with_arg_key(query)) if hasattr(query, "args") else None


def set_with_clause(query: exp.Expression, clause: exp.With | None) -> None:
    query.set(with_arg_key(query), clause)


def cte_alias_name(cte: exp.CTE) -> str | None:
    return identifier_name(cte.args.get("alias"))


def is_cte_reference_candidate(table: exp.Table) -> bool:
    """Whether a table reference is one-part and can therefore name a CTE."""

    return not table.db and not table.catalog


def nearest_parent_cte(node: exp.Expression) -> exp.CTE | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return parent
        parent = parent.parent
    return None


def nearest_root_cte(node: exp.Expression, root_ctes: list[exp.CTE]) -> exp.CTE | None:
    """Find the direct root CTE containing a node, including nested WITHs."""

    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE) and any(parent is cte for cte in root_ctes):
            return parent
        parent = parent.parent
    return None


def has_nested_with(query: exp.Expression) -> bool:
    root = with_clause(query)
    return any(isinstance(node, exp.With) and node is not root for node in query.walk())


def ambiguous_unnest_names(query: exp.Expression) -> set[str]:
    """Names of relations that also appear as a bare ``UNNEST`` argument.

    Older sqlglot releases (26.x) parse a repeated relation such as
    ``FROM shared JOIN shared AS s2`` as ``JOIN UNNEST(shared) AS s2``, so the
    AST no longer shows the second reference. A bare column inside UNNEST that
    matches a CTE or table name is therefore treated as a hidden reference.
    """

    names = {
        cte_alias_name(cte).lower()
        for cte in query.find_all(exp.CTE)
        if cte_alias_name(cte)
    }
    for table in query.find_all(exp.Table):
        if is_cte_reference_candidate(table):
            names.add(table.name.lower())
            if table.alias:
                names.add(table.alias.lower())
    hidden: set[str] = set()
    for unnest in query.find_all(exp.Unnest):
        for expression in unnest.expressions:
            if isinstance(expression, exp.Column) and not expression.table:
                if expression.name.lower() in names:
                    hidden.add(expression.name.lower())
    return hidden


def cte_dependency_errors(statement: exp.Expression) -> list[str]:
    """Find undefined or forward references in a statement's root CTE list."""

    query = top_level_query(statement)
    if query is None:
        return []
    clause = with_clause(query)
    if not clause:
        return []

    ctes = list(clause.expressions)
    positions = {
        name: index
        for index, cte in enumerate(ctes)
        if (name := cte_alias_name(cte))
    }
    recursive = bool(clause.args.get("recursive"))
    errors: list[str] = []

    for table in query.find_all(exp.Table):
        name = table.name
        if not is_cte_reference_candidate(table):
            continue
        if name.startswith("__lifted_subquery_") and name not in positions:
            errors.append(f"lifted CTE `{name}` is referenced but not defined")
            continue
        if name not in positions:
            continue

        owner = nearest_parent_cte(table)
        if owner is None:
            continue
        owner_name = cte_alias_name(owner)
        if owner_name is None or (name == owner_name and recursive):
            continue
        if positions[name] >= positions.get(owner_name, len(ctes)):
            errors.append(
                f"CTE `{name}` is referenced before it is defined by `{owner_name}`"
            )

    return list(dict.fromkeys(errors))
