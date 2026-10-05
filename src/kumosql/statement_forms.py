"""BigQuery statements sqlglot rejects or keeps as raw text, read here by their shape.

Each form is a fixed grammar over the tokens of one statement: the words that name it, the tables it names and the clauses
that may follow. A statement that matches is *recognised*: its kind, the table it writes and the table it reads are known,
and nothing else about it is guessed. A statement that does not match (an unknown clause, an unbalanced parenthesis, a
subquery where none belongs) is refused, so it stays an unrecognised statement and is never read as something it is not.

=====================================  ==========================  ==========================  =======================
statement                              kind                        writes                      reads
=====================================  ==========================  ==========================  =======================
``EXPORT MODEL m OPTIONS (...)``       ``export_model``            nothing (a model is not a   nothing
                                                                   table)
``EXPORT DATA OPTIONS (...) AS q``     ``export_data``             nothing                     what ``q`` reads
``UNDROP SCHEMA [IF NOT EXISTS] ds``   ``undrop``                  a dataset comes back        nothing
``LOAD DATA INTO|OVERWRITE t ...``     ``load_data``               ``t`` (a temporary table    nothing (it reads files)
                                                                   when ``TEMP TABLE``)
``CREATE SNAPSHOT TABLE t CLONE s``    ``create_snapshot_table``   ``t``                       ``s``
``CREATE EXTERNAL TABLE t (...) ...``  ``create_external_table``  ``t``                       nothing (it reads files)
``CREATE/DROP SEARCH|VECTOR INDEX``    ``index``                   nothing                     nothing
``CREATE/DROP ROW ACCESS POLICY``      ``row_access_policy``       ``t`` (who sees its rows)   nothing
``CREATE RESERVATION|CAPACITY|...``    ``reservation``             nothing                     nothing
=====================================  ==========================  ==========================  =======================

The parser hook in :mod:`kumosql.bigquery_syntax` keeps the forms sqlglot cannot read as the ``exp.Command`` it makes for
raw text, so they parse the same way on every sqlglot release; :func:`recognise` then says what such a statement is.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

from sqlglot import exp

from .scripts import Tok, lex


@dataclass(frozen=True)
class StatementForm:
    """What a recognised statement is and which tables it names."""

    kind: str
    table: exp.Table | None = None  # the table written, defined or changed
    source: exp.Table | None = None  # the table read (the table a snapshot clones)
    on: exp.Table | None = None  # the table an index or a policy is on
    temp: bool = False  # LOAD DATA INTO TEMP TABLE
    replace: bool = False  # CREATE OR REPLACE, LOAD DATA OVERWRITE
    columns: tuple[str, ...] | None = None  # column names LOAD DATA or CREATE EXTERNAL TABLE declares
    query: str = ""  # the query text of EXPORT DATA
    command: str = ""  # DROP or CREATE: whether the statement defines or removes

    @property
    def opaque(self) -> bool:
        """Kept as raw text by the parser (an ``exp.Command``) on every sqlglot release."""

        return self.kind in OPAQUE_KINDS


#: Forms the parser hook turns into an ``exp.Command`` on every sqlglot release; sqlglot reads some of them on its own, some
#: not at all, and one reading on one release and another on the next would be a different statement each time.
OPAQUE_KINDS = frozenset({"export_model", "undrop", "load_data"})


class _Clause(NamedTuple):
    heads: tuple[tuple[str, ...], ...]  # the words that open it; ``()`` is a clause with no word, a parenthesised group
    shape: str  # group | expr | name
    required: bool = False
    key: str = ""


def _group_end(tokens: list[Tok], i: int) -> int | None:
    """The index after the parenthesis that closes the one at ``tokens[i]``."""

    if i >= len(tokens) or tokens[i].text != "(" or tokens[i].kind != "p":
        return None
    depth = 0
    for k in range(i, len(tokens)):
        if tokens[k].kind != "p":
            continue
        if tokens[k].text == "(":
            depth += 1
        elif tokens[k].text == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    return None


def _adjacent(tokens: list[Tok], i: int) -> bool:
    return 0 < i < len(tokens) and tokens[i].start == tokens[i - 1].end


def _name(tokens: list[Tok], i: int, *, call_ok: bool = False) -> tuple[exp.Table | None, int]:
    """The dotted name at ``tokens[i]`` (``a.b.c``, a quoted path, ``my-project.d.t``) and the index after it.

    A name directly followed by ``(`` is a function call and is refused, unless ``call_ok`` (``ON t(col)`` of an index)."""

    parts: list[str] = []
    j = i
    first = True
    while j < len(tokens):
        token = tokens[j]
        if token.kind not in ("w", "q") and not (token.kind == "n" and not first):
            break
        text = token.text
        j += 1
        if first and token.kind == "w":  # a project id may hold hyphens: ``my-project.dataset.table``
            while (
                j + 1 < len(tokens) and tokens[j].text == "-" and _adjacent(tokens, j) and _adjacent(tokens, j + 1)
                and tokens[j + 1].kind in ("w", "n")
            ):
                text += "-" + tokens[j + 1].text
                j += 2
        parts.append(text)
        first = False
        if j + 1 < len(tokens) and tokens[j].text == "." and _adjacent(tokens, j) and _adjacent(tokens, j + 1):
            j += 1
            continue
        break
    if not parts or j == i:
        return None, i
    if not call_ok and j < len(tokens) and tokens[j].text == "(" and tokens[j].kind == "p" and _adjacent(tokens, j):
        return None, i  # a call, not a name
    try:
        table = exp.to_table(".".join(parts), dialect="bigquery")
    except Exception:  # noqa: BLE001 - sqlglot raises several error types on a malformed name
        return None, i
    return (table, j) if isinstance(table, exp.Table) and table.name else (None, i)


def _head(tokens: list[Tok], i: int, heads: tuple[tuple[str, ...], ...]) -> int | None:
    """Length of the first of ``heads`` that starts at ``tokens[i]``; ``None`` when none does."""

    for words in heads:
        if not words:
            if i < len(tokens) and tokens[i].text == "(" and tokens[i].kind == "p":
                return 0
            continue
        if i + len(words) <= len(tokens) and all(tokens[i + k].up == word for k, word in enumerate(words)):
            return len(words)
    return None


def _opens_later(tokens: list[Tok], i: int, later: list[_Clause]) -> bool:
    return any(c.heads != ((),) and _head(tokens, i, c.heads) is not None for c in later)


def _clauses(tokens: list[Tok], i: int, clauses: list[_Clause]) -> dict[str, tuple[int, int]] | None:
    """Match ``clauses`` in order from ``tokens[i]`` to the end of the statement.

    Returns where each clause that is present has its body, as ``{key: (first token, token after)}``; ``None`` when the
    tokens are not exactly these clauses. An expression runs to the next clause that follows it, outside any bracket.
    """

    found: dict[str, tuple[int, int]] = {}
    for position, clause in enumerate(clauses):
        length = _head(tokens, i, clause.heads)
        if length is None:
            if clause.required:
                return None
            continue
        i += length
        start = i
        if clause.shape == "group":
            end = _group_end(tokens, i)
            if end is None:
                return None
            i = end
        elif clause.shape == "name":
            table, end = _name(tokens, i)
            if table is None:
                return None
            i = end
        else:  # expr
            depth = 0
            later = clauses[position + 1 :]
            while i < len(tokens):
                token = tokens[i]
                if token.kind == "p" and token.text in "([":
                    depth += 1
                elif token.kind == "p" and token.text in ")]":
                    depth -= 1
                    if depth < 0:
                        return None
                elif depth == 0 and _opens_later(tokens, i, later):
                    break
                i += 1
            if depth != 0 or i == start:
                return None
        if clause.key:
            found[clause.key] = (start, i)
    return found if i == len(tokens) else None


def _skip_words(tokens: list[Tok], i: int, *sequences: tuple[str, ...]) -> tuple[int, bool]:
    """Skip the first of ``sequences`` found at ``tokens[i]``; the new index and whether one was skipped."""

    length = _head(tokens, i, sequences)
    return (i + length, True) if length else (i, False)


def _column_names(tokens: list[Tok], span: tuple[int, int] | None) -> tuple[str, ...] | None:
    """The names a parenthesised column list declares: the first word of each of its top-level items."""

    if span is None:
        return None
    names: list[str] = []
    depth = 0
    expect = True
    for token in tokens[span[0] + 1 : span[1] - 1]:
        if token.kind == "p" and token.text in "(<[":
            depth += 1
        elif token.kind == "p" and token.text in ")>]":
            depth -= 1
        elif depth == 0 and token.text == ",":
            expect = True
        elif expect and depth == 0 and token.kind in ("w", "q"):
            names.append(token.text.strip("`"))
            expect = False
        elif expect and depth == 0:
            return None
    return tuple(names) or None


_OPTIONS = _Clause((("OPTIONS",),), "group", key="options")


def _prefix_words(tokens: list[Tok], *words: str) -> int | None:
    return len(words) if _head(tokens, 0, (tuple(words),)) else None


def _export_model(tokens: list[Tok]) -> StatementForm | None:
    table, i = _name(tokens, 2)
    if table is None or _clauses(tokens, i, [_Clause(_OPTIONS.heads, "group", required=True)]) is None:
        return None
    return StatementForm("export_model")


def _export_data(tokens: list[Tok], text: str) -> StatementForm | None:
    i = 2
    if (length := _head(tokens, i, (("WITH", "CONNECTION"),))) is not None:
        _, i = _name(tokens, i + length)
        if i == 2 + length:
            return None
    if _head(tokens, i, (("OPTIONS",),)) is None:
        return None
    end = _group_end(tokens, i + 1)
    if end is None or end >= len(tokens) or tokens[end].up != "AS" or end + 1 >= len(tokens):
        return None
    query = text[tokens[end + 1].start : tokens[-1].end]
    if tokens[end + 1].up not in ("SELECT", "WITH", "FROM") and tokens[end + 1].text != "(":
        return None
    return StatementForm("export_data", query=query)


def _undrop(tokens: list[Tok]) -> StatementForm | None:
    i, _ = _skip_words(tokens, 2, ("IF", "NOT", "EXISTS"))
    table, i = _name(tokens, i)
    if table is None or _clauses(tokens, i, [_OPTIONS]) is None:
        return None
    return StatementForm("undrop")


def _load_data(tokens: list[Tok]) -> StatementForm | None:
    word = tokens[2].up if len(tokens) > 2 else ""
    if word not in ("INTO", "OVERWRITE"):
        return None
    i, temp = _skip_words(tokens, 3, ("TEMP", "TABLE"), ("TEMPORARY", "TABLE"))
    table, i = _name(tokens, i)
    if table is None:
        return None
    found = _clauses(
        tokens,
        i,
        [
            _Clause((("OVERWRITE", "PARTITIONS"), ("PARTITIONS",)), "group"),
            _Clause(((),), "group", key="columns"),
            _Clause((("PARTITION", "BY"),), "expr"),
            _Clause((("CLUSTER", "BY"),), "expr"),
            _OPTIONS,
            _Clause((("FROM", "FILES"),), "group", required=True),
            _Clause((("WITH", "PARTITION", "COLUMNS"),), "group"),
            _Clause((("WITH", "CONNECTION"),), "name"),
        ],
    )
    if found is None:
        return None
    return StatementForm("load_data", table=table, temp=temp, replace=word == "OVERWRITE", columns=_column_names(tokens, found.get("columns")))


def _create_head(tokens: list[Tok], *kind: str) -> tuple[int, bool] | None:
    """After ``CREATE [OR REPLACE] <kind words>``: the next index and whether OR REPLACE was written."""

    i, replace = _skip_words(tokens, 1, ("OR", "REPLACE"))
    length = _head(tokens, i, (tuple(kind),))
    return (i + length, replace) if length else None


def _create_snapshot(tokens: list[Tok]) -> StatementForm | None:
    head = _create_head(tokens, "SNAPSHOT", "TABLE")
    if head is None:
        return None
    i, _ = _skip_words(tokens, head[0], ("IF", "NOT", "EXISTS"))
    table, i = _name(tokens, i)
    if table is None or _head(tokens, i, (("CLONE",),)) is None:
        return None
    source, i = _name(tokens, i + 1)
    if source is None:
        return None
    found = _clauses(tokens, i, [_Clause((("FOR", "SYSTEM_TIME", "AS", "OF"),), "expr"), _OPTIONS])
    if found is None:
        return None
    return StatementForm("create_snapshot_table", table=table, source=source, replace=head[1], command="CREATE")


def _create_external_table(tokens: list[Tok]) -> StatementForm | None:
    head = _create_head(tokens, "EXTERNAL", "TABLE")
    if head is None:
        return None
    i, _ = _skip_words(tokens, head[0], ("IF", "NOT", "EXISTS"))
    table, i = _name(tokens, i)
    if table is None:
        return None
    found = _clauses(
        tokens,
        i,
        [
            _Clause(((),), "group", key="columns"),
            _Clause((("WITH", "PARTITION", "COLUMNS"),), "group"),
            _Clause((("WITH", "CONNECTION"),), "name"),
            _Clause((("OPTIONS",),), "group", required=True),
        ],
    )
    if found is None:
        return None
    return StatementForm("create_external_table", table=table, replace=head[1], columns=_column_names(tokens, found.get("columns")), command="CREATE")


def _index(tokens: list[Tok], create: bool) -> StatementForm | None:
    """``CREATE [OR REPLACE] SEARCH|VECTOR INDEX [IF NOT EXISTS] i ON t (...) ...`` and ``DROP SEARCH|VECTOR INDEX [IF EXISTS] i ON t``."""

    if create:
        head = _create_head(tokens, "SEARCH", "INDEX") or _create_head(tokens, "VECTOR", "INDEX")
        if head is None:
            return None
        i, _ = _skip_words(tokens, head[0], ("IF", "NOT", "EXISTS"))
        vector = tokens[head[0] - 2].up == "VECTOR"
    else:
        length = _head(tokens, 1, (("SEARCH", "INDEX"), ("VECTOR", "INDEX")))
        if length is None:
            return None
        i, _ = _skip_words(tokens, 1 + length, ("IF", "EXISTS"))
        vector = False
    index, i = _name(tokens, i)
    if index is None or index.db or _head(tokens, i, (("ON",),)) is None:
        return None
    on, i = _name(tokens, i + 1, call_ok=True)  # ``ON t(col)`` is written with no space as often as with one
    if on is None:
        return None
    if create:
        clauses = [_Clause(((),), "group", required=True)]
        if vector:
            clauses += [_Clause((("STORING",),), "group"), _Clause((("PARTITION", "BY"),), "expr")]
        clauses.append(_OPTIONS)
        if _clauses(tokens, i, clauses) is None:
            return None
    elif i != len(tokens):
        return None
    return StatementForm("index", on=on, command="CREATE" if create else "DROP")


def _row_access_policy(tokens: list[Tok], create: bool) -> StatementForm | None:
    if create:
        head = _create_head(tokens, "ROW", "ACCESS", "POLICY")
        if head is None:
            return None
        i, _ = _skip_words(tokens, head[0], ("IF", "NOT", "EXISTS"))
        policy, i = _name(tokens, i)
        if policy is None or policy.db or _head(tokens, i, (("ON",),)) is None:
            return None
        on, i = _name(tokens, i + 1)
        if on is None or _clauses(
            tokens, i, [_Clause((("GRANT", "TO"),), "group", required=True), _Clause((("FILTER", "USING"),), "group", required=True)]
        ) is None:
            return None
        return StatementForm("row_access_policy", on=on, replace=head[1], command="CREATE")
    if _head(tokens, 1, (("ALL", "ROW", "ACCESS", "POLICIES", "ON"),)):
        on, i = _name(tokens, 6)
        return StatementForm("row_access_policy", on=on, command="DROP") if on is not None and i == len(tokens) else None
    length = _head(tokens, 1, (("ROW", "ACCESS", "POLICY"),))
    if length is None:
        return None
    i, _ = _skip_words(tokens, 1 + length, ("IF", "EXISTS"))
    policy, i = _name(tokens, i)
    if policy is None or policy.db or _head(tokens, i, (("ON",),)) is None:
        return None
    on, i = _name(tokens, i + 1)
    return StatementForm("row_access_policy", on=on, command="DROP") if on is not None and i == len(tokens) else None


def _reservation(tokens: list[Tok]) -> StatementForm | None:
    """``CREATE RESERVATION|CAPACITY|ASSIGNMENT name OPTIONS (...)``: slot administration, no table involved."""

    for word in ("RESERVATION", "CAPACITY", "ASSIGNMENT"):
        head = _create_head(tokens, word)
        if head is not None:
            name, i = _name(tokens, head[0])
            if name is None or _clauses(tokens, i, [_Clause((("OPTIONS",),), "group", required=True)]) is None:
                return None
            return StatementForm("reservation", replace=head[1], command="CREATE")
    return None


_DISPATCH_CREATE = (_create_snapshot, _create_external_table, _reservation)


@lru_cache(maxsize=1024)
def recognise(text: str) -> StatementForm | None:
    """The form ``text`` (one statement) is, or ``None`` when it is none of them or does not match one exactly."""

    tokens = lex(text)
    while tokens and tokens[-1].kind == ";":
        tokens.pop()
    if len(tokens) < 3 or any(t.kind == ";" for t in tokens) or tokens[0].kind != "w":
        return None
    first, second = tokens[0].up, tokens[1].up
    if any(t.up == "SELECT" for t in tokens) and not (first == "EXPORT" and second == "DATA"):
        return None  # a subquery in a statement that reads no table: refused, not read
    if first == "EXPORT":
        return _export_model(tokens) if second == "MODEL" else _export_data(tokens, text) if second == "DATA" else None
    if first == "UNDROP":
        return _undrop(tokens) if second == "SCHEMA" else None
    if first == "LOAD":
        return _load_data(tokens) if second == "DATA" else None
    if first == "CREATE":
        for reader in _DISPATCH_CREATE:
            form = reader(tokens)
            if form is not None:
                return form
        return _index(tokens, True) or _row_access_policy(tokens, True)
    if first == "DROP":
        return _index(tokens, False) or _row_access_policy(tokens, False)
    return None


def recognise_command(statement: exp.Expression) -> StatementForm | None:
    """The form of a statement sqlglot (or the parser hook) kept as an ``exp.Command``; ``None`` for anything else."""

    if not isinstance(statement, exp.Command):
        return None
    return recognise(f"{statement.this} {statement.text('expression')}")
