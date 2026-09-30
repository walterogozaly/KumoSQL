"""KumoSQL."""

from .lift_subqueries import (
    LiftDiagnostic,
    LiftResult,
    count_inline_subqueries,
    lift_subqueries,
)
from .engine import (
    RewriteRule,
    RuleDiagnostic,
    RuleOutput,
    available_rules,
    get_rule,
    register_rule,
)
from .inline_ctes import InlineSingleUseCtesRule
from .cleanup import (
    DeduplicateCtesRule,
    RemoveRedundantParenthesesRule,
    RemoveTrivialPredicatesRule,
    RemoveUnusedCtesRule,
)
from .formatting import (
    Complexity,
    FormatPreferences,
    FormatSqlRule,
    complexity,
    format_sql,
)
from .scopes import Scope, get_scope, list_scopes, save_scope
from .rewrite import (
    PipelineResult,
    RewriteResult,
    Verification,
    VerificationStatus,
    apply_rule,
    apply_rules,
    verify_rewrite,
)
from .equivalence import (
    EquivalenceResult,
    EquivalenceStatus,
    build_bag_verifier_sql,
    prove_equivalent,
)
from .smt_equivalence import (
    Counterexample,
    SmtEquivalenceResult,
    SmtStatus,
    prove_equivalent_smt,
)
from .result_equivalence import (
    ResultEquivalence,
    ResultEquivalenceStatus,
    assert_result_equivalent,
    check_result_equivalence,
    generate_synthetic_dataset,
)
from .pipeline import (
    ColumnRef,
    Pipeline,
    Target,
    load_compiled_graph,
    load_sqlx_project,
)
from .identity import IdentityResolution, NodeIdentity, normalize_table_reference
from .near_duplicates import (
    ClauseDifference,
    NearDuplicateCluster,
    NearDuplicateVariant,
    QueryParameter,
    find_near_duplicates,
)
from .dryrun import (
    DryRunResult,
    RewriteCheck,
    check_rewrite,
    dry_run,
    fetch_table_schemas,
)
from .fingerprint import (
    ComparisonPlan,
    Location,
    ModelDiff,
    TableComparison,
    compare_snapshots,
    compare_tables_sql,
    diff_rows_sql,
    plan_output_comparison,
    summarize_comparison,
    table_fingerprint_sql,
)

__all__ = [
    "LiftDiagnostic",
    "LiftResult",
    "count_inline_subqueries",
    "lift_subqueries",
    "EquivalenceResult",
    "EquivalenceStatus",
    "build_bag_verifier_sql",
    "prove_equivalent",
    "Counterexample",
    "SmtEquivalenceResult",
    "SmtStatus",
    "prove_equivalent_smt",
    "ResultEquivalence",
    "ResultEquivalenceStatus",
    "assert_result_equivalent",
    "check_result_equivalence",
    "generate_synthetic_dataset",
    "Complexity",
    "FormatPreferences",
    "FormatSqlRule",
    "Scope",
    "complexity",
    "format_sql",
    "get_scope",
    "list_scopes",
    "save_scope",
    "InlineSingleUseCtesRule",
    "DeduplicateCtesRule",
    "RemoveRedundantParenthesesRule",
    "RemoveTrivialPredicatesRule",
    "RemoveUnusedCtesRule",
    "PipelineResult",
    "RewriteResult",
    "RewriteRule",
    "RuleDiagnostic",
    "RuleOutput",
    "Verification",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "available_rules",
    "get_rule",
    "register_rule",
    "verify_rewrite",
    "ColumnRef",
    "IdentityResolution",
    "NodeIdentity",
    "Pipeline",
    "Target",
    "normalize_table_reference",
    "load_compiled_graph",
    "load_sqlx_project",
    "ClauseDifference",
    "NearDuplicateCluster",
    "NearDuplicateVariant",
    "QueryParameter",
    "find_near_duplicates",
    "DryRunResult",
    "RewriteCheck",
    "check_rewrite",
    "dry_run",
    "fetch_table_schemas",
    "ComparisonPlan",
    "Location",
    "ModelDiff",
    "TableComparison",
    "compare_snapshots",
    "compare_tables_sql",
    "diff_rows_sql",
    "plan_output_comparison",
    "summarize_comparison",
    "table_fingerprint_sql",
]

__version__ = "0.1.0"
