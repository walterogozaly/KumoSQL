"""Independent checks of two small syntactic rewrites: redundant parentheses and redundant DISTINCT.

Like ``proof_steps`` and ``proof_ctes``, this module imports no rule, normalizer or prover. The
parenthesis rule (``cleanup._redundant``) and the prover's own normalizer (``equivalence._paren_is_semantic``)
each decide which parentheses are meaningless, and the DISTINCT rule and the prover's normalizer share
``distinct_safety.distinct_is_redundant``. A mistake in either decision would be approved by both sides of a proof.

**Parenthesization** (``remove_redundant_parentheses``, the prover's ``_strip_grouping_parens``). The after
statement, parsed from its own text, must have the same tree as the before statement once every
parenthesis node is removed from both. The tree already encodes grouping, so equal stripped trees mean the
two texts group every operator the same way, whatever parentheses were dropped, and a removal that changed
grouping (``(a OR b) AND c`` to ``a OR b AND c``) or text meaning (``-(-x)`` to ``--x``, a comment) fails.
Output column names follow the expression, not its parentheses (checked on BigQuery), and ``(t).x``
differs from ``t.x`` as trees, so the check needs no list of exceptions. It relies on sqlglot's parser
reading operator precedence as the engine does, which is the parser trust boundary.

**Redundant DISTINCT** (``remove_redundant_distinct``, the prover's ``_drop_redundant_distinct``). The after
statement must equal the before statement with DISTINCT cleared on some SELECTs, and each cleared SELECT
must have a plain GROUP BY (only columns: no ROLLUP, CUBE, GROUPING SETS, ALL or totals) whose keys are all
projected unchanged. Rows of a grouped query are unique on their keys, and GROUP BY and DISTINCT use the
same equality, so DISTINCT removes nothing. A projection alias spelled like a key is allowed only when it
projects that same column, because GROUP BY prefers a select alias over a FROM column of the same name
(checked on BigQuery). DISTINCT ON and DISTINCT without GROUP BY are refused.

Both statements are read again from the step's SQL text, never from the caller's trees: a normalizer that
edits a tree can leave one that prints, and so reads, differently (``(t).x`` as ``t.x``). A failed check,
including an error in the checker, rejects the step.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .proof_steps import RewriteStep, StepCheck, _key

PAREN_FAMILY = "parenthesization"
PAREN_ASSUMPTIONS = (
    "parse_structure_unchanged",
    "parentheses_carry_no_other_meaning",
)
DISTINCT_FAMILY = "redundant_distinct"
DISTINCT_ASSUMPTIONS = (
    "statement_frame_preserved",
    "group_keys_are_plain_columns",
    "group_keys_projected_unchanged",
    "distinct_and_group_by_share_equality",
)


class _Rejected(ValueError):
    pass


def _reparse(sql: str) -> exp.Expression:
    """The statement as its own text reads: a transition's in-memory tree is not trusted, only what it prints."""

    nodes = [node for node in sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if node is not None]
    if len(nodes) != 1:
        raise _Rejected("a step must hold exactly one statement on each side")
    return nodes[0]


def _rotate(node: exp.Expression) -> None:
    """Make a chain of one associative operator left-deep: ``a AND (b AND c)`` becomes ``(a AND b) AND c``."""

    while type(node.args.get("expression")) is type(node):
        right = node.args["expression"]
        left = type(node)(this=node.args["this"], expression=right.args["this"])
        node.set("this", left)
        node.set("expression", right.args["expression"])
        _rotate(left)


def _strip_parens(tree: exp.Expression) -> exp.Expression:
    """The tree without parenthesis nodes, and with AND and OR chains left-deep.

    AND and OR are associative in three-valued logic, so the grouping inside a chain of one of them carries no
    meaning (operand order is kept). Every other operator keeps its grouping: floating-point ``+`` and integer
    overflow make ``a + (b + c)`` differ from ``(a + b) + c``.
    """

    tree = tree.copy()
    for paren in reversed(list(tree.find_all(exp.Paren))):
        if paren.parent is None:
            if paren is tree:
                tree = paren.this.copy()
            continue
        paren.replace(paren.this)
    for node in reversed(list(tree.find_all(exp.And, exp.Or))):
        _rotate(node)
    return tree


def _check_parens(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> tuple[bool, str, int]:
    before, after = _reparse(step.before_sql), _reparse(step.after_sql)
    old, new = _strip_parens(before), _strip_parens(after)
    count = sum(1 for _ in before.find_all(exp.Paren)) - sum(1 for _ in after.find_all(exp.Paren))
    if _key(old) != _key(new):
        return False, "the statements group differently once parentheses are ignored", 1
    return True, f"identical once parentheses are ignored ({count} parenthesis node(s) removed)", 1


def _is_column(node) -> bool:
    return isinstance(node, exp.Column) and not isinstance(node.this, exp.Star)


def _column_key(column: exp.Column) -> tuple:
    return (str(column.table).lower(), str(column.name).lower())


def _redundant(select: exp.Select) -> str | None:
    """Why DISTINCT is not redundant on ``select``, or ``None`` when it is."""

    group = select.args.get("group")
    if group is None or not group.expressions:
        return "DISTINCT without a GROUP BY is not removed"
    if any(group.args.get(name) for name in ("grouping_sets", "rollup", "cube", "totals", "all")):
        return "the GROUP BY has ROLLUP, CUBE, GROUPING SETS, ALL or totals"
    keys = list(group.expressions)
    if not all(_is_column(key) for key in keys):
        return "a GROUP BY key is not a plain column"
    key_names = {str(key.name).lower() for key in keys}
    projected: set[tuple] = set()
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if _is_column(value):
            projected.add(_column_key(value))
        alias = item.alias if isinstance(item, exp.Alias) else ""
        if alias and alias.lower() in key_names and not (_is_column(value) and str(value.name).lower() == alias.lower()):
            return f"the alias {alias!r} spells a GROUP BY key but projects something else"
    for key in keys:
        if _column_key(key) not in projected:
            return f"the GROUP BY key {key.sql(dialect='bigquery')} is not projected unchanged"
    return None


def _check_distinct(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> tuple[bool, str, int]:
    before, after = _reparse(step.before_sql), _reparse(step.after_sql)
    old_selects = [node for node in before.walk() if isinstance(node, exp.Select)]
    new_selects = [node for node in after.walk() if isinstance(node, exp.Select)]
    if len(old_selects) != len(new_selects):
        return False, "the number of SELECTs changed", 0
    expected = before.copy()
    copies = [node for node in expected.walk() if isinstance(node, exp.Select)]
    cleared = 0
    for old, new, copy in zip(old_selects, new_selects, copies):
        had, has = old.args.get("distinct"), new.args.get("distinct")
        if had is not None and has is None:
            if isinstance(had, exp.Distinct) and had.args.get("on"):
                return False, "DISTINCT ON was removed", cleared
            problem = _redundant(old)
            if problem:
                return False, f"DISTINCT was removed but is not redundant: {problem}", cleared
            copy.set("distinct", None)
            cleared += 1
        elif had is None and has is not None:
            return False, "DISTINCT was added", cleared
    if not cleared:
        return False, "no DISTINCT was removed", 0
    if _key(expected) != _key(after):
        return False, "the statement changed in a way other than clearing DISTINCT", cleared
    return True, f"only DISTINCT changed; {cleared} SELECT(s) group on keys they all project", cleared


_FAMILIES = {
    PAREN_FAMILY: (PAREN_ASSUMPTIONS, _check_parens),
    DISTINCT_FAMILY: (DISTINCT_ASSUMPTIONS, _check_distinct),
}


def check_syntax_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a parenthesization or DISTINCT step from its parsed statements (the caller's copies are not changed)."""

    family = _FAMILIES.get(step.family)
    if family is None:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    assumptions, check = family
    if step.assumptions != assumptions:
        return StepCheck(step, False, "the step's assumptions are not the assumptions of its family")
    try:
        accepted, reason, cases = check(step, before, after)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases)


__all__ = [
    "DISTINCT_ASSUMPTIONS",
    "DISTINCT_FAMILY",
    "PAREN_ASSUMPTIONS",
    "PAREN_FAMILY",
    "check_syntax_transition",
]
