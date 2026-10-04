"""Explicit argument contracts for specialized SMT value compilation.

Generic uninterpreted functions encode all their arguments in their identity.
Specialized paths need explicit contracts: an upstream AST option must not
silently disappear merely because the compiler reads only ``this`` and ``to``.
Extend this table as the remaining compiler families are audited.
"""

from sqlglot import exp

from .ast_utils import UnmodeledConstruct


_ALLOWED = {
    exp.Cast: frozenset({"this", "to"}),
}


def check_args(node: exp.Expression, allowed: frozenset[str]) -> None:
    """Decline populated options the specialized compiler does not consume."""

    for key, value in node.args.items():
        if key not in allowed and value:
            raise UnmodeledConstruct(f"{type(node).__name__}.{key} is not modeled by the SMT compiler")


def check_specialized_args(tree: exp.Expression) -> exp.Expression:
    """Validate specialized nodes before identities or algebraic shortcuts erase their options."""

    for node in tree.walk():
        allowed = _ALLOWED.get(type(node))
        if allowed is not None:
            check_args(node, allowed)
    return tree
