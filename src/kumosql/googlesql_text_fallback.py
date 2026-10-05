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
    """Index of each ``(`` or ``[`` to the index of its closer; ``None`` when they do not balance."""

    stack: list[int] = []
    pairs: dict[int, int] = {}
    for i, t in enumerate(tokens):
        if t.type in ("L_PAREN", "L_BRACKET"):
            stack.append(i)
        elif t.type in ("R_PAREN", "R_BRACKET"):
            if not stack or tokens[stack[-1]].type != ("L_PAREN" if t.type == "R_PAREN" else "L_BRACKET"):
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
_GROUP_END = frozenset({"HAVING", "ORDER", "LIMIT"})
_FORBIDDEN = frozenset({"GRAPH_TABLE", "MATCH", "MATCH_RECOGNIZE", "|>"})


def _aggregate_clauses(sql: str, dialect: str) -> str | None:
    """``COUNT(x WHERE cond)`` and ``SUM(AVG(x) GROUP BY k)``: a filter picks which rows are aggregated and a
    multi-level ``GROUP BY`` groups the inner aggregate first; the aggregate's type is the same either way."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None or any(t.upper in _FORBIDDEN for t in tokens):
        return None
    spans: list[tuple[int, int, str]] = []
    for open_, close in pairs.items():
        if open_ == 0 or tokens[open_].type != "L_PAREN":
            continue
        before = tokens[open_ - 1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", before.text) or before.upper in _NOT_A_FUNCTION \
                or before.type in ("STRING", "QUOTED_IDENTIFIER"):
            continue
        direct = list(_direct(tokens, pairs, open_, close))
        if any(tokens[i].upper in ("SELECT", "FROM", "WITH") for i in direct):
            continue
        where = [i for i in direct if tokens[i].type == "WHERE"]
        group = [i for i in direct if tokens[i].upper.split(" ")[0] == "GROUP"]
        if len(where) > 1 or len(group) > 1:
            continue
        for first, enders in ((where, _FILTER_END), (group, _GROUP_END)):
            if not first:
                continue
            end = close
            for i in direct:
                word = tokens[i].upper.split(" ")[0]
                if i > first[0] and word in enders and not (
                        first is group and word == "HAVING" and not (i + 1 < close and tokens[i + 1].upper in ("MIN", "MAX"))):
                    end = i  # a HAVING after GROUP BY filters the groups: it goes with the clause
                    break
            spans.append((tokens[first[0]].start, tokens[end - 1].end, ""))
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
        if t.upper not in ("ARRAY", "STRUCT") or (tokens[i + 1].type != "LT" and tokens[i + 1].text != "<>"):
            continue
        depth = 0
        j = i + 1
        while j < n and tokens[i + 1].text != "<>":
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
        body = sql[opener.start:tokens[close].end] if opener.type == "L_BRACKET" else sql[opener.end:tokens[close].start]
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


_COMPARISONS = frozenset({"=", "!=", "<>", "<", ">", "<=", ">=", "LIKE"})


def _quantified_unnest(sql: str, dialect: str) -> str | None:
    """``x > ALL UNNEST(arr)``, ``x LIKE ANY UNNEST(arr)``: a quantified comparison over an array. It is a BOOL
    whatever the operands are, so the array becomes the equivalent one-column subquery sqlglot reads."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i in range(1, len(tokens) - 2):
        if tokens[i].upper not in ("ALL", "ANY", "SOME") or tokens[i - 1].upper not in _COMPARISONS:
            continue
        if tokens[i + 1].upper != "UNNEST" or tokens[i + 2].type != "L_PAREN":
            continue
        close = pairs[i + 2]
        spans.append((tokens[i + 1].start, tokens[close].end, f"(SELECT * FROM {sql[tokens[i + 1].start:tokens[close].end]})"))
    return _cut(sql, spans) if spans else None


_SELECT_LIST_END = frozenset({"FROM", "WHERE", "GROUP", "HAVING", "QUALIFY", "WINDOW", "ORDER", "LIMIT", "UNION",
                              "INTERSECT", "EXCEPT"})


def _recursion_depth(sql: str, dialect: str) -> str | None:
    """``WITH RECURSIVE t AS (...) WITH DEPTH AS d BETWEEN 1 AND 4``: the depth modifier makes ``t`` one column wider,
    an INT64 named ``d`` (``depth`` by default) after the others. It becomes that column in each branch of the CTE."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    opening = {close: open_ for open_, close in pairs.items()}
    spans: list[tuple[int, int, str]] = []
    n = len(tokens)
    for k in range(n - 2):
        if tokens[k].type != "R_PAREN" or tokens[k + 1].upper != "WITH" or tokens[k + 2].upper != "DEPTH":
            continue
        at = k + 3
        name = "depth"
        if at + 1 < n and tokens[at].upper == "AS" and re.fullmatch(r"[A-Za-z_]\w*", tokens[at + 1].text):
            name, at = tokens[at + 1].text, at + 2
        if at + 3 < n and tokens[at].upper == "BETWEEN" and tokens[at + 1].type == "NUMBER" \
                and tokens[at + 2].upper == "AND" and tokens[at + 3].type == "NUMBER":
            at += 4
        elif at + 1 < n and tokens[at].upper == "MAX" and tokens[at + 1].type == "NUMBER":
            at += 2
        body_open = opening[k]
        branches = [i for i in _direct(tokens, pairs, body_open, k) if tokens[i].upper == "SELECT"]
        if not branches or tokens[body_open + 1].upper != "SELECT" or any(tokens[i + 1].upper == "AS" for i in branches):
            return None
        for i in branches:
            end = next((j for j in _direct(tokens, pairs, i, k) if tokens[j].upper.split(" ")[0] in _SELECT_LIST_END), k)
            comma = "" if tokens[end - 1].type == "COMMA" else ","
            spans.append((tokens[end - 1].end, tokens[end - 1].end, f"{comma} CAST(0 AS INT64) AS {name}"))
        spans.append((tokens[k + 1].start, tokens[at - 1].end, ""))
    return _cut(sql, spans) if spans else None


_MR_CLAUSES = frozenset({"PARTITION", "ORDER", "MEASURES", "PATTERN", "DEFINE", "AFTER", "ONE", "OPTIONS", "SUBSET", "ALL"})
_MR_FOLLOWER = {"ONE": "ROW", "ALL": "ROWS", "AFTER": "MATCH"}  # keywords that start a clause only before these
_MR_PATTERN_FUNCTIONS = frozenset({"FIRST", "LAST", "PREV", "NEXT", "MATCH_NUMBER", "CLASSIFIER", "MATCH_ROW_NUMBER"})


def _split_commas(tokens: list[_Tok], pairs: dict[int, int], lo: int, hi: int) -> list[tuple[int, int]]:
    """Index ranges [start, end) of the comma-separated items between ``lo`` and ``hi``."""

    out, start = [], lo
    for i in _direct(tokens, pairs, lo - 1, hi):
        if tokens[i].type == "COMMA":
            out.append((start, i))
            start = i + 1
    out.append((start, hi))
    return out


def _match_recognize(sql: str, dialect: str) -> str | None:
    """``rel MATCH_RECOGNIZE(PARTITION BY p MEASURES m AS name ...)``: one row per match, whose columns are the
    partition columns and then the measures. A measure that names no pattern variable is an ordinary aggregate over
    the relation's columns, so the clause becomes ``(SELECT p, m AS name FROM rel)``. A measure that names a pattern
    variable other than as a column qualifier (``FIRST(x)``, ``CLASSIFIER()``) is a column of unknown type; a clause this
    does not know, or a partition key that is not a plain column, leaves the query unknown."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    opening = {close: open_ for open_, close in pairs.items()}
    for m, t in enumerate(tokens):
        if t.upper != "MATCH_RECOGNIZE":
            continue
        if m + 1 >= len(tokens) or tokens[m + 1].type != "L_PAREN":
            return None
        body_open = m + 1
        body_close = pairs[body_open]
        if any(tokens[i].upper == "MATCH_RECOGNIZE" for i in range(body_open, body_close)):
            continue  # the innermost one first; this one is looked at again afterwards
        starts = [i for i in _direct(tokens, pairs, body_open, body_close)
                  if tokens[i].upper.split(" ")[0] in _MR_CLAUSES and tokens[i - 1].upper != "AS"
                  and _MR_FOLLOWER.get(tokens[i].upper, tokens[i + 1].upper) == tokens[i + 1].upper]
        names = [tokens[i].upper.split(" ")[0] for i in starts]
        if any(n in ("SUBSET", "ALL") for n in names) or "MEASURES" not in names or "DEFINE" not in names \
                or "PATTERN" not in names or len(set(names)) != len(names):
            return None
        end_of = {n: (starts[k + 1] if k + 1 < len(starts) else body_close) for k, n in enumerate(names)}
        begin_of = {n: starts[k] for k, n in enumerate(names)}
        # pattern variables: the names DEFINE gives, and every identifier PATTERN uses
        variables = set()
        for lo, hi in _split_commas(tokens, pairs, begin_of["DEFINE"] + 1, end_of["DEFINE"]):
            if lo < hi:
                variables.add(tokens[lo].upper)
        for i in range(begin_of["PATTERN"] + 1, end_of["PATTERN"]):
            if re.fullmatch(r"[A-Za-z_]\w*", tokens[i].text):
                variables.add(tokens[i].upper)
        measures = []
        for lo, hi in _split_commas(tokens, pairs, begin_of["MEASURES"] + 1, end_of["MEASURES"]):
            if hi - lo < 3 or tokens[hi - 2].upper != "AS" or not re.fullmatch(r"[A-Za-z_]\w*", tokens[hi - 1].text):
                return None
            name = tokens[hi - 1].text
            base = tokens[lo].start
            cuts = []
            understood = not any(tokens[i].upper in _MR_PATTERN_FUNCTIONS or tokens[i].upper == "SELECT" for i in range(lo, hi - 2))
            for i in range(lo, hi - 2):
                if understood and tokens[i].upper in variables:
                    # only the qualifier of a column (A.x) is understood: A.x has the type of the relation's column x
                    if i + 1 >= hi - 2 or tokens[i + 1].type != "DOT" or (i > lo and tokens[i - 1].type == "DOT"):
                        understood = False
                    cuts.append((tokens[i].start - base, tokens[i + 1].end - base, ""))
            if understood:
                measures.append(f"{_cut(sql[base:tokens[hi - 2].start], cuts).strip()} AS {name}")
            else:  # the column is there, its type is not known
                measures.append(f"{UNKNOWN_FUNCTION}(NULL) AS {name}")
        keys = []
        if "PARTITION" in names:
            lo = begin_of["PARTITION"] + 1
            if tokens[begin_of["PARTITION"]].upper == "PARTITION":  # spelled as two tokens
                lo += 1 if tokens[lo].upper == "BY" else 0
            for a, b in _split_commas(tokens, pairs, lo, end_of["PARTITION"]):
                parts = tokens[a:b]
                if not parts or len(parts) % 2 == 0 or any(
                        (p.type != "DOT") if k % 2 else not re.fullmatch(r"[A-Za-z_]\w*|`[^`]+`", sql[p.start:p.end])
                        for k, p in enumerate(parts)):
                    return None
                keys.append(sql[parts[0].start:parts[-1].end])
        # the from item the clause follows
        if m == 0:
            return None
        k = m - 1
        if tokens[k].type == "R_PAREN":
            k = opening[k]
            if k > 0 and re.fullmatch(r"[A-Za-z_]\w*", tokens[k - 1].text) and tokens[k - 1].upper not in ("FROM", "JOIN"):
                k -= 1  # a table function: name(...)
        else:
            while k >= 2 and tokens[k - 1].type == "DOT":
                k -= 2
        if k == 0 or (tokens[k - 1].upper not in ("FROM", "JOIN") and tokens[k - 1].type != "COMMA"):
            return None
        from_item = sql[tokens[k].start:tokens[m].start].strip()
        select = ", ".join(keys + measures)
        return sql[:tokens[k].start] + f"(SELECT {select} FROM {from_item})" + sql[tokens[body_close].end:]
    return None


ANONYMOUS_PREFIX = "__kumo_anon"
_AFTER_UNNEST_OK = frozenset({"WITH", "ON", "USING", "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "QUALIFY", "UNION",
                              "INTERSECT", "EXCEPT", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "WINDOW"})


def _multiway_unnest(sql: str, dialect: str) -> str | None:
    """``UNNEST(a AS x, b AS y, mode => "PAD") WITH OFFSET``: one column per array, each of its array's element type,
    then the offset. Only the types matter, so it becomes an ``UNNEST`` of the one array of STRUCTs whose fields are
    those columns, built from a cross product (``UNNEST(ARRAY(SELECT AS STRUCT e1 AS x, e2 AS y FROM UNNEST(a) AS e1,
    UNNEST(b) AS e2))``): the same columns, in the same order, with the arrays' own scope. An array without an alias
    that could be named by its last identifier (``T.arr``) leaves the query alone, as does an alias after the call."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    counter = 0
    for u, t in enumerate(tokens[:-1]):
        if t.upper != "UNNEST" or tokens[u + 1].type != "L_PAREN" or (u > 0 and tokens[u - 1].type == "DOT"):
            continue
        open_, close = u + 1, pairs[u + 1]
        if any(tokens[i].type in ("LT", "GT") or tokens[i].text in ("<>", ">>") for i in _direct(tokens, pairs, open_, close)):
            continue  # a typed array literal (ARRAY<STRUCT<a INT64, b STRING>>[...]) has commas that separate nothing
        items = _split_commas(tokens, pairs, open_ + 1, close)
        named_mode = [it for it in items if it[1] - it[0] >= 3 and tokens[it[0]].upper == "MODE" and tokens[it[0] + 1].text == "=>"]
        arrays = [it for it in items if it not in named_mode]
        aliased = [any(tokens[i].upper == "AS" for i in _direct(tokens, pairs, it[0] - 1, it[1])) for it in arrays]
        if len(arrays) < 2 and not any(aliased) and not named_mode:
            continue  # an ordinary UNNEST
        if len(named_mode) > 1 or (named_mode and items[-1] != named_mode[0]) or not arrays or any(it[0] >= it[1] for it in items):
            return None
        after = tokens[close + 1] if close + 1 < len(tokens) else None
        if after is not None and (after.upper == "AS" or after.type in ("VAR", "IDENTIFIER") and after.upper not in _AFTER_UNNEST_OK):
            return None
        parts, sources = [], []
        for k, (lo, hi) in enumerate(arrays):
            counter += 1
            last_as = [i for i in _direct(tokens, pairs, lo - 1, hi) if tokens[i].upper == "AS"]
            if last_as:
                a = last_as[-1]
                if a + 2 != hi or not re.fullmatch(r"[A-Za-z_]\w*", tokens[a + 1].text):
                    return None
                array, name = sql[tokens[lo].start:tokens[a].start].strip(), tokens[a + 1].text
            else:
                if tokens[hi - 1].type in ("VAR", "IDENTIFIER", "QUOTED_IDENTIFIER"):
                    return None
                array, name = sql[tokens[lo].start:tokens[hi - 1].end], f"{ANONYMOUS_PREFIX}{counter}"
            parts.append(f"__kumo_e{counter} AS {name}")
            sources.append(f"UNNEST({array}) AS __kumo_e{counter}")
        spans.append((t.start, tokens[close].end, f"UNNEST(ARRAY(SELECT AS STRUCT {', '.join(parts)} FROM {', '.join(sources)}))"))
    return _cut(sql, spans) if spans else None


def _new_proto(sql: str, dialect: str) -> str | None:
    """``NEW pkg.Message(1 AS field)``: a protocol buffer constructor, a type the typer does not model."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i, t in enumerate(tokens):
        if t.upper != "NEW":
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].type in ("VAR", "IDENTIFIER", "DOT"):
            j += 1
        if j > i + 1 and j < len(tokens) and tokens[j].type == "L_PAREN":
            spans.append((t.start, tokens[pairs[j]].end, f"{UNKNOWN_FUNCTION}()"))
    return _cut(sql, spans) if spans else None


def _bit_aggregate_mode(sql: str, dialect: str) -> str | None:
    """``BIT_AND(bytes, mode => 'PAD')``: the mode says how bytes of different lengths combine; the result has the
    argument's type either way."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i, t in enumerate(tokens[:-1]):
        if t.upper not in ("BIT_AND", "BIT_OR", "BIT_XOR") or tokens[i + 1].type != "L_PAREN":
            continue
        close = pairs[i + 1]
        items = _split_commas(tokens, pairs, i + 2, close)
        for k, (lo, hi) in enumerate(items):
            if k > 0 and hi - lo >= 3 and tokens[lo].upper == "MODE" and tokens[lo + 1].text == "=>":
                spans.append((tokens[lo - 1].start, tokens[hi - 1].end, ""))
    return _cut(sql, spans) if spans else None


def _cast_format(sql: str, dialect: str) -> str | None:
    """``CAST(x AS STRING FORMAT fmt)``: the format changes the value, never the type the cast gives."""

    tokens = _tokens(sql, dialect)
    pairs = _pairs(tokens) if tokens is not None else None
    if tokens is None or pairs is None:
        return None
    spans: list[tuple[int, int, str]] = []
    for i, t in enumerate(tokens[:-1]):
        if t.upper not in ("CAST", "SAFE_CAST") or tokens[i + 1].type != "L_PAREN":
            continue
        close = pairs[i + 1]
        direct = list(_direct(tokens, pairs, i + 1, close))
        as_ = [k for k in direct if tokens[k].upper == "AS"]
        form = [k for k in direct if tokens[k].upper == "FORMAT"]
        if as_ and len(form) == 1 and form[0] > as_[-1]:
            spans.append((tokens[form[0]].start, tokens[close - 1].end, ""))
    return _cut(sql, spans) if spans else None


_REWRITES = (
    ("privacy clause", _privacy_clause),
    ("aggregate filter or group", _aggregate_clauses),
    ("recursion depth column", _recursion_depth),
    ("multiway unnest", _multiway_unnest),
    ("match recognize", _match_recognize),
    ("quantified comparison over an array", _quantified_unnest),
    ("protocol buffer constructor", _new_proto),
    ("bit aggregate mode", _bit_aggregate_mode),
    ("cast format", _cast_format),
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
    for _ in range(6):
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
