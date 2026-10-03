"""GoogleSQL behaviour eval on the BigQuery Utils UDF unit tests (GoogleCloudPlatform/bigquery-utils).

Each SQL UDF in ``udfs/community`` and ``udfs/migration/<dialect>`` ships with ``generate_udf_test`` /
``generate_udaf_test`` cases (inputs and an expected output) that Google runs on BigQuery, so the
expected value is an independent GoogleSQL oracle. For every case this builds the BigQuery query that
calls the UDF body on the inputs (the body is inlined: parameters replaced by the inputs, cast to the
declared type, and calls to other SQL UDFs of the same folder inlined the same way), translates it with
KumoSQL's BigQuery to DuckDB path (:func:`kumosql.bigquery_duckdb.to_duckdb`, the translation the
execution checks use), runs it in DuckDB and compares the value with the translated expected output.

Outcomes per case:

* ``agree``: both sides run and the values are equal.
* ``WRONG``: both sides run and the values differ; a translation bug (kept as a regression).
* ``unsupported``: no verdict, counted by reason (JavaScript or Python UDF, a UDF parse error, a
  construct KumoSQL declines to translate, DuckDB cannot run the translation, ...).

The pinned source (test files and SQL UDF files, Apache-2.0) is vendored in
``tests/fixtures/bigquery_utils_udfs`` with its licence; ``--vendor <clone>`` regenerates it.
A fifth of the cases (by SHA-1 of the case id) is held out and never printed by ``--failures``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kumosql.bigquery_duckdb import UntranslatableError, to_duckdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "bigquery_utils_udfs"
MANIFEST = FIXTURE / "manifest.json"
RESULTS_NAME = "bigquery-utils-udfs"
UDF_PREFIX = "__udf__"

# Dataform test template: test inputs become columns test_input_<i> of a one-row view.


# --- JavaScript test files ---------------------------------------------------------------------

class JsError(ValueError):
    pass


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class _JsReader:
    """A reader for the JS literals in ``test_cases.js`` (strings, template literals, arrays, objects)."""

    def __init__(self, text: str):
        self.text = text
        self.i = 0

    def skip(self) -> None:
        t = self.text
        while self.i < len(t):
            if t[self.i].isspace():
                self.i += 1
            elif t.startswith("//", self.i):
                end = t.find("\n", self.i)
                self.i = len(t) if end < 0 else end
            elif t.startswith("/*", self.i):
                end = t.find("*/", self.i + 2)
                self.i = len(t) if end < 0 else end + 2
            else:
                break

    def peek(self) -> str:
        self.skip()
        return self.text[self.i] if self.i < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise JsError(f"expected {char!r} at {self.i}: {self.text[self.i:self.i + 30]!r}")
        self.i += 1

    def string(self) -> str:
        t, quote = self.text, self.text[self.i]
        self.i += 1
        out = []
        while True:
            if self.i >= len(t):
                raise JsError("unterminated string")
            c = t[self.i]
            if c == quote:
                self.i += 1
                return "".join(out)
            if c == "\\":
                n = t[self.i + 1]
                if n == "\n":
                    self.i += 2
                elif n == "x":
                    out.append(chr(int(t[self.i + 2:self.i + 4], 16)))
                    self.i += 4
                elif n == "u" and t[self.i + 2] == "{":
                    end = t.index("}", self.i)
                    out.append(chr(int(t[self.i + 3:end], 16)))
                    self.i = end + 1
                elif n == "u":
                    out.append(chr(int(t[self.i + 2:self.i + 6], 16)))
                    self.i += 6
                else:
                    out.append(_JS_ESCAPES.get(n, n))
                    self.i += 2
                continue
            if quote == "`" and t.startswith("${", self.i):
                raise JsError("template substitution")
            out.append(c)
            self.i += 1

    def value(self):
        c = self.peek()
        if c in "'\"`":
            text = self.string()
            while self.peek() == "+":  # `a` + `b`
                self.i += 1
                if self.peek() not in ("'", '"', "`"):
                    raise JsError("computed string")
                text += self.string()
            return text
        if c == "[":
            self.i += 1
            items = []
            while self.peek() != "]":
                items.append(self.value())
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return items
        if c == "{":
            self.i += 1
            obj = {}
            while self.peek() != "}":
                m = re.compile(r"[A-Za-z_$][\w$]*|'[^']*'|\"[^\"]*\"").match(self.text, self.i)
                if not m:
                    raise JsError(f"bad key at {self.i}")
                key = m.group(0).strip("'\"")
                self.i = m.end()
                self.expect(":")
                obj[key] = self.value()
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return obj
        m = re.compile(r"-?\d+(\.\d+)?|true|false|null").match(self.text, self.i)
        if m:
            self.i = m.end()
            word = m.group(0)
            return {"true": True, "false": False, "null": None}.get(word, word)
        raise JsError(f"unexpected {self.text[self.i:self.i + 30]!r}")


@dataclass
class TestGroup:
    folder: str          # "community" or "migration/<dialect>"
    udf: str
    kind: str            # "udf" or "udaf"
    index: int           # position of the call in its file
    cases: list = field(default_factory=list)   # udf: [{"inputs": [...], "expected_output": ...}]
    udaf: dict | None = None                     # udaf: {"input_columns", "input_rows", "expected_output"}
    error: str = ""


def parse_test_file(text: str, folder: str) -> list[TestGroup]:
    groups = []
    for index, m in enumerate(re.finditer(r"\bgenerate_(udf|udaf)_test\s*\(", text)):
        reader = _JsReader(text)
        reader.i = m.end()
        group = TestGroup(folder, "", m.group(1), index)
        try:
            group.udf = reader.value()
            reader.expect(",")
            payload = reader.value()
            if group.kind == "udf":
                group.cases = payload
            else:
                group.udaf = payload
        except (JsError, IndexError, ValueError) as error:
            name = re.match(r"\s*[\"'`]([^\"'`]*)", text[m.end():])
            group.udf = group.udf or (name.group(1) if name else "?")
            group.error = f"test case not a plain literal ({error})"
        groups.append(group)
    return groups


# --- UDF definitions ---------------------------------------------------------------------------

@dataclass
class Udf:
    folder: str
    name: str
    language: str                   # "sql", "js", "python", "remote"
    aggregate: bool = False
    params: list = field(default_factory=list)   # [(name, type text or None for ANY TYPE, not_aggregate)]
    returns: str | None = None
    body: str = ""
    error: str = ""


def _scan_to_close(text: str, start: int) -> int:
    """Index of the parenthesis closing the one at ``start`` (strings and comments skipped)."""

    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if text.startswith("--", i) or c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if text.startswith("/*", i):
            i = text.find("*/", i) + 2
            continue
        if text.startswith(('r"""', "r'''", '"""', "'''"), i):
            start = i + 1 if text[i] == "r" else i
            i = text.find(text[start:start + 3], start + 3) + 3
            continue
        if c in "'\"`":
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    for c in text:
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def parse_udf(text: str, folder: str, name: str) -> Udf:
    lang = re.search(r"\bLANGUAGE\s+(js|python)\b", text, re.I)
    if lang:
        return Udf(folder, name, lang.group(1).lower())
    if re.search(r"\bREMOTE\s+WITH\s+CONNECTION\b", text, re.I):
        return Udf(folder, name, "remote")
    udf = Udf(folder, name, "sql")
    text = text.replace("${self()}", name)
    text = re.sub(r"\$\{ref\(\s*[\"']([\w]+)[\"']\s*\)\}", lambda m: UDF_PREFIX + m.group(1), text)
    head = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(AGGREGATE\s+)?FUNCTION\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                     + re.escape(name) + r"\s*\(", text, re.I)
    if not head:
        udf.error = "no CREATE FUNCTION"
        return udf
    if "${" in text:
        udf.error = "Dataform template in the body"
        return udf
    udf.aggregate = bool(head.group(1))
    open_ = head.end() - 1
    close = _scan_to_close(text, open_)
    for part in _split_top(text[open_ + 1:close]):
        words = part.strip()
        not_agg = bool(re.search(r"\bNOT\s+AGGREGATE\s*$", words, re.I))
        words = re.sub(r"\s+NOT\s+AGGREGATE\s*$", "", words, flags=re.I)
        pname, _, ptype = words.partition(" ")
        ptype = ptype.strip()
        udf.params.append((pname.strip("`"), None if re.fullmatch(r"ANY\s+TYPE", ptype, re.I) else ptype, not_agg))
    rest = text[close + 1:]
    # The body: the parenthesis after the first top-level AS (OPTIONS(...) and RETURNS come before it).
    i, outside = 0, []
    while i < len(rest):
        if rest[i] == "(":
            i = _scan_to_close(rest, i) + 1
            outside.append(" () ")
            continue
        m = re.compile(r"\bAS\s*\(", re.I).match(rest, i)
        if m and (i == 0 or not (rest[i - 1].isalnum() or rest[i - 1] == "_")):
            returns = re.search(r"\bRETURNS\s+(.+?)\s*(?:\bOPTIONS\b|$)", "".join(outside), re.I | re.S)
            udf.returns = returns.group(1).strip() if returns else None
            start = m.end() - 1
            udf.body = rest[start + 1:_scan_to_close(rest, start)].strip()
            return udf
        outside.append(rest[i])
        i += 1
    udf.error = "no AS (...) body"
    return udf


def load_udfs(root: Path = FIXTURE) -> dict[tuple[str, str], Udf]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    udfs = {}
    for entry in manifest["udfs"]:
        folder, name = entry["folder"], entry["name"]
        if entry["language"] == "sql":
            text = (root / "udfs" / folder / f"{name}.sqlx").read_text(encoding="utf-8")
            udfs[folder, name] = parse_udf(text, folder, name)
        else:
            udfs[folder, name] = Udf(folder, name, entry["language"])
    return udfs


def load_groups(root: Path = FIXTURE) -> list[TestGroup]:
    groups = []
    for path in sorted((root / "udfs").rglob("test_cases.js")):
        folder = path.parent.relative_to(root / "udfs").as_posix()
        groups.extend(parse_test_file(path.read_text(encoding="utf-8"), folder))
    return groups


# --- building the BigQuery query ---------------------------------------------------------------

class Unsupported(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _parse_expr(sql: str) -> exp.Expression:
    try:
        select = sqlglot.parse_one(f"SELECT ({sql})", read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        raise Unsupported("does not parse", str(error)[:120]) from error
    return select.selects[0].unalias().this if isinstance(select.selects[0].unalias(), exp.Paren) else select.selects[0]


def _parse_type(text: str) -> exp.DataType:
    try:
        return exp.DataType.build(text, dialect="bigquery")
    except Exception as error:  # noqa: BLE001
        raise Unsupported("does not parse", f"type {text}: {error}") from error


def _outputs(query: exp.Expression) -> set[str]:
    query = query.unnest() if isinstance(query, exp.Subquery) else query
    if isinstance(query, exp.SetOperation):
        query = query.left
    return {s.alias_or_name.lower() for s in getattr(query, "selects", [])}


def _cte_outputs(node: exp.Expression, name: str) -> set[str]:
    while node is not None:
        with_ = node.args.get("with_") if isinstance(node, exp.Query) else None
        for cte in with_.expressions if with_ else []:
            if cte.alias.lower() == name:
                return _outputs(cte.this)
        node = node.parent
    return set()


def _visible_names(select: exp.Select) -> set[str]:
    """Names a column reference in ``select`` can resolve to before reaching a UDF parameter."""

    names = set()
    sources = [select.args["from_"].this] if select.args.get("from_") else []
    sources += [join.this for join in select.args.get("joins") or []]
    for source in sources:
        alias = source.args.get("alias")
        if alias is not None and alias.name:
            names.add(alias.name.lower())
        if isinstance(source, exp.Unnest):
            names.update(c.name.lower() for c in ((alias.args.get("columns") or []) if alias is not None else []))
            if isinstance(source.args.get("offset"), exp.Expression):
                names.add(source.args["offset"].name.lower())
        elif isinstance(source, exp.Table):
            names.add(source.name.lower())
            names |= _cte_outputs(select, source.name.lower())
        elif isinstance(source, exp.Subquery):
            names |= _outputs(source)
    return names


def _shadowed(column: exp.Column, name: str) -> bool:
    """Whether a FROM item in scope at ``column`` also offers ``name`` (then the parameter may not be meant)."""

    child, node = column, column.parent
    while node is not None:
        if isinstance(node, exp.Select):
            # A CTE definition or a derived table in FROM does not see the FROM items of this SELECT.
            hidden = child is node.args.get("with_") or (
                isinstance(child, (exp.From, exp.Join)) and isinstance(child.this, exp.Subquery))
            if not hidden and name in _visible_names(node):
                return True
        child, node = node, node.parent
    return False


def _called_udfs(udfs, folder: str, name: str, seen=None) -> list[Udf]:
    seen = set() if seen is None else seen
    out = []
    udf = udfs.get((folder, name))
    if udf is None or name in seen:
        return out
    seen.add(name)
    for ref in re.findall(UDF_PREFIX + r"(\w+)", udf.body):
        if (folder, ref) in udfs:
            out.append(udfs[folder, ref])
            out.extend(_called_udfs(udfs, folder, ref, seen))
    return out


def inline_call(udfs, folder: str, name: str, args: list[exp.Expression], depth: int = 0) -> exp.Expression:
    """The expression a call of SQL UDF ``name`` on ``args`` stands for."""

    udf = udfs.get((folder, name))
    if udf is None:
        raise Unsupported("UDF not found", name)
    if udf.language != "sql":
        raise Unsupported(f"{udf.language} UDF" if depth == 0 else f"calls a {udf.language} UDF", name)
    if udf.error:
        raise Unsupported("UDF definition not read", f"{name}: {udf.error}")
    if depth > 8:
        raise Unsupported("UDF nesting too deep", name)
    if len(args) != len(udf.params):
        raise Unsupported("argument count", name)
    for called in _called_udfs(udfs, folder, name):
        if called.language != "sql":
            raise Unsupported(f"calls a {called.language} UDF", called.name)
    body = _parse_expr(udf.body)
    params = {}
    for (pname, ptype, _), arg in zip(udf.params, args):
        value = arg.copy()
        if ptype is not None:
            value = exp.Cast(this=value, to=_parse_type(ptype))
        params[pname.lower()] = value

    def substitute(node):
        if not isinstance(node, exp.Column) or node.args.get("db"):
            return node
        key = (node.table or node.name).lower()
        if key not in params:
            return node
        if _shadowed(node, key):
            raise Unsupported("parameter shadowed in the body", f"{name}: {key}")
        if not node.table:
            return exp.Paren(this=params[key].copy())
        # p.field on a STRUCT parameter
        return exp.Dot(this=exp.Paren(this=params[key].copy()), expression=node.this.copy())

    body = body.transform(substitute)

    def nested(node):
        if isinstance(node, exp.Anonymous) and str(node.this).startswith(UDF_PREFIX):
            return exp.Paren(this=inline_call(udfs, folder, str(node.this)[len(UDF_PREFIX):], list(node.expressions), depth + 1))
        return node

    body = body.transform(nested)
    if udf.returns:
        body = exp.Cast(this=exp.Paren(this=body), to=_parse_type(udf.returns))
    return body


@dataclass
class Case:
    id: str
    folder: str
    udf: str
    bigquery: str = ""
    expected: str = ""
    unsupported: tuple[str, str] | None = None


def build_cases(udfs=None, groups=None) -> list[Case]:
    udfs = load_udfs() if udfs is None else udfs
    groups = load_groups() if groups is None else groups
    cases = []
    for group in groups:
        stem = f"{group.folder}/{group.udf}#{group.index}"
        if group.error:
            cases.append(Case(stem, group.folder, group.udf, unsupported=("test case not a plain literal", group.error)))
            continue
        if group.kind == "udf":
            for j, test in enumerate(group.cases):
                case = Case(f"{stem}.{j}", group.folder, group.udf, expected=f"SELECT {test['expected_output']} AS udf_output")
                try:
                    inputs = [_parse_expr(str(v)) for v in test["inputs"]]
                    # The Dataform test passes the inputs as columns of a one-row view; the inputs are
                    # constant expressions, so they are substituted for the parameters directly.
                    call = inline_call(udfs, group.folder, group.udf, inputs)
                    case.bigquery = f"SELECT {call.sql('bigquery')} AS udf_output"
                except Unsupported as error:
                    case.unsupported = (error.reason, error.detail)
                cases.append(case)
        else:
            test = group.udaf
            case = Case(stem, group.folder, group.udf, expected=f"SELECT {test['expected_output']} AS udf_output")
            try:
                args, columns = [], []
                for k, col in enumerate(test["input_columns"]):
                    if " NOT AGGREGATE" in col:
                        args.append(_parse_expr(col.split(" NOT AGGREGATE")[0]))
                    else:
                        _parse_expr(col)
                        columns.append(f"{col} AS test_input_{k}")
                        args.append(exp.column(f"test_input_{k}"))
                call = inline_call(udfs, group.folder, group.udf, args)
                case.bigquery = (f"SELECT {call.sql('bigquery')} AS udf_output FROM "
                                 f"(SELECT {', '.join(columns)} FROM ({test['input_rows']}))")
            except Unsupported as error:
                case.unsupported = (error.reason, error.detail)
            cases.append(case)
    return cases


def held_out(case_id: str) -> bool:
    return int(hashlib.sha1(case_id.encode("utf-8")).hexdigest(), 16) % 5 == 0


# --- running and comparing ---------------------------------------------------------------------

_ANONYMOUS_FIELD = re.compile(r"_\d+|v\d+|_field_\d+")


def same(actual, expected, json_text: bool = False, unordered: bool = False) -> bool:
    """Whether two fetched values are the same GoogleSQL value.

    Numbers compare across INT64/FLOAT64/NUMERIC with a relative tolerance of 1e-9; STRUCT fields by position,
    and by name unless a name is an anonymous placeholder; JSON (``json_text``) as parsed JSON. With
    ``unordered`` arrays compare as multisets and strings as multisets of characters (a necessary condition
    only): used to tell an order GoogleSQL leaves unspecified from a wrong value.
    """

    a, e = actual, expected
    if a is None or e is None:
        return a is None and e is None
    if isinstance(a, bool) or isinstance(e, bool):
        return isinstance(a, bool) and isinstance(e, bool) and a == e
    if isinstance(a, (int, float, decimal.Decimal)) and isinstance(e, (int, float, decimal.Decimal)):
        if isinstance(a, (int, decimal.Decimal)) and isinstance(e, (int, decimal.Decimal)):
            return a == e
        fa, fe = float(a), float(e)
        if math.isnan(fa) or math.isnan(fe):
            return math.isnan(fa) and math.isnan(fe)
        return fa == fe or math.isclose(fa, fe, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, str) and isinstance(e, str):
        if a == e:
            return True
        if json_text:
            try:
                return json.loads(a) == json.loads(e)
            except ValueError:
                pass
        return unordered and sorted(a) == sorted(e)
    if isinstance(a, (list, tuple)) and isinstance(e, (list, tuple)):
        if len(a) != len(e):
            return False
        if not unordered:
            return all(same(x, y, json_text) for x, y in zip(a, e))
        rest = list(e)
        for x in a:
            match = next((i for i, y in enumerate(rest) if same(x, y, json_text, True)), None)
            if match is None:
                return False
            rest.pop(match)
        return True
    if isinstance(a, dict) and isinstance(e, dict):
        if len(a) != len(e):
            return False
        for (ka, va), (ke, ve) in zip(a.items(), e.items()):
            if ka.lower() != ke.lower() and not (_ANONYMOUS_FIELD.fullmatch(ka) or _ANONYMOUS_FIELD.fullmatch(ke)):
                return False
            if not same(va, ve, json_text, unordered):
                return False
        return True
    if isinstance(a, (bytes, bytearray, memoryview)) and isinstance(e, (bytes, bytearray, memoryview)):
        return bytes(a) == bytes(e)
    if isinstance(a, uuid.UUID) or isinstance(e, uuid.UUID):
        return str(a) == str(e)
    return type(a) is type(e) and a == e


def same_rows(actual, expected, json_text=False, unordered=False) -> bool:
    return len(actual) == len(expected) and all(same(list(x), list(y), json_text, unordered) for x, y in zip(actual, expected))


def order_unspecified(bigquery_sql: str) -> bool:
    """Whether the query builds an array or string in an order GoogleSQL leaves unspecified."""

    try:
        tree = sqlglot.parse_one(bigquery_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return False
    for node in tree.find_all(exp.ArrayAgg, exp.GroupConcat, exp.ArrayConcatAgg):
        if not isinstance(node.this, exp.Order):
            return True
    for node in tree.find_all(exp.Array):
        query = node.expressions[0] if len(node.expressions) == 1 else None
        if isinstance(query, exp.Subquery):
            query = query.this
        if isinstance(query, exp.Query) and not query.args.get("order"):
            return True
    return False


def _fetchable(duck_sql: str, types: list[str]) -> str | None:
    """The query with TIMESTAMP WITH TIME ZONE values read as UTC TIMESTAMP (fetching them needs pytz)."""

    if not any("TIME ZONE" in t for t in types):
        return None
    casts = ", ".join(f'CAST(#{i + 1} AS {t.replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")})' for i, t in enumerate(types))
    return f"SELECT {casts} FROM ({duck_sql})"


def run(con, duck_sql: str):
    con.execute(duck_sql)
    types = [str(d[1]) for d in con.description]
    wrapped = _fetchable(duck_sql, types)
    rows = con.execute(wrapped).fetchall() if wrapped else con.fetchall()
    # BigQuery returns a NULL array in a query result as an empty array.
    rows = [tuple(() if v is None and t.endswith("]") else v for v, t in zip(row, types)) for row in rows]
    return rows, types


def _rerun_unoptimized(con, duck_sql: str):
    """Rows with DuckDB's optimizer off, as :func:`kumosql.duckdb_load.run_unoptimized` (with this file's fetch)."""

    con.execute("PRAGMA disable_optimizer")
    try:
        return run(con, duck_sql)[0]
    finally:
        con.execute("PRAGMA enable_optimizer")


def connect():
    """A DuckDB session like a BigQuery one: UTC session time zone; one thread for repeatable results."""

    import duckdb

    con = duckdb.connect()
    con.execute("SET threads TO 1")
    con.execute("SET TimeZone = 'UTC'")
    return con


def evaluate(case: Case, con=None, fix: bool = True) -> dict:
    import duckdb

    out = {"id": case.id, "udf": f"{case.folder}/{case.udf}", "held_out": held_out(case.id)}
    if case.unsupported:
        out.update(outcome="unsupported", reason=case.unsupported[0], detail=case.unsupported[1])
        return out
    own = con is None
    if own:
        con = connect()
    try:
        try:
            actual_sql = to_duckdb(case.bigquery, fix=fix)
        except UntranslatableError as error:
            out.update(outcome="unsupported", reason="KumoSQL declines to translate", detail=str(error))
            return out
        except sqlglot.errors.SqlglotError as error:
            out.update(outcome="unsupported", reason="does not parse", detail=str(error)[:120])
            return out
        try:
            expected_sql = to_duckdb(case.expected, fix=fix)
        except (UntranslatableError, sqlglot.errors.SqlglotError) as error:
            out.update(outcome="unsupported", reason="expected output not translatable", detail=str(error)[:120])
            return out
        out["duckdb"] = actual_sql
        try:
            expected, etypes = run(con, expected_sql)
        except duckdb.Error as error:
            out.update(outcome="unsupported", reason="expected output not executable in DuckDB", detail=str(error)[:160])
            return out
        try:
            actual, atypes = run(con, actual_sql)
        except duckdb.Error as error:
            out.update(outcome="unsupported", reason="translation not executable in DuckDB", detail=str(error)[:160])
            return out
        json_text = "JSON" in " ".join(atypes + etypes)
        agree = same_rows(actual, expected, json_text)
        if not agree:
            # A difference counts only if DuckDB agrees with its optimizer off (DuckDB 1.5 optimizer bugs).
            if same_rows(_rerun_unoptimized(con, actual_sql), _rerun_unoptimized(con, expected_sql), json_text):
                out.update(outcome="unsupported", reason="DuckDB optimizer disagrees with itself")
                return out
            if order_unspecified(case.bigquery) and same_rows(actual, expected, json_text, unordered=True):
                out.update(outcome="unsupported", reason="element order not specified by GoogleSQL",
                           actual=repr(actual)[:300], expected=repr(expected)[:300])
                return out
        out["actual"] = repr(actual)[:300]
        out["expected"] = repr(expected)[:300]
        out["outcome"] = "agree" if agree else "WRONG"
        return out
    finally:
        if own:
            con.close()


def run_all(cases=None, fix: bool = True) -> list[dict]:
    import duckdb

    cases = build_cases() if cases is None else cases
    results = []
    for case in cases:
        con = connect()
        try:
            results.append(evaluate(case, con, fix))
        finally:
            con.close()
    return results


def summarize(results: list[dict], udfs=None) -> dict:
    def part(rows):
        c = Counter(r["outcome"] for r in rows)
        supported = c["agree"] + c["WRONG"]
        return {"cases": len(rows), "agree": c["agree"], "wrong": c["WRONG"], "supported": supported,
                "unsupported": c["unsupported"]}

    reasons = Counter(r["reason"] for r in results if r["outcome"] == "unsupported")
    return {
        "all": part(results),
        "dev": part([r for r in results if not r["held_out"]]),
        "held_out": part([r for r in results if r["held_out"]]),
        "unsupported_reasons": dict(reasons.most_common()),
        "declined": dict(Counter(r["detail"].split(":")[0] for r in results if r.get("reason") == "KumoSQL declines to translate").most_common()),
        "not_executable": dict(Counter(r["detail"].split(":")[0] for r in results if r.get("reason", "").endswith("not executable in DuckDB")).most_common()),
        "udfs_with_tests": len({r["udf"] for r in results}),
        "sql_udfs_with_tests": len({r["udf"] for r in results if udfs and udfs[tuple(r["udf"].rsplit("/", 1))].language == "sql"}),
    }


# --- vendoring ---------------------------------------------------------------------------------

def vendor(clone: Path) -> None:
    """Copy the pinned test files and SQL UDF files (with the licence) into the fixture."""

    commit = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    (FIXTURE / "udfs").mkdir(parents=True)
    shutil.copy(clone / "LICENSE", FIXTURE / "LICENSE")
    files, udfs = {}, []
    sources = sorted((clone / "udfs").glob("community/*.sqlx")) + sorted((clone / "udfs").glob("migration/*/*.sqlx"))
    for path in sources:
        rel = path.relative_to(clone / "udfs")
        folder = rel.parent.as_posix()
        text = path.read_text(encoding="utf-8")
        language = parse_udf(text, folder, path.stem).language
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        udfs.append({"folder": folder, "name": path.stem, "language": language, "sha256": digest})
        if language == "sql":
            target = FIXTURE / "udfs" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target)
            files[f"udfs/{rel.as_posix()}"] = digest
    for path in sorted((clone / "udfs").rglob("test_cases.js")):
        rel = path.relative_to(clone / "udfs")
        target = FIXTURE / "udfs" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, target)
        files[f"udfs/{rel.as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "source": "https://github.com/GoogleCloudPlatform/bigquery-utils",
        "commit": commit,
        "license": "Apache-2.0 (LICENSE)",
        "note": "SQL UDF files and test_cases.js copied unchanged; JavaScript, Python and remote UDFs are listed (name, language, sha256) but not copied.",
        "files": files,
        "udfs": udfs,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"vendored {len(files)} files ({sum(u['language'] == 'sql' for u in udfs)} SQL UDFs of {len(udfs)}) at {commit}")


def check_manifest(root: Path = FIXTURE) -> list[str]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    bad = []
    for rel, digest in manifest["files"].items():
        if hashlib.sha256((root / rel).read_bytes()).hexdigest() != digest:
            bad.append(rel)
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vendor", type=Path, help="bigquery-utils clone to copy the pinned files from")
    ap.add_argument("--failures", action="store_true", help="print WRONG development cases (held-out cases stay hidden)")
    ap.add_argument("--unsupported", action="store_true", help="print unsupported development cases with their reason")
    ap.add_argument("--only", help="run the cases whose id contains this text")
    ap.add_argument("--baseline", action="store_true", help="sqlglot's translation without KumoSQL's fixes")
    ap.add_argument("--write-results", action="store_true")
    args = ap.parse_args(argv)
    from bench_common import quiet

    quiet()
    if args.vendor:
        vendor(args.vendor)
        return 0
    cases = build_cases()
    if args.only:
        cases = [c for c in cases if args.only in c.id]
    t0 = time.perf_counter()
    results = run_all(cases, fix=not args.baseline)
    secs = time.perf_counter() - t0
    summary = summarize(results, load_udfs())
    print(json.dumps(summary, indent=1))
    print(f"{len(results)} cases in {secs:.1f}s")
    for r in results:
        if r["held_out"] and not args.only:
            continue
        if args.failures and r["outcome"] == "WRONG":
            print("WRONG", r["id"], "\n  duckdb:", r.get("duckdb"), "\n  actual:", r.get("actual"), "\n  expected:", r.get("expected"))
        if args.unsupported and r["outcome"] == "unsupported":
            print("unsupported", r["id"], r["reason"], "|", r.get("detail", "")[:160])
    if args.write_results:
        if args.only or args.baseline:
            ap.error("--write-results needs the full run with the fixes")
        baseline = summarize(run_all(cases, fix=False), load_udfs())
        path = write(summary, baseline, secs)
        print(f"wrote {path}")
    return 1 if summary["all"]["wrong"] else 0


def write(summary: dict, baseline: dict, secs: float) -> Path:
    from bench_common import today, write_results

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    a, h, b = summary["all"], summary["held_out"], baseline["all"]
    reasons = "; ".join(f"{k} {v}" for k, v in summary["unsupported_reasons"].items())
    row = {
        "suite": "BigQuery Utils UDF tests",
        "order": 64,
        "size": a["cases"],
        "score": f"{a['agree']}/{a['supported']} supported cases agree, {a['wrong']} wrong (of {a['cases']:,})",
        "metric": "Each unit test of a SQL UDF in GoogleCloudPlatform/bigquery-utils (inputs and the output Google checked on "
                  "BigQuery) becomes a BigQuery query calling the inlined UDF body; KumoSQL translates it to DuckDB, runs it "
                  "and compares the value with the translated expected output.",
        "evidence": "executed",
        "correctness": f"{a['wrong']} wrong values: every supported case returns the value BigQuery returned; a difference "
                       "counts only when DuckDB with its optimizer off agrees",
        "coverage": {"proven": a["agree"], "unsupported": a["unsupported"]},
        "held_out": f"{h['agree']}/{h['supported']} supported cases agree, {h['wrong']} wrong ({h['cases']} cases by SHA-1 of the id, "
                    f"{h['unsupported']} unsupported; never printed while fixing)",
        "docs": "docs/evals/bigquery-behavior-eval.md#bigquery-utils-udf-tests",
        "command": "python tools/bq_utils_udf_eval.py --write-results",
        "date": today(),
        "caveats": f"Source GoogleCloudPlatform/bigquery-utils @ {manifest['commit'][:7]} (Apache-2.0), "
                   f"{summary['udfs_with_tests']} tested UDFs ({summary['sql_udfs_with_tests']} SQL), all cases original, none adapted. "
                   f"Baseline (sqlglot's translation alone): {b['agree']}/{b['supported']} agree, {b['wrong']} wrong; the fixes "
                   "are general translation fixes in kumosql/bigquery_duckdb.py, and the cases they were found on stay as "
                   f"regressions. Unsupported: {reasons}. Inputs are substituted for the parameters (the Dataform test passes "
                   "them as columns of a one-row view, typed per test group); arrays or strings built in an order GoogleSQL "
                   "leaves unspecified are not compared when they match as multisets. DuckDB is not BigQuery: a value is "
                   "only as good as the translation's model of each function.",
        "performance": f"{a['cases']} cases in {secs:.0f} s",
    }
    return write_results(RESULTS_NAME, row)


if __name__ == "__main__":
    sys.exit(main())
