"""The reviewed registry of independent proof checkers, and the basis of every rewrite rule that has none.

A rewrite step is ``proven`` by the equivalence prover. For some rules a second check, written without any
code the rule or the prover uses (``proof_steps``, ``proof_ctes``, ``proof_syntax``, ``proof_qualify``, ``proof_format``), must also accept it.
This module is the single place that says which. Every rule in the rewrite registry is classified exactly one
way, and ``tests/test_proof_registry.py`` fails when a rule is registered without a classification, so adding
a rule means choosing between an independent checker and a named legacy basis. The acceptance layer
(``rewrite.apply_rule``) reads ``RULE_FAMILIES``, so neither a rule nor an override can opt out.

Moving a rule from ``LEGACY_BASIS`` to ``RULE_FAMILIES`` is done only after the checker has its corpus and
fault-injection tests (tests that corrupt the rule and the prover's normalizer the same way and require the
checker to refuse the result). Moving it back is a regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlglot import exp

from .proof_ctes import CTE_ASSUMPTIONS, CTE_FAMILY, check_cte_transition
from .proof_format import FORMAT_ASSUMPTIONS, FORMAT_FAMILY, check_format_transition
from .proof_steps import PREDICATE_ASSUMPTIONS, PREDICATE_FAMILY, RewriteStep, StepCheck, check_predicate_transition
from .proof_syntax import (
    DISTINCT_ASSUMPTIONS,
    DISTINCT_FAMILY,
    PAREN_ASSUMPTIONS,
    PAREN_FAMILY,
    check_syntax_transition,
)
from .proof_qualify import QUALIFY_ASSUMPTIONS, QUALIFY_FAMILY, check_qualify_transition


@dataclass(frozen=True)
class Family:
    """One checked family of steps: its assumptions and the function that re-derives them."""

    name: str
    label: str
    assumptions: tuple[str, ...]
    check: Callable[[RewriteStep, exp.Expression, exp.Expression], StepCheck]
    summary: str


FAMILIES: dict[str, Family] = {
    family.name: family
    for family in (
        Family(PREDICATE_FAMILY, "predicate", PREDICATE_ASSUMPTIONS, check_predicate_transition,
               "predicate folding: three-valued logic over atoms, exact literal domain, statement frame"),
        Family(CTE_FAMILY, "CTE", CTE_ASSUMPTIONS, check_cte_transition,
               "CTE binders: scope-resolved references replaced by definitions, trees compared"),
        Family(PAREN_FAMILY, "parenthesization", PAREN_ASSUMPTIONS, check_syntax_transition,
               "parentheses: identical trees once parenthesis nodes are ignored"),
        Family(DISTINCT_FAMILY, "DISTINCT", DISTINCT_ASSUMPTIONS, check_syntax_transition,
               "DISTINCT: only cleared where a plain GROUP BY keys are all projected"),
        Family(QUALIFY_FAMILY, "qualification", QUALIFY_ASSUMPTIONS, check_qualify_transition,
               "qualification: only qualifiers added, each naming the one readable source that has the column"),
        Family(FORMAT_FAMILY, "layout", FORMAT_ASSUMPTIONS, check_format_transition,
               "layout: identical tokens and comments apart from whitespace and the case of keywords, trees identical"),
    )
}

#: Rules whose every changed statement must also pass an independent checker, by rule name.
RULE_FAMILIES: dict[str, str] = {
    "remove_trivial_predicates": PREDICATE_FAMILY,
    "remove_unused_ctes": CTE_FAMILY,
    "inline_single_use_ctes": CTE_FAMILY,
    "deduplicate_ctes": CTE_FAMILY,
    "remove_redundant_parentheses": PAREN_FAMILY,
    "remove_redundant_distinct": DISTINCT_FAMILY,
    "qualify_columns": QUALIFY_FAMILY,
    "format_sql": FORMAT_FAMILY,
}

#: Rules with no independent checker yet, and what a proven step rests on instead.
LEGACY_BASIS: dict[str, str] = {
    "lift_subqueries": "the prover's normalized-AST comparison, which lifts both sides with the same lifter",
}


def classification(rule: str) -> str:
    """``"independent"`` or ``"legacy"``; raises ``KeyError`` for a rule that is in neither table."""

    if rule in RULE_FAMILIES:
        return "independent"
    if rule in LEGACY_BASIS:
        return "legacy"
    raise KeyError(f"rewrite rule {rule!r} has no proof-checker classification (see kumosql.proof_registry)")


__all__ = ["FAMILIES", "Family", "LEGACY_BASIS", "RULE_FAMILIES", "classification"]
