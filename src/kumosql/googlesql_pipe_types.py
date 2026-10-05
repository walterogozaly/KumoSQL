"""GoogleSQL pipe syntax (``FROM t |> WHERE ... |> SELECT ...``) for :mod:`kumosql.googlesql_types`.

sqlglot reads a pipe query by rewriting it into a chain of CTEs while it parses, and that rewrite is lossy (it reads
only some operators, merges a ``WHERE`` or ``LIMIT`` into the query before it, and invents a table name for each step),
so a typer working on its tree would be typing a different query. This module types the pipe query itself:

* :func:`prepare` finds every pipe chain in the SQL text (a chain is the text of one query, or of one parenthesised
  group, that has ``|>`` outside any deeper parentheses) and replaces it with ``SELECT * FROM __kumosql_pipe_N``, which
  sqlglot reads as an ordinary subquery. Chains nested in the operators of an outer chain are replaced first.
* :func:`chain_for` recognises such a placeholder when the typer reaches it, and :func:`type_chain` types the chain
  from its first query through each operator over the running relation, which is a :class:`~kumosql.googlesql_types._Scope`
  like the one a ``FROM`` clause builds. The typer's own machinery does the work of each operator (select items,
  ``*`` with ``EXCEPT`` and ``REPLACE``, joins and ``USING``, set operations, ``PIVOT``); a pipe operator is only
  turned into the call that machinery takes.

The rule is the typer's: an operator this module does not model, or whose text it cannot read exactly, makes the
relation unknown, and later operators on an unknown relation stay unknown (``DESCRIBE`` and ``WITH`` do not depend on
their input). Findings are not reported for pipe queries: a name this module cannot resolve is an unknown type, not
an error. Range variables (``FROM t |> WHERE t.a``) survive only the operators that keep the table as it is, and a
query where a range variable has the name of a column is left unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import TokenType

from . import googlesql_types as g

MARK = "__kumosql_pipe_"
_INPUT = "__kumosql_pipe_in"
_PIPE = getattr(TokenType, "PIPE_GT", None)
_OPEN = (TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE)
_CLOSE = (TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.R_BRACE)


@dataclass
class Chain:
    """One pipe query: the text of its first query and of each operator (``WHERE x > 1``), with inner chains replaced."""

    head: str
    ops: list[str]


class _VarRange(g._Range):
    """A MATCH_RECOGNIZE pattern variable: reachable by name (``a.z``), never by a bare column name."""

    def addressable(self):
        return []


_tokenizer = None


def _tokenize(text: str):
    """BigQuery tokens of ``text``. sqlglot reads the rest of the statement after RENAME, CALL and the like as one string
    (they are commands to it), which would swallow the operators after a pipe ``RENAME``, so no word is a command here."""

    global _tokenizer
    try:
        if _tokenizer is None:
            dialect = Dialect.get_or_raise("bigquery")
            base = dialect.tokenizer_class

            class PipeTokenizer(base):
                COMMANDS = set()

            _tokenizer = PipeTokenizer(dialect=dialect)
        return _tokenizer.tokenize(text)
    except Exception:  # noqa: BLE001 - text sqlglot cannot tokenize has no pipe chain to type
        return None


def _parse(text: str):
    try:
        return sqlglot.parse_one(text, read="bigquery")
    except Exception:  # noqa: BLE001 - text sqlglot does not read gives no types
        return None


# --- finding the chains --------------------------------------------------------------------------------------------

def prepare(sql: str) -> tuple[str, dict[str, Chain]] | None:
    """``sql`` with each pipe chain replaced by a placeholder query, and the chains by placeholder name (none when the
    ``|>`` was inside a string or a comment); None when the text cannot be read reliably (unbalanced parentheses, an
    empty operator, several statements, a pipe operator token this sqlglot lacks)."""

    if _PIPE is None or MARK in sql:
        return None
    tokens = _tokenize(sql)
    if not tokens:
        return None
    while tokens and tokens[-1].token_type == TokenType.SEMICOLON:
        tokens = tokens[:-1]
    if any(t.token_type == TokenType.SEMICOLON for t in tokens):
        return None
    match: dict[int, int] = {}
    stack: list[int] = []
    for i, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            stack.append(i)
        elif token.token_type == TokenType.R_PAREN:
            if not stack:
                return None
            match[stack.pop()] = i
    if stack:
        return None
    chains: dict[str, Chain] = {}

    def render(lo: int, hi: int) -> str | None:
        """The text of tokens[lo:hi] with the chains in its parenthesised groups replaced."""

        if lo >= hi:
            return ""
        out, position, i = [], tokens[lo].start, lo
        while i < hi:
            if tokens[i].token_type == TokenType.L_PAREN:
                j = match[i]
                inner = content(i + 1, j)
                if inner is None:
                    return None
                out.append(sql[position : tokens[i].end + 1])
                out.append(inner)
                position = tokens[j].start
                i = j + 1
            else:
                i += 1
        out.append(sql[position : tokens[hi - 1].end + 1])
        return "".join(out)

    def content(lo: int, hi: int) -> str | None:
        """The text standing for tokens[lo:hi], the inside of a group: a placeholder when it holds a chain."""

        pipes, i = [], lo
        while i < hi:
            if tokens[i].token_type == TokenType.L_PAREN:
                i = match[i] + 1
                continue
            if tokens[i].token_type == _PIPE:
                pipes.append(i)
            i += 1
        if not pipes:
            return render(lo, hi)
        bounds = [lo - 1, *pipes, hi]
        parts = [render(bounds[k] + 1, bounds[k + 1]) for k in range(len(bounds) - 1)]
        if any(not p or not p.strip() for p in parts):
            return None
        name = f"{MARK}{len(chains) + 1}"
        chains[name] = Chain(parts[0], parts[1:])
        return f"SELECT * FROM {name}"

    text = content(0, len(tokens))
    return None if text is None else (text, chains)


def chain_for(ty, node: exp.Expression) -> Chain | None:
    """The chain a placeholder query stands for, or None when ``node`` is not one."""

    if not isinstance(node, exp.Select):
        return None
    stars = node.args.get("expressions") or []
    from_ = node.args.get("from_") or node.args.get("from")
    if len(stars) != 1 or not isinstance(stars[0], exp.Star) or not isinstance(from_, exp.From):
        return None
    table = from_.this
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return None
    return ty.pipes.get(table.name)


# --- the running relation --------------------------------------------------------------------------------------------

def _copy(scope: g._Scope, outer) -> g._Scope:
    out = g._Scope(outer)
    out.ranges = list(scope.ranges)
    out.merged = dict(scope.merged)
    out.star = list(scope.star) if scope.star is not None else None
    return out


def _flat(columns: list[g._Col] | None, outer) -> g._Scope | None:
    """The relation of ``columns`` alone: no range variable, every type a plain column type."""

    if columns is None:
        return None
    plain = [g._Col(c.name, g._plain(c.t), c.required) for c in columns]
    scope = g._Scope(outer)
    scope.ranges = [g._Range(None, plain)]
    scope.star = list(plain)
    return scope


def _clash(scope: g._Scope) -> bool:
    """Whether a range variable has the name of a column of the relation (which one a name means is then not settled).
    The range variable of a value table such as ``UNNEST(...) AS x`` is its own column and does not count."""

    names = {r.name for r in scope.ranges if r.name}
    if not names:
        return False
    for r in scope.ranges:
        columns = r.addressable()
        if columns is not None and any(c.name and c.name.lower() in names for c in columns):
            return True
    return False


def _columns(scope: g._Scope | None) -> list[g._Col] | None:
    """The columns ``SELECT *`` gives; None when unknown or when the relation is a value table, which the operators
    that rebuild a relation from its columns leave alone."""

    if scope is None or scope.star is None or any(r.value is not None for r in scope.ranges):
        return None
    return list(scope.star)


def _star(scope: g._Scope | None) -> list[g._Col] | None:
    """The columns ``SELECT *`` gives, value table or not."""

    return None if scope is None or scope.star is None else list(scope.star)


def _is_value_table(scope: g._Scope) -> bool:
    return any(r.value is not None for r in scope.ranges)


# --- token helpers ---------------------------------------------------------------------------------------------------

def _depth_split(tokens: list, separator) -> list[list]:
    """``tokens`` cut at the separator tokens (a predicate) that sit outside any brackets; empty pieces are dropped."""

    pieces: list[list] = [[]]
    depth = 0
    for token in tokens:
        if token.token_type in _OPEN:
            depth += 1
        elif token.token_type in _CLOSE:
            depth -= 1
        if depth == 0 and separator(token):
            pieces.append([])
        else:
            pieces[-1].append(token)
    return [p for p in pieces if p]


def _commas(tokens: list) -> list[list]:
    return _depth_split(tokens, lambda t: t.token_type == TokenType.COMMA)


def _text(source: str, tokens: list) -> str:
    return source[tokens[0].start : tokens[-1].end + 1]


def _word(token) -> str:
    return " ".join(token.text.upper().split())


def _top_level_index(tokens: list, predicate) -> int | None:
    depth = 0
    for i, token in enumerate(tokens):
        if token.token_type in _OPEN:
            depth += 1
        elif token.token_type in _CLOSE:
            depth -= 1
        elif depth == 0 and predicate(token):
            return i
    return None


def _name_token(token) -> bool:
    """A token that can be an identifier here: a quoted name, or a bare word (which may be a keyword such as KEY)."""

    if token.token_type == TokenType.IDENTIFIER:
        return True
    return token.token_type not in (TokenType.STRING, TokenType.NUMBER) and re.fullmatch(r"[A-Za-z_]\w*", token.text) is not None


# --- items -------------------------------------------------------------------------------------------------------------

def _alias_of(tokens: list) -> str | None:
    """The name an item gives itself with ``AS name`` (or a trailing bare name after an expression)."""

    if len(tokens) >= 3 and tokens[-2].token_type == TokenType.ALIAS and _name_token(tokens[-1]):
        return tokens[-1].text
    return None


def _items(ty, text: str, scope: g._Scope, ctes) -> list[g._Col] | None:
    """The columns of a select list (``a, b + 1 AS c, * EXCEPT (d)``), each item read on its own so one the parser
    cannot read is one column of unknown type; None when the width is unknown too (a star the typer cannot expand)."""

    tokens = _tokenize(text)
    if not tokens:
        return None
    cut = _top_level_index(tokens, lambda t: _word(t) == "WINDOW")
    if cut is not None:
        tokens = tokens[:cut]  # the named windows only define what OVER w means; a window function's type does not use them
    columns: list[g._Col] = []
    for item in _commas(tokens):
        item_text = _text(text, item)
        tree = _parse("SELECT " + item_text)
        expressions = tree.args.get("expressions") if isinstance(tree, exp.Select) else None
        clean = isinstance(tree, exp.Select) and len(expressions or []) == 1 and not any(
            v for k, v in tree.args.items() if k not in ("expressions", "distinct") and v
        )
        if clean:
            expanded = ty.select_item(expressions[0], scope, ctes)
            if expanded is None:
                return None
            columns.extend(expanded)
            continue
        if _top_level_index(item, lambda t: t.token_type == TokenType.STAR) is not None:
            return None
        columns.append(g._Col(_alias_of(item), g.UNKNOWN))
    return columns


def _strip_ordering(tokens: list) -> list:
    """An AGGREGATE or GROUP BY item without its ``ASC`` / ``DESC`` / ``NULLS FIRST`` suffix."""

    words = [_word(t) for t in tokens]
    if len(words) >= 2 and words[-2] == "NULLS" and words[-1] in ("FIRST", "LAST"):
        tokens, words = tokens[:-2], words[:-2]
    if words and words[-1] in ("ASC", "DESC"):
        tokens = tokens[:-1]
    return tokens


# --- the chain ------------------------------------------------------------------------------------------------------------

def type_chain(ty, chain: Chain, outer, ctes) -> g._Rel | None:
    """The result of a pipe query: its running relation after the last operator."""

    mark = len(ty.findings)
    try:
        try:
            state, ctes = _start(ty, chain.head, outer, ctes)
        except (g._Unsupported, RecursionError):
            raise
        except Exception:  # noqa: BLE001
            state = None
        for text in chain.ops:
            try:
                state, ctes = _apply(ty, text, state, outer, ctes)
            except (g._Unsupported, RecursionError):
                raise
            except Exception:  # noqa: BLE001 - an operator this module cannot read leaves the relation unknown
                state = None
    finally:
        del ty.findings[mark:]  # a name this module could not resolve is an unknown type, not a certain error
    columns = _star(state)
    if columns is None:
        return g._Rel(None)
    return g._Rel([g._Col(c.name, g._plain(c.t), c.required) for c in columns])


def _start(ty, head: str, outer, ctes):
    """The relation of the first query of a chain, and the CTEs its ``WITH`` defines for the rest of it."""

    tokens = _tokenize(head)
    if not tokens:
        return None, ctes
    first = _top_level_index(tokens, lambda t: t.token_type in (TokenType.FROM, TokenType.SELECT))
    from_style = first is not None and tokens[first].token_type == TokenType.FROM
    if from_style:
        head = head[: tokens[first].start] + "SELECT * " + head[tokens[first].start :]
    tree = _parse(head)
    if not isinstance(tree, exp.Query):
        return None, ctes
    if isinstance(tree, exp.Select) and (tree.args.get("with_") or tree.args.get("with")):
        ctes = ty.with_clause(tree, outer, ctes)
        tree = tree.copy()
        tree.set("with_", None)
        tree.set("with", None)
    if from_style and isinstance(tree, exp.Select):
        scope = ty.from_clause(tree, outer, ctes)
        return (None if _clash(scope) else scope), ctes
    rel = ty.query(tree, outer, ctes)
    if rel is None or rel.columns is None or rel.as_struct:
        return None, ctes
    if rel.value is not None:
        scope = g._Scope(outer)
        value = g._Range(None, None, value=g._plain(rel.value))
        scope.ranges = [value]
        scope.star = ty.star_columns(value)
        return scope, ctes
    return _flat(rel.columns, outer), ctes


_KEEPS = {"WHERE", "ORDER BY", "LIMIT", "TABLESAMPLE", "ASSERT", "STATIC_DESCRIBE"}
_JOINS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL"}
_SETS = {"UNION", "INTERSECT", "EXCEPT"}


def _apply(ty, text: str, state, outer, ctes):
    tokens = _tokenize(text)
    if not tokens:
        return None, ctes
    word = _word(tokens[0])
    if word == "WITH":
        return state, _with(ty, text, tokens, outer, ctes)
    if word == "DESCRIBE" and len(tokens) == 1:
        return _flat([g._Col("Describe", g.known(g.STRING))], outer), ctes
    if state is None:
        return None, ctes
    if word in _KEEPS:
        return state, ctes
    if _clash(state):
        return None, ctes
    handler = {
        "SELECT": _select, "EXTEND": _extend, "WINDOW": _extend, "SET": _set, "DROP": _drop, "RENAME": _rename,
        "AS": _as, "AGGREGATE": _aggregate, "DISTINCT": _distinct, "PIVOT": _pivot, "UNPIVOT": _pivot,
        "MATCH_RECOGNIZE": _match_recognize,
    }.get(word)
    if handler is not None:
        return handler(ty, text, tokens, state, outer, ctes), ctes
    if word in _JOINS:
        return _join(ty, text, state, outer, ctes), ctes
    if word in _SETS:
        return _set_operation(ty, text, tokens, state, outer, ctes), ctes
    return None, ctes


def _with(ty, text: str, tokens: list, outer, ctes):
    if len(tokens) > 1 and _word(tokens[1]) == "RECURSIVE":
        return ctes
    tree = _parse(text + " SELECT 1")
    if not isinstance(tree, exp.Select) or not (tree.args.get("with_") or tree.args.get("with")):
        return ctes
    return ty.with_clause(tree, outer, ctes)


def _select(ty, text, tokens, state, outer, ctes):
    rest = tokens[1:]
    if rest and _word(rest[0]) in ("AS", "ALL"):
        return None  # SELECT AS STRUCT / AS VALUE
    if rest and _word(rest[0]) == "DISTINCT":
        rest = rest[1:]
    if not rest:
        return None
    return _flat(_items(ty, _text(text, rest), state, ctes), outer)


def _extend(ty, text, tokens, state, outer, ctes):
    base = _columns(state)
    if base is None or len(tokens) < 2:
        return None
    added = _items(ty, _text(text, tokens[1:]), state, ctes)
    if added is None:
        return None
    return _flat(base + added, outer)


def _distinct(ty, text, tokens, state, outer, ctes):
    return _flat(_columns(state), outer) if len(tokens) == 1 else None


def _unique(columns: list[g._Col], name: str) -> int | None:
    found = [i for i, c in enumerate(columns) if c.name is not None and c.name.lower() == name.lower()]
    return found[0] if len(found) == 1 else None


def _set(ty, text, tokens, state, outer, ctes):
    base = _columns(state)
    if base is None:
        return None
    targets: list[tuple[str, list]] = []
    for item in _commas(tokens[1:]):
        if len(item) < 3 or not _name_token(item[0]) or item[1].token_type != TokenType.EQ:
            return None
        targets.append((item[0].text, item[2:]))
    if not targets or len({n.lower() for n, _ in targets}) != len(targets):
        return None
    columns = list(base)
    for name, value in targets:
        position = _unique(base, name)
        expressions = _items(ty, _text(text, value), state, ctes)
        if position is None or expressions is None or len(expressions) != 1:
            return None
        columns[position] = g._Col(base[position].name, expressions[0].t)
    return _flat(columns, outer)


def _drop(ty, text, tokens, state, outer, ctes):
    base = _columns(state)
    if base is None:
        return None
    names = []
    for item in _commas(tokens[1:]):
        if len(item) != 1 or not _name_token(item[0]):
            return None
        names.append(item[0].text)
    if not names or len({n.lower() for n in names}) != len(names):
        return None
    positions = [_unique(base, n) for n in names]
    if any(p is None for p in positions):
        return None
    return _flat([c for i, c in enumerate(base) if i not in positions], outer)


def _rename(ty, text, tokens, state, outer, ctes):
    base = _columns(state)
    if base is None:
        return None
    pairs = []
    for item in _commas(tokens[1:]):
        if len(item) != 3 or not _name_token(item[0]) or item[1].token_type != TokenType.ALIAS or not _name_token(item[2]):
            return None
        pairs.append((item[0].text, item[2].text))
    if not pairs or len({a.lower() for a, _ in pairs}) != len(pairs):
        return None
    columns = list(base)
    for old, new in pairs:
        position = _unique(base, old)
        if position is None:
            return None
        columns[position] = g._Col(new, base[position].t, base[position].required)
    return _flat(columns, outer)


def _as(ty, text, tokens, state, outer, ctes):
    if len(tokens) != 2 or not _name_token(tokens[1]):
        return None
    columns = _columns(state)
    if columns is None:
        return None
    alias = tokens[1].text
    if any(c.name and c.name.lower() == alias.lower() for c in columns):
        return None
    scope = g._Scope(outer)
    plain = [g._Col(c.name, g._plain(c.t), c.required) for c in columns]
    scope.ranges = [g._Range(alias.lower(), plain)]
    scope.star = list(plain)
    return scope


def _aggregate(ty, text, tokens, state, outer, ctes):
    body = tokens[1:]
    cut = _top_level_index(body, lambda t: _word(t).split()[0] == "GROUP")
    aggregates, groups = body, []
    if cut is not None:
        aggregates, rest = body[:cut], body[cut:]
        words = [_word(t) for t in rest]
        if words[:2] == ["GROUP", "BY"] or words[0] == "GROUP BY":
            groups = rest[1:] if words[0] == "GROUP BY" else rest[2:]
        elif words[:3] == ["GROUP", "AND", "ORDER BY"] or words[:4] == ["GROUP", "AND", "ORDER", "BY"]:
            groups = rest[3:] if words[2] == "ORDER BY" else rest[4:]
        else:
            return None
        if not groups:
            return None
    forbidden = {"ROLLUP", "CUBE", "GROUPING", "ALL"}
    if any(_word(t) in forbidden for t in groups):
        return None
    group_columns: list[g._Col] = []
    for item in _commas(groups):
        item = _strip_ordering(item)
        if not item or all(t.token_type == TokenType.NUMBER for t in item):
            return None  # GROUP BY 1: which expression that is, is not settled here
        columns = _items(ty, _text(text, item), state, ctes)
        if columns is None:
            return None
        group_columns.extend(columns)
    aggregate_columns: list[g._Col] = []
    for item in _commas(aggregates):
        item = _strip_ordering(item)
        if not item:
            return None
        columns = _items(ty, _text(text, item), state, ctes)
        if columns is None:
            return None
        aggregate_columns.extend(columns)
    return _flat(group_columns + aggregate_columns, outer)


def _join(ty, text, state, outer, ctes):
    tree = _parse(f"SELECT * FROM {_INPUT} {text}")
    joins = tree.args.get("joins") if isinstance(tree, exp.Select) else None
    if not joins or len(joins) != 1 or tree.args.get("where") or joins[0].args.get("method"):
        return None
    scope = _copy(state, outer)
    ty.add_item(joins[0].this, scope, outer, ctes, joins[0])
    return None if _clash(scope) else scope


def _set_operation(ty, text, tokens, state, outer, ctes):
    """``UNION ALL [BY NAME | CORRESPONDING] (q1), (q2)`` as ``input UNION ALL (q1) UNION ALL (q2)``."""

    columns = _columns(state)
    if columns is None:
        return None
    operands = _commas(tokens)
    first = operands[0]
    # The modifiers come before the first query, which is the last group of the first item.
    group = None
    depth = 0
    for i in range(len(first) - 1, -1, -1):
        if first[i].token_type == TokenType.R_PAREN:
            depth += 1
        elif first[i].token_type == TokenType.L_PAREN:
            depth -= 1
            if depth == 0:
                group = i
                break
    if group is None:
        return None
    modifiers = _text(text, first[:group]) if group else ""
    queries = [_text(text, first[group:])] + [_text(text, item) for item in operands[1:]]
    wrapper = f"SELECT * FROM {_INPUT}" + "".join(f" {modifiers} {query}" for query in queries)
    tree = _parse(wrapper)
    if not isinstance(tree, exp.SetOperation):
        return None
    rel = ty.query(tree, outer, {**ctes, _INPUT: g._Rel(list(columns))})
    return _flat(rel.columns, outer) if rel is not None and rel.columns is not None and rel.value is None else None


def _pivot(ty, text, tokens, state, outer, ctes):
    columns = _columns(state)
    if columns is None:
        return None
    tree = _parse(f"SELECT * FROM {_INPUT} {text}")
    from_ = tree.args.get("from_") if isinstance(tree, exp.Select) else None
    table = from_.this if isinstance(from_, exp.From) else None
    if not isinstance(table, exp.Table) or not table.args.get("pivots") or tree.args.get("joins"):
        return None
    result = ty.pivoted([g._Range(None, list(columns))], table.args["pivots"], outer, ctes, table)
    return _flat(result.columns, outer) if result.columns is not None else None


# --- MATCH_RECOGNIZE -----------------------------------------------------------------------------------------------------

_MATCH_CLAUSES = ("PARTITION BY", "ORDER BY", "MEASURES", "AFTER", "PATTERN", "DEFINE", "OPTIONS")


def _match_recognize(ty, text, tokens, state, outer, ctes):
    """The output of MATCH_RECOGNIZE is its PARTITION BY columns, then its MEASURES; pattern variables are range
    variables over the input's columns, and MATCH_NUMBER(), MATCH_ROW_NUMBER(), CLASSIFIER(), FIRST(e) and LAST(e)
    have the types the clause documents."""

    columns = _star(state)
    if columns is None or len(tokens) < 3 or tokens[1].token_type != TokenType.L_PAREN or tokens[-1].token_type != TokenType.R_PAREN:
        return None
    inner = tokens[2:-1]
    clauses: dict[str, list] = {}
    depth, current = 0, None
    for token in inner:
        if token.token_type in _OPEN:
            depth += 1
        elif token.token_type in _CLOSE:
            depth -= 1
        word = _word(token)
        if depth == 0 and word in _MATCH_CLAUSES:
            if word in clauses:
                return None
            current = word
            clauses[current] = []
            continue
        if current is None:
            return None
        clauses[current].append(token)
    if "MEASURES" not in clauses or "PATTERN" not in clauses:
        return None
    variables = {t.text.lower() for t in clauses["PATTERN"] if _name_token(t)}
    for item in _commas(clauses.get("DEFINE", [])):
        if _name_token(item[0]):
            variables.add(item[0].text.lower())
    taken = {c.name.lower() for c in columns if c.name} | {r.name for r in state.ranges if r.name}
    if variables & taken:
        return None
    output: list[g._Col] = []
    for item in _commas(clauses.get("PARTITION BY", [])):
        tree = _parse("SELECT " + _text(text, item))
        expressions = tree.args.get("expressions") if isinstance(tree, exp.Select) else None
        if not expressions or len(expressions) != 1 or not isinstance(expressions[0], exp.Column) or isinstance(
            expressions[0].this, exp.Star
        ):
            return None
        expanded = ty.select_item(expressions[0], state, ctes)
        if expanded is None or len(expanded) != 1:
            return None
        output.extend(expanded)
    names = [c.name.lower() for c in output if c.name]
    if len(set(names)) != len(output) or len(names) != len(output):
        return None
    scope = _copy(state, outer)
    scope.ranges += [_VarRange(v, list(columns)) for v in sorted(variables)]
    measures = _measure_text(ty, text, clauses["MEASURES"])
    if measures is None:
        return None
    measure_columns = _items(ty, measures, scope, ctes)
    if measure_columns is None:
        return None
    return _flat(output + measure_columns, outer)


def _measure_text(ty, text: str, tokens: list) -> str | None:
    """The MEASURES clause with its special functions written as expressions of the same type, and the ``GROUP BY`` of a
    multi-level aggregate (``MAX(x + AVG(z) GROUP BY x)``) dropped, which fixes what is grouped, not the type."""

    pieces, position, i = [], tokens[0].start if tokens else 0, 0
    user = ty.catalog.functions
    openers: list[int] = []
    while i < len(tokens):
        token, word = tokens[i], tokens[i].text.upper()
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if token.token_type == TokenType.L_PAREN:
            openers.append(i)
        elif token.token_type == TokenType.R_PAREN and openers:
            openers.pop()
        if word == "GROUP BY" and openers:
            o = openers[-1]
            call = o > 0 and _name_token(tokens[o - 1]) and _word(tokens[o + 1]) not in ("SELECT", "FROM", "WITH")
            close = _closing(tokens, o) if call else None
            if close is None:
                return None
            pieces.append(text[position : token.start])
            position = tokens[close].start
            i = close
            continue
        if _name_token(token) and following is not None and following.token_type == TokenType.L_PAREN and token.text.lower() not in user:
            close = _closing(tokens, i + 1)
            if close is None:
                return None
            argument = tokens[i + 2 : close]
            if word in ("MATCH_NUMBER", "MATCH_ROW_NUMBER", "CLASSIFIER") and not argument:
                pieces.append(text[position : token.start])
                pieces.append("CAST(NULL AS STRING)" if word == "CLASSIFIER" else "CAST(NULL AS INT64)")
                position = tokens[close].end + 1
                i = close + 1
                continue
            if word in ("FIRST", "LAST") and argument and len(_commas(argument)) == 1:
                pieces.append(text[position : token.start])
                position = following.start
                i += 1
                continue
        i += 1
    pieces.append(text[position : tokens[-1].end + 1] if tokens else "")
    return "".join(pieces)


def _closing(tokens: list, open_index: int) -> int | None:
    depth = 0
    for i in range(open_index, len(tokens)):
        if tokens[i].token_type in _OPEN:
            depth += 1
        elif tokens[i].token_type in _CLOSE:
            depth -= 1
            if depth == 0:
                return i
    return None
