"""Rewrite-rule base class, shared driver, and rule registry.

A rule only describes how to rewrite one parsed statement. The driver in
``RewriteRule.apply`` owns everything rules have in common: SQLX sectioning
and interpolation masking, strict parsing with a visible recovery fallback,
per-statement error isolation, output formatting, byte-for-byte no-ops,
re-parsing the output, and CTE dependency checks.

Semantic verification lives one layer up in ``kumosql.rewrite`` so the
equivalence prover can itself use rules without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import ClassVar

from sqlglot import exp

from .ast_utils import cte_dependency_errors, parse_statements, render_statement
from .sqlx import (
    SqlxRestorationError,
    looks_like_sqlx,
    mask_sqlx_interpolations,
    restore_sqlx_interpolations,
    split_sqlx_sections,
    with_preserved_whitespace,
)


def _sql_comments(sql: str) -> list[str]:
    """Extract SQL comments without mistaking comment markers in literals."""

    comments: list[str] = []
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            if end < 0:
                end = len(sql)
            comments.append(sql[i:end])
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                break
            comments.append(sql[i : end + 2])
            i = end + 2
            continue
        if sql[i] in "'\"`":
            quote = sql[i]
            triple = sql.startswith(quote * 3, i)
            delimiter = quote * (3 if triple else 1)
            i += len(delimiter)
            while i < len(sql):
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql.startswith(delimiter, i):
                    # SQL's doubled quote escapes a quote in single/double
                    # quoted strings and identifiers.
                    if not triple and sql.startswith(quote * 2, i):
                        i += 2
                        continue
                    i += len(delimiter)
                    break
                i += 1
            continue
        i += 1
    return comments


def _comment_key(comment: str) -> str:
    if comment.startswith("--"):
        return comment[2:].strip()
    return comment[2:-2].strip()


def _preserve_comments(source: str, rendered: str) -> str:
    """Restore comments dropped by sqlglot while formatting a changed query.

    sqlglot retains comments attached to surviving AST nodes, but comments
    between tokens and comments attached to removed predicates can disappear.
    Keep those comments as leading comments on the rendered statement.
    """

    source_comments = _sql_comments(source)
    output_comments = _sql_comments(rendered)
    available: dict[str, int] = {}
    for comment in output_comments:
        key = _comment_key(comment)
        available[key] = available.get(key, 0) + 1

    missing: list[str] = []
    for comment in source_comments:
        key = _comment_key(comment)
        if available.get(key, 0):
            available[key] -= 1
        else:
            missing.append(comment)
    if not missing:
        return rendered

    # Put each comment on its own line so a line comment cannot swallow SQL.
    return "\n".join(missing) + "\n" + rendered


FATAL_DIAGNOSTIC_CODES = frozenset(
    {
        "parse_error",
        "sqlx_parse_error",
        "sqlx_restore_error",
        "transform_error",
        "cte_dependency_error",
        "inline_subqueries_remaining",
        "output_parse_error",
    }
)


@dataclass(frozen=True)
class RuleDiagnostic:
    """One statement-level parse or transformation diagnostic."""

    statement_index: int
    code: str
    message: str


@dataclass(frozen=True)
class RuleOutput:
    """Syntactic result of applying one rule to a SQL or SQLX text."""

    sql: str
    statements: int
    changed_statements: int
    changes: int
    remaining: int
    diagnostics: tuple[RuleDiagnostic, ...]

    @property
    def success(self) -> bool:
        """Whether the rule ran cleanly and left nothing it was meant to remove."""

        return (
            not any(d.code in FATAL_DIAGNOSTIC_CODES for d in self.diagnostics)
            and self.remaining == 0
        )


class RewriteRule:
    """Base class for a deterministic, statement-local rewrite rule.

    Subclasses set ``name`` and ``summary`` and implement ``rewrite_statement``.
    Rules whose goal is to eliminate a construct (such as inline subqueries)
    also override ``count_remaining`` so leftovers are reported as failures.
    """

    name: ClassVar[str]
    summary: ClassVar[str]

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        """Rewrite ``statement`` in place; return the change count and diagnostics."""

        raise NotImplementedError

    def count_remaining(self, statements: list[exp.Expression]) -> int:
        """Count constructs this rule should have removed but did not."""

        return 0

    def apply(self, sql: str) -> RuleOutput:
        """Apply the rule to BigQuery SQL or Dataform SQLX."""

        if looks_like_sqlx(sql):
            return self._apply_sqlx(sql)
        return self._apply_sql(sql)

    def _apply_sql(self, sql: str) -> RuleOutput:
        if not sql or not sql.strip():
            return RuleOutput("", 0, 0, 0, 0, ())

        try:
            statements = parse_statements(sql)
            recovered = False
        except Exception as exc:
            if re.search(r"\b(?:FROM|JOIN|SELECT|AS|WHERE|ON)\s*$", sql.strip(), re.IGNORECASE):
                return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(0, "parse_error", str(exc)),))
            try:
                statements = parse_statements(sql, recover=True)
            except Exception:
                return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(0, "parse_error", str(exc)),))
            recovered = True

        diagnostics: list[RuleDiagnostic] = []
        if recovered:
            diagnostics.append(
                RuleDiagnostic(
                    -1,
                    "recovered_parse",
                    "Strict BigQuery parsing failed; transformed statements using sqlglot recovery mode",
                )
            )

        initial_remaining = self.count_remaining(statements)
        rendered: list[str] = []
        changed_statements = 0
        changes = 0
        for index, statement in enumerate(statements):
            try:
                count, statement_diagnostics = self.rewrite_statement(statement, index)
                diagnostics.extend(statement_diagnostics)
                for error in cte_dependency_errors(statement):
                    diagnostics.append(RuleDiagnostic(index, "cte_dependency_error", error))
                if count:
                    changed_statements += 1
                    changes += count
                rendered.append(render_statement(statement))
            except Exception as exc:
                diagnostics.append(RuleDiagnostic(index, "transform_error", str(exc)))
                # Preserve the original statement if an AST edge case is hit.
                rendered.append(statement.sql(dialect="bigquery"))

        if changes == 0:
            return RuleOutput(sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics))

        output = ";\n\n".join(part.rstrip() for part in rendered if part.strip())
        output = _preserve_comments(sql, output)
        output_statements: list[exp.Expression] | None = None
        try:
            output_statements = parse_statements(output)
            remaining = self.count_remaining(output_statements)
        except Exception as exc:
            if recovered:
                # Recovery mode is intentionally used for BigQuery constructs
                # that sqlglot cannot round-trip strictly (for example FOR
                # SYSTEM_TIME). The transformed AST is still available, so use
                # it rather than declaring the serialized text a failure.
                remaining = self.count_remaining(statements)
                diagnostics.append(RuleDiagnostic(-1, "recovered_output_parse", str(exc)))
            else:
                try:
                    remaining = self.count_remaining(parse_statements(output, recover=True))
                    diagnostics.append(RuleDiagnostic(-1, "recovered_output_parse", str(exc)))
                except Exception:
                    remaining = 0
                    diagnostics.append(RuleDiagnostic(-1, "output_parse_error", str(exc)))

        if output_statements is not None:
            for index, statement in enumerate(output_statements):
                for error in cte_dependency_errors(statement):
                    diagnostics.append(RuleDiagnostic(index, "cte_dependency_error", error))

        return RuleOutput(
            output,
            len(statements),
            changed_statements,
            changes,
            remaining,
            tuple(diagnostics),
        )

    def _apply_sqlx(self, sql: str) -> RuleOutput:
        try:
            sections = split_sqlx_sections(sql)
        except Exception as exc:
            return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(-1, "sqlx_parse_error", str(exc)),))

        rendered: list[str] = []
        diagnostics: list[RuleDiagnostic] = []
        statements = 0
        changed_statements = 0
        changes = 0

        for kind, section in sections:
            if kind == "block" or not section.strip():
                rendered.append(section)
                continue
            try:
                masked, restorations = mask_sqlx_interpolations(section)
                result = self._apply_sql(masked)
                restored = restore_sqlx_interpolations(result.sql, restorations)
                rendered.append(with_preserved_whitespace(section, restored))
                statements += result.statements
                changed_statements += result.changed_statements
                changes += result.changes
                diagnostics.extend(result.diagnostics)
            except SqlxRestorationError as exc:
                diagnostics.extend(result.diagnostics)
                diagnostics.append(RuleDiagnostic(-1, "sqlx_restore_error", str(exc)))
                # Treat SQLX rewriting transactionally: a failed restoration
                # must not expose output with a missing template expression.
                return RuleOutput(sql, statements, 0, 0, 0, tuple(diagnostics))
            except Exception as exc:
                rendered.append(section)
                diagnostics.append(RuleDiagnostic(-1, "sqlx_parse_error", str(exc)))

        output = "".join(rendered)
        try:
            remaining = self._count_remaining_sqlx(output)
        except Exception as exc:
            remaining = 0
            diagnostics.append(RuleDiagnostic(-1, "output_parse_error", str(exc)))

        if changes == 0:
            output = sql

        return RuleOutput(
            output, statements, changed_statements, changes, remaining, tuple(diagnostics)
        )

    def _count_remaining_sqlx(self, sql: str) -> int:
        total = 0
        for kind, section in split_sqlx_sections(sql):
            if kind == "block" or not section.strip():
                continue
            masked, _ = mask_sqlx_interpolations(section)
            total += self.count_remaining(parse_statements(masked))
        return total


_REGISTRY: dict[str, RewriteRule] = {}


def register_rule(rule_class: type[RewriteRule]) -> type[RewriteRule]:
    """Class decorator that adds a rule to the global registry."""

    name = rule_class.name
    existing = _REGISTRY.get(name)
    if existing is not None and type(existing) is not rule_class:
        raise ValueError(f"rewrite rule `{name}` is already registered")
    _REGISTRY[name] = rule_class()
    return rule_class


def get_rule(name: str) -> RewriteRule:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"unknown rewrite rule `{name}` (known rules: {known})") from None


def available_rules() -> dict[str, RewriteRule]:
    """Registered rules by name, in registration order."""

    return dict(_REGISTRY)
