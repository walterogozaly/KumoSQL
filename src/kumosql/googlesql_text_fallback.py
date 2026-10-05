"""Parse-failure fallbacks for the GoogleSQL typer (:mod:`kumosql.googlesql_types`).

sqlglot cannot parse some GoogleSQL: aggregate filters, differential-privacy selects, casts to types BigQuery lacks.
When :func:`kumosql.googlesql_types.infer` meets a ``ParseError`` it asks :func:`rewrite` for a different text that
sqlglot parses *and whose output column types are the same*; it types that text instead. Every rewrite here keeps one
of two promises:

* it removes syntax that cannot change any type (a filter on an aggregate, the privacy options of a select), or
* it replaces an expression the typer cannot type by a call to ``__KUMO_UNKNOWN__(...)``, which the typer reads as an
  expression of unknown type, so everything computed from it is unknown too (never a guess).

Anything else stays unparsed, and the query stays unknown: the typer's rule is that a wrong type is the only failure.
No sqlglot class is subclassed and nothing here depends on sqlglot fixes: tokens give positions, the text is cut at
those positions, and the result goes back through the ordinary parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

UNKNOWN_FUNCTION = "__KUMO_UNKNOWN__"
_DP_MARK = "kumo_privacy_select"


@dataclass(frozen=True)
class Rewritten:
    """A query text sqlglot parses, the tree, and the names of the rewrites that were needed."""

    sql: str
    tree: exp.Expression
    rewrites: tuple[str, ...]


class _Tok:
    __slots__ = ("text", "upper", "start", "end", "type")

    def __init__(self, token):
        self.text = token.text
        self.upper = token.text.upper()
        self.start = token.start
        self.end = token.end + 1
        self.type = token.token_type.name


def _tokens(sql: str, dialect: str) -> list[_Tok] | None:
    try:
        return [_Tok(t) for t in Dialect.get_or_raise(dialect).tokenize(sql)]
    except Exception:  # noqa: BLE001 - text that does not tokenize is left alone
        return None


def _pairs(tokens: list[_Tok]) -> dict[int, int] | None:
    """Index of each ``(`` to the index of its ``)``; ``None`` when the parentheses do not balance."""

    stack: list[int] = []
    pairs: dict[int, int] = {}
    for i, t in enumerate(tokens):
        if t.type == "L_PAREN":
            stack.append(i)
        elif t.type == "R_PAREN":
            if not stack:
                return None
            pairs[stack.pop()] = i
    return None if stack else pairs


def _cut(sql: str, spans: list[tuple[int, int, str]]) -> str:
    out = []
    at = 0
    for start, end, text in sorted(spans):
        if start < at:
            continue  # overlapping edits: the first wins, the query is re-examined after it
        out.append(sql[at:start])
        out.append(text)
        at = end
    out.append(sql[at:])
    return "".join(out)


def _direct(tokens: list[_Tok], pairs: dict[int, int], lo: int, hi: int):
    """Indexes of the tokens in (lo, hi) that are not inside a nested parenthesis."""

    i = lo + 1
    while i < hi:
        yield i
        i = pairs[i] + 1 if i in pairs else i + 1


# --- the rewrites: each takes the text and returns the new text, or None when it does not apply --------------------

_PRIVACY = ("DIFFERENTIAL_PRIVACY", "AGGREGATION_THRESHOLD")
_PRIVACY_ARGUMENTS = ("CONTRIBUTION_BOUNDS_PER_GROUP", "CONTRIBUTION_BOUNDS_PER_ROW")


def _privacy_clause(sql: str, dialect: str) -> str | None:
    """``SELECT WITH DIFFERENTIAL_PRIVACY OPTIONS(...)`` and ``WITH AGGREGATION_THRESHOLD``: the options change which
    groups are returned and add noise, not a column's type; the contribution-bound arguments likewise."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i, t in enumerate(tokens[:-2]):
        if t.upper != "SELECT" or tokens[i + 1].upper != "WITH" or tokens[i + 2].upper not in _PRIVACY:
            continue
        end = i + 3
        if end + 1 < len(tokens) and tokens[end].upper == "OPTIONS" and end + 1 in pairs:
            end = pairs[end + 1] + 1
        spans.append((tokens[i + 1].start, tokens[end - 1].end, f"/*{_DP_MARK}*/"))
    if not spans:
        return None
    for i, t in enumerate(tokens):
        if t.upper in _PRIVACY_ARGUMENTS and i > 0 and tokens[i - 1].type == "COMMA" and i + 1 < len(tokens) \
                and tokens[i + 1].text == "=>":
            j = i + 2
            while j < len(tokens) and tokens[j].type not in ("COMMA", "R_PAREN"):
                j = pairs[j] + 1 if j in pairs else j + 1
            spans.append((tokens[i - 1].start, tokens[j - 1].end, ""))
    cut = _cut(sql, spans)
    # Any other named argument may change the result type (report_format => "JSON" makes the aggregate a JSON report),
    # and the typer does not read them, so a query that still has one is left alone.
    rest = _tokens(cut, dialect)
    if rest is None or any(t.text == "=>" for t in rest):
        return None
    return cut


_NOT_A_FUNCTION = frozenset({
    "IN", "AS", "ON", "AND", "OR", "NOT", "FROM", "JOIN", "USING", "OVER", "BY", "WHERE", "SELECT", "WITH", "THEN",
    "ELSE", "WHEN", "CASE", "BETWEEN", "LIKE", "IS", "EXISTS", "ALL", "ANY", "SOME", "UNION", "INTERSECT", "EXCEPT",
    "MATCH", "MATCH_RECOGNIZE", "GRAPH_TABLE", "PARTITION", "ORDER", "GROUP", "HAVING", "QUALIFY", "LIMIT",
})
_FILTER_END = frozenset({"HAVING", "ORDER", "LIMIT", "GROUP"})
_FORBIDDEN = frozenset({"GRAPH_TABLE", "MATCH", "MATCH_RECOGNIZE", "|>"})


def _aggregate_filter(sql: str, dialect: str) -> str | None:
    """``COUNT(x WHERE cond)``: the filter picks which rows are aggregated; the aggregate's type is the same."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None or any(t.upper in _FORBIDDEN for t in tokens):
        return None
    spans: list[tuple[int, int, str]] = []
    for open_, close in pairs.items():
        if open_ == 0:
            continue
        before = tokens[open_ - 1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", before.text) or before.upper in _NOT_A_FUNCTION \
                or before.type in ("STRING", "QUOTED_IDENTIFIER"):
            continue
        direct = list(_direct(tokens, pairs, open_, close))
        if any(tokens[i].upper in ("SELECT", "FROM", "WITH") for i in direct):
            continue
        where = [i for i in direct if tokens[i].type == "WHERE"]
        if len(where) != 1:
            continue
        end = close
        for i in direct:
            if i > where[0] and tokens[i].upper.split(" ")[0] in _FILTER_END:
                end = i
                break
        spans.append((tokens[where[0]].start, tokens[end - 1].end, ""))
    return _cut(sql, spans) if spans else None


def _parses(sql: str, dialect: str) -> bool:
    try:
        sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return False
    return True


def _unparsable_type(type_text: str, dialect: str) -> bool:
    """Whether ``CAST(NULL AS <type_text>)`` fails to parse (and the type text is a plain type: names, brackets,
    commas, digits and parentheses only)."""

    if not re.fullmatch(r"[\w\s<>,().`]*", type_text):
        return False
    return not _parses(f"SELECT CAST(NULL AS {type_text})", dialect)


def _unknown_casts(sql: str, dialect: str) -> str | None:
    """``CAST(x AS UINT64)`` and its ``SAFE_CAST``: a type sqlglot cannot read. The cast becomes an expression of
    unknown type over the same operand, so the operand is still resolved and nothing is guessed."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i, t in enumerate(tokens[:-1]):
        if t.upper not in ("CAST", "SAFE_CAST") or tokens[i + 1].type != "L_PAREN":
            continue
        open_, close = i + 1, pairs[i + 1]
        as_ = [k for k in _direct(tokens, pairs, open_, close) if tokens[k].upper == "AS"]
        if not as_:
            continue
        operand = sql[tokens[open_].end:tokens[as_[-1]].start]
        type_text = sql[tokens[as_[-1]].end:tokens[close].start]
        type_text = re.split(r"\bFORMAT\b", type_text, flags=re.I)[0]
        if _unparsable_type(type_text, dialect):
            spans.append((t.start, tokens[close].end, f"{UNKNOWN_FUNCTION}({operand})"))
    return _cut(sql, spans) if spans else None


def _unknown_typed_arrays(sql: str, dialect: str) -> str | None:
    """``ARRAY<UINT32>[1, 2]`` and ``STRUCT<UINT64>(1)``: a typed constructor whose type sqlglot cannot read."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    n = len(tokens)
    for i, t in enumerate(tokens[:-1]):
        if t.upper not in ("ARRAY", "STRUCT") or tokens[i + 1].type != "LT":
            continue
        depth = 0
        j = i + 1
        while j < n:
            if tokens[j].type == "LT":
                depth += 1
            elif tokens[j].type == "GT":
                depth -= 1
            elif tokens[j].text == ">>":
                depth -= 2
            elif tokens[j].type == "L_PAREN" and j in pairs:
                j = pairs[j]
            elif tokens[j].type in ("R_PAREN", "SEMICOLON"):
                break
            if depth == 0:
                break
            j += 1
        if depth != 0 or j + 1 >= n:
            continue
        opener = tokens[j + 1]
        if opener.type == "L_BRACKET":
            close = _matching_bracket(tokens, j + 1)
        elif opener.type == "L_PAREN" and t.upper == "STRUCT":
            close = pairs[j + 1]
        else:
            continue
        type_text = sql[t.start:tokens[j].end]
        if close is None or not _unparsable_type(type_text, dialect):
            continue
        body = sql[opener.start:tokens[close].end]
        spans.append((t.start, tokens[close].end, f"{UNKNOWN_FUNCTION}({body})"))
    return _cut(sql, spans) if spans else None


def _matching_bracket(tokens: list[_Tok], open_index: int) -> int | None:
    depth = 0
    for k in range(open_index, len(tokens)):
        if tokens[k].type == "L_BRACKET":
            depth += 1
        elif tokens[k].type == "R_BRACKET":
            depth -= 1
            if depth == 0:
                return k
    return None


_REWRITES = (
    ("privacy clause", _privacy_clause),
    ("aggregate filter", _aggregate_filter),
    ("unknown cast type", _unknown_casts),
    ("unknown typed constructor", _unknown_typed_arrays),
)

# The aggregates a differential-privacy select may use, which type as their ordinary forms (DP never changes the result
# type of these: SUM keeps its argument's type, AVG and the variance family give FLOAT64, COUNT gives INT64).
_PRIVACY_AGGREGATES = (exp.Count, exp.Sum, exp.Avg, exp.VariancePop, exp.StddevPop, exp.PercentileCont)


def _privacy_selects_are_ordinary(tree: exp.Expression) -> bool:
    for select in tree.find_all(exp.Select):
        if not any(_DP_MARK in c for c in (select.comments or [])):
            continue
        for node in select.walk(prune=lambda n: isinstance(n, exp.Subquery) or (n is not select and isinstance(n, exp.Select))):
            if isinstance(node, exp.AggFunc) and not isinstance(node, _PRIVACY_AGGREGATES):
                return False
            if isinstance(node, exp.Anonymous) and node.name.upper().startswith("ANON_"):
                return False
    return True


def rewrite(sql: str, dialect: str = "bigquery") -> Rewritten | None:
    """A text sqlglot parses with the same output column types as ``sql``, or ``None``."""

    text = sql
    used: list[str] = []
    for _ in range(3):
        changed = False
        for name, step in _REWRITES:
            try:
                new = step(text, dialect)
            except Exception:  # noqa: BLE001 - a rewrite that trips on odd input just does not apply
                new = None
            if new is not None and new != text:
                text, changed = new, True
                if name not in used:
                    used.append(name)
        if not changed or _parses(text, dialect):
            break
    if not used:
        return None
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    if not _privacy_selects_are_ordinary(tree):
        return None
    return Rewritten(text, tree, tuple(used))
