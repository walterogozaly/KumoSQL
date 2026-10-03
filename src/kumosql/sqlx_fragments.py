"""Which Dataform ``${...}`` expressions a proof may treat as a single, opaque name.

To prove a SQLX rewrite, every interpolation is masked with a sentinel derived from its text
(``mask_sqlx_by_content``), and the prover reads that sentinel as an ordinary identifier. That is
only faithful for an expression that expands to one relation name: a single ``ref(...)`` or
``resolve(...)`` call (``${ref("t")}``, ``${ref({schema: vars.S, name: "t"})}``) and ``${self()}``. Any other
expression (``${when(incremental(), "AND ts > ...")}``, ``${"x OR y"}``, a JavaScript constant) can
expand to any SQL: several operators, a whole clause, nothing at all, or a reference to a CTE the
masked text never mentions. A changed statement holding one cannot be proven from the masked text,
so the verifier asks for the compiled SQL instead (see ``rewrite._verify_sqlx``).

An expression inside a string literal (``WHERE d = '${constants.START}'``) only adds text to that
literal, so it is not counted here; the verifier blanks string literals before looking.
"""

from __future__ import annotations

import hashlib
import re

from sqlglot import exp

from .sqlx import mask_sqlx_interpolations

_SELF = re.compile(r"\$\{\s*self\(\s*\)\s*\}")
_CALL = re.compile(r"\$\{\s*(?:ref|resolve)\s*\(")
_CLOSE = re.compile(r"\s*\}")


def is_relation_reference(interpolation: str) -> bool:
    """Whether a ``${...}`` expression always expands to exactly one table or view name.

    That is ``self()``, or one ``ref(...)`` or ``resolve(...)`` call whose closing parenthesis ends the
    expression: Dataform returns a relation name whatever the arguments are. Arguments may hold quoted
    strings, objects and names, but no template literal, comment, ``/`` or backslash, so the call cannot end
    anywhere the scan does not see (``${ref("t") + " OR x"}`` and ``${ref(a /* ( */) + x)}`` are dynamic).
    """

    if _SELF.fullmatch(interpolation):
        return True
    call = _CALL.match(interpolation)
    if call is None:
        return False
    depth, quote, index = 1, "", call.end()
    while index < len(interpolation):
        char = interpolation[index]
        if char in "`/\\" or (quote and char == "\n"):
            return False
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return char == ")" and bool(_CLOSE.fullmatch(interpolation, index + 1))
        index += 1
    return False


def dynamic_sentinels(sql: str) -> frozenset[str]:
    """The ``mask_sqlx_by_content`` sentinels of every expression in ``sql`` that is not a relation reference."""

    _, restorations = mask_sqlx_interpolations(sql)
    return frozenset(
        f"__sqlx_{hashlib.sha256(item.original.encode('utf-8')).hexdigest()[:16]}__"
        for item in restorations
        if not is_relation_reference(item.original)
    )


def holds_dynamic_fragment(statement: exp.Expression, sentinels: frozenset[str]) -> bool:
    """Whether a masked statement uses one of ``sentinels`` outside a string literal (comments included)."""

    if not sentinels:
        return False
    text_nodes = tuple(
        node_type for node_type in (getattr(exp, name, None) for name in ("RawString", "ByteString", "National"))
        if node_type is not None
    )
    blanked = statement.copy()
    for node in list(blanked.walk()):
        if (isinstance(node, exp.Literal) and node.is_string) or isinstance(node, text_nodes):
            node.set("this", "")
    rendered = blanked.sql(dialect="bigquery")
    return any(sentinel in rendered for sentinel in sentinels)


__all__ = ["dynamic_sentinels", "holds_dynamic_fragment", "is_relation_reference"]
