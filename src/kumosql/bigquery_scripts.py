"""Procedural BigQuery that sqlglot rejects, kept as opaque commands one block at a time.

sqlglot's BigQuery parser reads part of the scripting language (``BEGIN``, ``IF``, ``WHILE``, ``FOR``, ``DECLARE``) and fails
on the rest: ``CREATE PROCEDURE p(IN a INT64, OUT b INT64)``, ``label: LOOP ... END LOOP label``, ``REPEAT ... UNTIL c END REPEAT``
and the ``CASE n WHEN 1 THEN stmt; ... END CASE`` statement. A ``ParseError`` there threw away a script that
:mod:`kumosql.scripts` can read, and made every caller treat the text as broken SQL.

When the text holds a procedural block and the ``ParseError`` is the only thing wrong with it, each top-level statement is
returned instead: the ones sqlglot reads as the trees it always built, each block (a procedure, a labelled or unlabelled
loop, an ``IF``, a ``CASE``) as one :class:`~sqlglot.expressions.Command` that prints back as written. The block's
statements are not trees, so a rewrite or a prover treats the block as opaque, and nothing inside it is changed or proved.
The lexer that finds the end of each block is the one the scripts reader uses, so a ``;`` or ``END`` inside a label, a
``CASE`` or a ``REPEAT`` never ends it early. Text that holds no block, or a statement outside a block that still fails
to parse, keeps its ``ParseError``.
"""

from __future__ import annotations

from typing import Callable

from sqlglot import exp

# A single statement that is part of the scripting language, not SQL: kept as written when sqlglot cannot read it.
_SCRIPT_WORDS = frozenset(
    "DECLARE SET CALL EXECUTE RAISE RETURN BREAK LEAVE CONTINUE ITERATE ASSERT COMMIT ROLLBACK BEGIN".split()
)


def script_statements(sql: str, parse: Callable[[str], list]) -> list | None:
    """The top-level statements of a script, or None when ``sql`` is not a script with a procedural block.

    ``parse`` reads one plain statement (it raises when sqlglot cannot).
    """

    from .scripts import parse_script

    nodes = parse_script(sql)
    if not any(node.kind != "stmt" for node in nodes):
        return None
    statements: list = []
    for node in nodes:
        text = sql[node.start : node.end].strip()
        if not text:
            continue
        if node.kind == "stmt":
            first = text.split(None, 1)[0].upper()
            try:
                parsed = [tree for tree in parse(text) if tree is not None and not isinstance(tree, exp.Semicolon)]
            except Exception:  # noqa: BLE001
                if first not in _SCRIPT_WORDS:
                    return None
                parsed = []
            if parsed:
                statements.extend(parsed)
                continue
        statements.append(_command(text))
    return statements


def _command(text: str) -> exp.Command:
    parts = text.split(None, 1)
    return exp.Command(this=parts[0], expression=exp.Literal.string(parts[1].strip() if len(parts) > 1 else ""))
