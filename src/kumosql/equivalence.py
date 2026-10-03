"""Conservative equivalence proofs for BigQuery query statements.

This module intentionally proves only a narrow class of equivalences. It
normalizes relational shape after CTE lifting and compares query ASTs under
bag semantics (row order ignored). It refuses to prove queries with value
nondeterminism or row-selection nondeterminism. A false negative is therefore
preferred to a false positive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import re

import sqlglot
from sqlglot import exp

from .ast_utils import (
    ambiguous_unnest_names,
    distinct_on,
    is_cte_reference_candidate,
    nearest_root_cte,
    set_with_clause,
    table_function_reads_cte,
    with_clause as _with_clause,
)
from .distinct_safety import distinct_is_redundant
from .lift_subqueries import lift_subqueries
from .named_windows import inline_named_windows
from .string_literals import canonical_literals, invalid_literal


class EquivalenceStatus(str, Enum):
    """Possible outcomes of the conservative prover."""

    PROVEN_EQUIVALENT = "proven_equivalent"
    NOT_PROVEN = "not_proven"


@dataclass(frozen=True)
class EquivalenceResult:
    """A proof result and the evidence needed to audit it."""

    status: EquivalenceStatus
    reason: str
    left_fingerprint: str | None = None
    right_fingerprint: str | None = None
    normalized_left: str | None = None
    normalized_right: str | None = None
    verifier_sql: str | None = None
    diagnostics: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return self.status is EquivalenceStatus.PROVEN_EQUIVALENT


_VALUE_NONDETERMINISTIC_TYPES = {
    "AnyValue",
    "ApproxDistinct",
    "ApproxQuantile",
    "ArrayAgg",
    "CurrentDate",
    "CurrentTime",
    "CurrentTimestamp",
    "GroupConcat",
    "MaxBy",
    "MinBy",
    "Rand",
    "TableSample",
    "Uuid",
}

_VALUE_NONDETERMINISTIC_NAMES = {
    "ANY_VALUE",
    "APPROX_COUNT_DISTINCT",
    "APPROX_QUANTILES",
    "CURRENT_DATE",
    "CURRENT_DATETIME",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "GENERATE_UUID",
    "RAND",
    "SESSION_USER",
}


# Value functions that are safe to leave in place when a rewrite does not touch
# them. Everything else in the nondeterminism sets above (windows, tie-sensitive
# or order-sensitive aggregates, sampling) depends on the input row set, so an
# identical expression is not enough and it keeps blocking the proof.
_STABLE_IF_UNCHANGED_TYPES = {
    "CurrentDate",
    "CurrentTime",
    "CurrentTimestamp",
    "Rand",
    "Uuid",
}

_STABLE_IF_UNCHANGED_NAMES = {
    "CURRENT_DATE",
    "CURRENT_DATETIME",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "GENERATE_UUID",
    "RAND",
    "SESSION_USER",
}


def _parse_single_query(sql: str) -> exp.Expression:
    statements = sqlglot.parse(sql, read="bigquery")
    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        raise ValueError(f"expected exactly one SQL statement, found {len(statements)}")
    statement = statements[0]
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise ValueError(
            f"only SELECT/UNION query statements are supported, got {type(statement).__name__}"
        )
    return inline_named_windows(statement)


def _cte_alias(cte: exp.CTE) -> str | None:
    alias = cte.args.get("alias")
    if alias is None:
        return None
    return getattr(alias, "name", None) or getattr(alias, "this", None)


def _root_with(query: exp.Expression) -> exp.With | None:
    return _with_clause(query)


def _canonicalize_cte_names(query: exp.Expression) -> None:
    """Alpha-rename root CTEs while preserving physical table references."""

    with_clause = _root_with(query)
    if not with_clause:
        return
    if any(
        isinstance(node, exp.With) and node is not with_clause
        for node in query.walk()
    ):
        raise ValueError("nested WITH scopes are not canonicalized conservatively")

    _canonicalize_cte_order(query, with_clause)

    # BigQuery resolves CTE names case-insensitively (`FROM A` reads CTE `a`), so
    # references are matched by lower-cased name; a CTE name repeated up to case
    # is ambiguous and not canonicalized.
    mapping: dict[str, str] = {}
    for index, cte in enumerate(with_clause.expressions, start=1):
        old = _cte_alias(cte)
        if old:
            if old.lower() in mapping:
                raise ValueError("CTE names repeat up to case")
            mapping[old.lower()] = f"__canonical_cte_{index:03d}"

    for cte in with_clause.expressions:
        old = _cte_alias(cte)
        if old and old.lower() in mapping:
            alias = cte.args.get("alias")
            alias.set("this", exp.to_identifier(mapping[old.lower()]))

    for table in query.find_all(exp.Table):
        old = table.name
        # A one-part table reference can be a CTE reference. Qualified tables
        # are physical objects and must never be renamed by this pass.
        if old.lower() in mapping and is_cte_reference_candidate(table):
            # ``FROM a`` is ``FROM a AS a``: keep the implicit range-variable
            # name so column qualifiers still match after renaming.
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(old)))
            table.set("this", exp.to_identifier(mapping[old.lower()]))


def _canonicalize_cte_order(query: exp.Expression, with_clause: exp.With) -> None:
    """Order root CTEs by first use, dependencies first.

    Non-recursive CTE order has no meaning beyond "defined before use", so two
    queries that differ only in CTE order (for example, after a rule moves a
    lifted CTE) should normalize identically. The pass refuses to reorder when
    references are ambiguous: recursive WITH, duplicate names, references that
    differ from a CTE name only in case, or references to a CTE that is not
    defined earlier (those could resolve to a physical table).
    """

    if with_clause.args.get("recursive"):
        return
    ctes = list(with_clause.expressions)
    names = [_cte_alias(cte) for cte in ctes]
    if any(name is None for name in names):
        return
    if len({name.lower() for name in names}) != len(names):
        return
    position = {name: index for index, name in enumerate(names)}
    lowered = {name.lower() for name in names}

    references: dict[int | None, list[str]] = {None: []}
    references.update({index: [] for index in range(len(ctes))})
    for table in query.find_all(exp.Table):
        if not is_cte_reference_candidate(table) or table.name.lower() not in lowered:
            continue
        if table.name not in position:
            return
        owner = nearest_root_cte(table, ctes)
        owner_index = None
        if owner is not None:
            owner_index = next(i for i, cte in enumerate(ctes) if cte is owner)
            if position[table.name] >= owner_index:
                return
        references[owner_index].append(table.name)

    ordered: list[int] = []
    seen: set[int] = set()

    def visit(index: int) -> None:
        if index in seen:
            return
        seen.add(index)
        for name in references[index]:
            visit(position[name])
        ordered.append(index)

    for name in references[None]:
        visit(position[name])
    for index in range(len(ctes)):
        visit(index)
    with_clause.set("expressions", [ctes[index] for index in ordered])


def _merge_duplicate_ctes(query: exp.Expression) -> bool:
    """Merge root CTEs with identical bodies and drop unreferenced CTEs.

    Runs after ``_canonicalize_cte_names``, so every CTE reference in a body
    already uses a canonical name and two bodies with the same text read the
    same relations. A non-recursive BigQuery CTE is evaluated per reference,
    so two deterministic CTEs with the same body produce the same rows, and a
    CTE nobody references cannot affect the result. A change to a RAND()-style
    call is refused later by comparing the calls before and after normalization,
    so merging never hides a RAND() difference.

    Returns whether anything changed, so the caller can re-canonicalize.
    """

    clause = _root_with(query)
    if not clause or clause.args.get("recursive"):
        return False
    ctes = list(clause.expressions)
    names = [_cte_alias(cte) for cte in ctes]
    if any(name is None or not name.startswith("__canonical_cte_") for name in names):
        return False

    changed = False
    while True:
        tables = [
            table
            for table in query.find_all(exp.Table)
            if is_cte_reference_candidate(table)
        ]
        by_body: dict[str, exp.CTE] = {}
        merged = False
        for cte in list(clause.expressions):
            body = _canonical_sql(cte.this)
            first = by_body.get(body)
            if first is None:
                by_body[body] = cte
                continue
            old, new = _cte_alias(cte), _cte_alias(first)
            for table in tables:
                if table.name == old:
                    table.set("this", exp.to_identifier(new))
            cte.pop()
            merged = changed = True
            break
        if merged:
            continue

        referenced = {table.name for table in tables}
        unused = [cte for cte in clause.expressions if _cte_alias(cte) not in referenced]
        if not unused:
            break
        for cte in unused:
            cte.pop()
        changed = True

    if not clause.expressions:
        set_with_clause(query, None)
    return changed


def _cte_references_are_unambiguous(query: exp.Expression) -> bool:
    """Whether every root CTE reference resolves exactly and in order.

    CTE merging and unused-CTE removal depend on knowing every reference. A
    reference that differs from a CTE name only in case, or names a CTE that
    is defined later (so it may be a physical table), makes that unsafe.
    """

    clause = _root_with(query)
    if not clause:
        return True
    ctes = list(clause.expressions)
    names = [_cte_alias(cte) for cte in ctes]
    if any(name is None for name in names):
        return False
    if len({name.lower() for name in names}) != len(names):
        return False
    position = {name: index for index, name in enumerate(names)}
    lowered = {name.lower() for name in names}
    for table in query.find_all(exp.Table):
        if not is_cte_reference_candidate(table) or table.name.lower() not in lowered:
            continue
        if table.name not in position:
            return False
        owner = nearest_root_cte(table, ctes)
        if owner is not None:
            owner_index = next(i for i, cte in enumerate(ctes) if cte is owner)
            if position[table.name] >= owner_index:
                return False
    return True


def _paren_is_semantic(paren: exp.Paren) -> bool:
    """Parentheses that carry meaning beyond grouping.

    BigQuery derives an output column name from an unaliased projection, so
    ``SELECT (a)`` is kept as written. Parentheses around a query are part of
    the query syntax, not an expression grouping.
    """

    if isinstance(paren.this, (exp.Query, exp.Subquery)):
        return True
    parent = paren.parent
    # ``(a).b`` reads field b of a; ``a.b`` reads column b of table a.
    if isinstance(parent, (exp.Dot, exp.Bracket)):
        return True
    return isinstance(parent, (exp.Select, exp.Union)) and paren.arg_key == "expressions"


def _strip_grouping_parens(query: exp.Expression) -> None:
    """Remove expression-grouping parentheses; the AST already encodes grouping."""

    for paren in list(query.find_all(exp.Paren)):
        if paren.parent is None and paren is not query:
            continue  # already detached by an enclosing replacement
        if _paren_is_semantic(paren):
            continue
        paren.replace(paren.this)


def _flatten_connectors(query: exp.Expression) -> None:
    """Rebuild AND/OR chains left-deep so associativity does not matter.

    AND and OR are associative in BigQuery's three-valued logic, and BigQuery
    does not promise an evaluation order, so ``a AND (b AND c)`` and
    ``(a AND b) AND c`` are the same predicate.
    """

    for kind in (exp.And, exp.Or):
        for node in list(query.find_all(kind)):
            if node.parent is None and node is not query:
                continue
            if isinstance(node.parent, kind):
                continue  # rebuilt as part of the enclosing chain
            operands = list(node.flatten())
            if len(operands) <= 2:
                continue
            rebuilt = operands[0]
            for operand in operands[1:]:
                rebuilt = kind(this=rebuilt, expression=operand)
            node.replace(rebuilt)


_INT64_MAX = 2**63 - 1


def _literal_compare(node, left, right) -> bool | None:
    """Compare two numeric literals the way BigQuery would, when certain.

    Two INT64 literals compare exactly. Any other pair (a FLOAT64 or NUMERIC
    literal) is only decided when the texts are identical, because BigQuery
    may coerce INT64 to FLOAT64 and lose precision.
    """

    if not (
        isinstance(left, exp.Literal)
        and isinstance(right, exp.Literal)
        and not left.is_string
        and not right.is_string
    ):
        return None
    texts = (left.this, right.this)
    if all(re.fullmatch(r"[0-9]+", text) for text in texts):
        a, b = (int(text) for text in texts)
        if max(a, b) > _INT64_MAX:
            return None
    elif texts[0] == texts[1]:
        a = b = 0
    else:
        return None
    return {
        exp.EQ: a == b,
        exp.NEQ: a != b,
        exp.GT: a > b,
        exp.GTE: a >= b,
        exp.LT: a < b,
        exp.LTE: a <= b,
    }[type(node)]


def _constant_truth(node: exp.Expression) -> bool | None:
    """The truth value of a predicate made only of literals, if known."""

    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Not):
        inner = _constant_truth(node.this)
        return None if inner is None else not inner
    if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    left, right = node.this, node.expression
    folded = _literal_compare(node, left, right)
    if folded is not None:
        return folded
    # Strings: only identical text without escapes is known to be equal.
    if (
        isinstance(node, (exp.EQ, exp.NEQ))
        and isinstance(left, exp.Literal)
        and isinstance(right, exp.Literal)
        and left.is_string
        and right.is_string
        and left.this == right.this
        and "\\" not in left.this
    ):
        return isinstance(node, exp.EQ)
    return None


def _normalize_predicate(node: exp.Expression) -> exp.Expression:
    """Apply three-valued-logic identities to a predicate tree.

    Only identities that hold for TRUE, FALSE and NULL are used:
    ``p AND TRUE = p`` and ``p OR FALSE = p``. Literal-only comparisons are
    evaluated. Annihilators such as ``p AND FALSE`` are deliberately not used,
    because they discard ``p`` (and any error it would raise). The walk stays
    in boolean context: it descends only through AND, OR and NOT.
    """

    if isinstance(node, (exp.And, exp.Or)):
        identity = isinstance(node, exp.And)
        left = _normalize_predicate(node.this)
        right = _normalize_predicate(node.expression)
        if _constant_truth(left) is identity:
            return right
        if _constant_truth(right) is identity:
            return left
        node.set("this", left)
        node.set("expression", right)
        return node
    if isinstance(node, exp.Not):
        node.set("this", _normalize_predicate(node.this))
    truth = _constant_truth(node)
    if truth is not None:
        return exp.Boolean(this=truth)
    return node


def _normalize_predicates(query: exp.Expression) -> None:
    """Normalize WHERE, HAVING, QUALIFY and JOIN ... ON conditions.

    ``WHERE TRUE`` and ``QUALIFY TRUE`` filter nothing and are dropped.
    ``HAVING TRUE`` is dropped only when there is a GROUP BY, because a HAVING
    clause can otherwise turn the query into an aggregate.
    """

    for clause_type in (exp.Where, exp.Having, exp.Qualify):
        for clause in list(query.find_all(clause_type)):
            clause.set("this", _normalize_predicate(clause.this))
            if _constant_truth(clause.this) is not True:
                continue
            owner = clause.parent
            if clause_type is exp.Having and not (
                owner is not None and owner.args.get("group")
            ):
                continue
            clause.pop()
    for join in list(query.find_all(exp.Join)):
        on = join.args.get("on")
        if on is not None:
            join.set("on", _normalize_predicate(on))


_OPERATOR_TYPES = (exp.Binary, exp.Unary, exp.Between, exp.In)


def _parenthesize_operators(query: exp.Expression) -> exp.Expression:
    """Return a copy with every operator expression in parentheses.

    Grouping parentheses were stripped during normalization, so the rendered
    text alone could make ``(a OR b) AND c`` and ``a OR (b AND c)`` look the
    same. Fully parenthesizing operator operands makes the rendering encode
    the tree unambiguously before it is fingerprinted.
    """

    copy = query.copy()
    for node in list(copy.walk()):
        if not isinstance(node, _OPERATOR_TYPES) or isinstance(node, exp.Paren):
            continue
        # Wrap every operator, not just operator operands: some non-operator
        # nodes (INTERVAL, ORDER BY items) also render a child inline.
        if node.parent is not None and not isinstance(node.parent, exp.Paren):
            wrapper = exp.Paren()
            node.replace(wrapper)
            wrapper.set("this", node)
    return copy


def _remove_comments(query: exp.Expression) -> None:
    for node in query.walk():
        if node.args.get("comments") is not None:
            node.set("comments", None)


def _remove_unordered_result_order(query: exp.Expression) -> None:
    """Ignore root result ordering when it cannot affect row membership.

    Under ``DISTINCT ON`` it can: the ORDER BY picks the row kept for each key.
    """

    if query.args.get("limit") is None and query.args.get("offset") is None and not distinct_on(query):
        query.set("order", None)


def _drop_redundant_distinct(query: exp.Expression) -> None:
    """DISTINCT over a GROUP BY whose keys are all projected removes no rows."""

    for select in query.find_all(exp.Select):
        if distinct_is_redundant(select):
            select.set("distinct", None)


def _is_unchanged_safe_value(node: exp.Expression) -> bool:
    if type(node).__name__ in _STABLE_IF_UNCHANGED_TYPES:
        return True
    return isinstance(node, exp.Anonymous) and (node.name or "").upper() in _STABLE_IF_UNCHANGED_NAMES


def _value_nondeterminism_sites(query: exp.Expression) -> tuple[str, ...]:
    """Canonical SQL of each RAND/CURRENT_*/UUID-style call, in walk order."""

    return tuple(
        node.sql(dialect="bigquery", normalize_functions="upper", comments=False)
        for node in query.walk()
        if _is_unchanged_safe_value(node)
    )


# Window functions whose value for a row depends only on the row's partition and its peers
# (rows with equal ORDER BY keys), never on how ties happen to be ordered.
_PEER_STABLE_WINDOW_FUNCTIONS = {"Rank", "DenseRank", "PercentRank", "CumeDist"}
_ANONYMOUS_WINDOW_NAMES = {"RANK": "Rank", "DENSE_RANK": "DenseRank", "PERCENT_RANK": "PercentRank", "CUME_DIST": "CumeDist"}
_TIE_STABLE_AGGREGATES = {
    "Sum", "Count", "Avg", "Min", "Max", "CountIf", "LogicalAnd", "LogicalOr", "Stddev", "StddevPop",
    "StddevSamp", "Variance", "VariancePop", "BitwiseAndAgg", "BitwiseOrAgg", "BitwiseXorAgg",
}


def window_is_tie_stable(window: exp.Window) -> bool:
    """Whether a window function gives the same value on every execution of the same input.

    RANK-style functions and aggregates over a whole partition or a RANGE frame (the
    default when there is an ORDER BY) give tied rows the same value. ROW_NUMBER, LAG,
    FIRST_VALUE, NTILE and aggregates over a ROWS frame depend on how ties are broken,
    which BigQuery leaves unspecified. A named window (``OVER w``) is judged unstable
    unless the function itself is peer stable.
    """

    function = window.this
    while isinstance(function, (exp.IgnoreNulls, exp.RespectNulls)):
        function = function.this
    name = type(function).__name__
    if isinstance(function, exp.Anonymous):  # sqlglot 26 parses RANK() and friends as plain calls
        name = _ANONYMOUS_WINDOW_NAMES.get((function.name or "").upper(), name)
    if name in _PEER_STABLE_WINDOW_FUNCTIONS:
        return True
    if name not in _TIE_STABLE_AGGREGATES or window.args.get("alias") is not None:
        return False
    spec = window.args.get("spec")
    if spec is None:
        return True
    if (spec.args.get("kind") or "").upper() == "RANGE" and window.args.get("order") is not None:
        return True
    start, end = spec.args.get("start"), spec.args.get("end")
    whole = (
        str(start or "").upper() == "UNBOUNDED" and (spec.args.get("start_side") or "").upper() == "PRECEDING"
        and str(end or "").upper() == "UNBOUNDED" and (spec.args.get("end_side") or "").upper() == "FOLLOWING"
    )
    return whole


def _nondeterminism_reasons(
    query: exp.Expression, *, allow_unchanged_values: bool = False
) -> list[str]:
    reasons: list[str] = []
    for name in sorted(ambiguous_unnest_names(query)):
        reasons.append(f"ambiguous relation reference: UNNEST({name}) may be a misparsed join")
    for node in query.walk():
        if allow_unchanged_values and _is_unchanged_safe_value(node):
            continue
        node_type = type(node).__name__
        if node_type in _VALUE_NONDETERMINISTIC_TYPES:
            reasons.append(f"value nondeterminism: {node.sql(dialect='bigquery')}")
            continue
        if node_type in {"ArgMax", "ArgMin"}:
            reasons.append(f"tie-sensitive aggregate: {node.sql(dialect='bigquery')}")
            continue
        if isinstance(node, exp.Window) and not window_is_tie_stable(node):
            reasons.append(f"window function not proven tie-stable: {node.sql(dialect='bigquery')}")
            continue
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").upper()
            if name in _VALUE_NONDETERMINISTIC_NAMES or name.startswith("APPROX_"):
                reasons.append(f"value nondeterminism: {node.sql(dialect='bigquery')}")
    return list(dict.fromkeys(reasons))


def _has_row_selection_nondeterminism(query: exp.Expression) -> bool:
    # Without a provably unique ORDER BY key, LIMIT/OFFSET can select a
    # different subset on separate executions. Proving key uniqueness requires
    # schema and constraint metadata, which this offline tool intentionally does
    # not assume.
    return any(
        type(node).__name__ in {"Limit", "Offset"}
        for node in query.walk()
    )


def _canonical_sql(query: exp.Expression) -> str:
    return _parenthesize_operators(query).sql(
        dialect="bigquery",
        pretty=False,
        normalize_functions="upper",
        identify=False,
    )


def _fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def build_bag_verifier_sql(left_sql: str, right_sql: str) -> str:
    """Build BigQuery SQL that compares two query outputs as multisets.

    The generated SQL is an execution artifact, not a static proof. It counts
    each JSON-encoded result row on both sides and compares multiplicities, so
    output ordering does not affect the result. Callers must still reject
    value-level nondeterminism before treating a result as a proof.
    """

    left = left_sql.strip().rstrip(";").strip()
    right = right_sql.strip().rstrip(";").strip()
    return f"""WITH
left_counts AS (
  SELECT
    TO_JSON_STRING(left_row) AS row_signature,
    COUNT(*) AS row_count
  FROM (
    {left}
  ) AS left_row
  GROUP BY row_signature
),
right_counts AS (
  SELECT
    TO_JSON_STRING(right_row) AS row_signature,
    COUNT(*) AS row_count
  FROM (
    {right}
  ) AS right_row
  GROUP BY row_signature
),
joined_counts AS (
  SELECT
    COALESCE(left_counts.row_signature, right_counts.row_signature) AS row_signature,
    COALESCE(left_counts.row_count, 0) AS left_count,
    COALESCE(right_counts.row_count, 0) AS right_count
  FROM left_counts
  FULL OUTER JOIN right_counts
    USING (row_signature)
)
SELECT COUNTIF(left_count != right_count) = 0 AS equivalent
FROM joined_counts"""


_GENERATED_CTE_PREFIXES = ("__lifted_subquery_", "__canonical_cte_")


def _reads_table_named_like_generated_cte(query: exp.Expression) -> bool:
    cte_names = {
        alias.lower() for cte in query.find_all(exp.CTE) if (alias := _cte_alias(cte))
    }
    return any(
        table.name.lower().startswith(_GENERATED_CTE_PREFIXES)
        and (table.args.get("db") or table.name.lower() not in cte_names)
        for table in query.find_all(exp.Table)
    )


def _prepare_query(
    sql: str, *, ignore_row_order: bool
) -> tuple[exp.Expression, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    # Require strict parsing before invoking the lifting transformer. Recovery
    # mode is useful for formatting, but a proof must not be based on a
    # partially recovered AST.
    parsed = _parse_single_query(sql)
    if _reads_table_named_like_generated_cte(parsed):
        # Lifting and CTE canonicalization give CTEs these names; a real table with one
        # could be confused with them, and the proof would compare the wrong relation.
        raise ValueError("the query reads a table named like a CTE the normalizer generates")
    if table_function_reads_cte(parsed):
        raise ValueError("a table function reads a CTE by name, so CTE use cannot be tracked")
    if any(cast.to.find(exp.DataTypeParam) for cast in parsed.find_all(exp.Cast)):
        # BigQuery rejects CAST(x AS NUMERIC(10, 2)), and the printed form drops the
        # parameters, so the query would compare equal to its valid unparameterized twin.
        raise ValueError("BigQuery does not allow parameterized types in CAST")
    lifted = lift_subqueries(sql, rewrite_pipe_syntax=True)
    if lifted.diagnostics:
        details = "; ".join(f"{d.code}: {d.message}" for d in lifted.diagnostics)
        raise ValueError(f"query could not be normalized without diagnostics: {details}")
    query = _parse_single_query(lifted.sql)
    if ignore_row_order:
        _remove_unordered_result_order(query)
    # Recorded before the remaining structure-changing normalizations so that
    # merging, dropping or simplifying around a call cannot hide a change to it.
    sites_before = _value_nondeterminism_sites(query)
    _drop_redundant_distinct(query)
    _strip_grouping_parens(query)
    _flatten_connectors(query)
    _normalize_predicates(query)
    unambiguous = _cte_references_are_unambiguous(query)
    _canonicalize_cte_names(query)
    if unambiguous and _merge_duplicate_ctes(query):
        _canonicalize_cte_names(query)
    _remove_comments(query)
    canonical = _canonical_sql(query)
    reasons = tuple(_nondeterminism_reasons(query, allow_unchanged_values=True))
    return query, canonical, reasons, sites_before, _value_nondeterminism_sites(query)


def _output_names(query: exp.Expression) -> list[str] | None:
    select = query
    while isinstance(select, exp.SetOperation):
        select = select.this
    if not isinstance(select, exp.Select) or any(isinstance(e, exp.Star) or (
        isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
        return None
    return [e.alias_or_name.lower() for e in select.expressions]


def _order_keys_are_outputs(query: exp.Expression, order: exp.Order) -> bool:
    """Whether every ORDER BY key is a function of the output row alone.

    A key may name an output column, give its position, or (in a plain SELECT) repeat a
    projected expression exactly. Then two queries with equal result bags order them the
    same way, so the same LIMIT picks among the same candidate rows.
    """

    names = _output_names(query)
    if names is None:
        return False
    projected = set()
    if isinstance(query, exp.Select):
        projected = {
            e.unalias().sql(dialect="bigquery", comments=False) for e in query.expressions
        }
    for item in order.expressions:
        key = item.this if isinstance(item, exp.Ordered) else item
        if _nondeterminism_reasons(key) or any(type(n).__name__ in _VALUE_NONDETERMINISTIC_TYPES for n in key.walk()):
            return False
        if isinstance(key, exp.Literal) and not key.is_string and key.name.isdigit():
            if not 1 <= int(key.name) <= len(names):
                return False
            continue
        if isinstance(key, exp.Column) and not key.table and key.name.lower() in names:
            continue
        if key.sql(dialect="bigquery", comments=False) in projected:
            continue
        return False
    return True


def _max_rows(query: exp.Expression) -> int | None:
    """An upper bound on the rows a query returns, when its shape gives one.

    A SELECT with aggregates and no GROUP BY returns at most one row; a UNION ALL adds.
    """

    if isinstance(query, exp.SetOperation):
        if query.args.get("limit") is not None or query.args.get("offset") is not None:
            return None
        left, right = _max_rows(query.this), _max_rows(query.expression)
        if left is None or right is None:
            return None
        return left + right if isinstance(query, exp.Union) else left
    if isinstance(query, exp.Subquery):
        return _max_rows(query.this)
    if not isinstance(query, exp.Select) or query.args.get("group") is not None:
        return None
    if query.args.get("limit") is not None or query.args.get("offset") is not None:
        return None
    if any(e.find(exp.Window) is not None for e in query.expressions):
        return None
    has_aggregate = any(
        isinstance(node, exp.AggFunc) and not _inside_subquery(node, query)
        for e in query.expressions for node in e.walk()
    )
    return 1 if has_aggregate else None


def _inside_subquery(node: exp.Expression, top: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not top:
        if isinstance(parent, (exp.Subquery, exp.Select)):
            return True
        parent = parent.parent
    return False


def _drop_noop_limit(sql: str) -> str | None:
    """``SELECT SUM(x) FROM t LIMIT 100``: a LIMIT at least the most rows the query can return does nothing."""

    try:
        query = _parse_single_query(sql)
    except Exception:
        return None
    limit = query.args.get("limit")
    if limit is None or query.args.get("offset") is not None:
        return None
    count = limit.expression if isinstance(limit, exp.Limit) else None
    if not isinstance(count, exp.Literal) or count.is_string or not count.name.isdigit():
        return None
    body = query.copy()
    body.set("limit", None)
    body.set("order", None)
    bound = _max_rows(body)
    if bound is None or bound > int(count.name):
        return None
    return body.sql(dialect="bigquery")


def _peel_root_limit(left_sql: str, right_sql: str) -> tuple[str, str, str] | None:
    """Strip a shared root ``ORDER BY ... LIMIT`` so the rows underneath can be compared.

    Both queries must end in the same ORDER BY, LIMIT and OFFSET, ordered only by output
    columns. Equal result bags then have the same candidate rows for every position, so
    the two queries can return exactly the same results (ties are broken arbitrarily on
    both sides alike).
    """

    try:
        left, right = _parse_single_query(left_sql), _parse_single_query(right_sql)
    except Exception:
        return None
    if left.args.get("limit") is None and left.args.get("offset") is None:
        return None
    clauses = ("order", "limit", "offset")
    render = lambda q, k: q.args[k].sql(dialect="bigquery", comments=False) if q.args.get(k) is not None else None
    if any(render(left, k) != render(right, k) for k in clauses):
        return None
    order = left.args.get("order")
    if order is None or not _order_keys_are_outputs(left, order) or not _order_keys_are_outputs(right, right.args["order"]):
        return None
    tail = " ".join(render(left, k) for k in clauses if left.args.get(k) is not None)
    for query in (left, right):
        for key in clauses:
            query.set(key, None)
    return left.sql(dialect="bigquery"), right.sql(dialect="bigquery"), tail


def prove_equivalent(
    left_sql: str,
    right_sql: str,
    *,
    ignore_row_order: bool = True,
) -> EquivalenceResult:
    """Prove a conservative subset of BigQuery query equivalences.

    A positive result means the normalized ASTs are identical, all supported
    nondeterminism checks are clear, and (by default) only bag semantics are
    being compared. All other cases return ``NOT_PROVEN``; they are not called
    inequivalent because a structural mismatch alone is not a counterexample.

    Two queries ending in the same ``ORDER BY ... LIMIT`` over output columns are
    proven when the rows underneath are.
    """

    if invalid_literal(left_sql) or invalid_literal(right_sql):
        return EquivalenceResult(
            status=EquivalenceStatus.NOT_PROVEN,
            reason="a single-quoted literal holds a line break, which GoogleSQL rejects",
        )
    left_sql, right_sql = canonical_literals(left_sql), canonical_literals(right_sql)
    if ignore_row_order:
        left_sql = _drop_noop_limit(left_sql) or left_sql
        right_sql = _drop_noop_limit(right_sql) or right_sql
    peeled = _peel_root_limit(left_sql, right_sql) if ignore_row_order else None
    if peeled is not None:
        inner = _prove_equivalent(peeled[0], peeled[1], ignore_row_order=True)
        if inner.proven:
            return replace(
                inner,
                reason=(
                    f"{inner.reason}; both apply the same {peeled[2]} (rows tied on the ordering"
                    " may be picked differently, as on any two runs of either query)"
                ),
                diagnostics=inner.diagnostics + (f"same root {peeled[2]} over output columns",),
            )
    return _prove_equivalent(left_sql, right_sql, ignore_row_order=ignore_row_order)


def _prove_equivalent(
    left_sql: str,
    right_sql: str,
    *,
    ignore_row_order: bool = True,
) -> EquivalenceResult:
    verifier_sql: str | None = None
    try:
        left_query, left_canonical, left_nondeterminism, left_before, left_after = _prepare_query(
            left_sql, ignore_row_order=ignore_row_order
        )
        right_query, right_canonical, right_nondeterminism, right_before, right_after = _prepare_query(
            right_sql, ignore_row_order=ignore_row_order
        )
        verifier_sql = build_bag_verifier_sql(left_sql, right_sql)
    except Exception as exc:
        return EquivalenceResult(
            status=EquivalenceStatus.NOT_PROVEN,
            reason="input could not be normalized conservatively",
            diagnostics=(str(exc),),
        )

    diagnostics = tuple(dict.fromkeys(left_nondeterminism + right_nondeterminism))
    sites_untouched = (
        left_before == right_before
        and left_before == left_after
        and right_before == right_after
    )
    if not diagnostics and not sites_untouched:
        diagnostics = tuple(
            f"value nondeterminism changed: {site}"
            for site in dict.fromkeys(left_before + right_before + left_after + right_after)
        )
    if diagnostics:
        return EquivalenceResult(
            status=EquivalenceStatus.NOT_PROVEN,
            reason="nondeterministic value or window behavior was not proven stable",
            left_fingerprint=_fingerprint(left_canonical),
            right_fingerprint=_fingerprint(right_canonical),
            normalized_left=left_canonical,
            normalized_right=right_canonical,
            verifier_sql=verifier_sql,
            diagnostics=diagnostics,
        )

    if _has_row_selection_nondeterminism(left_query) or _has_row_selection_nondeterminism(right_query):
        return EquivalenceResult(
            status=EquivalenceStatus.NOT_PROVEN,
            reason="LIMIT/OFFSET can select different rows without schema constraints",
            left_fingerprint=_fingerprint(left_canonical),
            right_fingerprint=_fingerprint(right_canonical),
            normalized_left=left_canonical,
            normalized_right=right_canonical,
            verifier_sql=verifier_sql,
            diagnostics=("row-selection nondeterminism",),
        )

    left_fingerprint = _fingerprint(left_canonical)
    right_fingerprint = _fingerprint(right_canonical)
    if left_fingerprint != right_fingerprint:
        return EquivalenceResult(
            status=EquivalenceStatus.NOT_PROVEN,
            reason="normalized query structures differ",
            left_fingerprint=left_fingerprint,
            right_fingerprint=right_fingerprint,
            normalized_left=left_canonical,
            normalized_right=right_canonical,
            verifier_sql=verifier_sql,
        )

    if not ignore_row_order:
        if left_query.args.get("order") is None or right_query.args.get("order") is None:
            return EquivalenceResult(
                status=EquivalenceStatus.NOT_PROVEN,
                reason="exact sequence comparison requires explicit ordering on both queries",
                left_fingerprint=left_fingerprint,
                right_fingerprint=right_fingerprint,
                normalized_left=left_canonical,
                normalized_right=right_canonical,
                verifier_sql=verifier_sql,
            )

    unchanged = (
        (f"{len(left_before)} nondeterministic value expression(s) left unchanged",)
        if left_before
        else ()
    )
    return EquivalenceResult(
        status=EquivalenceStatus.PROVEN_EQUIVALENT,
        reason="canonical query structures match under the selected result-order semantics",
        left_fingerprint=left_fingerprint,
        right_fingerprint=right_fingerprint,
        normalized_left=left_canonical,
        normalized_right=right_canonical,
        verifier_sql=verifier_sql,
        diagnostics=unchanged,
    )
