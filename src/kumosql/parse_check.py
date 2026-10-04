"""Check sqlglot's reading of a query against an independent reading of the same text.

Every prover trusts sqlglot's parse. When sqlglot groups operators differently from the engine, drops a
``NOT`` or reads code as a comment, the provers reason about a query nobody wrote, and a proof about it is
wrong although no rule is. This module reads the text again with its own tokenizer and its own precedence
tables, taken from each engine's grammar and checked against the engine (``docs/parser-checks.md``):

* GoogleSQL (``bigquery``): ten levels, comparisons non-associative, ``|``, ``^`` and ``&`` on separate levels
  and ``||`` beside ``*``.
* MySQL (``mysql``): ``sql_yacc.yy`` of MySQL 8.0, where comparisons are one left-to-right level below
  ``IN``/``LIKE``/``BETWEEN``, ``XOR`` sits between ``OR`` and ``AND``, ``!`` binds tighter than ``^``, and
  ``--`` starts a comment only before a space.
* PostgreSQL and DuckDB (``postgres``, ``duckdb``): ``gram.y``, where ``IS`` binds looser than ``=``, prefix
  ``~`` takes a whole sum, and ``INTERSECT`` binds tighter than ``UNION``.

Both readings are reduced to the same facts: for every operation (an operator, a clause, a set operation, a
``LIMIT``), its kind and the source positions of the names and literals under each operand. sqlglot records
those positions on identifiers and literals, so the two readings line up without trusting sqlglot's spans.
Every operation of the independent reading must appear in sqlglot's tree; one that does not is a
disagreement. :func:`disagreement` also requires sqlglot to read back its own SQL unchanged (a round trip).

A construct this reader does not know leaves the query unchecked rather than disagreeing, so the check only
ever turns a proof into ``not_proven``. :func:`guarded` is the hook the provers call where a proof is
accepted.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import lru_cache, wraps
import re

import sqlglot
from sqlglot import exp


# ---------------------------------------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class ParseCheck:
    """The outcome of comparing sqlglot's reading of one query with the independent one.

    ``status`` is ``agree``, ``disagree`` or ``unchecked`` (a dialect without a table, or a construct the
    independent reader does not know; ``note`` says which). ``compared`` counts the operations matched.
    """

    dialect: str
    status: str
    reasons: tuple[str, ...] = ()
    compared: int = 0
    note: str = ""

    @property
    def disagrees(self) -> bool:
        return self.status == "disagree"


class _Unsupported(Exception):
    """A construct the independent reader does not know: the query is left unchecked."""


class _Rejected(Exception):
    """Text the engine's own grammar rejects (sqlglot read it anyway)."""


# ---------------------------------------------------------------------------------------------------------
# Dialects

_FAMILIES = {"bigquery": "googlesql", "mysql": "mysql", "postgres": "postgres", "duckdb": "duckdb"}
_LABELS = {"googlesql": "GoogleSQL", "mysql": "MySQL", "postgres": "PostgreSQL", "duckdb": "DuckDB"}


def family(dialect: str | None) -> str | None:
    """The precedence table used for ``dialect``, or ``None`` when there is none (the query is unchecked)."""

    return _FAMILIES.get((dialect or "").lower())


# ---------------------------------------------------------------------------------------------------------
# Tokens


@dataclass(frozen=True)
class _Tok:
    kind: str  # word, qident, number, string, op, param, eof
    text: str
    start: int
    end: int  # exclusive

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


_FIXED_OPS = {
    "googlesql": ("<<=", ">>=", "<>", "!=", "<=", ">=", "<<", ">>", "||", "=>", "->", "|>"),
    "mysql": ("<=>", "->>", "<>", "!=", "<=", ">=", "<<", ">>", "||", "&&", ":=", "->"),
}
_PG_OP_CHARS = set("+-*/<>=~!@#%^&|`?")
_SINGLE = set("()[]{},;.+-*/%=<>&|^~!:?@#")


class _Tokenizer:
    def __init__(self, sql: str, fam: str):
        self.sql = sql
        self.fam = fam
        self.n = len(sql)
        self.toks: list[_Tok] = []
        self.notes: list[str] = []

    def run(self) -> list[_Tok]:
        s, i, n, fam = self.sql, 0, self.n, self.fam
        while i < n:
            c = s[i]
            if c.isspace():
                i += 1
                continue
            if c == "-" and s.startswith("--", i):
                if fam != "mysql" or i + 2 >= n or s[i + 2].isspace() or ord(s[i + 2]) < 32:
                    i = self._line_end(i)
                    continue
            if c == "#" and fam in ("googlesql", "mysql"):
                i = self._line_end(i)
                continue
            if c == "/" and s.startswith("/*", i):
                if fam == "mysql" and s.startswith("/*!", i):
                    self.notes.append("MySQL runs the text of a /*! ... */ comment, which sqlglot skips")
                i = self._block_end(i)
                continue
            if c in "'\"" or (c == "`" and fam in ("googlesql", "mysql")):
                i = self._quoted(i, "")
                continue
            if c == "$" and fam in ("postgres", "duckdb"):
                m = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$").match(s, i)
                if m:
                    close = s.find(m.group(0), m.end())
                    if close < 0:
                        raise _Unsupported("an unterminated dollar-quoted string")
                    self._add("string", i, close + len(m.group(0)))
                    i = close + len(m.group(0))
                    continue
                m = re.compile(r"\$\d+").match(s, i)
                if m:
                    self._add("param", i, m.end())
                    i = m.end()
                    continue
            if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
                i = self._number(i)
                continue
            if c.isalpha() or c == "_" or ord(c) > 127:
                j = i + 1
                while j < n and (s[j].isalnum() or s[j] == "_" or ord(s[j]) > 127 or (s[j] == "$" and fam != "googlesql")):
                    j += 1
                word = s[i:j]
                if j < n and s[j] in "'\"" and self._string_prefix(word, s[j]):
                    i = self._quoted(j, word, start=i)
                    continue
                if fam in ("postgres", "duckdb") and word.upper() == "U" and s.startswith("&'", j):
                    i = self._quoted(j + 1, "U&", start=i)
                    continue
                self._add("word", i, j)
                i = j
                continue
            if c == "@" and fam in ("googlesql", "mysql"):
                m = re.compile(r"@@?[A-Za-z_][A-Za-z0-9_.$]*|@`[^`]*`").match(s, i)
                if m:
                    self._add("param", i, m.end())
                    i = m.end()
                    continue
            if c == "?" and fam in ("googlesql", "mysql"):
                self._add("param", i, i + 1)
                i += 1
                continue
            i = self._operator(i)
        self.toks.append(_Tok("eof", "", n, n))
        return self.toks

    def _add(self, kind: str, start: int, end: int) -> None:
        self.toks.append(_Tok(kind, self.sql[start:end], start, end))

    def _line_end(self, i: int) -> int:
        j = self.sql.find("\n", i)
        return self.n if j < 0 else j + 1

    def _block_end(self, i: int) -> int:
        s, depth, j = self.sql, 1, i + 2
        nested = self.fam in ("postgres", "duckdb")
        while j < self.n:
            if s.startswith("*/", j):
                depth -= 1
                j += 2
                if depth == 0 or not nested:
                    return j
                continue
            if nested and s.startswith("/*", j):
                depth += 1
                j += 2
                continue
            j += 1
        raise _Unsupported("an unterminated comment")

    def _string_prefix(self, word: str, quote: str) -> bool:
        w = word.upper()
        if self.fam == "googlesql":
            return w in ("R", "B", "RB", "BR")
        if self.fam == "mysql":
            return w in ("N", "X", "B") or (w.startswith("_") and len(w) > 1)
        return quote == "'" and w in ("E", "B", "X", "N")

    def _quoted(self, i: int, prefix: str, start: int | None = None) -> int:
        s, q, fam = self.sql, self.sql[i], self.fam
        start = i if start is None else start
        p = prefix.upper()
        if q == "`":
            kind = "qident"
        elif q == '"':
            kind = "qident" if fam in ("postgres", "duckdb") else "string"
        else:
            kind = "string"
        if fam == "googlesql" and q in "'\"" and s.startswith(q * 3, i):
            close = i + 3
            while close < self.n:
                if s[close] == "\\":
                    close += 2
                    continue
                if s.startswith(q * 3, close):
                    self._add(kind, start, close + 3)
                    return close + 3
                close += 1
            raise _Unsupported("an unterminated string")
        if fam == "googlesql":
            backslash = True  # also in a raw string, a backslash keeps the next character from ending it
        elif fam == "mysql":
            backslash = q != "`"
        else:
            backslash = p == "E"
        j = i + 1
        while j < self.n:
            ch = s[j]
            if backslash and ch == "\\":
                j += 2
                continue
            if ch == q:
                if j + 1 < self.n and s[j + 1] == q and fam != "googlesql":
                    j += 2  # a doubled quote stands for one
                    continue
                self._add(kind, start, j + 1)
                return j + 1
            j += 1
        raise _Unsupported("an unterminated string or quoted name")

    def _number(self, i: int) -> int:
        s = self.sql
        m = re.compile(r"0[xX][0-9a-fA-F]+").match(s, i)
        if not m:
            m = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?").match(s, i)
        j = m.end()
        if j < self.n and (s[j].isalpha() or s[j] == "_") and self.fam == "mysql":
            raise _Unsupported("a MySQL name that starts with digits")
        self._add("number", i, j)
        return j

    def _operator(self, i: int) -> int:
        s, fam = self.sql, self.fam
        if fam in ("postgres", "duckdb"):
            if s[i] in _PG_OP_CHARS:
                j = i
                while j < self.n and s[j] in _PG_OP_CHARS:
                    if s.startswith("--", j) or s.startswith("/*", j):
                        break
                    j += 1
                text = s[i:j]
                if len(text) > 1 and text[-1] in "+-" and not any(ch in "~!@#%^&|`?" for ch in text):
                    while len(text) > 1 and text[-1] in "+-":
                        text = text[:-1]
                if text:
                    self._add("op", i, i + len(text))
                    return i + len(text)
            if s.startswith("::", i):
                self._add("op", i, i + 2)
                return i + 2
        else:
            for op in _FIXED_OPS[fam]:
                if s.startswith(op, i):
                    self._add("op", i, i + len(op))
                    return i + len(op)
        if s[i] in _SINGLE:
            self._add("op", i, i + 1)
            return i + 1
        raise _Unsupported(f"the character {s[i]!r}")


# ---------------------------------------------------------------------------------------------------------
# The independent reading


class _N:
    """One node of the independent reading: an operation (``kind``) or a plain span (``kind`` is None)."""

    __slots__ = ("kind", "kids", "lo", "hi", "lv", "binary", "paren")

    def __init__(self, kind: str | None, kids: list["_N"], lo: int, hi: int, lv: int | None = None, binary: bool = False):
        self.kind = kind
        self.kids = kids
        self.lo = lo  # first token
        self.hi = hi  # one past the last token
        self.lv = lv  # precedence level of the operator that made this node, None for atoms and parentheses
        self.binary = binary
        self.paren = False


# Words that end an expression or a select item: never read as an implicit alias.
_STOP = frozenset(
    """
    ALL AND ANY ARRAY AS ASC BETWEEN BY CASE CAST COLLATE CROSS CUBE DESC DISTINCT DIV ELSE END ESCAPE EXCEPT
    EXISTS FALSE FETCH FOLLOWING FOR FROM FULL GLOB GROUP GROUPING HAVING ILIKE IN INNER INTERSECT INTERVAL INTO
    IS ISNULL JOIN LATERAL LEFT LIKE LIMIT MINUS MOD NATURAL NOT NOTNULL NULL NULLS OFFSET ON OR ORDER OUTER OVER
    PARTITION PIVOT PRECEDING QUALIFY RANGE REGEXP RETURNING RIGHT RLIKE ROLLUP ROWS SELECT SEMI ANTI SET SIMILAR
    SOME STRAIGHT_JOIN TABLESAMPLE THEN TO TRUE UNBOUNDED UNION UNNEST UNPIVOT USING WHEN WHERE WINDOW WITH XOR
    LOCK SOUNDS MEMBER ASOF POSITIONAL GROUPS EXCLUDE CORRESPONDING
    """.split()
)

_TYPED_LITERALS = frozenset(
    "DATE TIME TIMESTAMP DATETIME NUMERIC BIGNUMERIC DECIMAL BIGDECIMAL JSON TIMESTAMPTZ INT INTEGER BIGINT "
    "SMALLINT REAL FLOAT DOUBLE VARCHAR CHAR TEXT BOOLEAN BOOL UUID INTERVAL".split()
)
_BARE_FUNCTIONS = frozenset(
    "CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_DATETIME CURRENT_USER SESSION_USER LOCALTIME "
    "LOCALTIMESTAMP CURRENT_ROLE CURRENT_SCHEMA CURRENT_CATALOG USER".split()
)
_DATE_PARTS = frozenset(
    "MICROSECOND MILLISECOND SECOND MINUTE HOUR DAY DAYOFWEEK DAYOFYEAR WEEK ISOWEEK MONTH QUARTER YEAR ISOYEAR "
    "DATE TIME DATETIME EPOCH DOW DOY CENTURY DECADE MILLENNIUM MICROSECONDS MILLISECONDS TIMEZONE TIMEZONE_HOUR "
    "TIMEZONE_MINUTE YEAR_MONTH DAY_HOUR DAY_MINUTE DAY_SECOND HOUR_MINUTE HOUR_SECOND MINUTE_SECOND "
    "SECOND_MICROSECOND MINUTE_MICROSECOND HOUR_MICROSECOND DAY_MICROSECOND JULIAN".split()
)

_COMPARISON_OPS = {"=": "=", "!=": "<>", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


class _Parser:
    def __init__(self, toks: list[_Tok], fam: str, sql: str):
        self.sql = sql
        self.t = toks
        self.i = 0
        self.fam = fam
        self.label = _LABELS[fam]
        self.leaves: set[int] = set()  # token indexes read as names or literals

    # ---- token helpers ----------------------------------------------------------------------------

    def peek(self, k: int = 0) -> _Tok:
        j = min(self.i + k, len(self.t) - 1)
        return self.t[j]

    def w(self, k: int = 0) -> str:
        return self.peek(k).upper

    def is_op(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "op" and tok.text == text

    def at(self, *words: str) -> bool:
        return all(self.w(k) == word for k, word in enumerate(words))

    def accept(self, *words: str) -> bool:
        if self.at(*words):
            self.i += len(words)
            return True
        return False

    def accept_op(self, text: str) -> bool:
        if self.is_op(text):
            self.i += 1
            return True
        return False

    def expect_op(self, text: str) -> None:
        if not self.accept_op(text):
            raise _Unsupported(f"expected {text!r} near {self.peek().text!r}")

    def expect(self, *words: str) -> None:
        if not self.accept(*words):
            raise _Unsupported(f"expected {' '.join(words)} near {self.peek().text!r}")

    def leaf(self) -> _N:
        tok = self.peek()
        if tok.kind == "eof":
            raise _Unsupported("unexpected end of the query")
        self.leaves.add(self.i)
        self.i += 1
        return _N(None, [], self.i - 1, self.i)

    def skip_balanced(self) -> None:
        """Skip one token, or a whole bracketed group starting at it."""

        pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
        tok = self.peek()
        if tok.kind == "op" and tok.text in pairs:
            close, depth = pairs[tok.text], 0
            while True:
                tok = self.peek()
                if tok.kind == "eof":
                    raise _Unsupported("unbalanced brackets")
                if tok.kind == "op" and tok.text in pairs and pairs[tok.text] == close:
                    depth += 1
                elif tok.kind == "op" and tok.text == close:
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        return
                elif tok.kind == "op" and tok.text == ">>" and close == ">":
                    depth -= 2
                    if depth <= 0:
                        self.i += 1
                        return
                self.i += 1
        if tok.kind == "eof":
            raise _Unsupported("unexpected end of the query")
        self.i += 1

    def skip_type(self) -> None:
        """Skip a type name such as ``NUMERIC(10, 2)``, ``ARRAY<STRUCT<a INT64>>`` or ``DOUBLE PRECISION``."""

        if self.peek().kind not in ("word", "qident"):
            raise _Unsupported("expected a type")
        self.i += 1
        while True:
            if self.is_op("<") or self.is_op("("):
                self.skip_balanced()
            elif self.is_op("[") and self.is_op("]", 1):
                self.i += 2
            elif self.peek().kind == "word" and self.w() in ("PRECISION", "VARYING", "UNSIGNED", "SIGNED", "ZONE", "WITH", "WITHOUT", "LOCAL", "TIME", "ARRAY", "CHARACTER", "INTEGER", "INT") and not self.at("WITH", "OFFSET"):
                self.i += 1
            elif self.is_op("."):
                self.i += 2
            else:
                return

    # ---- statements -----------------------------------------------------------------------------------

    def statements(self) -> list[_N]:
        out = []
        while True:
            while self.accept_op(";"):
                pass
            if self.peek().kind == "eof":
                return out
            if not (self.w() in ("SELECT", "WITH", "VALUES") or self.is_op("(")):
                raise _Unsupported(f"a statement that starts with {self.peek().text!r}")
            out.append(self.query())
            if not (self.is_op(";") or self.peek().kind == "eof"):
                raise _Unsupported(f"unread text from {self.peek().text!r}")

    # ---- queries --------------------------------------------------------------------------------------

    def query(self) -> _N:
        lo = self.i
        kids: list[_N] = []
        if self.accept("WITH"):
            self.accept("RECURSIVE")
            while True:
                name = self.leaf()
                if self.is_op("("):
                    self.skip_balanced()
                self.expect("AS")
                self.accept("NOT")
                self.accept("MATERIALIZED")
                self.expect_op("(")
                body = self.query()
                self.expect_op(")")
                kids += [name, body]
                if not self.accept_op(","):
                    break
        body = self.set_expr(0)
        kids.append(body)
        tails: list[tuple[str, _N]] = []
        if self.accept("ORDER", "BY"):
            items = self.ordering(kids)
            tails.append(("ORDER BY", items))
        if self.fam == "googlesql" or self.fam == "mysql" or True:
            if self.accept("LIMIT"):
                if self.accept("ALL"):
                    pass
                else:
                    first = self.expr(0)
                    if self.fam == "mysql" and self.accept_op(","):
                        tails.append(("OFFSET", first))
                        tails.append(("LIMIT", self.expr(0)))
                    else:
                        tails.append(("LIMIT", first))
            if self.accept("OFFSET"):
                tails.append(("OFFSET", self.expr(0)))
                if not self.accept("ROWS"):
                    self.accept("ROW")
            if self.at("LIMIT") and self.fam in ("postgres", "duckdb"):
                self.i += 1
                tails.append(("LIMIT", self.expr(0)))
            if self.accept("FETCH"):
                if not self.accept("FIRST"):
                    self.expect("NEXT")
                count = self.expr(0) if not self.at("ROW") and not self.at("ROWS") else None
                if not self.accept("ROWS"):
                    self.expect("ROW")
                if not self.accept("ONLY"):
                    raise _Unsupported("FETCH .. WITH TIES")
                if count is not None:
                    tails.append(("LIMIT", count))
        if self.at("FOR", "UPDATE") or self.at("FOR", "SHARE") or self.at("LOCK"):
            raise _Unsupported("a locking clause")
        node = _N(None, kids, lo, self.i)
        owner = _N(None, [], lo, self.i)
        for kind, part in tails:
            node.kids.append(_N(kind, [owner, part], part.lo, part.hi))
        return node

    def ordering(self, sink: list[_N]) -> _N:
        lo = self.i
        items = []
        while True:
            item = self.expr(0)
            desc = False
            if self.accept("DESC"):
                desc = True
            else:
                self.accept("ASC")
            if desc:
                sink.append(_N("DESC", [item], item.lo, self.i))
            if self.accept("NULLS", "FIRST"):
                sink.append(_N("NULLS FIRST", [item], item.lo, self.i))
            elif self.accept("NULLS", "LAST"):
                sink.append(_N("NULLS LAST", [item], item.lo, self.i))
            if self.at("USING"):
                raise _Unsupported("ORDER BY .. USING")
            items.append(item)
            if not self.accept_op(","):
                break
        return _N(None, items, lo, self.i)

    def set_level(self) -> int | None:
        word = self.w()
        if word in ("UNION", "EXCEPT", "MINUS"):
            return 1
        if word == "INTERSECT":
            return 2 if self.fam != "googlesql" else 1
        if self.fam == "googlesql" and word in ("INNER", "FULL", "LEFT", "OUTER") and any(self.w(k) in ("UNION", "INTERSECT", "EXCEPT") for k in (1, 2)):
            raise _Unsupported("a set operation with a column match mode")
        return None

    def set_expr(self, minlv: int) -> _N:
        left = self.set_operand()
        first_spelling = None
        while True:
            level = self.set_level()
            if level is None or level <= minlv:
                return left
            lo_op = self.i
            word = self.w()
            self.i += 1
            if word == "MINUS":
                raise _Unsupported("MINUS")
            all_ = False
            if self.accept("ALL"):
                all_ = True
            elif self.accept("DISTINCT"):
                pass
            elif self.fam == "googlesql":
                raise _Rejected(f"GoogleSQL requires ALL or DISTINCT after {word}")
            if self.at("BY", "NAME") or self.at("CORRESPONDING"):
                raise _Unsupported("a set operation by name")
            spelling = (word, all_)
            if self.fam == "googlesql":
                if first_spelling is not None and spelling != first_spelling:
                    raise _Rejected("GoogleSQL rejects different set operations without parentheses")
                first_spelling = spelling
            right = self.set_expr(level)
            kind = word + (" ALL" if all_ else "")
            del lo_op
            left = _N(kind, [left, right], left.lo, right.hi)

    def set_operand(self) -> _N:
        if self.is_op("("):
            lo = self.i
            self.i += 1
            inner = self.query()
            self.expect_op(")")
            node = _N(None, [inner], lo, self.i)
            node.paren = True
            return node
        if self.at("SELECT"):
            return self.select()
        if self.at("VALUES"):
            return self.values()
        if self.at("TABLE"):
            raise _Unsupported("TABLE t")
        raise _Unsupported(f"a query that starts with {self.peek().text!r}")

    def values(self) -> _N:
        lo = self.i
        self.expect("VALUES")
        kids = []
        while True:
            self.accept("ROW")
            kids.append(self.primary())
            if not self.accept_op(","):
                break
        return _N(None, kids, lo, self.i)

    def select(self) -> _N:
        lo = self.i
        self.expect("SELECT")
        kids: list[_N] = []
        distinct = None
        while self.w() in ("SQL_CALC_FOUND_ROWS", "HIGH_PRIORITY", "STRAIGHT_JOIN", "SQL_SMALL_RESULT", "SQL_BIG_RESULT", "SQL_BUFFER_RESULT", "SQL_NO_CACHE", "SQL_CACHE") and self.fam == "mysql":
            self.i += 1
        if self.accept("DISTINCT"):
            distinct = "SELECT DISTINCT"
            if self.accept("ON"):
                self.expect_op("(")
                on_lo = self.i
                on = self.expr_list()
                kids.append(_N("DISTINCT ON", [_N(None, on, on_lo, self.i)], on_lo, self.i))
                self.expect_op(")")
                distinct = None
        else:
            self.accept("ALL")
        if self.fam == "googlesql" and (self.at("AS", "STRUCT") or self.at("AS", "VALUE")):
            self.i += 2
        items_lo = self.i
        items = []
        while True:
            items.append(self.select_item(kids))
            if not self.accept_op(","):
                break
        items_node = _N(None, items, items_lo, self.i)
        kids.append(items_node)
        if distinct:
            kids.append(_N(distinct, [items_node], items_lo, self.i))
        if self.at("INTO"):
            raise _Unsupported("SELECT .. INTO")
        if self.accept("FROM"):
            kids.append(self.from_list(kids))
        if self.accept("WHERE"):
            cond = self.expr(0)
            kids.append(_N("WHERE", [cond], cond.lo, cond.hi))
        if self.accept("GROUP", "BY"):
            if not self.accept("ALL"):
                while True:
                    kids.append(self.group_item())
                    if not self.accept_op(","):
                        break
            if self.fam == "mysql" and self.accept("WITH", "ROLLUP"):
                pass
        if self.accept("HAVING"):
            cond = self.expr(0)
            kids.append(_N("HAVING", [cond], cond.lo, cond.hi))
        for _ in range(2):
            if self.accept("QUALIFY"):
                cond = self.expr(0)
                kids.append(_N("QUALIFY", [cond], cond.lo, cond.hi))
            if self.accept("WINDOW"):
                while True:
                    name = self.leaf()
                    kids.append(name)
                    self.expect("AS")
                    kids.append(self.window_spec(kids, name.lo))
                    if not self.accept_op(","):
                        break
        return _N(None, kids, lo, self.i)

    def select_item(self, sink: list[_N]) -> _N:
        lo = self.i
        if self.is_op("*"):
            self.i += 1
            node = _N(None, [], lo, self.i)
            self.star_modifiers(node)
            return node
        item = self.expr(0)
        if self.is_op(".") and self.is_op("*", 1):
            self.i += 2
            node = _N(None, [item], lo, self.i)
            self.star_modifiers(node)
            return node
        alias = None
        if self.accept("AS"):
            alias = self.alias_token(required=True)
        elif self.can_alias():
            alias = self.alias_token(required=True)
        if alias is not None:
            sink.append(_N("ALIAS", [item, alias], lo, self.i))
        return _N(None, [item] + ([alias] if alias else []), lo, self.i)

    def star_modifiers(self, node: _N) -> None:
        if self.fam == "googlesql" or self.fam == "duckdb":
            for word in ("EXCEPT", "REPLACE", "EXCLUDE"):
                if self.at(word) and self.is_op("(", 1):
                    self.i += 1
                    self.skip_balanced()
                    node.hi = self.i

    def can_alias(self) -> bool:
        tok = self.peek()
        if tok.kind == "qident":
            return True
        if tok.kind == "string" and self.fam == "mysql":
            return True
        return tok.kind == "word" and tok.upper not in _STOP

    def alias_token(self, required: bool) -> _N | None:
        tok = self.peek()
        if tok.kind in ("word", "qident") or (tok.kind == "string" and self.fam in ("mysql", "googlesql")):
            return self.leaf()
        if required:
            raise _Unsupported(f"an alias {tok.text!r}")
        return None

    def group_item(self) -> _N:
        lo = self.i
        if self.at("ROLLUP") or self.at("CUBE") or self.at("GROUPING", "SETS"):
            self.i += 2 if self.at("GROUPING") else 1
            self.expect_op("(")
            kids = self.grouping_list()
            self.expect_op(")")
            return _N(None, kids, lo, self.i)
        if self.is_op("(") and self.is_op(")", 1):
            self.i += 2
            return _N(None, [], lo, self.i)
        key = self.expr(0)
        return _N("GROUP KEY", [key], key.lo, key.hi)

    def grouping_list(self) -> list[_N]:
        kids = []
        if self.is_op(")"):
            return kids
        while True:
            if self.at("ROLLUP") or self.at("CUBE") or self.at("GROUPING", "SETS"):
                kids.append(self.group_item())
            else:
                kids.append(self.expr(0))
            if not self.accept_op(","):
                return kids

    # ---- FROM -----------------------------------------------------------------------------------------

    def join_kind(self) -> str | None:
        """Read a join operator (``LEFT OUTER JOIN``, ``,`` ...) and say what it is, or None when there is none."""

        if self.accept_op(","):
            return "INNER"
        save = self.i
        natural = self.accept("NATURAL")
        kind = None
        if self.accept("JOIN") or self.accept("STRAIGHT_JOIN"):
            kind = "INNER"
        elif self.accept("INNER", "JOIN") or self.accept("CROSS", "JOIN"):
            kind = "INNER"
        else:
            for side in ("LEFT", "RIGHT", "FULL"):
                if self.accept(side):
                    if self.accept("SEMI", "JOIN"):
                        kind = f"{side} SEMI"
                    elif self.accept("ANTI", "JOIN"):
                        kind = f"{side} ANTI"
                    else:
                        self.accept("OUTER")
                        self.expect("JOIN")
                        kind = side
                    break
            else:
                if self.at("SEMI", "JOIN") or self.at("ANTI", "JOIN"):
                    kind = self.w()
                    self.i += 2
                elif self.at("CROSS") or self.at("OUTER") or self.at("ASOF") or self.at("POSITIONAL") or self.at("GLOBAL"):
                    raise _Unsupported(f"{self.peek().text} join")
        if kind is None:
            self.i = save
            return None
        return ("NATURAL " if natural else "") + kind

    def from_list(self, sink: list[_N]) -> _N:
        lo = self.i
        kids = [self.from_item(sink)]
        while True:
            kind = self.join_kind()
            if kind is None:
                break
            right = self.from_item(sink)
            cond_lo = self.i
            cond = None
            if self.accept("ON"):
                cond = self.expr(0)
            elif self.accept("USING"):
                self.expect_op("(")
                names = []
                while True:
                    names.append(self.leaf())
                    if not self.accept_op(","):
                        break
                self.expect_op(")")
                cond = _N(None, names, cond_lo, self.i)
            if cond is None:
                cond = _N(None, [], self.i, self.i)
            sink.append(_N("JOIN " + kind, [right, cond], right.lo, self.i))
            kids += [right, cond]
        return _N(None, kids, lo, self.i)

    def from_item(self, sink: list[_N]) -> _N:
        lo = self.i
        if self.accept("LATERAL"):
            raise _Unsupported("LATERAL")
        if self.is_op("("):
            save = self.i
            self.i += 1
            if self.w() in ("SELECT", "WITH", "VALUES") or self.is_op("("):
                try:
                    inner = self.query()
                    self.expect_op(")")
                except _Unsupported:
                    self.i = save + 1
                    inner = self.from_list(sink)
                    self.expect_op(")")
            else:
                inner = self.from_list(sink)
                self.expect_op(")")
            node = _N(None, [inner], lo, self.i)
            self.table_suffix(node)
            return node
        if self.at("UNNEST") and self.is_op("(", 1):
            self.i += 1
            self.expect_op("(")
            args = self.expr_list()
            self.expect_op(")")
            node = _N(None, args, lo, self.i)
            self.table_suffix(node)
            if self.accept("WITH", "OFFSET"):
                if self.accept("AS") or self.can_alias():
                    node.kids.append(self.leaf())
                node.hi = self.i
            return node
        if self.at("VALUES"):
            node = self.values()
            self.table_suffix(node)
            return node
        if self.peek().kind not in ("word", "qident"):
            raise _Unsupported(f"a table that starts with {self.peek().text!r}")
        path = self.table_path()
        node = _N(None, path, lo, self.i)
        if self.is_op("("):  # a table function: its name is not a table name
            self.leaves.difference_update(part.lo for part in path)
            self.i += 1
            args = [] if self.is_op(")") else self.call_args()
            self.expect_op(")")
            node = _N(None, path + args, lo, self.i)
        if self.at("FOR", "SYSTEM_TIME") or self.at("FOR", "SYSTEM", "TIME"):
            raise _Unsupported("time travel")
        self.table_suffix(node)
        return node

    def table_path(self) -> list[_N]:
        parts = [self.leaf()]
        while True:
            tok, nxt = self.peek(), self.peek(1)
            if tok.kind == "op" and tok.text == "-" and self.fam == "googlesql" and nxt.kind in ("word", "number") and self.t[self.i - 1].end == tok.start and tok.end == nxt.start:
                self.i += 1
                parts.append(self.leaf())
                continue
            if tok.kind == "op" and tok.text == "." and nxt.kind in ("word", "qident", "number"):
                self.i += 1
                parts.append(self.leaf())
                continue
            return parts

    def table_suffix(self, node: _N) -> None:
        if self.at("TABLESAMPLE") or self.at("PIVOT") or self.at("UNPIVOT") or self.at("MATCH_RECOGNIZE"):
            raise _Unsupported(self.peek().text)
        if self.fam == "mysql" and self.w() in ("USE", "FORCE", "IGNORE") and self.w(1) in ("INDEX", "KEY"):
            raise _Unsupported("an index hint")
        if self.accept("AS") or (self.can_alias() and self.peek().kind != "string"):
            node.kids.append(self.leaf())
            if self.is_op("("):
                self.skip_balanced()
        node.hi = self.i
        if self.at("TABLESAMPLE") or self.at("PIVOT") or self.at("UNPIVOT"):
            raise _Unsupported(self.peek().text)

    # ---- expressions: shared parts ----------------------------------------------------------------------

    def expr_list(self) -> list[_N]:
        out = []
        while True:
            out.append(self.expr(0))
            if not self.accept_op(","):
                return out

    def expr(self, minlv: int) -> _N:
        left = self.prefix(minlv)
        while True:
            op = self.infix(left)
            if op is None:
                return left
            level, apply = op
            if level <= minlv:
                return left
            left = apply(left)

    def binary(self, kind: str | None, level: int, ntoks: int, right_level: int | None = None):
        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=True)
            self.i += ntoks
            right = self.expr(level if right_level is None else right_level)
            return _N(kind, [left, right], left.lo, right.hi, lv=level, binary=True)

        return level, apply

    def check_left(self, left: _N, level: int, binary: bool) -> None:
        """Refuse an unparenthesized left operand the engine's grammar does not allow (family rules)."""

        if left.lv is None:
            return
        rule = self.left_rule(level)
        if rule is None:
            return
        allowed, why = rule
        if not allowed(left):
            raise _Rejected(why)

    def left_rule(self, level: int):
        return None

    def node_text(self, node: _N) -> str:
        return " ".join(tok.text for tok in self.t[node.lo:node.hi])

    def primary(self) -> _N:
        tok = self.peek()
        lo = self.i
        if tok.kind in ("number", "param"):
            return self.leaf()
        if tok.kind == "string":
            node = self.leaf()
            while self.peek().kind == "string":  # adjacent strings are one literal
                if self.fam in ("postgres", "duckdb") and "\n" not in self.gap(node.hi - 1, self.i):
                    raise _Rejected(f"{_LABELS[self.fam]} joins adjacent strings only across a line break")
                self.leaf()
                node = _N(None, [], lo, self.i)
            return node
        if tok.kind == "op":
            if tok.text == "(":
                return self.paren()
            if tok.text == "[" and self.fam in ("googlesql", "duckdb"):
                self.i += 1
                kids = [] if self.is_op("]") else self.expr_list()
                self.expect_op("]")
                return _N(None, kids, lo, self.i)
            if tok.text == "*":
                self.i += 1
                return _N(None, [], lo, self.i)
            raise _Unsupported(f"an expression that starts with {tok.text!r}")
        if tok.kind == "qident":
            return self.name_or_call()
        if tok.kind != "word":
            raise _Unsupported("unexpected end of the query")
        word = tok.upper
        if word in ("NULL", "TRUE", "FALSE", "UNKNOWN", "DEFAULT"):
            self.i += 1
            return _N(None, [], lo, self.i)
        if word == "CASE":
            return self.case()
        if word == "EXISTS" and self.is_op("(", 1):
            self.i += 2
            inner = self.query()
            self.expect_op(")")
            return _N(None, [inner], lo, self.i)
        if word == "INTERVAL":
            return self.interval()
        if word in ("ARRAY", "STRUCT", "ROW", "MAP") and (self.is_op("(", 1) or self.is_op("[", 1) or self.is_op("<", 1)):
            return self.constructor()
        if word == "RANGE" and self.is_op("<", 1) and self.fam == "googlesql":  # RANGE<DATE> '[a, b)'
            self.i += 1
            self.skip_balanced()
            if self.peek().kind != "string":
                raise _Unsupported("RANGE without a literal")
            self.leaf()
            return _N(None, [], lo, self.i)
        if word in _TYPED_LITERALS and self.peek(1).kind == "string":
            self.i += 1
            self.leaf()
            return _N(None, [], lo, self.i)
        if word in ("ANY", "SOME", "ALL") and self.is_op("(", 1):
            self.i += 2
            inner = self.query() if self.w() in ("SELECT", "WITH") else _N(None, self.expr_list(), self.i, self.i)
            self.expect_op(")")
            return _N(None, [inner], lo, self.i)
        if word in _STOP and word not in ("LEFT", "RIGHT", "IF", "ARRAY", "STRUCT", "REPLACE", "MOD", "SET", "COLLATE", "RANGE", "GROUPING", "UNNEST", "CAST", "INTERVAL", "TRUNCATE", "DATE", "TIME", "TIMESTAMP"):
            if not self.is_op("(", 1):
                raise _Unsupported(f"a keyword where an expression was expected: {tok.text}")
        if word in _BARE_FUNCTIONS and not self.is_op("(", 1) and not self.is_op(".", 1):
            self.i += 1
            return _N(None, [], lo, self.i)
        return self.name_or_call()

    def gap(self, a: int, b: int) -> str:
        """The source text between token ``a`` and token ``b``."""

        return self.sql[self.t[a].end:self.t[b].start]

    def paren(self) -> _N:
        lo = self.i
        self.i += 1
        if self.w() in ("SELECT", "WITH", "VALUES") or (self.is_op("(") and self.looks_like_query()):
            save = self.i
            try:
                inner = self.query()
                self.expect_op(")")
                return _N(None, [inner], lo, self.i)
            except _Unsupported:
                if self.w(0) in ("SELECT", "WITH", "VALUES") and save == lo + 1 and not self.t[save].text == "(":
                    raise
                self.i = save
        first = self.expr(0)
        if self.accept_op(","):
            kids = [first] + self.expr_list()
            self.expect_op(")")
            return _N(None, kids, lo, self.i)
        self.expect_op(")")
        node = _N(None, [first], lo, self.i)
        node.paren = True
        return node

    def looks_like_query(self) -> bool:
        k = 0
        while self.is_op("(", k):
            k += 1
        return self.w(k) in ("SELECT", "WITH", "VALUES")

    def case(self) -> _N:
        lo = self.i
        self.expect("CASE")
        kids = []
        if not self.at("WHEN"):
            kids.append(self.expr(0))
        while self.accept("WHEN"):
            kids.append(self.expr(0))
            self.expect("THEN")
            kids.append(self.expr(0))
        if self.accept("ELSE"):
            kids.append(self.expr(0))
        self.expect("END")
        whole = _N(None, kids, lo, self.i)
        return _N("CASE", [whole], lo, self.i)

    def interval(self) -> _N:
        lo = self.i
        self.expect("INTERVAL")
        if self.fam in ("postgres", "duckdb") and self.peek().kind == "string":
            self.leaf()
            if self.w() in _DATE_PARTS or self.w().rstrip("S") in _DATE_PARTS:
                self.i += 1
                if self.accept("TO"):
                    self.i += 1
            return _N(None, [], lo, self.i)
        value = self.expr(self.interval_level())
        if not (self.w() in _DATE_PARTS or self.w().rstrip("S") in _DATE_PARTS):
            raise _Unsupported("an INTERVAL without a date part")
        self.i += 1
        if self.accept("TO"):
            self.i += 1
        return _N(None, [value], lo, self.i)

    def interval_level(self) -> int:
        return 0

    def constructor(self) -> _N:
        lo = self.i
        word = self.w()
        self.i += 1
        if self.is_op("<"):
            self.skip_balanced()
        if self.accept_op("["):
            kids = [] if self.is_op("]") else self.expr_list()
            self.expect_op("]")
            return _N(None, kids, lo, self.i)
        self.expect_op("(")
        kids = []
        if word == "ARRAY" and self.w() in ("SELECT", "WITH"):
            kids.append(self.query())
        elif not self.is_op(")"):
            while True:
                item = self.expr(0)
                if self.accept("AS"):
                    self.leaf()
                kids.append(item)
                if not self.accept_op(","):
                    break
        self.expect_op(")")
        return _N(None, kids, lo, self.i)

    def name_or_call(self) -> _N:
        lo = self.i
        first = self.peek()
        self.i += 1
        names = [lo]
        while self.is_op(".") and self.peek(1).kind in ("word", "qident"):
            self.i += 2
            names.append(self.i - 1)
        if self.is_op("("):
            name = first.upper if len(names) == 1 else self.t[names[-1]].upper
            return self.call(name, lo)
        for k in names:
            self.leaves.add(k)
        return _N(None, [], lo, self.i)

    def call(self, name: str, lo: int) -> _N:
        self.expect_op("(")
        kids: list[_N] = []
        extra: list[_N] = []
        if name in ("CAST", "SAFE_CAST", "TRY_CAST") or (name == "CONVERT" and self.fam != "mysql"):
            kids.append(self.expr(0))
            if not self.accept("AS"):
                raise _Unsupported(f"{name} without AS")
            self.skip_type()
            if self.accept("FORMAT"):
                kids.append(self.expr(0))
                if self.accept("AT", "TIME", "ZONE"):
                    kids.append(self.expr(0))
        elif name == "CONVERT" and self.fam == "mysql":
            kids.append(self.expr(0))
            if self.accept("USING"):
                self.i += 1
            else:
                self.expect_op(",")
                self.skip_type()
        elif name == "EXTRACT":
            if self.peek().kind != "word":
                raise _Unsupported("EXTRACT without a date part")
            self.i += 1
            if self.is_op("("):
                self.skip_balanced()
            self.expect("FROM")
            kids.append(self.expr(0))
            if self.accept("AT", "TIME", "ZONE"):
                kids.append(self.expr(0))
        elif name == "TRIM" and not self.is_op(")"):
            self.accept("BOTH") or self.accept("LEADING") or self.accept("TRAILING")
            if not self.at("FROM"):
                kids.append(self.expr(0))
            if self.accept("FROM"):
                kids.append(self.expr(0))
            elif self.accept_op(","):
                kids += self.expr_list()
        elif name in ("SUBSTRING", "SUBSTR") and not self.is_op(")"):
            kids.append(self.expr(0))
            if self.accept("FROM"):
                kids.append(self.expr(0))
                if self.accept("FOR"):
                    kids.append(self.expr(0))
            elif self.accept("FOR"):
                kids.append(self.expr(0))
            elif self.accept_op(","):
                kids += self.expr_list()
        elif name == "POSITION" and not self.is_op(")"):
            kids.append(self.expr(self.above_in_level()))
            if self.accept("IN"):
                kids.append(self.expr(0))
            elif self.accept_op(","):
                kids += self.expr_list()
        elif name == "COUNT" and self.is_op("*") and self.is_op(")", 1):
            self.i += 1
        elif not self.is_op(")"):
            kids += self.call_args(extra, name)
        self.expect_op(")")
        node = _N(None, kids + extra, lo, self.i)
        return self.call_suffix(node, extra)

    def call_args(self, sink: list[_N] | None = None, name: str = "") -> list[_N]:
        sink = [] if sink is None else sink
        distinct = self.accept("DISTINCT")
        if not distinct:
            self.accept("ALL")
        args_lo = self.i
        args: list[_N] = []
        while True:
            if self.peek().kind in ("word", "qident") and self.is_op("=>", 1):
                self.i += 2
            if self.w() in ("SEPARATOR", "ORDER", "LIMIT", "IGNORE", "RESPECT", "HAVING") and args:
                break
            arg = self.expr(0)
            if self.accept("AS"):
                if self.peek().kind in ("word", "qident") and not self.is_op("(", 1):
                    self.skip_type()
                else:
                    raise _Unsupported("AS inside a function call")
            args.append(arg)
            if not self.accept_op(","):
                break
        args_hi = self.i
        if distinct:
            if name in ("STRING_AGG", "LISTAGG") and len(args) > 1:
                args_hi = args[0].hi  # DISTINCT applies to the value, not to the separator
            sink.append(_N("AGG DISTINCT", [_N(None, args, args_lo, args_hi)], args_lo, args_hi))
        if self.accept("IGNORE", "NULLS") or self.accept("RESPECT", "NULLS"):
            pass
        if self.accept("HAVING"):
            if not (self.accept("MAX") or self.accept("MIN")):
                raise _Unsupported("HAVING inside a call")
            sink.append(self.expr(0))
        if self.accept("ORDER", "BY"):
            sink.append(self.ordering(sink))
        if self.accept("LIMIT"):
            sink.append(self.expr(0))
        if self.accept("SEPARATOR"):
            sink.append(self.leaf())
        return args

    def call_suffix(self, node: _N, sink: list[_N]) -> _N:
        if self.accept("WITHIN", "GROUP"):
            self.expect_op("(")
            self.expect("ORDER", "BY")
            node.kids.append(self.ordering(node.kids))
            self.expect_op(")")
            node.hi = self.i
        if self.accept("FILTER"):
            self.expect_op("(")
            self.expect("WHERE")
            node.kids.append(self.expr(0))
            self.expect_op(")")
            node.hi = self.i
        if self.accept("IGNORE", "NULLS") or self.accept("RESPECT", "NULLS"):
            node.hi = self.i
        if self.accept("OVER"):
            parts: list[_N] = []
            if self.is_op("("):
                parts.append(self.window_spec(parts, node.lo))
            else:
                parts.append(self.leaf())
            return _N(None, [node] + parts, node.lo, self.i)
        return node

    def window_spec(self, sink: list[_N], owner_lo: int) -> _N:
        """``(PARTITION BY .. ORDER BY .. frame)``; its ORDER BY belongs to the tokens from ``owner_lo`` on."""

        lo = self.i
        order = None
        self.expect_op("(")
        kids: list[_N] = []
        if self.peek().kind in ("word", "qident") and not self.at("PARTITION") and not self.at("ORDER") and self.w() not in ("ROWS", "RANGE", "GROUPS"):
            kids.append(self.leaf())
        if self.accept("PARTITION", "BY"):
            while True:
                key = self.expr(0)
                kids.append(_N("PARTITION KEY", [key], key.lo, key.hi))
                if not self.accept_op(","):
                    break
        if self.accept("ORDER", "BY"):
            order = self.ordering(kids)
        if self.w() in ("ROWS", "RANGE", "GROUPS"):
            self.i += 1
            if self.accept("BETWEEN"):
                kids.append(self.frame_bound())
                self.expect("AND")
                kids.append(self.frame_bound())
            else:
                kids.append(self.frame_bound())
            if self.accept("EXCLUDE"):
                raise _Unsupported("a frame EXCLUDE clause")
        self.expect_op(")")
        if order is not None:
            sink.append(_N("ORDER BY", [_N(None, [], owner_lo, self.i), order], order.lo, order.hi))
        return _N(None, kids, lo, self.i)

    def frame_bound(self) -> _N:
        lo = self.i
        if self.accept("UNBOUNDED"):
            if not (self.accept("PRECEDING") or self.accept("FOLLOWING")):
                raise _Unsupported("a frame bound")
            return _N(None, [], lo, self.i)
        if self.accept("CURRENT", "ROW"):
            return _N(None, [], lo, self.i)
        value = self.expr(self.frame_level())
        if not (self.accept("PRECEDING") or self.accept("FOLLOWING")):
            raise _Unsupported("a frame bound")
        return _N(None, [value], lo, self.i)

    def frame_level(self) -> int:
        return 0

    def above_in_level(self) -> int:
        return 0

    # ---- family hooks ---------------------------------------------------------------------------------

    def prefix(self, minlv: int) -> _N:
        raise NotImplementedError

    def infix(self, left: _N):
        raise NotImplementedError

    # shared pieces of the comparison-level forms

    def postfix_is(self, level: int, left_rule_level: int):
        """``IS [NOT] NULL|TRUE|FALSE|UNKNOWN`` and ``IS [NOT] DISTINCT FROM x`` (the latter is binary)."""

        k = 1
        negated = self.w(k) == "NOT"
        if negated:
            k += 1
        target = self.w(k)
        if target == "DISTINCT" and self.w(k + 1) == "FROM":
            kind = "<=>" if negated else "IS DISTINCT"
            return self.binary(kind, level, k + 2, right_level=left_rule_level)
        if target not in ("NULL", "TRUE", "FALSE", "UNKNOWN"):
            raise _Unsupported(f"IS {target}")

        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=False)
            self.i += k + 1
            node = _N("IS", [left, _N(None, [], self.i - 1, self.i)], left.lo, self.i, lv=level)
            if negated:
                node = _N("NOT", [node], left.lo, self.i, lv=level)
            return node

        return level, apply

    def postfix_in(self, level: int, ntoks: int, negated: bool):
        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=True)
            self.i += ntoks
            lo = self.i
            if self.at("UNNEST") and self.fam == "googlesql":
                self.i += 1
                self.expect_op("(")
                arg = self.expr(0)
                self.expect_op(")")
                items = [_N(None, [arg], lo, self.i)]
            elif self.is_op("(") and self.looks_like_query():
                self.i += 1
                query = self.query()
                self.expect_op(")")
                items = [_N(None, [query], lo, self.i)]
            elif self.is_op("("):
                self.i += 1
                items = [] if self.is_op(")") else self.expr_list()
                self.expect_op(")")
            else:
                raise _Unsupported("IN without a list")
            node = _N("IN", [left] + items, left.lo, self.i, lv=level, binary=True)
            if negated:
                node = _N("NOT", [node], left.lo, self.i, lv=level, binary=True)
            return node

        return level, apply

    def postfix_between(self, level: int, ntoks: int, negated: bool, low_level: int, high_level: int):
        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=True)
            self.i += ntoks
            if self.at("SYMMETRIC") or self.at("ASYMMETRIC"):
                raise _Unsupported("BETWEEN SYMMETRIC")
            low = self.expr(low_level)
            self.expect("AND")
            high = self.expr(high_level)
            node = _N("BETWEEN", [left, low, high], left.lo, high.hi, lv=level, binary=True)
            if negated:
                node = _N("NOT", [node], left.lo, high.hi, lv=level, binary=True)
            return node

        return level, apply

    def postfix_like(self, kind: str, level: int, ntoks: int, negated: bool, pattern_level: int, quantified: bool = False):
        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=True)
            self.i += ntoks
            if quantified and self.w() in ("ANY", "SOME", "ALL") and self.is_op("(", 1):
                pattern = self.primary()
            else:
                pattern = self.expr(pattern_level)
            node = _N(kind, [left, pattern], left.lo, pattern.hi, lv=level, binary=True)
            if negated:
                node = _N("NOT", [node], left.lo, pattern.hi, lv=level, binary=True)
            if self.accept("ESCAPE"):
                escape = self.expr(pattern_level)
                node = _N("ESCAPE", [node, escape], left.lo, escape.hi, lv=level, binary=True)
            return node

        return level, apply

    def comparison(self, kind: str, level: int, right_level: int):
        def apply(left: _N) -> _N:
            self.check_left(left, level, binary=True)
            self.i += 1
            if self.w() in ("ANY", "SOME", "ALL") and self.is_op("(", 1):
                right = self.primary()
            else:
                right = self.expr(right_level)
            return _N(kind, [left, right], left.lo, right.hi, lv=level, binary=True)

        return level, apply


# ---- GoogleSQL ------------------------------------------------------------------------------------------
# 1 OR, 2 AND, 3 NOT, 4 comparisons (=, <, LIKE, BETWEEN, IN, IS; non-associative), 5 |, 6 ^, 7 &, 8 << >>,
# 9 + -, 10 * / ||, 11 unary + - ~, 12 . and [ ].
_G_BINARY = {"|": ("|", 5), "^": ("BITXOR", 6), "&": ("&", 7), "<<": ("<<", 8), ">>": (">>", 8), "+": ("+", 9), "-": ("-", 9), "*": ("*", 10), "/": ("/", 10), "||": ("||", 10)}


class _GoogleSQL(_Parser):
    def interval_level(self) -> int:
        return 4

    def frame_level(self) -> int:
        return 4

    def above_in_level(self) -> int:
        return 4

    def left_rule(self, level: int):
        if level == 4:
            return (lambda left: left.lv > 4, "GoogleSQL comparisons are not associative: an operand that is itself a comparison needs parentheses")
        return None

    def prefix(self, minlv: int) -> _N:
        lo = self.i
        if self.at("NOT"):
            if minlv > 3:
                raise _Rejected("GoogleSQL does not allow NOT as an operand of a tighter operator without parentheses")
            self.i += 1
            inner = self.expr(3)
            return _N("NOT", [inner], lo, inner.hi, lv=3)
        tok = self.peek()
        if tok.kind == "op" and tok.text in ("-", "+", "~"):
            self.i += 1
            inner = self.expr(10)
            kind = {"-": "NEG", "~": "~", "+": None}[tok.text]
            return _N(kind, [inner], lo, inner.hi, lv=11)
        return self.primary()

    def infix(self, left: _N):
        tok = self.peek()
        if tok.kind == "op":
            text = tok.text
            if text in _COMPARISON_OPS:
                return self.comparison(_COMPARISON_OPS[text], 4, 4)
            if text in _G_BINARY:
                kind, level = _G_BINARY[text]
                return self.binary(kind, level, 1)
            if text == "[":
                return 12, self.subscript
            if text == "." and self.peek(1).kind in ("word", "qident"):
                return 12, self.field
            return None
        word = tok.upper
        if word == "OR":
            return self.binary("OR", 1, 1)
        if word == "AND":
            return self.binary("AND", 2, 1)
        if word == "IS":
            return self.postfix_is(4, 4)
        negated = word == "NOT"
        k = 1 if negated else 0
        nxt = self.w(k)
        if nxt == "LIKE":
            return self.postfix_like("LIKE", 4, k + 1, negated, 4, quantified=True)
        if nxt == "IN":
            return self.postfix_in(4, k + 1, negated)
        if nxt == "BETWEEN":
            return self.postfix_between(4, k + 1, negated, 4, 4)
        return None

    def subscript(self, left: _N) -> _N:
        self.i += 1
        index = self.expr(0)
        self.expect_op("]")
        return _N("SUBSCRIPT", [left, index], left.lo, self.i, lv=12)

    def field(self, left: _N) -> _N:
        self.i += 2
        return _N(None, [left], left.lo, self.i, lv=12)


# ---- MySQL ----------------------------------------------------------------------------------------------
# sql_yacc.yy (8.0): expr = OR(1) XOR(2) AND(3) NOT(4) | bool_pri IS [NOT] TRUE/FALSE/UNKNOWN (5);
# bool_pri = bool_pri comp_op predicate | bool_pri IS [NOT] NULL (6, left to right);
# predicate = bit_expr [NOT] IN/BETWEEN/LIKE/REGEXP/SOUNDS LIKE/MEMBER OF (7, no chaining);
# bit_expr = | (8) & (9) << >> (10) + - (11) * / % DIV MOD (12) ^ (13); simple_expr = unary + - ~ ! BINARY (14),
# COLLATE and -> ->> (15).
_M_BINARY = {"|": ("|", 8), "&": ("&", 9), "<<": ("<<", 10), ">>": (">>", 10), "+": ("+", 11), "-": ("-", 11), "*": ("*", 12), "/": ("/", 12), "%": ("%", 12), "^": ("BITXOR", 13)}
_M_COMPARISONS = dict(_COMPARISON_OPS, **{"<=>": "<=>"})


class _MySQL(_Parser):
    def frame_level(self) -> int:
        return 5

    def above_in_level(self) -> int:
        return 7

    def left_rule(self, level: int):
        if level in (5, 6):
            return (lambda left: left.lv >= 6, "MySQL needs parentheses here: the left operand of a comparison or IS cannot be NOT, AND or IS TRUE")
        if level == 7:
            return (lambda left: left.lv >= 8, "MySQL needs parentheses around the left operand of IN, LIKE, BETWEEN or REGEXP")
        if level >= 8:
            return (lambda left: left.lv >= level, "MySQL needs parentheses around a comparison used as an operand of an arithmetic operator")
        return None

    def prefix(self, minlv: int) -> _N:
        lo = self.i
        if self.at("NOT"):
            if minlv > 4:
                raise _Rejected("MySQL reads NOT only where a whole condition may stand; it needs parentheses here")
            self.i += 1
            inner = self.expr(4)
            return _N("NOT", [inner], lo, inner.hi, lv=4)
        tok = self.peek()
        if tok.kind == "op" and tok.text in ("-", "+", "~", "!"):
            self.i += 1
            inner = self.expr(14)
            kind = {"-": "NEG", "~": "~", "+": None, "!": "NOT"}[tok.text]
            return _N(kind, [inner], lo, inner.hi, lv=14)
        if self.at("BINARY") and not self.is_op("(", 1):
            self.i += 1
            inner = self.expr(14)
            return _N(None, [inner], lo, inner.hi, lv=14)
        return self.primary()

    def infix(self, left: _N):
        tok = self.peek()
        if tok.kind == "op":
            text = tok.text
            if text in ("||",):
                return self.binary("OR", 1, 1)
            if text == "&&":
                return self.binary("AND", 3, 1)
            if text in _M_COMPARISONS:
                return self.comparison(_M_COMPARISONS[text], 6, 6)
            if text in _M_BINARY:
                kind, level = _M_BINARY[text]
                return self.binary(kind, level, 1)
            if text in ("->", "->>"):
                return self.binary(None, 15, 1, right_level=15)
            return None
        word = tok.upper
        if word == "OR":
            return self.binary("OR", 1, 1)
        if word == "XOR":
            return self.binary("XOR", 2, 1)
        if word == "AND":
            return self.binary("AND", 3, 1)
        if word == "DIV":
            return self.binary("DIV", 12, 1)
        if word == "MOD":
            return self.binary("%", 12, 1)
        if word == "COLLATE":
            return 15, self.collate
        if word == "IS":
            k = 2 if self.w(1) == "NOT" else 1
            if self.w(k) in ("TRUE", "FALSE", "UNKNOWN"):
                return self.is_truth(k)
            if self.w(k) == "NULL":
                return self.postfix_is(6, 6)
            raise _Unsupported(f"IS {self.w(k)}")
        negated = word == "NOT"
        k = 1 if negated else 0
        nxt = self.w(k)
        if nxt == "IN":
            return self.postfix_in(7, k + 1, negated)
        if nxt == "BETWEEN":
            return self.postfix_between(7, k + 1, negated, 7, 6)
        if nxt == "LIKE":
            return self.postfix_like("LIKE", 7, k + 1, negated, 13)
        if nxt in ("REGEXP", "RLIKE"):
            return self.postfix_like("RLIKE", 7, k + 1, negated, 7)
        if nxt in ("SOUNDS", "MEMBER"):
            raise _Unsupported(nxt)
        return None

    def is_truth(self, k: int):
        negated = k == 2

        def apply(left: _N) -> _N:
            self.check_left(left, 5, binary=False)
            self.i += k + 1
            node = _N("IS", [left, _N(None, [], self.i - 1, self.i)], left.lo, self.i, lv=5)
            if negated:
                node = _N("NOT", [node], left.lo, self.i, lv=5)
            return node

        return 5, apply

    def collate(self, left: _N) -> _N:
        self.i += 1
        name = self.leaf()
        return _N("COLLATE", [left, name], left.lo, self.i, lv=15)


# ---- PostgreSQL and DuckDB --------------------------------------------------------------------------------
# gram.y: 1 OR, 2 AND, 3 NOT, 4 IS ISNULL NOTNULL, 5 < > = <= >= <>, 6 BETWEEN IN LIKE ILIKE SIMILAR (GLOB),
# 8 other operators (|| & | # << >> ~ ...), 9 + -, 10 * / % (//), 11 ^ (**), 12 AT TIME ZONE, 13 COLLATE,
# 14 unary + -, 15 [ ], 16 ::, 17 . ; levels 4, 5 and 6 are non-associative.
_P_OTHER = {"||": "||", "&": "&", "|": "|", "#": "BITXOR", "<<": "<<", ">>": ">>"}


class _Postgres(_Parser):
    def frame_level(self) -> int:
        return 3

    def above_in_level(self) -> int:
        return 6

    def check_left(self, left: _N, level: int, binary: bool) -> None:
        if left.lv == level and left.binary and level in (4, 5, 6):
            raise _Rejected(f"{self.label} operators at one level of comparison are not associative")

    def prefix(self, minlv: int) -> _N:
        lo = self.i
        if self.at("NOT"):
            self.i += 1
            inner = self.expr(3)
            return _N("NOT", [inner], lo, inner.hi, lv=3)
        tok = self.peek()
        if tok.kind == "op" and tok.text in ("-", "+"):
            self.i += 1
            inner = self.expr(14)
            return _N("NEG" if tok.text == "-" else None, [inner], lo, inner.hi, lv=14)
        if tok.kind == "op" and tok.text not in ("(", "[", "*", "::", ",", ")", "]"):
            self.i += 1
            inner = self.expr(8)
            return _N("~" if tok.text == "~" else None, [inner], lo, inner.hi, lv=8)
        return self.primary()

    def infix(self, left: _N):
        tok = self.peek()
        duck = self.fam == "duckdb"
        if tok.kind == "op":
            text = tok.text
            if text in _COMPARISON_OPS:
                return self.comparison(_COMPARISON_OPS[text], 5, 5)
            if text in ("+", "-"):
                return self.binary(text, 9, 1)
            if text in ("*", "/", "%"):
                return self.binary(text, 10, 1)
            if text == "//" and duck:
                return self.binary("DIV", 10, 1)
            if text == "^" or (text == "**" and duck):
                return self.binary("POW", 11, 1)
            if text == "::":
                return 16, self.cast
            if text == "[":
                return 15, self.subscript
            if text == "." and self.peek(1).kind in ("word", "qident"):
                return 17, self.field
            if text in ("(", ")", ",", ";", "]", "{", "}", "=>", ":="):
                return None
            if duck and text in ("->", "->>"):
                raise _Unsupported("DuckDB -> (a lambda or a JSON path)")
            if duck and self.peek(1).kind == "word" and self.peek(1).upper == "NOT":
                # DuckDB keeps the PostgreSQL 13 grammar, where such an operator may also be a postfix one: it
                # then expects NOT IN, NOT LIKE or NOT BETWEEN and fails on what follows.
                raise _Rejected(f"DuckDB rejects NOT as the right operand of {text}")
            return self.binary(_P_OTHER.get(text), 8, 1)
        word = tok.upper
        if word == "OR":
            return self.binary("OR", 1, 1)
        if word == "AND":
            return self.binary("AND", 2, 1)
        if word == "IS":
            return self.postfix_is(4, 4)
        if word in ("ISNULL", "NOTNULL"):
            return 4, self.isnull
        if word == "AT" and self.at("AT", "TIME", "ZONE"):
            return self.binary("AT TIME ZONE", 12, 3)
        if word == "COLLATE":
            return 13, self.collate
        negated = word == "NOT"
        k = 1 if negated else 0
        nxt = self.w(k)
        if nxt == "IN":
            return self.postfix_in(6, k + 1, negated)
        if nxt == "BETWEEN":
            return self.postfix_between(6, k + 1, negated, 6, 6)
        if nxt in ("LIKE", "ILIKE") or (nxt == "GLOB" and duck):
            return self.postfix_like(nxt, 6, k + 1, negated, 6)
        if nxt == "SIMILAR" and self.w(k + 1) == "TO":
            return self.postfix_like("SIMILAR", 6, k + 2, negated, 6)
        return None

    def isnull(self, left: _N) -> _N:
        negated = self.w() == "NOTNULL"
        self.i += 1
        node = _N("IS", [left, _N(None, [], self.i - 1, self.i)], left.lo, self.i, lv=4)
        if negated:
            node = _N("NOT", [node], left.lo, self.i, lv=4)
        return node

    def cast(self, left: _N) -> _N:
        self.i += 1
        self.skip_type()
        return _N("CAST", [left], left.lo, self.i, lv=16)

    def subscript(self, left: _N) -> _N:
        self.i += 1
        index = self.expr(0)
        if self.is_op(":"):
            raise _Unsupported("an array slice")
        self.expect_op("]")
        return _N("SUBSCRIPT", [left, index], left.lo, self.i, lv=15)

    def field(self, left: _N) -> _N:
        self.i += 2
        return _N(None, [left], left.lo, self.i, lv=17)

    def collate(self, left: _N) -> _N:
        self.i += 1
        name = self.leaf()
        return _N("COLLATE", [left, name], left.lo, self.i, lv=13)


_PARSERS = {"googlesql": _GoogleSQL, "mysql": _MySQL, "postgres": _Postgres, "duckdb": _Postgres}


# ---------------------------------------------------------------------------------------------------------
# Operation keys


_FLAT = ("AND", "OR")
# Operations that flip what a query returns without moving any operand: a tree that has one the text does
# not is as wrong as one that lost one the text has. (For every other operation, sqlglot adding one that the
# text lacks changes nothing a proof depends on: the operations the text has are all still there.)
_NOT_INVENTED = ("NOT", "NEG", "~", "DESC", "SELECT DISTINCT", "AGG DISTINCT", "DISTINCT ON")


def _keys(nodes: list[_N], starts: list[int], universe: frozenset[int], toks: list[_Tok]) -> tuple[Counter, dict]:
    """``(kind, anchors of each operand)`` for every operation of the independent reading, with examples."""

    candidate = [tok.kind in ("word", "qident", "number", "string") for tok in toks]
    keys: Counter = Counter()
    example: dict = {}

    def anchors(node: _N) -> frozenset[int]:
        return frozenset(starts[k] for k in range(node.lo, node.hi) if candidate[k] and starts[k] in universe)

    def operands(node: _N, kind: str) -> list[_N]:
        inner = node
        while inner.kind is None and inner.paren and len(inner.kids) == 1:
            inner = inner.kids[0]
        if inner.kind == kind:
            out = []
            for kid in inner.kids:
                out += operands(kid, kind)
            return out
        return [node]

    skip: set[int] = set()
    seen: set[int] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        stack.extend(node.kids)
        if node.kind is None or id(node) in skip:
            continue
        if node.kind in _FLAT:
            parts = []
            for kid in node.kids:
                parts += operands(kid, node.kind)
            for kid in node.kids:
                inner = kid
                while inner.kind is None and inner.paren and len(inner.kids) == 1:
                    inner = inner.kids[0]
                _mark_chain(inner, node.kind, skip)
        else:
            parts = node.kids
        key = (node.kind, tuple(anchors(part) for part in parts))
        if not any(key[1]):
            continue
        keys[key] += 1
        example.setdefault(key, node)
    return keys, example


def _mark_chain(node: _N, kind: str, skip: set[int]) -> None:
    if node.kind == kind:
        skip.add(id(node))
        for kid in node.kids:
            inner = kid
            while inner.kind is None and inner.paren and len(inner.kids) == 1:
                inner = inner.kids[0]
            _mark_chain(inner, kind, skip)


_CLASS_KINDS = {
    "And": "AND", "Or": "OR", "Xor": "XOR", "Not": "NOT", "EQ": "=", "NEQ": "<>", "LT": "<", "GT": ">", "LTE": "<=",
    "GTE": ">=", "NullSafeEQ": "<=>", "NullSafeNEQ": "IS DISTINCT", "Is": "IS", "Like": "LIKE", "ILike": "ILIKE",
    "RegexpLike": "RLIKE", "SimilarTo": "SIMILAR", "Glob": "GLOB", "In": "IN", "Between": "BETWEEN",
    "Escape": "ESCAPE", "Add": "+", "Sub": "-", "Mul": "*", "Div": "/", "IntDiv": "DIV", "Mod": "%", "Pow": "POW",
    "DPipe": "||", "BitwiseAnd": "&", "BitwiseOr": "|", "BitwiseXor": "BITXOR", "BitwiseLeftShift": "<<",
    "BitwiseRightShift": ">>", "BitwiseNot": "~", "Neg": "NEG", "Collate": "COLLATE", "AtTimeZone": "AT TIME ZONE",
    "Bracket": "SUBSCRIPT", "Case": "CASE", "Cast": "CAST",
}
_QUERY_OWNERS = ("Select", "Union", "Intersect", "Except", "Subquery", "Values")


def _start(node, alias: dict[int, int]) -> int | None:
    """Where sqlglot places ``node`` in the text, moved to the start of a prefixed string (``_utf8'a'``)."""

    meta = node._meta
    if meta and "start" in meta:
        return alias.get(meta["start"], meta["start"])
    return None


_WEEK_START = getattr(exp, "WeekStart", None)  # sqlglot 26.0.0 has no such node
_VALUE_WORDS = frozenset(("NULL", "TRUE", "FALSE"))


def _anchor_values(tree: exp.Expression, toks: list[_Tok], alias: dict[int, int], length: int) -> None:
    """Give sqlglot's ``NULL``, ``TRUE`` and ``FALSE`` nodes (it records no position for them) their token.

    Without a position the operand ``NULL`` of ``!2 IS NULL`` has nothing to compare, and sqlglot's ``NOT (2 IS
    NULL)`` looks the same as the engine's ``(NOT 2) IS NULL``. The nodes are placed by order: between two
    positioned names or literals that sqlglot's tree visits one after the other, the value tokens lying
    between the same two offsets in the text belong to the value nodes between them, in order. A tree that
    visits its positioned leaves out of text order, or a gap with another number of tokens, keeps no positions
    (those operands stay unanchored, as before).
    """

    words = sorted(tok.start for tok in toks if tok.kind == "word" and tok.upper in _VALUE_WORDS)
    if not words:
        return
    plan: list[tuple[list, int, int]] = []
    pending: list = []
    before = -1
    for node in tree.walk(bfs=False):
        if isinstance(node, (exp.Null, exp.Boolean)) and not (node._meta and "start" in node._meta):
            pending.append(node)
            continue
        start = _start(node, alias)
        if start is None:
            continue
        if start <= before:
            return  # not in text order: leave the whole tree alone
        if pending:
            plan.append((pending, before, start))
            pending = []
        before = start
    if pending:
        plan.append((pending, before, length))
    for nodes, low, high in plan:
        inside = [pos for pos in words if low < pos < high]
        if len(inside) == len(nodes):
            for node, pos in zip(nodes, inside):
                node.meta["start"] = pos


def _positions(tree: exp.Expression, alias: dict[int, int]) -> set[int]:
    out = set()
    for node in tree.walk():
        start = _start(node, alias)
        if start is not None:
            out.add(start)
    return out


def _sqlglot_keys(trees: list[exp.Expression], universe: frozenset[int], alias: dict[int, int]) -> Counter:
    cache: dict[int, frozenset[int]] = {}

    def anchors(node) -> frozenset[int]:
        if node is None:
            return frozenset()
        if isinstance(node, list):
            out = frozenset()
            for item in node:
                out |= anchors(item)
            return out
        if not isinstance(node, exp.Expression):
            return frozenset()
        key = id(node)
        if key not in cache:
            found = set()
            for sub in node.walk():
                start = _start(sub, alias)
                if start in universe:
                    found.add(start)
            cache[key] = frozenset(found)
        return cache[key]

    def unparen(node):
        while isinstance(node, exp.Paren):
            node = node.this
        return node

    def operands(node, cls) -> list:
        inner = unparen(node)
        if type(inner) is cls:
            return operands(inner.this, cls) + operands(inner.expression, cls)
        return [node]

    keys: Counter = Counter()

    def add(kind: str, parts: list) -> None:
        key = (kind, tuple(anchors(part) for part in parts))
        if any(key[1]):
            keys[key] += 1

    for tree in trees:
        for node in tree.walk():
            name = type(node).__name__
            kind = _CLASS_KINDS.get(name)
            if kind in _FLAT:
                parent = node.parent
                while isinstance(parent, exp.Paren):
                    parent = parent.parent
                if type(parent) is type(node):
                    continue
                add(kind, operands(node.this, type(node)) + operands(node.expression, type(node)))
            elif kind == "CASE":
                add(kind, [node])
            elif kind == "IN":
                rest = node.args.get("query") or node.args.get("unnest") or node.args.get("field")
                add(kind, [node.this] + (list(node.expressions) if rest is None else [rest]))
            elif kind == "BETWEEN":
                add(kind, [node.this, node.args.get("low"), node.args.get("high")])
            elif kind == "SUBSCRIPT":
                add(kind, [node.this, list(node.expressions)])
            elif kind == "CAST":
                add(kind, [node.this])
            elif kind == "AT TIME ZONE":
                add(kind, [node.this, node.args.get("zone")])
            elif kind is not None:
                if isinstance(node, exp.Binary) or "expression" in node.arg_types:
                    add(kind, [node.this, node.args.get("expression")])
                else:
                    add(kind, [node.this])
            elif name in ("Union", "Intersect", "Except"):
                word = {"Union": "UNION", "Intersect": "INTERSECT", "Except": "EXCEPT"}[name]
                add(word + ("" if node.args.get("distinct") else " ALL"), [node.this, node.expression])
            elif name in ("Where", "Having", "Qualify"):
                add(name.upper(), [node.this])
            elif name == "Join":
                add("JOIN " + _join_kind(node), [node.this, [node.args.get("on"), node.args.get("using")]])
            elif name == "Group":
                for key in node.expressions:
                    add("GROUP KEY", [key])
            elif name == "Order" and type(node.parent).__name__ in _QUERY_OWNERS + ("Window",):
                add("ORDER BY", [node.parent, list(node.expressions)])
            elif name == "Ordered":
                if node.args.get("desc"):
                    add("DESC", [node.this])
                nulls_first = node.args.get("nulls_first")
                if nulls_first is True:
                    add("NULLS FIRST", [node.this])
                elif nulls_first is False:
                    add("NULLS LAST", [node.this])
            elif name == "Limit" and node.parent is not None:
                add("LIMIT", [node.parent, node.args.get("expression")])
                if node.args.get("offset") is not None:
                    add("OFFSET", [node.parent, node.args.get("offset")])
            elif name == "Fetch" and node.parent is not None:
                add("LIMIT", [node.parent, node.args.get("count")])
            elif name == "Offset" and node.parent is not None:
                add("OFFSET", [node.parent, node.args.get("expression")])
            elif name == "Distinct":
                if node.arg_key == "distinct" and type(node.parent).__name__ == "Select":
                    if node.args.get("on") is not None:
                        add("DISTINCT ON", [node.args.get("on")])
                    else:
                        add("SELECT DISTINCT", [list(node.parent.expressions)])
                else:
                    add("AGG DISTINCT", [list(node.expressions)])
            elif name == "Alias":
                add("ALIAS", [node.this, node.args.get("alias")])
            elif name == "Window":
                for key in node.args.get("partition_by") or []:
                    add("PARTITION KEY", [key])
    return keys


def _join_kind(join: exp.Join) -> str:
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    method = (join.args.get("method") or "").upper()
    if kind in ("SEMI", "ANTI"):
        text = f"{side} {kind}" if side else kind
    elif side:
        text = side
    else:
        text = "INNER"
    return ("NATURAL " + text) if method == "NATURAL" else text


# ---------------------------------------------------------------------------------------------------------
# The check


def _describe(node: _N, toks: list[_Tok], sql: str) -> str:
    def text(part: _N | None) -> str:
        if part is None or part.lo >= part.hi:
            return ""
        return " ".join(sql[toks[part.lo].start:toks[part.hi - 1].end].split())

    kind = node.kind
    parts = [text(kid) for kid in node.kids]
    if kind in ("NOT", "NEG", "~"):
        return f"{kind} ({parts[0]})" if kind != "NEG" else f"-({parts[0]})"
    if kind in ("CASE", "CAST"):
        return text(node)
    if len(parts) == 2 and kind not in ("LIMIT", "OFFSET", "ORDER BY", "ALIAS") and not kind.startswith("JOIN"):
        return f"({parts[0]}) {kind} ({parts[1]})"
    return f"{kind} of " + ", ".join(f"({p})" for p in parts if p)


_LITERAL_TYPES = frozenset(
    "STRING RAW_STRING BYTE_STRING NATIONAL_STRING HEX_STRING BIT_STRING HEREDOC_STRING UNICODE_STRING IDENTIFIER "
    "NUMBER".split()
)


def _token_mismatch(sql: str, toks: list[_Tok], theirs: list, label: str) -> str | None:
    """Where sqlglot's tokens and the independent ones split the text differently, or ``None``.

    A comment one side skips and the other reads, or a string or name one side ends earlier, shows up here
    before any tree is compared. Names, literals and keywords must start and end at the same places; sqlglot's
    ``ORDER BY`` and other multi-word keywords count as their words, and a MySQL character set introducer
    (``_utf8'a'``) as part of its string. Operators and parameters only need to cover the same characters:
    sqlglot reads ``<<`` as two ``<`` and ``@x`` as ``@`` and ``x``, and the trees show what it made of them.
    """

    spans: list[tuple[int, int]] = []
    pending: int | None = None
    for tok in theirs:
        lo, hi = tok.start, tok.end + 1
        name = tok.token_type.name
        if name == "INTRODUCER":
            pending = lo
            continue
        if pending is not None:
            lo, pending = pending, None
        if name in _LITERAL_TYPES:
            spans.append((lo, hi))
        else:
            spans.extend((lo + m.start(), lo + m.end()) for m in re.finditer(r"\S+", sql[lo:hi]))
    mine = [(tok.start, tok.end) for tok in toks if tok.kind != "eof"]
    if set(spans) == set(mine):
        return None
    loose = [tok.kind in ("op", "param") for tok in toks if tok.kind != "eof"]
    loose_chars = {k for (a, b), flag in zip(mine, loose) if flag for k in range(a, b)}
    covered = {k for a, b in spans for k in range(a, b)}
    exact = set(spans)
    for (a, b), flag in zip(mine, loose):
        if flag:
            if any(k not in covered for k in range(a, b)):
                return f"sqlglot skips {sql[a:b + 12]!r} at offset {a}, which {label} reads"
        elif (a, b) not in exact:
            return f"sqlglot splits {sql[a:b + 12]!r} at offset {a} into different tokens"
    mine_set = set(mine)
    for a, b in spans:
        if (a, b) not in mine_set and any(k not in loose_chars for k in range(a, b)):
            return f"sqlglot reads {sql[a:b]!r} at offset {a}, where {label} has a comment or a different token"
    return None


def _name_parts(text: str) -> list[str]:
    return [part.upper() for part in re.split(r"[.\-]", text) if part]


def _dropped(leaves: set[int], toks: list[_Tok], positioned: set[int], trees: list) -> int | None:
    """The offset of a name or literal the independent reading has and sqlglot's tree lost, or ``None``.

    Names are compared by text, since sqlglot keeps some without a position or at another name's position (a
    date part such as ``DAY``, the parts of ``a-b.c`` or of a quoted ``a.b``): every part of every name read
    here must appear among sqlglot's names as often. sqlglot rebuilds some literals without a position (a
    format string or JSON path it translates, an ``INTERVAL`` count), so a literal only counts as lost when
    sqlglot has no unplaced literal left to account for it.
    """

    theirs: Counter = Counter()
    unplaced_literals = 0
    for tree in trees:
        for node in tree.walk():
            placed = bool(node._meta and "start" in node._meta)
            if isinstance(node, (exp.Identifier, exp.Var)) and isinstance(node.this, str):
                theirs.update(_name_parts(node.this))
            elif _WEEK_START is not None and isinstance(node, _WEEK_START):
                theirs["WEEK"] += 1  # WEEK is read as WEEK(SUNDAY)
            elif isinstance(node, exp.Literal) and not placed and re.fullmatch(r"[A-Za-z_]\w*", str(node.this)):
                theirs[str(node.this).upper()] += 1  # a date part kept as a string, such as ISOWEEK
            if not placed and isinstance(node, (exp.Literal, exp.Var, exp.JSONPath)):
                unplaced_literals += 1
    mine: Counter = Counter()
    first: dict[str, int] = {}
    literals = []
    for k in sorted(leaves):
        tok = toks[k]
        if tok.kind in ("word", "qident"):
            for part in _name_parts(tok.text[1:-1] if tok.kind == "qident" else tok.text):
                mine[part] += 1
                first.setdefault(part, tok.start)
        elif tok.kind in ("number", "string") and tok.start not in positioned:
            literals.append(tok.start)
    lost = mine - theirs
    if lost:
        return min(first[part] for part in lost)
    return literals[0] if len(literals) > unplaced_literals else None


@lru_cache(maxsize=8192)
def check_query(sql: str, dialect: str = "bigquery") -> ParseCheck:
    """Compare sqlglot's reading of ``sql`` (in ``dialect``) with the independent reading."""

    return _check(sql, dialect)


def _positioned_parser(engine):
    """``engine``'s parser, made to record where each name and literal starts when sqlglot does not.

    sqlglot 30 puts the token's offsets in ``_meta`` of every identifier and literal; sqlglot 26 records none, so
    the independent reading would have nothing to line up with and every query would "agree". For such a release
    the parser class is subclassed (never an expression class) to do the same as each node is made: the token it
    was made from is the one the parser has just consumed.
    """

    parser = engine.parser()
    cls = type(parser)
    if cls not in _POSITIONED:
        probe = sqlglot.parse_one("SELECT 1", read=None)
        literal = probe.find(exp.Literal)
        if literal is not None and literal._meta and "start" in literal._meta:
            _POSITIONED[cls] = cls
        else:

            class Positioned(cls):  # type: ignore[misc, valid-type]
                def expression(self, exp_class, comments=None, **kwargs):
                    instance = super().expression(exp_class, comments=comments, **kwargs)
                    if isinstance(instance, _POSITIONED_NODES) and self._prev is not None and not instance._meta:
                        token = self._prev
                        instance.meta.update(line=token.line, col=token.col, start=token.start, end=token.end)
                    return instance

            _POSITIONED[cls] = Positioned
    return _POSITIONED[cls]()


_POSITIONED: dict[type, type] = {}
# sqlglot 30 gives the byte and raw string literals a position as well; they are separate classes in sqlglot 26
_POSITIONED_NODES = tuple(getattr(exp, name) for name in ("Identifier", "Literal", "ByteString", "RawString") if hasattr(exp, name))


def _check(sql: str, dialect: str, mutate=None) -> ParseCheck:
    """:func:`check_query` without the cache. ``mutate`` (fault injection, ``tests/test_parse_check_faults.py``)
    receives the trees sqlglot read and returns the trees to compare, standing in for a misreading parser."""

    from sqlglot.dialects.dialect import Dialect

    from .ast_utils import canonical_negation, quiet_parser

    fam = family(dialect)
    if fam is None:
        return ParseCheck(dialect, "unchecked", note=f"no precedence table for {dialect or 'the default dialect'}")
    try:
        tokenizer = _Tokenizer(sql, fam)
        toks = tokenizer.run()
    except _Unsupported as error:
        return ParseCheck(dialect, "unchecked", note=str(error))
    if not (toks[0].upper in ("SELECT", "WITH", "VALUES") or (toks[0].kind == "op" and toks[0].text == "(")):
        return ParseCheck(dialect, "unchecked", note=f"a statement that starts with {toks[0].text!r}")
    if any(tok.kind == "op" and tok.text == "|>" for tok in toks):
        return ParseCheck(dialect, "unchecked", note="pipe syntax")
    # sqlglot's own first attempt, as ``sqlglot.parse`` makes it. SQL it reads only after KumoSQL's syntax
    # rewrites (``bigquery_syntax``) is left unchecked, since its positions are those of the rewritten text.
    try:
        engine = Dialect.get_or_raise(dialect)
        with quiet_parser():
            theirs = engine.tokenize(sql)
            trees = [canonical_negation(t) for t in _positioned_parser(engine).parse(theirs, sql) if t is not None]
        if mutate is not None:
            trees = mutate(trees)
    except Exception as error:  # sqlglot raises more than its own errors on some inputs
        first = (str(error).splitlines() or [type(error).__name__])[0]
        return ParseCheck(dialect, "unchecked", note=f"sqlglot does not parse it: {first[:80]}")
    reasons = list(tokenizer.notes)
    mismatch = _token_mismatch(sql, toks, theirs, _LABELS[fam])
    if mismatch:
        reasons.append(mismatch)
    alias = {}
    for tok in toks:
        if tok.kind == "string" and tok.text[0] not in "'\"$":
            alias[tok.start + min(tok.text.find(q) % len(tok.text) for q in "'\"")] = tok.start
    for tree in trees:
        _anchor_values(tree, toks, alias, len(sql))
    positioned: set[int] = set()
    for tree in trees:
        positioned |= _positions(tree, alias)
    starts = [tok.start for tok in toks]
    token_starts = set(starts)
    stray = sorted(p for p in positioned if p not in token_starts)
    if stray:
        where = stray[0]
        reasons.append(f"sqlglot reads a name or literal at offset {where} ({sql[where:where + 12]!r}), where {_LABELS[fam]} has none")
    parser = _PARSERS[fam](toks, fam, sql)
    try:
        nodes = parser.statements()
    except _Rejected as error:
        reasons.append(str(error))
        return ParseCheck(dialect, "disagree", tuple(reasons))
    except _Unsupported as error:
        if reasons:
            return ParseCheck(dialect, "disagree", tuple(reasons))
        return ParseCheck(dialect, "unchecked", note=str(error))
    except RecursionError:
        return ParseCheck(dialect, "unchecked", note="too deeply nested")
    dropped = _dropped(parser.leaves, toks, positioned, trees)
    if dropped is not None:
        reasons.append(f"sqlglot's tree has nothing for {sql[dropped:dropped + 20]!r} at offset {dropped}")
    candidates = {tok.start for tok in toks if tok.kind in ("word", "qident", "number", "string")}
    universe = frozenset(p for p in positioned if p in candidates)
    mine, examples = _keys(nodes, starts, universe, toks)
    theirs_keys = _sqlglot_keys(trees, universe, alias)
    missing = mine - theirs_keys
    for key in sorted(missing, key=lambda k: examples[k].lo):
        reasons.append(f"{_LABELS[fam]} reads {_describe(examples[key], toks, sql)}; sqlglot's tree does not")
    for key in sorted((theirs_keys - mine).elements(), key=repr):
        if key[0] in _NOT_INVENTED:
            reasons.append(f"sqlglot's tree has {key[0]} over text that {_LABELS[fam]} reads without it")
    compared = sum(mine.values())
    if reasons:
        return ParseCheck(dialect, "disagree", tuple(reasons), compared)
    return ParseCheck(dialect, "agree", (), compared)


def reading(sql: str, dialect: str = "bigquery") -> str | None:
    """``sql`` with the independent reading's grouping spelled out in parentheses, or ``None`` when unchecked.

    ``SELECT a | b & c`` under GoogleSQL comes back as ``SELECT (a | (b & c))``. Running that text and the
    original on the engine itself shows whether the precedence tables are right (``tools/parser_oracle.py``).
    """

    fam = family(dialect)
    if fam is None:
        return None
    try:
        toks = _Tokenizer(sql, fam).run()
        nodes = _PARSERS[fam](toks, fam, sql).statements()
    except (_Unsupported, _Rejected, RecursionError):
        return None

    def render(node: _N) -> str:
        lo, hi = toks[node.lo].start, toks[node.hi - 1].end
        out, at = [], lo
        for kid in sorted((k for k in node.kids if k is not None and k.lo < k.hi), key=lambda k: k.lo):
            start, end = toks[kid.lo].start, toks[kid.hi - 1].end
            if start < at or end > hi:
                continue
            out.append(sql[at:start])
            out.append(render(kid))
            at = end
        out.append(sql[at:hi])
        text = "".join(out)
        return f"({text})" if node.kind is not None else text

    if not nodes:
        return None
    pieces, at = [], 0
    for node in nodes:
        if node is None or node.lo >= node.hi:
            continue
        start, end = toks[node.lo].start, toks[node.hi - 1].end
        pieces.append(sql[at:start])
        pieces.append(render(node))
        at = end
    pieces.append(sql[at:])
    return "".join(pieces)


# The operations whose grouping the round trip compares. Everything else (a ``FULL JOIN`` that MySQL cannot
# print, ``DIV`` printed as a cast, ``TRUE`` printed as ``1``) is a leaf: the provers read those trees, not the
# text, and a printing idiom of one engine is not a reading of the text.
_GROUPING = (
    exp.Connector, exp.Not, exp.Neg, exp.BitwiseNot, exp.Binary, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike,
    exp.Case, exp.Escape,
)
_NOT_GROUPING = (exp.Alias, exp.Cast, exp.TryCast)
_DIV_CASTS = ("SIGNED", "INT", "INTEGER", "BIGINT")


def _undo_div_cast(tree: exp.Expression) -> exp.Expression:
    """Read MySQL's ``CAST(a / b AS SIGNED)``, which is how sqlglot prints ``a DIV b``, as ``a DIV b`` again."""

    for node in list(tree.find_all(exp.Cast)):
        kind = node.to.sql(dialect="mysql").upper() if node.to is not None else ""
        if isinstance(node.this, exp.Div) and kind in _DIV_CASTS:
            division = exp.IntDiv(this=node.this.this, expression=node.this.expression)
            if node is tree:
                return division
            node.replace(division)
    return tree


def _skeleton(node: object) -> object:
    """The grouping of operators in ``node``: ``(class, (operand, ...))``, a plain operand being ``"_"``."""

    if isinstance(node, exp.Paren):
        return _skeleton(node.this)
    if isinstance(node, _GROUPING) and not isinstance(node, _NOT_GROUPING):
        if isinstance(node, exp.Connector):
            kind, operands = type(node), []
            stack = [node]
            while stack:
                item = stack.pop()
                inner = item
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                if type(inner) is kind:
                    stack += [inner.expression, inner.this]
                else:
                    operands.append(inner)
            return (kind.__name__, tuple(_skeleton(o) for o in operands))
        children = [
            child for key, value in node.args.items() for child in (value if isinstance(value, list) else [value])
            if isinstance(child, exp.Expression) and key not in ("kind",)
        ]
        return (type(node).__name__, tuple(_skeleton(c) for c in children))
    found = []
    for child in node.iter_expressions():
        item = _skeleton(child)
        if item != "_":
            found += list(item) if isinstance(item, tuple) and item and isinstance(item[0], tuple) else [item]
    return tuple(found) if found else "_"


@lru_cache(maxsize=8192)
def round_trip(sql: str, dialect: str = "bigquery") -> str | None:
    """Why sqlglot does not read its own printing of ``sql`` back to the same grouping, or ``None``.

    The provers print trees and read them again, so a printing that regroups operators (a dropped parenthesis,
    a ``NOT`` moved) turns one query into another between two stages. Only the grouping is compared (see
    ``_skeleton``): sqlglot's printing idioms for what an engine cannot say are not misreads.
    """

    from .ast_utils import canonical_negation, quiet_parser

    try:
        with quiet_parser():
            trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception:  # nothing was read, so nothing can be proved about it
        return None
    for tree in trees:
        tree = canonical_negation(tree)
        if family(dialect) == "mysql" and (
            any(str(j.args.get("side") or "").upper() == "FULL" or str(j.args.get("kind") or "").upper() in ("ANTI", "SEMI") for j in tree.find_all(exp.Join))
            or any(o.args.get("nulls_first") is not None and o.args.get("nulls_first") == bool(o.args.get("desc")) for o in tree.find_all(exp.Ordered))
        ):
            continue  # MySQL has no FULL, ANTI or SEMI JOIN and no NULLS FIRST on DESC: sqlglot prints an emulation, a different tree by design
        try:
            with quiet_parser():
                text = tree.sql(dialect=dialect)
                back = canonical_negation(sqlglot.parse_one(text, read=dialect))
        except Exception:
            return f"sqlglot cannot read back its own {dialect} SQL"
        if family(dialect) == "mysql":
            tree, back = _undo_div_cast(tree), _undo_div_cast(back)
        if _skeleton(back) != _skeleton(tree):
            return f"sqlglot reads its own {dialect} SQL back with different operator grouping"
    return None


def disagreement(sql: str, dialect: str = "bigquery") -> str | None:
    """Why sqlglot's reading of ``sql`` cannot be trusted, or ``None`` when nothing was found."""

    check = check_query(sql, dialect)
    if check.disagrees:
        return check.reasons[0]
    return round_trip(sql, dialect)


# ---------------------------------------------------------------------------------------------------------
# The hook where proofs are accepted

_ACTIVE: ContextVar[bool] = ContextVar("kumosql_parse_check_active", default=False)


@contextmanager
def outermost():
    """``True`` for the outermost prover call; nested calls (on texts the provers wrote) get ``False``."""

    if _ACTIVE.get():
        yield False
        return
    token = _ACTIVE.set(True)
    try:
        yield True
    finally:
        _ACTIVE.reset(token)


def guarded(left_sql: str, right_sql: str, dialect: str = "bigquery") -> str | None:
    """The reason to refuse a proof of ``left_sql`` against ``right_sql``, or ``None``."""

    for sql in (left_sql, right_sql):
        try:
            reason = disagreement(sql, dialect)
        except (RecursionError, Exception):  # a failing check must never turn into a failing prover
            continue
        if reason:
            return f"parser disagreement: {reason}"
    return None


def refuse_misread_proofs(prover):
    """Decorate a public prover entry point so a proof of text sqlglot misreads becomes ``not_proven``.

    The decorated function takes the two query texts first and returns a result with a ``status`` whose enum
    has ``NOT_PROVEN`` (``EquivalenceResult``, ``SmtEquivalenceResult``). A proven or conditionally proven
    result is checked with :func:`guarded` under the call's ``dialect`` (``bigquery`` when it has none); a
    disagreement replaces it with ``not_proven`` and the reason. Only the outermost call checks, so the texts
    the provers write for their own stages are not read again, and a result that is not a proof passes
    through untouched: the check can only remove a proof.
    """

    @wraps(prover)
    def checked(*args, **kwargs):
        with outermost() as top:
            result = prover(*args, **kwargs)
            if not top or not (result.proven or getattr(result, "conditionally_proven", False)):
                return result
            left_sql = args[0] if args else kwargs["left_sql"]
            right_sql = args[1] if len(args) > 1 else kwargs["right_sql"]
            reason = guarded(left_sql, right_sql, kwargs.get("dialect") or "bigquery")
            if reason is None:
                return result
            changes = {"status": type(result.status)["NOT_PROVEN"], "reason": reason}
            for name in ("conditions", "assumptions"):
                if hasattr(result, name):
                    changes[name] = ()
            return replace(result, **changes)

    return checked
