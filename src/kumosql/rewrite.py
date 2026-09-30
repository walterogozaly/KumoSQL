"""Apply registered rewrite rules and check each output for equivalence.

Every changed output is compared with its input by the conservative prover in
``kumosql.equivalence``. Each result has one evidence label and separate
records for the checks that support it. Only ``unchanged`` and ``proven`` are
trusted for automatic acceptance; a fatal rule failure is labeled ``failed``.

Statements are checked pairwise. For ``CREATE ... AS`` and ``INSERT ...
SELECT`` the statement text around the query must be unchanged and the
queries must be proven equivalent. Other statements must render identically.
Dataform SQLX is checked by requiring config/js/operation blocks to be
identical and treating each ``${...}`` interpolation as an opaque fragment
identified by its text.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping
from urllib.error import URLError

from sqlglot import exp

from .ast_utils import parse_statements, top_level_query
from .dryrun import Transport, check_rewrite
from .engine import RewriteRule, RuleDiagnostic, RuleOutput, available_rules, get_rule
from .equivalence import prove_equivalent
from .sqlx import looks_like_sqlx, mask_sqlx_by_content, split_sqlx_sections

# Import built-in rules so they are registered.
from . import cleanup as _cleanup  # noqa: F401
from . import cost_rules as _cost_rules  # noqa: F401
from . import formatting as _formatting  # noqa: F401
from . import inline_ctes as _inline_ctes  # noqa: F401
from . import lift_subqueries as _lift_subqueries  # noqa: F401


class VerificationStatus(str, Enum):
    """The single evidence label assigned to a rewrite result."""

    UNCHANGED = "unchanged"
    PROVEN = "proven"
    PLANNER_CHECKED = "planner_checked"
    UNPROVEN = "unproven"
    FAILED = "failed"


@dataclass(frozen=True)
class VerificationCheck:
    """One independently reported check supporting an evidence label."""

    kind: str
    outcome: str
    detail: str
    evidence: tuple[tuple[str, object], ...] = ()

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "outcome": self.outcome,
            "detail": self.detail,
        }
        if self.evidence:
            result["evidence"] = {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.evidence
            }
        return result


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    reason: str
    details: tuple[str, ...] = ()
    checks: tuple[VerificationCheck, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status in (VerificationStatus.UNCHANGED, VerificationStatus.PROVEN)

    def to_json(self) -> dict[str, object]:
        """Return the same JSON shape for pipeline and per-rule results."""

        return {
            "status": self.status.value,
            "reason": self.reason,
            "details": list(self.details),
            "checks": [check.to_json() for check in self.checks],
        }


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


def verify_rewrite(
    before: str,
    after: str,
    *,
    planner_check: VerificationCheck | None = None,
) -> Verification:
    """Check a rewrite and return one label with the evidence supporting it.

    A caller may attach a planner check when one has run. Planner evidence is
    reported separately from the structural proof; it can yield
    ``planner_checked`` when no proof is available, but that label is not
    trusted for automatic acceptance.
    """

    return _verify_rewrite(before, after, planner_check=planner_check)


def _verify_rewrite(
    before: str,
    after: str,
    *,
    planner_check: VerificationCheck | None = None,
    rewrite_succeeded: bool = True,
    failure_details: tuple[str, ...] = (),
) -> Verification:
    if planner_check is not None and planner_check.kind != "planner":
        raise ValueError("planner_check must have kind='planner'")

    checks: list[VerificationCheck] = []
    if before == after:
        checks.append(
            VerificationCheck("change_detection", "passed", "Output is identical to input.")
        )
        if planner_check is not None:
            checks.append(planner_check)
        if not rewrite_succeeded:
            details = failure_details or ("The rewrite rule did not complete successfully.",)
            checks.append(VerificationCheck("rewrite", "failed", "; ".join(details)))
            return Verification(
                VerificationStatus.FAILED,
                "the rewrite rule did not complete successfully",
                details,
                tuple(checks),
            )
        if planner_check is not None and planner_check.outcome == "failed":
            return Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_check.detail,),
                tuple(checks),
            )
        return Verification(
            VerificationStatus.UNCHANGED,
            "output is identical to input",
            checks=tuple(checks),
        )

    if looks_like_sqlx(before) or looks_like_sqlx(after):
        ok, problems = _verify_sqlx(before, after)
    else:
        ok, problems = _verify_sql(before, after)

    proof_detail = (
        "Every changed statement was proven equivalent."
        if ok
        else "; ".join(problems) or "The equivalence prover could not establish equivalence."
    )
    checks.append(
        VerificationCheck("equivalence_proof", "passed" if ok else "not_proven", proof_detail)
    )
    checks.append(
        planner_check
        or VerificationCheck("planner", "not_run", "No planner check was run.")
    )

    if not rewrite_succeeded:
        details = failure_details or ("The rewrite rule did not complete successfully.",)
        checks.append(VerificationCheck("rewrite", "failed", "; ".join(details)))
        return Verification(
            VerificationStatus.FAILED,
            "the rewrite rule did not complete successfully",
            details,
            tuple(checks),
        )

    if ok and (planner_check is None or planner_check.outcome in ("passed", "not_run")):
        return Verification(
            VerificationStatus.PROVEN,
            "every changed statement was proven equivalent",
            checks=tuple(checks),
        )
    if planner_check is not None and planner_check.outcome == "passed":
        return Verification(
            VerificationStatus.PLANNER_CHECKED,
            "the planner check passed, but equivalence could not be proven",
            tuple(problems),
            tuple(checks),
        )
    if planner_check is not None and planner_check.outcome == "failed":
        return Verification(
            VerificationStatus.UNPROVEN,
            "the planner check failed",
            (planner_check.detail,),
            tuple(checks),
        )
    return Verification(
        VerificationStatus.UNPROVEN,
        "equivalence could not be proven for every changed statement",
        tuple(problems),
        tuple(checks),
    )


def _result(rule_name: str, sql: str, output: RuleOutput) -> RewriteResult:
    failure_details = tuple(
        f"{diagnostic.code}: {diagnostic.message}" for diagnostic in output.diagnostics
    )
    verification = _verify_rewrite(
        sql,
        output.sql,
        rewrite_succeeded=output.success,
        failure_details=failure_details,
    )
    return RewriteResult(
        rule=rule_name,
        input_sql=sql,
        sql=output.sql,
        changes=output.changes,
        diagnostics=output.diagnostics,
        verification=verification,
        rule_success=output.success,
    )


def apply_rule(
    name: str, sql: str, *, overrides: Mapping[str, RewriteRule] | None = None
) -> RewriteResult:
    """Apply one registered rule and verify its output.

    ``overrides`` swaps in a differently configured instance for a rule name.
    """

    rule = overrides[name] if overrides and name in overrides else get_rule(name)
    return _result(name, sql, rule.apply(sql))


def apply_rules(
    names: Iterable[str], sql: str, *, overrides: Mapping[str, RewriteRule] | None = None
) -> PipelineResult:
    """Apply rules in order, verifying every step and the end-to-end result."""

    steps: list[RewriteResult] = []
    current = sql
    for name in names:
        step = apply_rule(name, current, overrides=overrides)
        steps.append(step)
        current = step.sql

    step_checks = tuple(
        VerificationCheck(
            check.kind,
            check.outcome,
            f"{step.rule}: {check.detail}",
            (
                tuple(
                    (key, value)
                    for key, value in check.evidence
                    if key not in ("scope", "rule")
                )
                + (("scope", "step"), ("rule", step.rule))
                if check.kind == "planner"
                else check.evidence
            ),
        )
        for step in steps
        for check in step.verification.checks
    )
    rule_succeeded = all(step.rule_success for step in steps)
    failure_details = tuple(
        f"{step.rule}: {diagnostic.code}: {diagnostic.message}"
        for step in steps
        if not step.rule_success
        for diagnostic in step.diagnostics
    ) or tuple(
        f"{step.rule}: {step.verification.reason}"
        for step in steps
        if not step.rule_success
    )

    if not rule_succeeded:
        base = _verify_rewrite(
            sql,
            current,
            rewrite_succeeded=False,
            failure_details=failure_details,
        )
        verification = Verification(
            base.status,
            base.reason,
            base.details,
            step_checks + base.checks,
        )
    elif current == sql:
        base = verify_rewrite(sql, current)
        planner_failure = next(
            (
                check
                for check in step_checks
                if check.kind == "planner" and check.outcome == "failed"
            ),
            None,
        )
        if planner_failure is not None:
            verification = Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_failure.detail,),
                step_checks + base.checks,
            )
        else:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
    elif all(step.verification.trusted for step in steps):
        verification = Verification(
            VerificationStatus.PROVEN,
            "every step was unchanged or proven equivalent",
            checks=step_checks,
        )
    else:
        # A chain with an unproven step can still be proven directly.
        base = verify_rewrite(sql, current)
        planner_failure = next(
            (
                check
                for check in step_checks
                if check.kind == "planner" and check.outcome == "failed"
            ),
            None,
        )
        if planner_failure is not None:
            verification = Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_failure.detail,),
                step_checks + base.checks,
            )
        elif base.trusted:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
        else:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
    return PipelineResult(sql, current, tuple(steps), verification)


def _planner_check_evidence(
    *,
    original_planned: bool | None = None,
    rewritten_planned: bool | None = None,
    schema_matches: bool | None = None,
    schema_differences: tuple[str, ...] = (),
    estimated_bytes_delta: int | None = None,
    compiled_sql_used: bool = False,
    scope: str = "end_to_end",
) -> tuple[tuple[str, object], ...]:
    return (
        ("scope", scope),
        ("original_planned", original_planned),
        ("rewritten_planned", rewritten_planned),
        ("schema_matches", schema_matches),
        ("schema_differences", schema_differences),
        ("estimated_bytes_delta", estimated_bytes_delta),
        ("results_compared", False),
        ("compiled_sql_used", compiled_sql_used),
    )


def _replace_end_to_end_planner_check(
    verification: Verification, planner_check: VerificationCheck
) -> Verification:
    checks = tuple(
        check
        for check in verification.checks
        if not (
            check.kind == "planner"
            and (
                (check.outcome == "not_run" and check.detail == "No planner check was run.")
                or dict(check.evidence).get("scope") == "end_to_end"
            )
        )
    ) + (planner_check,)

    if verification.status is VerificationStatus.FAILED:
        return replace(verification, checks=checks)
    if planner_check.outcome == "failed":
        return Verification(
            VerificationStatus.UNPROVEN,
            "the planner check failed; query result equality was not established",
            (planner_check.detail,),
            checks,
        )
    if (
        planner_check.outcome == "passed"
        and verification.status is VerificationStatus.UNPROVEN
    ):
        return Verification(
            VerificationStatus.PLANNER_CHECKED,
            "both queries planned with matching schemas; query result equality was not established",
            verification.details,
            checks,
        )
    return replace(verification, checks=checks)


def attach_planner_check(
    result: RewriteResult | PipelineResult,
    project: str,
    *,
    location: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
    compiled_original_sql: str | None = None,
    compiled_rewritten_sql: str | None = None,
) -> RewriteResult | PipelineResult:
    """Opt in to planner evidence for a changed result.

    Planner checks compare whether both queries plan and whether their output
    schemas match. They do not compare rows and never establish equivalence.
    This function is the only rewrite-result entry point that invokes the
    BigQuery dry-run transport; ordinary rewriting remains offline.

    SQLX sources need both compiled SQL strings. Only a single SELECT query
    on each side is planned; multi-statement scripts and DML/DDL are reported
    as not planned.
    """

    if not project:
        raise ValueError("project is required to run a planner check")

    before = result.input_sql
    after = result.sql
    verification = result.verification
    rule_failed = verification.status is VerificationStatus.FAILED or (
        isinstance(result, RewriteResult) and not result.rule_success
    ) or (
        isinstance(result, PipelineResult)
        and any(not step.rule_success for step in result.steps)
    )

    def not_run(detail: str, *, compiled: bool = False) -> RewriteResult | PipelineResult:
        check = VerificationCheck(
            "planner",
            "not_run",
            detail,
            _planner_check_evidence(compiled_sql_used=compiled),
        )
        return replace(
            result,
            verification=_replace_end_to_end_planner_check(verification, check),
        )

    if rule_failed:
        return not_run("Planner check was skipped because the rewrite failed.")
    if before == after:
        return not_run("Planner check was skipped because the output did not change.")

    compiled_supplied = compiled_original_sql is not None or compiled_rewritten_sql is not None
    is_sqlx = looks_like_sqlx(before) or looks_like_sqlx(after)
    if compiled_supplied and not is_sqlx:
        return not_run(
            "Planner check was skipped; compiled SQL overrides are only accepted for SQLX."
        )
    if compiled_supplied and (
        compiled_original_sql is None or compiled_rewritten_sql is None
    ):
        return not_run(
            "Planner check was skipped; provide both compiled SQL inputs for SQLX.",
        )
    if is_sqlx and not compiled_supplied:
        return not_run(
            "Planner check was skipped because SQLX must be compiled first; provide both compiled SQL inputs."
        )

    planner_before = compiled_original_sql if compiled_supplied else before
    planner_after = compiled_rewritten_sql if compiled_supplied else after
    assert planner_before is not None and planner_after is not None
    if looks_like_sqlx(planner_before) or looks_like_sqlx(planner_after):
        return not_run(
            "Planner check was skipped because the supplied SQLX inputs are not compiled SQL.",
            compiled=compiled_supplied,
        )

    try:
        check = check_rewrite(
            planner_before,
            planner_after,
            project,
            location=location,
            token=token,
            transport=transport,
        )
    except (RuntimeError, OSError, URLError) as exc:
        not_run_check = VerificationCheck(
            "planner",
            "not_run",
            f"Planner check was not run: {exc}",
            _planner_check_evidence(compiled_sql_used=compiled_supplied),
        )
        return replace(
            result,
            verification=_replace_end_to_end_planner_check(verification, not_run_check),
        )

    outcome = check.outcome
    detail = f"{check.reason}; query result equality was not checked"
    planner_record = VerificationCheck(
        "planner",
        outcome,
        detail,
        _planner_check_evidence(
            original_planned=check.original_planned,
            rewritten_planned=check.rewritten_planned,
            schema_matches=check.schema_matches,
            schema_differences=check.schema_differences,
            estimated_bytes_delta=check.estimated_bytes_delta,
            compiled_sql_used=compiled_supplied,
        ),
    )
    return replace(
        result,
        verification=_replace_end_to_end_planner_check(verification, planner_record),
    )


__all__ = [
    "PipelineResult",
    "RewriteResult",
    "Verification",
    "VerificationCheck",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "attach_planner_check",
    "available_rules",
    "verify_rewrite",
]
