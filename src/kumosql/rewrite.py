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
from .scripts import block_statements, script_skeleton
from .dryrun import Transport, check_rewrite
from .engine import RewriteRule, RuleDiagnostic, RuleOutput, available_rules, get_rule
from .equivalence import prove_equivalent
from .layout_equivalence import created_function_calls, layout_only_change, touching_literal_chunks
from . import prover_context
from .smt_equivalence import SmtStatus, prove_equivalent_smt
from .sqlx import looks_like_sqlx, mask_sqlx_by_content, split_sqlx_sections

# Import built-in rules so they are registered.
from . import cleanup as _cleanup  # noqa: F401
from . import cost_rules as _cost_rules  # noqa: F401
from . import formatting as _formatting  # noqa: F401
from . import inline_ctes as _inline_ctes  # noqa: F401
from . import lift_subqueries as _lift_subqueries  # noqa: F401
from . import qualify_columns as _qualify_columns  # noqa: F401


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


DEFAULT_SMT_TIMEOUT_MS = 5000


def _predicates_masked(query: exp.Expression) -> str:
    """Render a query with every WHERE, HAVING, QUALIFY and JOIN ON condition blanked."""

    masked = query.copy()
    for node in list(masked.find_all(exp.Where, exp.Having, exp.Qualify)):
        node.set("this", exp.Placeholder())
    for join in list(masked.find_all(exp.Join)):
        if join.args.get("on") is not None:
            join.set("on", exp.Placeholder())
    return masked.sql(dialect="bigquery", comments=False)


def _predicate_only_change(old_sql: str, new_sql: str) -> bool:
    try:
        old = parse_statements(old_sql)[0]
        new = parse_statements(new_sql)[0]
        return _predicates_masked(old) == _predicates_masked(new)
    except Exception:
        return False


def _lossy_types(query: exp.Expression) -> str | None:
    for node in query.find_all(exp.DataType):
        if node.this in (exp.DataType.Type.FLOAT, exp.DataType.Type.UUID):
            return node.this.value
    return None


def _verify_sql(
    before: str,
    after: str,
    smt_checks: list[VerificationCheck] | None = None,
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
) -> tuple[bool, list[str]]:
    if layout_only_change(before, after):
        # Only whitespace and the case of reserved words and built-in calls changed: proven for any
        # statement, including ones sqlglot cannot parse or keeps as an opaque command.
        return True, []
    if touching_literal_chunks(before) or touching_literal_chunks(after):
        return False, ["two string literals touch ('a''b'); GoogleSQL needs whitespace or a comment between them"]
    calls = created_function_calls(before), created_function_calls(after)
    if calls[0] is None or calls[0] != calls[1]:
        # sqlglot prints `abs(1)` and `ABS(1)` alike, but a script can create both as temporary functions.
        return False, ["a call to a function the script creates changed (function names are case sensitive)"]
    try:
        left = block_statements(before)
        right = block_statements(after)
        left = parse_statements(before) if left is None else left
        right = parse_statements(after) if right is None else right
    except Exception as exc:
        return False, [f"strict parse failed: {exc}"]
    if len(left) != len(right):
        return False, [f"statement count changed from {len(left)} to {len(right)}"]
    if block_statements(before) is not None or block_statements(after) is not None:
        # In a script only the queries are proven; the control flow, conditions and declarations around them must not change.
        if script_skeleton(before) != script_skeleton(after):
            return False, ["script structure changed: control flow, conditions or declarations outside the queries differ"]

    problems: list[str] = []
    for index, (old, new) in enumerate(zip(left, right)):
        old_shell, old_query = _statement_shell(old)
        new_shell, new_query = _statement_shell(new)
        if old_shell != new_shell:
            problems.append(f"statement {index}: text outside the query changed")
            continue
        if old_query is None:
            continue
        lossy = _lossy_types(old_query)
        if lossy:
            # The BigQuery generator prints FLOAT (a 32-bit float in GoogleSQL) as FLOAT64 and UUID as STRING,
            # on both sides of the comparison, so a rewrite would pass the check while changing the type.
            problems.append(f"statement {index}: type {lossy} is not rendered faithfully for BigQuery")
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
            message = f"statement {index}: {proof.reason}" + (f" ({detail})" if detail else "")
            # The solver (algebraic rewrites plus SMT, using the project's declared keys and
            # NOT NULL columns) tries any change; with it off, only a change confined to predicates.
            solver_on = prover_context.settings()["enabled"]
            if solver_on or _predicate_only_change(old_sql, new_sql):
                if solver_on:
                    smt = prover_context.prove(
                        old_sql,
                        new_sql,
                        timeout_ms=None if smt_timeout_ms == DEFAULT_SMT_TIMEOUT_MS else smt_timeout_ms,
                    )
                else:
                    smt = prove_equivalent_smt(old_sql, new_sql, timeout_ms=smt_timeout_ms)
                if smt.status is SmtStatus.PROVEN_EQUIVALENT:
                    if smt_checks is not None:
                        smt_checks.append(
                            VerificationCheck(
                                "smt_proof",
                                "passed",
                                f"statement {index}: "
                                + ("change proven equivalent by the solver" if solver_on else "predicate-only change proven equivalent by SMT"),
                                (("assumptions", tuple(smt.assumptions)),),
                            )
                        )
                    continue
                label = (
                    "SMT found a counterexample"
                    if smt.status is SmtStatus.NOT_EQUIVALENT
                    else "SMT could not prove it"
                )
                message += f"; {label}: {smt.reason}"
                if smt_checks is not None:
                    smt_checks.append(
                        VerificationCheck(
                            "smt_proof",
                            "refuted" if smt.status is SmtStatus.NOT_EQUIVALENT else "not_proven",
                            f"statement {index}: {smt.reason}",
                        )
                    )
            problems.append(message)
    return not problems, problems


def _verify_sqlx(
    before: str,
    after: str,
    smt_checks: list[VerificationCheck] | None = None,
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
) -> tuple[bool, list[str]]:
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
            ok, section_problems = _verify_sql(
                mask_sqlx_by_content(old), mask_sqlx_by_content(new), smt_checks, smt_timeout_ms
            )
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
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
) -> Verification:
    """Check a rewrite and return one label with the evidence supporting it.

    A caller may attach a planner check when one has run. Planner evidence is
    reported separately from the structural proof; it can yield
    ``planner_checked`` when no proof is available, but that label is not
    trusted for automatic acceptance.
    """

    return _verify_rewrite(
        before, after, planner_check=planner_check, smt_timeout_ms=smt_timeout_ms
    )


def _verify_rewrite(
    before: str,
    after: str,
    *,
    planner_check: VerificationCheck | None = None,
    rewrite_succeeded: bool = True,
    failure_details: tuple[str, ...] = (),
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
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

    smt_checks: list[VerificationCheck] = []
    if looks_like_sqlx(before) or looks_like_sqlx(after):
        ok, problems = _verify_sqlx(before, after, smt_checks, smt_timeout_ms)
    else:
        ok, problems = _verify_sql(before, after, smt_checks, smt_timeout_ms)

    proof_detail = (
        "Every changed statement was proven equivalent."
        if ok
        else "; ".join(problems) or "The equivalence prover could not establish equivalence."
    )
    checks.append(
        VerificationCheck("equivalence_proof", "passed" if ok else "not_proven", proof_detail)
    )
    checks.extend(smt_checks)
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


@dataclass(frozen=True)
class IdempotenceCheck:
    """Whether re-running a rule list on its own output changes it again."""

    rules: tuple[str, ...]
    idempotent: bool
    sql: str
    rerun_sql: str
    rules_that_changed: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "idempotence",
            "outcome": "passed" if self.idempotent else "failed",
            "rules": list(self.rules),
            "rules_that_changed": list(self.rules_that_changed),
        }


def check_idempotence(
    names: Iterable[str],
    sql: str,
    *,
    overrides: Mapping[str, RewriteRule] | None = None,
) -> IdempotenceCheck:
    """Apply ``names`` to ``sql``, then again to the output, and compare the text.

    The comparison is on exact text so formatting oscillation is caught. Running
    the rules twice doubles the work, so this is opt in (``python -m kumosql rewrite-sql
    --check-idempotence``) rather than part of ``apply_rules``.
    """

    rules = tuple(names)
    first = apply_rules(rules, sql, overrides=overrides)
    second = apply_rules(rules, first.sql, overrides=overrides)
    changed = tuple(step.rule for step in second.steps if step.changes or step.sql != step.input_sql)
    return IdempotenceCheck(
        rules=rules,
        idempotent=second.sql == first.sql and not changed,
        sql=first.sql,
        rerun_sql=second.sql,
        rules_that_changed=changed,
    )


def canonical_rule_order() -> tuple[str, ...]:
    """The rules that can run together, in an order whose output is a fixed point.

    ``lift_subqueries`` and ``inline_single_use_ctes`` are inverses, so a
    pipeline holding both undoes and redoes its own work on every run; the
    canonical order keeps ``inline_single_use_ctes`` and leaves the lifter to
    be run on its own, and so does ``qualify_columns``, which makes SQL longer
    and is a style choice. Rules that re-render a statement discard the layout
    ``format_sql`` produced, so ``format_sql`` goes last.
    """

    names = [n for n in available_rules() if n not in ("lift_subqueries", "format_sql", "qualify_columns")]
    # Removing unused and duplicate CTEs can leave another CTE with a single
    # reader, so inlining has to come after both or one pass is not enough.
    if "inline_single_use_ctes" in names:
        names.remove("inline_single_use_ctes")
        after = max((names.index(n) for n in ("deduplicate_ctes", "remove_unused_ctes") if n in names), default=-1)
        names.insert(after + 1, "inline_single_use_ctes")
    if "format_sql" in available_rules():
        names.append("format_sql")
    return tuple(names)


__all__ = [
    "IdempotenceCheck",
    "PipelineResult",
    "RewriteResult",
    "Verification",
    "VerificationCheck",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "attach_planner_check",
    "available_rules",
    "canonical_rule_order",
    "check_idempotence",
    "verify_rewrite",
]
