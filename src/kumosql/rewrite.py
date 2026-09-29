"""Apply registered rewrite rules and check each output for equivalence.

Every changed output is compared with its input by the conservative prover in
``kumosql.equivalence``. The result is ``proven`` only when every changed
statement is proven equivalent; anything else is returned but flagged
``unproven`` with the reason, so callers can decide whether to accept it.

Statements are checked pairwise. For ``CREATE ... AS`` and ``INSERT ...
SELECT`` the statement text around the query must be unchanged and the
queries must be proven equivalent. Other statements must render identically.
Dataform SQLX is checked by requiring config/js/operation blocks to be
identical and treating each ``${...}`` interpolation as an opaque fragment
identified by its text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from sqlglot import exp

from .ast_utils import parse_statements, top_level_query
from .engine import RuleDiagnostic, RuleOutput, available_rules, get_rule
from .equivalence import prove_equivalent
from .sqlx import looks_like_sqlx, mask_sqlx_by_content, split_sqlx_sections

# Import built-in rules so they are registered.
from . import cleanup as _cleanup  # noqa: F401
from . import inline_ctes as _inline_ctes  # noqa: F401
from . import lift_subqueries as _lift_subqueries  # noqa: F401


class VerificationStatus(str, Enum):
    """How far a rewrite's output has been checked against its input."""

    UNCHANGED = "unchanged"
    PROVEN = "proven"
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    reason: str
    details: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status is not VerificationStatus.UNPROVEN


@dataclass(frozen=True)
class RewriteResult:
    """Output of one rule, with its equivalence verification."""

    rule: str
    input_sql: str
    sql: str
    changes: int
    diagnostics: tuple[RuleDiagnostic, ...]
    verification: Verification
    rule_success: bool

    @property
    def success(self) -> bool:
        """The rule ran cleanly and its output is unchanged or proven equivalent."""

        return self.rule_success and self.verification.trusted


@dataclass(frozen=True)
class PipelineResult:
    """Output of several rules applied in sequence."""

    input_sql: str
    sql: str
    steps: tuple[RewriteResult, ...]
    verification: Verification

    @property
    def success(self) -> bool:
        return all(step.rule_success for step in self.steps) and self.verification.trusted


def _statement_shell(statement: exp.Expression) -> tuple[str, exp.Expression | None]:
    """Render a statement with its query replaced by a placeholder."""

    if isinstance(statement, (exp.Select, exp.Union)):
        return "<query>", statement
    if isinstance(statement, (exp.Create, exp.Insert)) and top_level_query(statement) is not None:
        shell = statement.copy()
        query = top_level_query(statement)
        shell.set("expression", exp.Placeholder())
        return shell.sql(dialect="bigquery", comments=False), query
    return statement.sql(dialect="bigquery", comments=False), None


def _order_sql(query: exp.Expression) -> str | None:
    order = query.args.get("order")
    return order.sql(dialect="bigquery", comments=False) if order is not None else None


def _verify_sql(before: str, after: str) -> tuple[bool, list[str]]:
    try:
        left = parse_statements(before)
        right = parse_statements(after)
    except Exception as exc:
        return False, [f"strict parse failed: {exc}"]
    if len(left) != len(right):
        return False, [f"statement count changed from {len(left)} to {len(right)}"]

    problems: list[str] = []
    for index, (old, new) in enumerate(zip(left, right)):
        old_shell, old_query = _statement_shell(old)
        new_shell, new_query = _statement_shell(new)
        if old_shell != new_shell:
            problems.append(f"statement {index}: text outside the query changed")
            continue
        if old_query is None:
            continue
        old_sql = old_query.sql(dialect="bigquery")
        new_sql = new_query.sql(dialect="bigquery")
        if old_sql == new_sql:
            continue
        # The prover compares result bags; a rewrite must also keep ordering.
        if _order_sql(old_query) != _order_sql(new_query):
            problems.append(f"statement {index}: final ORDER BY changed")
            continue
        proof = prove_equivalent(old_sql, new_sql)
        if not proof.proven:
            detail = "; ".join(proof.diagnostics)
            problems.append(
                f"statement {index}: {proof.reason}" + (f" ({detail})" if detail else "")
            )
    return not problems, problems


def _verify_sqlx(before: str, after: str) -> tuple[bool, list[str]]:
    try:
        left = split_sqlx_sections(before)
        right = split_sqlx_sections(after)
    except Exception as exc:
        return False, [f"SQLX could not be split: {exc}"]
    if [kind for kind, _ in left] != [kind for kind, _ in right]:
        return False, ["SQLX section layout changed"]

    problems: list[str] = []
    for (kind, old), (_, new) in zip(left, right):
        if kind == "block":
            if old != new:
                problems.append("a SQLX config/js/operations block changed")
            continue
        if old == new or (not old.strip() and not new.strip()):
            continue
        try:
            ok, section_problems = _verify_sql(mask_sqlx_by_content(old), mask_sqlx_by_content(new))
        except Exception as exc:
            ok, section_problems = False, [f"SQLX interpolations could not be masked: {exc}"]
        if not ok:
            problems.extend(section_problems)
    return not problems, problems


def verify_rewrite(before: str, after: str) -> Verification:
    """Check that ``after`` is a proven-equivalent rewrite of ``before``."""

    if before == after:
        return Verification(VerificationStatus.UNCHANGED, "output is identical to input")
    if looks_like_sqlx(before) or looks_like_sqlx(after):
        ok, problems = _verify_sqlx(before, after)
    else:
        ok, problems = _verify_sql(before, after)
    if ok:
        return Verification(
            VerificationStatus.PROVEN, "every changed statement was proven equivalent"
        )
    return Verification(
        VerificationStatus.UNPROVEN,
        "equivalence could not be proven for every changed statement",
        tuple(problems),
    )


def _result(rule_name: str, sql: str, output: RuleOutput) -> RewriteResult:
    verification = verify_rewrite(sql, output.sql)
    return RewriteResult(
        rule=rule_name,
        input_sql=sql,
        sql=output.sql,
        changes=output.changes,
        diagnostics=output.diagnostics,
        verification=verification,
        rule_success=output.success,
    )


def apply_rule(name: str, sql: str) -> RewriteResult:
    """Apply one registered rule and verify its output."""

    return _result(name, sql, get_rule(name).apply(sql))


def apply_rules(names: Iterable[str], sql: str) -> PipelineResult:
    """Apply rules in order, verifying every step and the end-to-end result."""

    steps: list[RewriteResult] = []
    current = sql
    for name in names:
        step = apply_rule(name, current)
        steps.append(step)
        current = step.sql

    if current == sql:
        verification = Verification(VerificationStatus.UNCHANGED, "output is identical to input")
    elif all(step.verification.trusted for step in steps):
        verification = Verification(
            VerificationStatus.PROVEN, "every step was unchanged or proven equivalent"
        )
    else:
        # A chain with an unproven step can still be proven directly.
        verification = verify_rewrite(sql, current)
    return PipelineResult(sql, current, tuple(steps), verification)


__all__ = [
    "PipelineResult",
    "RewriteResult",
    "Verification",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "available_rules",
    "verify_rewrite",
]
