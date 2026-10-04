"""Independent check of a formatting-only rewrite (``format_sql``).

Like ``proof_steps``, ``proof_ctes``, ``proof_syntax`` and ``proof_qualify``, this module imports no rule,
normalizer, lifter or prover. It does not use ``layout_equivalence`` either, which ``format_sql`` itself calls
to decide whether its own output is acceptable. The check reads the step's two texts with its own BigQuery
scanner and accepts only when they are the same statements apart from layout.

A step is accepted when all of these hold:

1. **Same tokens.** Both texts are scanned completely (an unterminated string, comment or quoted name refuses
   the step) into the same sequence of tokens: words, quoted names, string and bytes literals, numbers,
   operators and punctuation, ``@param`` references, ``@{...}`` statement hints and ``${...}`` templates. Only
   whitespace and comments may lie between tokens, so a dropped, added, reordered or edited token is refused.
   Every token must be spelled the same, character for character (literals, quoted names, numbers, hints and
   templates included), with these exceptions, which differ in case only:

   * a **reserved keyword** (``select`` and ``SELECT``) that is not a path part. Reserved words cannot be
     names, so case cannot matter. A word beside a dot is never one (``s.group`` is a field);
   * the name of a **call to a built-in function** (``count(x)``). Built-in names are case-insensitive, but
     the name of a function the texts create (``CREATE TEMP FUNCTION``), of a table function or procedure read
     after ``FROM``, ``JOIN`` or ``CALL``, and any dotted name are not, so those must match exactly;
   * any **other keyword** (``view``, ``offset``, ``month``, ``int64``, ``matched``), and only when sqlglot reads both
     texts and the trees are identical (rule 4). sqlglot's trees keep the case of every table, column, alias,
     struct field and unknown function name, so a word that is one of those changes the tree when its case
     changes, and one that is only syntax does not. Never accepted this way: a word beside a dot (a path part:
     ``s.f``, ``ML.PREDICT``, ``NET.HOST``), a word followed by ``=>`` or ``AS`` (a named argument, or a variable of
     ``WITH(a AS 1, a + 1)``, whose case sqlglot's tree does not keep), and a name the texts create.

   Case of any other word is never accepted: a table, dataset, column, alias, struct field, named argument or
   option name. BigQuery reads some of those case-insensitively, but this check does not list which, so ``Id``
   to ``id`` is a refusal even where the engine would accept it.

2. **Layout the meaning depends on.** Whether the two sides of a dot touch must not change (``a.b`` is not
   ``a . b``), a dash inside a table path (``my-project.ds.t``) keeps its neighbours, and two string
   literals either touch in both texts or in neither (``'a' 'b'`` is one literal, ``'a''b'`` is invalid).

3. **Comments are not edited.** The comments, taken in order, must be the same. A line comment may lose trailing
   whitespace, and a block comment may be re-indented line by line (words, line breaks and order are
   unchanged). A comment that holds a hint (``/*+``), a template (``${``, ``{{``, ``{%``) or ``@{`` must be
   identical. A comment may move between tokens (a formatter moves a trailing comment when it moves a comma):
   comments carry no meaning in BigQuery, and rule 1 already refuses any change to the tokens, including a
   token that a moved line comment would swallow. Added or dropped comments are refused, because a change in
   a comment is the easiest way to hide a change.

4. **Same trees.** The text is split into statements at top-level semicolons. Each statement is parsed by sqlglot
   (BigQuery) before and after, and the trees, which keep identifier case and leave out comments, must be identical.
   A statement sqlglot cannot read, or reads as an opaque command or raw text (a script statement, ``LOAD DATA``,
   ``ALTER SCHEMA``, a ``GRAPH_TABLE`` body), has no tree that could confirm anything: it is checked by rules 1 to 3
   alone, and a case change there is accepted only for a reserved keyword, a built-in call, or a word of the
   listed statement keywords, types and date parts (``SCRIPT_KEYWORDS``) that does not follow a word introducing a
   table, routine or schema name. A statement that parses before but not after is refused.

Template text: ``${...}`` expressions are single verbatim tokens, and ``{{ ... }}`` or ``{% ... %}`` Jinja is
refused as a whole, because its expansion is not SQL this check can read.

Any failure, including an error in the checker, refuses the step.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery

from .proof_steps import RewriteStep, StepCheck, _key

FORMAT_FAMILY = "layout_only"
FORMAT_ASSUMPTIONS = (
    "tokens_identical_apart_from_layout",
    "case_changes_only_where_case_is_insignificant",
    "comments_unedited_hints_and_templates_verbatim",
    "parse_trees_identical",
)

# GoogleSQL's reserved keywords, from the language reference. A reserved word can never be an unquoted name.
RESERVED = frozenset(
    """ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE CONTAINS CREATE CROSS CUBE
    CURRENT DEFAULT DEFINE DESC DISTINCT ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS EXTRACT FALSE FETCH FOLLOWING
    FOR FROM FULL GROUP GROUPING GROUPS HASH HAVING IF IGNORE IN INNER INTERSECT INTERVAL INTO IS JOIN LATERAL
    LEFT LIKE LIMIT LOOKUP MERGE NATURAL NEW NO NOT NULL NULLS OF ON OR ORDER OUTER OVER PARTITION PRECEDING
    PROTO QUALIFY RANGE RECURSIVE RESPECT RIGHT ROLLUP ROWS SELECT SET SOME STRUCT TABLESAMPLE THEN TO TREAT TRUE
    UNBOUNDED UNION UNNEST USING WHEN WHERE WINDOW WITH WITHIN""".split()
)
# Keywords, type names and date parts of statements sqlglot keeps as opaque commands or raw text (scripts, DDL, DCL):
# where no tree can confirm that a word is syntax rather than a name, a case change is accepted only for these, and never right after a word that
# introduces a case-sensitive name (a table, view, routine, model, schema or dataset).
SCRIPT_KEYWORDS = frozenset(
    """DECLARE BEGIN EXCEPTION REPEAT UNTIL WHILE DO LOOP LEAVE ITERATE BREAK CONTINUE RAISE RETURN ELSEIF EXECUTE
    IMMEDIATE ALTER DROP TRUNCATE LOAD EXPORT DATA GRANT REVOKE TEMP TEMPORARY VIEW MATERIALIZED EXTERNAL SNAPSHOT
    SCHEMA TABLE FUNCTION PROCEDURE MODEL OPTIONS CALL COMMIT ROLLBACK TRANSACTION ADD COLUMN RENAME CLONE COPY
    OVERWRITE FILES FORMAT URIS CONNECTION REMOTE LANGUAGE RETURNS DETERMINISTIC AGGREGATE UNDROP PRIMARY KEY FOREIGN
    REFERENCES CONSTRAINT ENFORCED POLICY ACCESS INDEX SEARCH VECTOR ASSIGNMENT RESERVATION CAPACITY ORGANIZATION
    PROJECT MESSAGE ERROR REPLACE OUT INOUT DEFAULT COLLATE DATASET USER ROLE ROW
    INT64 INT SMALLINT INTEGER BIGINT TINYINT BYTEINT NUMERIC DECIMAL BIGNUMERIC BIGDECIMAL FLOAT64 BOOL BOOLEAN
    STRING BYTES DATE DATETIME TIME TIMESTAMP GEOGRAPHY JSON
    NANOSECOND MICROSECOND MILLISECOND SECOND MINUTE HOUR DAY WEEK ISOWEEK MONTH QUARTER YEAR ISOYEAR DAYOFWEEK DAYOFYEAR
    EXTEND PIVOT UNPIVOT TABLESAMPLE INSERT UPDATE DELETE MATCHED SOURCE TARGET VALUES""".split()
)
_GUARDED = frozenset(
    """FROM JOIN INTO TABLE VIEW FUNCTION PROCEDURE MODEL SCHEMA DATASET UPDATE MERGE EXISTS CALL INDEX POLICY ON
    TRUNCATE DELETE CLONE COPY LIKE REFERENCES""".split()
)
# Words after which a name is a table, view, routine or other object whose name is case sensitive.
NAME_INTRODUCERS = frozenset(
    """FROM JOIN INTO TABLE VIEW FUNCTION PROCEDURE MODEL SCHEMA DATASET UPDATE MERGE USING EXISTS CALL SNAPSHOT
    INDEX POLICY ON TRUNCATE DELETE""".split()
)
_FROM_LIST = frozenset({"FROM", "JOIN"})
_CLAUSES = frozenset(
    """SELECT FROM WHERE GROUP HAVING QUALIFY WINDOW ORDER LIMIT JOIN ON USING UNION INTERSECT EXCEPT SET VALUES
    OFFSET""".split()
)
_OPERATORS = ("<=>", "||", "|>", "<=", ">=", "!=", "<>", "<<", ">>", "=>", "->")
_NUMBER = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_STRING_PREFIX = re.compile(r"(?i)(?:rb|br|r|b)?('''|\"\"\"|'|\")")
_HINT_COMMENT = re.compile(r"/\*\+|\$\{|@\{|\{\{|\{%")
_MAX_TOKENS = 200_000


class _Rejected(ValueError):
    pass


@dataclass(frozen=True)
class _Tok:
    kind: str  # word, quoted, string, number, param, hint, template, op
    text: str
    start: int
    end: int  # exclusive


def _scan(sql: str) -> tuple[list[_Tok], list[tuple[str, str]]]:
    """Every token and every comment of ``sql``; raises ``_Rejected`` when anything is left open."""

    if "{{" in sql or "{%" in sql:
        raise _Rejected("Jinja template syntax is not read by this check")
    tokens: list[_Tok] = []
    comments: list[tuple[str, str]] = []
    pos, size = 0, len(sql)
    while pos < size:
        ch = sql[pos]
        if ch.isspace():
            pos += 1
            continue
        if sql.startswith("--", pos) or ch == "#":
            end = sql.find("\n", pos)
            end = size if end < 0 else end
            comments.append(("line", sql[pos:end]))
            pos = end
            continue
        if sql.startswith("/*", pos):
            end = sql.find("*/", pos + 2)
            if end < 0:
                raise _Rejected("an unterminated block comment")
            comments.append(("block", sql[pos : end + 2]))
            pos = end + 2
            continue
        start = pos
        match = _STRING_PREFIX.match(sql, pos)
        if match:
            pos = _scan_string(sql, match.start(1), match.group(1))
            tokens.append(_Tok("string", sql[start:pos], start, pos))
        elif ch == "`":
            pos = _scan_quoted(sql, pos)
            tokens.append(_Tok("quoted", sql[start:pos], start, pos))
        elif sql.startswith("@{", pos):
            end = sql.find("}", pos)
            if end < 0:
                raise _Rejected("an unterminated @{ hint")
            pos = end + 1
            tokens.append(_Tok("hint", sql[start:pos], start, pos))
        elif sql.startswith("${", pos):
            pos = _scan_template(sql, pos)
            tokens.append(_Tok("template", sql[start:pos], start, pos))
        elif ch == "@":
            pos += 2 if sql.startswith("@@", pos) else 1
            while pos < size and (sql[pos].isalnum() or sql[pos] == "_"):
                pos += 1
            kind = "param" if pos > start + (2 if sql.startswith("@@", start) else 1) else "op"
            tokens.append(_Tok(kind, sql[start:pos], start, pos))
        elif ch.isalpha() or ch == "_":
            while pos < size and (sql[pos].isalnum() or sql[pos] == "_"):
                pos += 1
            tokens.append(_Tok("word", sql[start:pos], start, pos))
        elif ch.isdigit() or (ch == "." and pos + 1 < size and sql[pos + 1].isdigit() and not _word_before(sql, pos)):
            number = _NUMBER.match(sql, pos)
            pos = number.end() if number else pos + 1
            tokens.append(_Tok("number", sql[start:pos], start, pos))
        else:
            operator = next((op for op in _OPERATORS if sql.startswith(op, pos)), ch)
            pos += len(operator)
            tokens.append(_Tok("op", operator, start, pos))
        if len(tokens) > _MAX_TOKENS:
            raise _Rejected("the text is too long for this check")
    return tokens, comments


def _word_before(sql: str, pos: int) -> bool:
    """Whether the character before ``pos`` ends a word, a number, a quoted name or a closing bracket."""

    return pos > 0 and (sql[pos - 1].isalnum() or sql[pos - 1] in "_`)]")


def _scan_string(sql: str, pos: int, quote: str) -> int:
    """The end of the string whose opening quote is at ``pos``; backslashes escape the next character in every kind."""

    i, triple = pos + len(quote), len(quote) == 3
    while i < len(sql):
        if sql[i] == "\\":
            i += 2
            continue
        if sql.startswith(quote, i):
            return i + len(quote)
        if sql[i] == "\n" and not triple:
            raise _Rejected("a line break inside a single-quoted string")
        i += 1
    raise _Rejected("an unterminated string literal")


def _scan_quoted(sql: str, pos: int) -> int:
    i = pos + 1
    while i < len(sql):
        if sql[i] == "\\":
            i += 2
            continue
        if sql[i] == "`":
            return i + 1
        i += 1
    raise _Rejected("an unterminated quoted name")


def _scan_template(sql: str, pos: int) -> int:
    """The end of a ``${...}`` expression: balanced braces, with quoted strings skipped."""

    depth, i = 0, pos + 1
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"`":
            i = _scan_string(sql, i, ch) if ch != "`" else _scan_quoted(sql, i)
            continue
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return i
    raise _Rejected("an unterminated ${ template")


def _upper(token: _Tok) -> str:
    return token.text.upper() if token.kind == "word" else ""


def _dotted(tokens: list[_Tok], index: int) -> bool:
    return (index > 0 and tokens[index - 1].text == ".") or (index + 1 < len(tokens) and tokens[index + 1].text == ".")


def _created_functions(tokens: list[_Tok]) -> set[str]:
    """Upper-cased name parts of every function the text creates (after ``FUNCTION``, quoted or not)."""

    created: set[str] = set()
    for index, token in enumerate(tokens):
        if _upper(token) != "FUNCTION":
            continue
        position = index + 1
        if [_upper(t) for t in tokens[position : position + 3]] == ["IF", "NOT", "EXISTS"]:
            position += 3
        elif [_upper(t) for t in tokens[position : position + 2]] == ["IF", "EXISTS"]:
            position += 2
        while position < len(tokens) and tokens[position].text not in ("(", ")", ";"):
            part = tokens[position]
            if part.kind == "word":
                created.add(part.text.upper())
            elif part.kind == "quoted":
                created.update(piece.upper() for piece in part.text.strip("`").split("."))
            position += 1
    return created


def _builtin_names() -> frozenset[str]:
    parser = BigQuery.Parser
    return frozenset(
        name.upper() for name in (*parser.FUNCTIONS, *parser.FUNCTION_PARSERS, *parser.NO_PAREN_FUNCTIONS) if isinstance(name, str)
    )


_BUILTINS = _builtin_names()


def _from_list_calls(tokens: list[_Tok]) -> set[int]:
    """Indexes of calls read in a FROM list after a comma (``FROM a, my_tvf(x)``): table functions, case sensitive."""

    found: set[int] = set()
    last: list[str] = [""]
    for index, token in enumerate(tokens):
        if token.text == "(":
            last.append("")
        elif token.text == ")":
            if len(last) > 1:
                last.pop()
        elif token.kind == "word":
            word = token.text.upper()
            if word in _CLAUSES and not _dotted(tokens, index):
                last[-1] = word
            if (
                last[-1] in _FROM_LIST and index > 0 and tokens[index - 1].text == ","
                and index + 1 < len(tokens) and tokens[index + 1].text == "("
            ):
                found.add(index)
    return found


def _classify(tokens: list[_Tok], index: int, created: set[str], table_calls: set[int]) -> str | None:
    """How a case change of this word is accepted, or ``None`` when it never is.

    ``reserved`` and ``builtin`` stand on the language (a reserved word is never a name, a built-in function name
    is case-insensitive). ``keyword`` is any other word: it is accepted only when both texts parse to identical
    trees, because sqlglot's trees keep the case of every table, column, alias, field and unknown function name, so
    a word that is one of those changes the tree when its case changes.
    """

    token = tokens[index]
    word = token.text.upper()
    if _dotted(tokens, index) or word in created:
        return None
    if word in RESERVED:
        return "reserved"
    nxt = tokens[index + 1].text if index + 1 < len(tokens) else ""
    if nxt == "(" and word in _BUILTINS:
        if index in table_calls or (index and _upper(tokens[index - 1]) in NAME_INTRODUCERS):
            return None
        return "builtin"
    before = tokens[index - 1].text if index else ""
    if nxt == "=>" or (nxt.upper() == "AS" and before in ("(", ",")):
        # a named argument or a local variable (WITH(a AS 1, a + 1)): sqlglot's tree does not keep their case
        return None
    return "keyword"


def _touching(tokens: list[_Tok], index: int) -> tuple[bool, bool]:
    token = tokens[index]
    before = index > 0 and tokens[index - 1].end == token.start
    after = index + 1 < len(tokens) and token.end == tokens[index + 1].start
    return before, after


def _path_dash(tokens: list[_Tok], index: int) -> bool:
    """Whether a ``-`` is part of an unquoted dashed project name: word-like tokens touching up to a dot."""

    if tokens[index].text != "-":
        return False
    before, after = _touching(tokens, index)
    if not (before and after):
        return False
    j = index + 1
    while j < len(tokens) and (tokens[j].kind in ("word", "number") or tokens[j].text == "-"):
        if j + 1 >= len(tokens) or tokens[j].end != tokens[j + 1].start:
            return False
        if tokens[j + 1].text == ".":
            return True
        j += 1
    return False


def _normal_comment(kind: str, text: str) -> str:
    if _HINT_COMMENT.search(text):
        return text
    if kind == "line":
        return text.rstrip()
    return "\n".join(line.strip() for line in text.splitlines())


def _compare_tokens(before: str, after: str) -> tuple[list[_Tok], str, set[str], list[int], str | None]:
    """The before tokens, the after text with every language-vetted case change put back, the kinds of case change
    seen, the indexes of the tokens accepted only if a tree confirms them, and the refusal, if any.

    A reserved word or built-in call that changed case is accepted on the language's rules alone, so it is restored
    to its old spelling in the text whose tree is compared: sqlglot reads some of them as names (``DEFAULT`` in
    ``SET c = DEFAULT``, ``RANGE(`` as a call it does not know) and keeps their case in the tree.
    """

    left, left_comments = _scan(before)
    right, right_comments = _scan(after)
    if len(left) != len(right):
        return left, after, set(), [], f"the number of tokens changed from {len(left)} to {len(right)}"
    if len(left_comments) != len(right_comments):
        return left, after, set(), [], f"the number of comments changed from {len(left_comments)} to {len(right_comments)}"
    for (old_kind, old), (new_kind, new) in zip(left_comments, right_comments):
        if old_kind != new_kind or _normal_comment(old_kind, old) != _normal_comment(new_kind, new):
            return left, after, set(), [], f"a comment changed: {old.strip()[:40]!r} became {new.strip()[:40]!r}"
    created = _created_functions(left) | _created_functions(right)
    table_calls = _from_list_calls(left) | _from_list_calls(right)
    seen: set[str] = set()
    keywords: list[int] = []
    restored, position = [], 0
    for index, (a, b) in enumerate(zip(left, right)):
        if a.kind != b.kind:
            return left, after, seen, keywords, f"a token changed kind: {a.text!r} became {b.text!r}"
        if a.text != b.text:
            if a.kind != "word" or not (a.text.isascii() and b.text.isascii()) or a.text.lower() != b.text.lower():
                return left, after, seen, keywords, f"a token changed: {a.text[:40]!r} became {b.text[:40]!r}"
            why = _classify(left, index, created, table_calls)
            other = _classify(right, index, created, table_calls)
            if why is None or other is None:
                return left, after, seen, keywords, f"the case of {a.text!r} changed, which is a name, a table function, or a path part"
            seen.add(why)
            if why == "keyword":
                keywords.append(index)
            else:
                restored += [after[position : b.start], a.text]
                position = b.end
        if a.text == "." and _touching(left, index) != _touching(right, index):
            return left, after, seen, keywords, "the spacing around a dot changed"
        if _path_dash(left, index) != _path_dash(right, index) or (
            _path_dash(left, index) and _touching(left, index) != _touching(right, index)
        ):
            return left, after, seen, keywords, "the spacing inside a dashed table path changed"
        if a.kind == "string" and index and left[index - 1].kind == "string" and (
            (left[index - 1].end == a.start) != (right[index - 1].end == b.start)
        ):
            return left, after, seen, keywords, "two adjacent string literals were joined or separated"
    restored.append(after[position:])
    return left, "".join(restored), seen, keywords, None


def _parse(sql: str) -> list[exp.Expression | None] | None:
    """sqlglot's trees, or ``None`` when it cannot read the text or keeps part of it as opaque text."""

    try:
        trees = sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001 - unreadable text falls back to the token check alone
        return None
    for tree in trees:
        if tree is None:
            continue
        # a command, or a span KumoSQL's parser keeps as raw text (``__KUMO_VERBATIM__``, for example a GRAPH_TABLE
        # body): the tree holds the text with its whitespace, so it cannot confirm that only layout changed
        if tree.find(exp.Command) is not None or any(
            str(node.this).startswith("__KUMO") for node in tree.find_all(exp.Anonymous)
        ):
            return None
    return trees


def _statement_ranges(tokens: list[_Tok]) -> list[tuple[int, int]]:
    """Token index ranges (first, last inclusive) of the statements between top-level semicolons."""

    ranges, first = [], 0
    for index, token in enumerate(tokens):
        if token.kind == "op" and token.text == ";":
            if index > first:
                ranges.append((first, index - 1))
            first = index + 1
    if first < len(tokens):
        ranges.append((first, len(tokens) - 1))
    return ranges


def _folded_key(value):
    """``_key`` with the case of keyword-like ``Var`` nodes ignored (sqlglot keeps ``DELETE`` in ``THEN DELETE`` as written)."""

    if isinstance(value, exp.Var):
        return ("Var", str(value.this).lower())
    if isinstance(value, exp.Expression):
        return (type(value).__name__, tuple(
            (name, _folded_key(child)) for name, child in sorted(value.args.items())
            if child is not None and child != [] and name != "comments"
        ))
    if isinstance(value, list):
        return tuple(_folded_key(child) for child in value)
    return value


def _script_keyword(tokens: list[_Tok], index: int) -> bool:
    """Whether a word that changed case in a statement no tree confirms is one of the listed statement keywords."""

    word = tokens[index].text.upper()
    previous = _upper(tokens[index - 1]) if index else ""
    return word in SCRIPT_KEYWORDS and previous not in _GUARDED and not _dotted(tokens, index)


def _check_format(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> tuple[bool, str, int]:
    old_text, new_text = step.before_sql, step.after_sql
    left, vetted, seen, keywords, problem = _compare_tokens(old_text, new_text)
    if problem:
        return False, problem, 0
    right = _scan(vetted)[0]
    cases = len(left)
    compared = unread = 0
    for first, last in _statement_ranges(left):
        old_part = old_text[left[first].start : left[last].end]
        new_part = vetted[right[first].start : right[last].end]
        old_trees = _parse(old_part)
        in_range = [index for index in keywords if first <= index <= last]
        if old_trees is None:
            unread += 1
            for index in in_range:
                if not _script_keyword(left, index):
                    return False, (
                        f"the case of {left[index].text!r} changed in a statement sqlglot cannot read, "
                        "where it is not a listed statement keyword and no tree can confirm it"
                    ), cases
            continue
        new_trees = _parse(new_part)
        if new_trees is None:
            return False, "the formatted text can no longer be read by sqlglot", cases
        if len(old_trees) != len(new_trees):
            return False, f"the statement starting at token {first} parses differently", cases
        for old, new in zip(old_trees, new_trees):
            if (old is None) != (new is None):
                return False, f"the statement starting at token {first} parses differently", cases
            if old is None or _key(old) == _key(new):
                continue
            # The trees differ. That is only the case of keyword-like Var nodes when every word whose case changed is
            # a listed keyword: a Var is syntax (a merge action, a unit), never a table, column or function name.
            if not (in_range and _folded_key(old) == _folded_key(new) and all(_script_keyword(left, i) for i in in_range)):
                return False, f"the statement starting at token {first} parses differently", cases
        compared += 1
    trees = f"{compared} statement tree(s) identical" + (f", {unread} statement(s) sqlglot cannot read checked by tokens alone" if unread else "")
    return True, f"same {cases} tokens and comments apart from {_describe(seen)}; {trees}", cases


def _describe(seen: set[str]) -> str:
    names = {"reserved": "reserved keywords", "builtin": "built-in calls", "keyword": "other keywords"}
    return "layout" + (" and the case of " + ", ".join(names[kind] for kind in sorted(seen)) if seen else "")


def check_format_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a formatting step from the two texts in ``step`` (the trees are not read: a tree prints, and so reads, differently)."""

    if step.family != FORMAT_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != FORMAT_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the assumptions of its family")
    try:
        accepted, reason, cases = _check_format(step, before, after)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases)


__all__ = ["FORMAT_ASSUMPTIONS", "FORMAT_FAMILY", "check_format_transition"]
