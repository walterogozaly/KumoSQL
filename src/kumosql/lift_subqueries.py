"""Safely lift relational BigQuery subqueries into named CTEs.

The supported transformation is deliberately narrow: subqueries used as a
FROM or JOIN relation are moved into the statement's top-level WITH clause.
Scalar, EXISTS, and correlated predicate subqueries are not rewritten because
turning those into CTEs changes cardinality or correlation semantics. Neither is
a FROM/JOIN subquery whose body reads a name bound around it (a column of an
enclosing query, or a table of a WITH nested between it and the top level): a
top-level CTE cannot see that name, so it stays in place and counts as remaining.

This module is the ``lift_subqueries`` rule in the rewrite-rule registry; the
``lift_subqueries()`` function is kept as the stable public entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .ast_utils import (
    free_reads,
    identifier_name,
    leading_qualifier,
    nearest_parent_cte,
    nearest_root_cte,
    parse_statements,
    relation_names,
    scope_relations,
    set_with_clause,
    top_level_query,
    visible_ctes,
    with_clause,
)
from .engine import FATAL_DIAGNOSTIC_CODES, RewriteRule, RuleDiagnostic, register_rule
from .sqlx import looks_like_sqlx, mask_sqlx_interpolations, split_sqlx_sections

# Backwards-compatible names for code that imported the private helpers.
from .ast_utils import with_clause as _with_clause  # noqa: F401

LiftDiagnostic = RuleDiagnostic


@dataclass(frozen=True)
class LiftResult:
    """Result of transforming one SQL workbook."""

    sql: str
    statements: int
    transformed_statements: int
    lifted_subqueries: int
    remaining_inline_subqueries: int
    diagnostics: tuple[LiftDiagnostic, ...]

    @property
    def success(self) -> bool:
        """Whether every parsed relational subquery was lifted."""

        return (
            not any(diagnostic.code in FATAL_DIAGNOSTIC_CODES for diagnostic in self.diagnostics)
            and self.remaining_inline_subqueries == 0
        )


def _is_relation_subquery(node: exp.Expression) -> bool:
    return isinstance(node, exp.Subquery) and isinstance(node.parent, (exp.From, exp.Join))


def _relation_subqueries(node: exp.Expression) -> list[exp.Subquery]:
    """Return inline FROM/JOIN subqueries in deterministic tree order."""

    return [candidate for candidate in node.walk() if _is_relation_subquery(candidate)]


def count_inline_subqueries(sql: str) -> int:
    """Count FROM/JOIN subqueries in parseable BigQuery SQL.

    Raises ``sqlglot.ParseError`` for invalid or unsupported input instead of
    silently claiming that the input is transformed.
    """

    if looks_like_sqlx(sql):
        total = 0
        for kind, section in split_sqlx_sections(sql):
            if kind == "block" or not section.strip():
                continue
            masked, _ = mask_sqlx_interpolations(section)
            total += sum(len(_relation_subqueries(s)) for s in parse_statements(masked))
        return total
    return sum(len(_relation_subqueries(statement)) for statement in parse_statements(sql))


def _used_cte_names(query: exp.Expression) -> set[str]:
    clause = with_clause(query)
    if not clause:
        return set()
    return {
        alias
        for cte in clause.expressions
        if (alias := identifier_name(cte.args.get("alias")))
    }


def _next_name(used: set[str], counter: list[int]) -> str:
    while True:
        counter[0] += 1
        candidate = f"__lifted_subquery_{counter[0]:03d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _replace_relation_subquery(subquery: exp.Subquery, name: str) -> None:
    """Replace a relation subquery while preserving its source alias."""

    replacement = exp.to_table(name)
    if subquery.alias:
        replacement.set("alias", exp.TableAlias(this=exp.to_identifier(subquery.alias)))
    # PIVOT / UNPIVOT / TABLESAMPLE attach to the subquery in the tree; they belong to the relation
    # and must move onto the new table reference, or the lifted query silently loses them.
    for key in ("pivots", "sample", "laterals"):
        if subquery.args.get(key):
            replacement.set(key, subquery.args[key])
    subquery.replace(replacement)


def _make_cte(name: str, body: exp.Expression) -> exp.CTE:
    return exp.CTE(
        this=body,
        alias=exp.TableAlias(this=exp.to_identifier(name)),
    )


@dataclass(frozen=True)
class _PendingCte:
    cte: exp.CTE
    parent_cte: exp.CTE | None


def _tree_depth(node: exp.Expression) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _escapes_scope(subquery: exp.Subquery, query: exp.Expression) -> bool:
    """Whether the subquery's body reads a name bound around it, which ``query``'s WITH clause cannot see.

    Only a closed body keeps its meaning as a top-level CTE. It must not read a table by a name that a WITH
    between it and ``query`` defines (a nested WITH, including one that hides a top-level CTE or a real
    table of that name), nor a column of a query around it: one qualified by a name of their relations, or,
    where an enclosing query's relations are in scope (inside a scalar, EXISTS, IN or ARRAY subquery), any
    column that no relation inside the body binds, since without a schema an unqualified column there may
    be correlated. A column with no relation at all to read inside the body must come from around it.
    """

    body = subquery.this
    anchor = subquery
    while anchor.parent is not None and anchor.parent is not query and anchor.parent is not with_clause(query):
        anchor = anchor.parent
    if free_reads(body) & visible_ctes(subquery, anchor):
        return True
    correlated = bool(scope_relations(subquery))
    around: set[str] = set()
    node = subquery.parent
    while node is not None:
        around |= relation_names(node)
        node = node.parent
    for column in body.find_all(exp.Column):
        qualifier = leading_qualifier(column)
        if qualifier:
            if qualifier not in scope_relations(column, body) and (correlated or qualifier in around):
                return True
        elif correlated or not scope_relations(column, body):
            return True
    for table in body.find_all(exp.Table):
        qualifier = leading_qualifier(table)
        # ``FROM o.items`` reads an array column of an enclosing relation ``o``.
        if qualifier and qualifier in around and qualifier not in scope_relations(table, body):
            return True
    return False


def _lift_query(query: exp.Expression, counter: list[int] | None = None) -> int:
    """Lift relation subqueries from a SELECT/UNION query, recursively."""

    used = _used_cte_names(query)
    counter = counter if counter is not None else [0]
    lifted = 0
    current = with_clause(query)
    original_ctes = list(current.expressions) if current else []
    pending: list[_PendingCte] = []

    # Work from the deepest currently visible relation subquery. Processing
    # one at a time is intentional: nested relations are added to this
    # statement's WITH clause, never to a nested WITH clause inside another
    # lifted CTE. That gives every lifted relation one top-level name and
    # keeps dependency order (inner before outer). A subquery whose body
    # reads a name bound around it stays in place and counts as remaining;
    # an enclosing subquery that binds that name can still be lifted with it.
    kept: list[exp.Subquery] = []
    while True:
        candidates = [
            candidate
            for candidate in _relation_subqueries(query)
            if not any(candidate is subquery for subquery in kept)
        ]
        if not candidates:
            break

        subquery = max(candidates, key=_tree_depth)
        if not subquery.parent:
            break
        if _escapes_scope(subquery, query):
            kept.append(subquery)
            continue
        body = subquery.this.copy()
        name = _next_name(used, counter)
        pending.append(
            _PendingCte(
                cte=_make_cte(name, body),
                parent_cte=(
                    nearest_root_cte(subquery, original_ctes)
                    if current
                    else nearest_parent_cte(subquery)
                ),
            )
        )
        _replace_relation_subquery(subquery, name)
        lifted += 1

    if pending:
        if current:
            ordered: list[exp.CTE] = []
            for original_cte in original_ctes:
                ordered.extend(
                    item.cte for item in pending if item.parent_cte is original_cte
                )
                ordered.append(original_cte)
            ordered.extend(item.cte for item in pending if item.parent_cte is None)
            current.set("expressions", ordered)
        else:
            set_with_clause(query, exp.With(expressions=[item.cte for item in pending]))

    return lifted


def _transform_statement(statement: exp.Expression) -> int:
    query = top_level_query(statement)
    if query is not None:
        return _lift_query(query)

    # BigQuery scripting statements such as SET can contain a query inside a
    # scalar expression. There is no statement-level WITH slot for those, so
    # lift within the innermost query scope instead (for example, SET x =
    # (WITH ... SELECT ...)).
    lifted = 0
    query_nodes = [
        node
        for node in statement.walk()
        if isinstance(node, (exp.Select, exp.Union)) and _relation_subqueries(node)
    ]
    for query_node in query_nodes:
        # An earlier lift may have moved this query into a CTE as a copy; the
        # original no longer belongs to the statement.
        if query_node.root() is statement:
            lifted += _lift_query(query_node)
    return lifted


@register_rule
class LiftSubqueriesRule(RewriteRule):
    """Promote every FROM/JOIN subquery into a uniquely named top-level CTE."""

    name = "lift_subqueries"
    summary = "Lift FROM/JOIN subqueries into named top-level CTEs"

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        before = len(_relation_subqueries(statement))
        lifted = _transform_statement(statement)
        after = len(_relation_subqueries(statement))
        diagnostics: list[RuleDiagnostic] = []
        if before and after:
            diagnostics.append(
                RuleDiagnostic(
                    index,
                    "inline_subqueries_remaining",
                    f"{after} relational subquery/subqueries remain after transformation"
                    " (one whose body reads a column or WITH table bound around it stays in place)",
                )
            )
        return lifted, diagnostics

    def count_remaining(self, statements: list[exp.Expression]) -> int:
        return sum(len(_relation_subqueries(statement)) for statement in statements)


_RULE = LiftSubqueriesRule()


class _AnalysisLiftRule(LiftSubqueriesRule):
    rewrite_pipe_syntax = True


_ANALYSIS_RULE = _AnalysisLiftRule()


def lift_subqueries(sql: str, *, rewrite_pipe_syntax: bool = False) -> LiftResult:
    """Lift relational subqueries in BigQuery SQL or Dataform SQLX.

    Pipe syntax is left as written unless ``rewrite_pipe_syntax`` asks for sqlglot's standard-SQL
    translation of it, which the prover normalizes but a user should not be shown.
    """

    output = (_ANALYSIS_RULE if rewrite_pipe_syntax else _RULE).apply(sql)
    return LiftResult(
        sql=output.sql,
        statements=output.statements,
        transformed_statements=output.changed_statements,
        lifted_subqueries=output.changes,
        remaining_inline_subqueries=output.remaining,
        diagnostics=output.diagnostics,
    )


__all__ = [
    "LiftDiagnostic",
    "LiftResult",
    "LiftSubqueriesRule",
    "count_inline_subqueries",
    "lift_subqueries",
]
