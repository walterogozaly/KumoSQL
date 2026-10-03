"""Run the local generic rewrite safety gate.

Run from the repository root with ``python tools/check_safety_corpus.py``.
The report contains aggregate counts only; the synthetic corpus and generated
rows contain no project or user SQL.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumosql import apply_rule, apply_rules, available_rules
from kumosql.ast_utils import cte_dependency_errors, parse_statements
from kumosql.result_equivalence import ResultEquivalenceStatus, check_result_equivalence
from kumosql.sqlx import looks_like_sqlx, mask_sqlx_interpolations, split_sqlx_sections


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "tests" / "fixtures" / "rewrite_safety_corpus.json"
DML_CORPUS_PATH = ROOT / "tests" / "fixtures" / "rewrite_safety_dml_corpus.json"
SCHEMA = {
    "generic_table": {
        "record_id": "INT64",
        "category": "STRING",
        "is_active": "BOOL",
        "amount": "FLOAT64",
    },
    "aux_table": {"record_id": "INT64", "category": "STRING", "amount": "FLOAT64"},
}
SEEDS = range(5)


def _load_corpus() -> list[str]:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(corpus, list) or not corpus or any(
        not isinstance(sql, str) or not sql.strip() for sql in corpus
    ):
        raise ValueError("the generic safety corpus must be a nonempty list of SQL strings")
    return corpus


def _load_dml_corpus() -> list[str]:
    corpus = json.loads(DML_CORPUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(corpus, list) or not corpus or any(
        not isinstance(sql, str) or not sql.strip() for sql in corpus
    ):
        raise ValueError("the generic DML safety corpus must be a nonempty list of SQL strings")
    return corpus


def _parseable_without_broken_ctes(sql: str) -> bool:
    sections = split_sqlx_sections(sql) if looks_like_sqlx(sql) else [("sql", sql)]
    statement_count = 0
    try:
        for kind, section in sections:
            if kind == "block" or not section.strip():
                continue
            masked, _ = mask_sqlx_interpolations(section) if looks_like_sqlx(sql) else (section, ())
            statements = parse_statements(masked)
            statement_count += len(statements)
            if any(cte_dependency_errors(statement) for statement in statements):
                return False
    except Exception:
        return False
    return statement_count > 0


def _sqlx_interpolations(sql: str) -> list[str]:
    """Return exact, balanced SQLX interpolation fragments, including nested ones.

    The scanner handles braces inside quoted strings and backslash escapes. It
    deliberately does not parse JavaScript; its job is to independently detect
    lost or changed interpolation text, not to validate expression semantics.
    """

    fragments: list[str] = []
    for start in range(len(sql) - 1):
        if sql[start : start + 2] != "${":
            continue
        depth = 1
        quote: str | None = None
        escaped = False
        end = start + 2
        while end < len(sql) and depth:
            char = sql[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quote is not None:
                if char == quote:
                    quote = None
            elif char in ("'", '"', "`"):
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            end += 1
        if depth == 0:
            fragments.append(sql[start:end])
    return fragments


def _sqlx_placeholders_preserved(source: str, candidate: str) -> bool:
    """Compare exact interpolation text and multiplicity, independent of order."""

    source_is_sqlx = looks_like_sqlx(source)
    candidate_is_sqlx = looks_like_sqlx(candidate)
    if source_is_sqlx != candidate_is_sqlx:
        return False
    if not source_is_sqlx:
        return True
    return Counter(_sqlx_interpolations(source)) == Counter(_sqlx_interpolations(candidate))


def _parsed_ast_is_unchanged(source: str, candidate: str) -> bool:
    """Check a DML candidate is parseable and structurally identical to its input."""

    if not _parseable_without_broken_ctes(source) or not _parseable_without_broken_ctes(candidate):
        return False
    try:
        source_statements = parse_statements(source)
        candidate_statements = parse_statements(candidate)
    except Exception:
        return False
    return source_statements == candidate_statements


def _check_successful_output(source: str, candidate: str, totals: dict[str, int]) -> None:
    if not _sqlx_placeholders_preserved(source, candidate):
        totals["placeholder_mismatches"] += 1
        totals["invalid_successes"] += 1
        return
    if not _parseable_without_broken_ctes(candidate):
        totals["invalid_successes"] += 1
        return

    execution = check_result_equivalence(source, candidate, SCHEMA, seeds=SEEDS)
    if execution.status is ResultEquivalenceStatus.DIFFERENT:
        totals["unsafe_successes"] += 1
    elif execution.status is not ResultEquivalenceStatus.EQUIVALENT:
        totals["uncheckable_successes"] += 1


def run_gate() -> dict[str, object]:
    """Run every registered rule and their ordered full pipeline on the corpus."""

    corpus = _load_corpus()
    rules = available_rules()
    # Opt-in rules (qualify_columns) need table columns the corpus does not declare; tests cover them.
    names = [name for name, rule in rules.items() if not rule.opt_in]
    totals: dict[str, int] = {
        "rule_count": len(names),
        "rule_applications": 0,
        "pipeline_applications": 0,
        "successful_outputs": 0,
        "rule_failures": 0,
        "unproven_outputs": 0,
        "invalid_successes": 0,
        "placeholder_mismatches": 0,
        "unsafe_successes": 0,
        "uncheckable_successes": 0,
    }
    changes_by_rule = {name: 0 for name in names}

    for source in corpus:
        for name in names:
            result = apply_rule(name, source)
            totals["rule_applications"] += 1
            if not result.rule_success:
                totals["rule_failures"] += 1
                continue
            if not result.verification.trusted:
                totals["unproven_outputs"] += 1
                continue
            totals["successful_outputs"] += 1
            if result.sql != source:
                changes_by_rule[name] += 1
            _check_successful_output(source, result.sql, totals)

        result = apply_rules(names, source)
        totals["pipeline_applications"] += 1
        if any(not step.rule_success for step in result.steps):
            totals["rule_failures"] += 1
            continue
        if not result.verification.trusted:
            totals["unproven_outputs"] += 1
            continue
        totals["successful_outputs"] += 1
        _check_successful_output(source, result.sql, totals)

    totals["rules_with_observed_changes"] = sum(count > 0 for count in changes_by_rule.values())
    totals["successful_changes_by_rule"] = changes_by_rule

    dml_totals = _run_dml_gate(names)
    totals.update(dml_totals)
    return totals


def _run_dml_gate(names: list[str]) -> dict[str, int]:
    """Check generic UPDATE/DELETE/MERGE outputs remain valid and AST-identical."""

    corpus = _load_dml_corpus()
    totals = {
        "dml_cases": len(corpus),
        "dml_rule_applications": 0,
        "dml_pipeline_applications": 0,
        "dml_rule_failures": 0,
        "dml_unproven_outputs": 0,
        "dml_invalid_or_changed_outputs": 0,
    }
    for source in corpus:
        if not _parsed_ast_is_unchanged(source, source):
            totals["dml_invalid_or_changed_outputs"] += 1
        for name in names:
            result = apply_rule(name, source)
            totals["dml_rule_applications"] += 1
            if not result.rule_success:
                totals["dml_rule_failures"] += 1
            elif not result.verification.trusted:
                totals["dml_unproven_outputs"] += 1
            if not _parsed_ast_is_unchanged(source, result.sql):
                totals["dml_invalid_or_changed_outputs"] += 1

        result = apply_rules(names, source)
        totals["dml_pipeline_applications"] += 1
        if any(not step.rule_success for step in result.steps):
            totals["dml_rule_failures"] += 1
        elif not result.verification.trusted:
            totals["dml_unproven_outputs"] += 1
        if not _parsed_ast_is_unchanged(source, result.sql):
            totals["dml_invalid_or_changed_outputs"] += 1
    return totals


def main() -> int:
    totals = run_gate()
    report = {
        "rules_checked": totals["rule_count"],
        "successful_results": totals["successful_outputs"],
        "failed_results": totals["rule_failures"],
        "unproven_results": totals["unproven_outputs"],
        "invalid_successes": totals["invalid_successes"],
        "placeholder_mismatches": totals["placeholder_mismatches"],
        "unsafe_successes": totals["unsafe_successes"],
        "uncheckable_successes": totals["uncheckable_successes"],
        "rules_with_observed_changes": totals["rules_with_observed_changes"],
        "successful_changes_by_rule": totals["successful_changes_by_rule"],
        "dml_cases": totals["dml_cases"],
        "dml_rule_applications": totals["dml_rule_applications"],
        "dml_pipeline_applications": totals["dml_pipeline_applications"],
        "dml_failed_results": totals["dml_rule_failures"],
        "dml_unproven_results": totals["dml_unproven_outputs"],
        "dml_invalid_or_changed_outputs": totals["dml_invalid_or_changed_outputs"],
    }
    print(json.dumps(report, sort_keys=True))
    failed = any(
        totals[key]
        for key in (
            "rule_failures",
            "unproven_outputs",
            "invalid_successes",
            "placeholder_mismatches",
            "unsafe_successes",
            "uncheckable_successes",
            "dml_invalid_or_changed_outputs",
        )
    )
    all_rules_changed = totals["rules_with_observed_changes"] == totals["rule_count"]
    return 1 if failed or not all_rules_changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
