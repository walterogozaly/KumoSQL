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
literal, so it is not counted here; the verifier blanks string literals before looking. The literal's
value is still unknown: masked, ``'__sqlx_...__'`` would read as one fixed string that differs from every
other literal, so ``status = '${vars.paid}' AND status = 'paid'`` would look contradictory. The verifier
turns such a literal into an unknown constant first (``unknown_string_constants``), and every prover
refuses text that still holds one (``masked_template_problem``), as it refuses the loader's positional
``__sqlx_token_N__`` names, which stand for different expressions in different models.
"""

from __future__ import annotations

import hashlib
import re

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

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


_LOADER_TOKEN = re.compile(r"__sqlx_token_\d+__")
LOADER_TOKEN_REASON = (
    "unsupported: it holds a Dataform expression the loader masked by position (__sqlx_token_N__), which can "
    "expand to any SQL and stands for a different expression in each model; compile the SQLX to prove it"
)
STRING_REASON = (
    "unsupported: a string literal holds a masked Dataform expression, so its value is unknown; "
    "compile the SQLX to prove it"
)


def _masked_strings(sql: str, dialect: str) -> list[tuple[int, int, str]]:
    """``(start, end, text)`` of every string literal in ``sql`` that holds a masked expression."""

    return [
        (token.start, token.end, token.text)
        for token in Dialect.get_or_raise(dialect).tokenize(sql)
        if "STRING" in token.token_type.name and "__sqlx_" in token.text
    ]


def masked_template_problem(*sqls: str, dialect: str = "bigquery") -> str | None:
    """Why a prover must not read ``sqls`` as they are, or ``None``.

    The loader's ``__sqlx_token_N__`` names are numbered per model, so two models' texts can be identical
    while their expressions differ (``status = "${vars.paid}"`` and ``status = "${vars.free}"``). A masked
    expression inside a string literal makes the literal's value unknown. Text the tokenizer cannot read
    is refused too.
    """

    for sql in sqls:
        if not isinstance(sql, str) or "__sqlx_" not in sql:
            continue
        if _LOADER_TOKEN.search(sql):
            return LOADER_TOKEN_REASON
        try:
            if _masked_strings(sql, dialect):
                return STRING_REASON
        except Exception:  # noqa: BLE001 - unreadable text is never proven over
            return STRING_REASON
    return None


def unknown_string_constants(sql: str, dialect: str = "bigquery") -> str:
    """``sql`` with every string literal that holds a masked expression replaced by an unknown constant.

    The constant is a call with no arguments, named after the literal's text: the same literal on both
    sides of a proof is the same value, and no prover can compare it with another literal. Text the
    tokenizer cannot read comes back unchanged, and the provers then refuse it.
    """

    if "__sqlx_" not in sql:
        return sql
    try:
        found = _masked_strings(sql, dialect)
    except Exception:  # noqa: BLE001
        return sql
    for start, end, text in reversed(found):
        name = f"__sqlx_value_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}__()"
        sql = sql[:start] + name + sql[end + 1 :]
    return sql


__all__ = [
    "LOADER_TOKEN_REASON",
    "STRING_REASON",
    "dynamic_sentinels",
    "holds_dynamic_fragment",
    "is_relation_reference",
    "masked_template_problem",
    "unknown_string_constants",
]
