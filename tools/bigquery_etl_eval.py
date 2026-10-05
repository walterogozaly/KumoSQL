"""BigQuery behaviour eval on Mozilla bigquery-etl: UDF assertions and query tests as native expected results.

Source: https://github.com/mozilla/bigquery-etl (MPL-2.0), pinned at one commit and fetched at run time
(nothing but a small licensed sample is committed). Two kinds of expected result, both produced by Mozilla's
own CI on real BigQuery:

* **UDF assertions.** Every ``udf.sql`` ends with test statements such as
  ``SELECT assert.equals(45, norm.browser_version_info('45.9.0').major_version)``. Mozilla's CI runs each
  statement on BigQuery and it must not raise. For every ``assert.*`` call in the select list of such a
  statement this builds a BigQuery query that returns the assertion's arguments (the UDFs it calls inlined
  from the repository), translates it with KumoSQL's BigQuery to DuckDB path, runs it, and checks the
  assertion's own predicate on the returned values. Statements marked ``#xfail`` must fail on BigQuery.
* **Query tests.** ``tests/sql/<project>/<dataset>/<table>/<test>/`` holds input rows for the tables a
  ``query.sql`` reads, optional query parameters, and ``expect.yaml``. The query is rendered, its parameters
  and UDF calls are substituted, the input tables are loaded into DuckDB, and the result rows are compared
  with ``expect`` the way Mozilla's own test runner compares them.

Outcomes per case: ``agree``, ``WRONG`` (the translation ran and returned another value: a translation bug,
kept as a regression), ``unsupported`` (no verdict, counted by reason: a JavaScript UDF, a construct KumoSQL
declines to translate, DuckDB cannot run it, a table or schema the harness cannot build, ...).
A difference counts only when DuckDB with its optimizer off agrees. A fifth of the source units (by SHA-1
of the unit id: a UDF file or a test directory) is held out and never printed by ``--failures``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import decimal
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import sqlglot
import yaml
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bq_utils_udf_eval as bu  # noqa: E402  (the sibling eval: values, running, UDF inlining)
from kumosql.bigquery_duckdb import UntranslatableError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/mozilla/bigquery-etl.git"
PIN = "1e58c389e1856bb68baa87b7461db52fbdfb4d86"
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "bigquery-etl"
FIXTURE = ROOT / "tests" / "fixtures" / "bigquery_etl"
MANIFEST = FIXTURE / "sources.sha256"
SAMPLE = FIXTURE / "sample"
SAMPLE_MANIFEST = FIXTURE / "sample.sha256"
RESULTS_NAME = "bigquery-etl-tests"
ETL = "etl"  # the one "folder" of the sibling's UDF registry


# --- the source tree ---------------------------------------------------------------------------

class Tree:
    """A bigquery-etl checkout (or the committed sample of one): the files the eval reads."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def listdir(self, rel: str) -> list[str]:
        path = self.root / rel
        return sorted(p.name for p in path.iterdir()) if path.is_dir() else []

    def routine_files(self) -> list[str]:
        out = []
        for path in sorted((self.root / "sql").glob("*/*/*/udf.sql")):
            out.append(path.relative_to(self.root).as_posix())
        out += [p.relative_to(self.root).as_posix() for p in sorted((self.root / "sql").glob("*/udf_legacy/*.sql"))]
        return out

    def procedure_files(self) -> list[str]:
        return [p.relative_to(self.root).as_posix() for p in sorted((self.root / "sql").glob("*/*/*/stored_procedure.sql"))]

    def test_dirs(self) -> list[str]:
        out = []
        for path in sorted((self.root / "tests" / "sql").glob("*/*/*/*")):
            if path.is_dir() and any(p.name.startswith("expect.") for p in path.iterdir()):
                out.append(path.relative_to(self.root).as_posix())
        return out

    def used_paths(self) -> list[str]:
        """Every file the eval reads, for the SHA-256 manifest."""

        paths = ["LICENSE"]
        paths += self.routine_files() + self.procedure_files()
        seen = set()
        for test in self.test_dirs():
            for name in self.listdir(test):
                paths.append(f"{test}/{name}")
            project, dataset, table, _ = test.split("/")[2:]
            base = f"sql/{project}/{dataset}/{table}"
            if base not in seen:
                seen.add(base)
                paths += [f"{base}/{n}" for n in self.listdir(base) if n in ("query.sql", "view.sql", "script.sql", "schema.yaml")]
        return sorted(set(p for p in paths if self.exists(p)))


def checksums(tree: Tree, paths: list[str] | None = None) -> dict[str, str]:
    paths = tree.used_paths() if paths is None else paths
    return {p: hashlib.sha256((tree.root / p).read_bytes()).hexdigest() for p in paths}


def read_manifest(path: Path) -> dict[str, str]:
    return dict(line.split(maxsplit=1)[::-1] for line in path.read_text().splitlines() if line.strip())


def write_manifest(path: Path, sums: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{h}  {p}\n" for p, h in sorted(sums.items())), encoding="utf-8")


def check_sources(tree: Tree, manifest: Path = MANIFEST, subset: bool = False) -> list[str]:
    """Paths whose SHA-256 differs from the pinned manifest (all of the manifest, or ``subset`` of it present)."""

    pinned = read_manifest(manifest)
    actual = checksums(tree, [p for p in pinned if tree.exists(p)] if subset else None)
    return sorted(p for p in set(pinned) | set(actual) if pinned.get(p) != actual.get(p) and (not subset or p in actual))


def fetch(dest: Path = CACHE) -> Tree:
    """A sparse, blobless clone of mozilla/bigquery-etl at :data:`PIN` (once); the files the manifest lists."""

    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        git = lambda *a: subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        git("remote", "add", "origin", URL)
        git("sparse-checkout", "set", "--no-cone", "/LICENSE", "/sql/*/*/*/udf.sql", "/sql/*/udf_legacy/*.sql",
            "/sql/*/*/*/stored_procedure.sql", "/tests/sql/", "/sql/*/*/*/query.sql", "/sql/*/*/*/view.sql",
            "/sql/*/*/*/script.sql", "/sql/*/*/*/schema.yaml")
        git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", PIN)
        git("checkout", "-q", "FETCH_HEAD")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
    if head != PIN:
        raise RuntimeError(f"{dest} is at {head}, expected {PIN}")
    return Tree(dest)


# --- reading SQL files -------------------------------------------------------------------------

def split_statements(text: str) -> list[str]:
    """Top-level statements of a SQL file (``;`` outside strings, comments and triple quotes); comments stay in."""

    out, cur, i, n = [], [], 0, len(text)
    start = 0
    while i < n:
        c = text[i]
        if text.startswith("--", i) or c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        raw = c in "rR" and i + 1 < n and text[i + 1] in "'\""
        q = i + 1 if raw else i
        if q < n and text[q] in "'\"":
            quote = text[q]
            if text.startswith(quote * 3, q):
                j = text.find(quote * 3, q + 3)
                i = n if j < 0 else j + 3
                continue
            j = q + 1
            while j < n and text[j] != quote:
                j += 1 if raw else (2 if text[j] == "\\" else 1)
            i = j + 1
            continue
        if c == "`":
            j = text.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == ";":
            out.append(text[start:i])
            start = i + 1
        i += 1
    out.append(text[start:])
    return [s for s in out if _code(s).strip()]


def _code(statement: str) -> str:
    """The statement without its comments (strings kept)."""

    out, i, n = [], 0, len(statement)
    while i < n:
        c = statement[i]
        if statement.startswith("--", i) or c == "#":
            j = statement.find("\n", i)
            i = n if j < 0 else j
            continue
        if statement.startswith("/*", i):
            j = statement.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        raw = c in "rR" and i + 1 < n and statement[i + 1] in "'\""
        q = i + 1 if raw else i
        if q < n and statement[q] in "'\"":
            quote = statement[q]
            if statement.startswith(quote * 3, q):
                j = statement.find(quote * 3, q + 3)
                j = n if j < 0 else j + 3
            else:
                j = q + 1
                while j < n and statement[j] != quote:
                    j += 1 if raw else (2 if statement[j] == "\\" else 1)
                j += 1
            out.append(statement[i:j])
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


_DEFINITION = re.compile(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(TEMP(?:ORARY)?\s+)?(AGGREGATE\s+|TABLE\s+)?FUNCTION\s+"
                         r"(?:IF\s+NOT\s+EXISTS\s+)?(`[^`]+`|[\w.\-]+)\s*\(", re.I)
_PROCEDURE = re.compile(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\b", re.I)
ASSERT_PREFIX = "__udf__assert__"


@dataclass
class Routine:
    path: str                 # sql/<project>/<dataset>/<name>/udf.sql (or udf_legacy/<name>.sql)
    project: str
    dataset: str
    name: str
    definitions: list = field(default_factory=list)   # [Udf] (the sibling's record), keyed in the registry
    tests: list = field(default_factory=list)         # [statement text, comments kept]
    keys: dict = field(default_factory=dict)          # name as written -> registry key
    errors: list = field(default_factory=list)

    @property
    def unit(self) -> str:
        return f"{self.project}/{self.dataset}/{self.name}"


def _name_parts(token: str) -> list[str]:
    return [p for p in token.replace("`", "").split(".") if p]


def _global_key(parts: list[str]) -> str:
    return f"{parts[-2]}__{parts[-1]}"


class Registry:
    """The routines of the checkout: SQL UDF definitions (inlinable) and their test statements."""

    def __init__(self, tree: Tree):
        self.tree = tree
        self.routines: dict[str, Routine] = {}      # by path
        self.udfs: dict[tuple[str, str], bu.Udf] = {}
        self.js: set[str] = set()                    # registry keys of JavaScript or Python UDFs
        self._globals: dict[tuple[str, str], str] = {}   # (dataset, name) -> key
        self._read()

    def _read(self) -> None:
        files = self.tree.routine_files()
        parsed = []
        for path in files:
            parts = path.split("/")
            if path.endswith("/udf.sql"):
                project, dataset, name = parts[1], parts[2], parts[3]
            else:  # udf_legacy/<name>.sql
                project, dataset, name = parts[1], parts[2], parts[3][:-4]
            routine = Routine(path, project, dataset, name)
            self.routines[path] = routine
            statements = split_statements(self.tree.read(path))
            parsed.append((routine, statements))
            for statement in statements:
                m = _DEFINITION.match(_code(statement))
                if m:
                    toks = _name_parts(m.group(3))
                    if len(toks) >= 2:
                        self._globals[(toks[-2], toks[-1])] = _global_key(toks)
                        routine.keys[m.group(3)] = _global_key(toks)
                    else:
                        routine.keys[m.group(3)] = f"{dataset}__{name}__local__{toks[0]}"
        for routine, statements in parsed:
            for statement in statements:
                code = _code(statement)
                m = _DEFINITION.match(code)
                if m:
                    key = routine.keys[m.group(3)]
                    udf = self._parse_definition(code, key, m, routine)
                    self.udfs[ETL, key] = udf
                    routine.definitions.append(udf)
                    if udf.language != "sql":
                        self.js.add(key)
                elif _PROCEDURE.match(code):
                    routine.errors.append("stored procedure")
                else:
                    routine.tests.append(statement)

    def rewrite_calls(self, text: str, routine: Routine) -> str:
        """Calls of routines of the repository become ``__udf__<key>(`` (the sibling's inlining syntax)."""

        text = re.sub(r"(?<![\w.`])(?:`?(?:mozfun|moz-fx-data-shared-prod)`?\.)?`?((?:" + "|".join(
            sorted({re.escape(d) + r"`?\.`?" + re.escape(n) for d, n in self._globals}, key=len, reverse=True)) + r"))`?(?=\s*\()",
            lambda m: bu.UDF_PREFIX + re.sub(r"[`.]+", "__", m.group(1)), text)
        for written, key in routine.keys.items():
            if "." not in written.replace("`", ""):
                text = re.sub(r"(?<![\w.`])`?" + re.escape(written.strip("`")) + r"`?(?=\s*\()", bu.UDF_PREFIX + key, text)
        return text

    def _parse_definition(self, code: str, key: str, head: re.Match, routine: Routine) -> bu.Udf:
        udf = bu.Udf(ETL, key, "sql")
        tail = code[head.end() - 1:]
        lang = re.search(r"\bLANGUAGE\s+(js|python)\b", code, re.I)
        if lang:
            udf.language = lang.group(1).lower()
            return udf
        if head.group(2) and head.group(2).strip().upper() in ("AGGREGATE", "TABLE"):
            udf.language = head.group(2).strip().lower()
            return udf
        try:
            close = bu._scan_to_close(tail, 0)
            for part in bu._split_top(tail[1:close]):
                words = part.strip()
                pname, _, ptype = words.partition(" ")
                ptype = ptype.strip()
                udf.params.append((pname.strip("`"), None if re.fullmatch(r"ANY\s+TYPE", ptype, re.I) else ptype, False))
            rest = tail[close + 1:]
            i, outside = 0, []
            while i < len(rest):
                if rest[i] == "(":
                    i = bu._scan_to_close(rest, i) + 1
                    outside.append(" () ")
                    continue
                m = re.compile(r"\bAS\s*\(", re.I).match(rest, i)
                if m and (i == 0 or not (rest[i - 1].isalnum() or rest[i - 1] == "_")):
                    returns = re.search(r"\bRETURNS\s+(.+?)\s*(?:\bDETERMINISTIC\b|\bNOT\s+DETERMINISTIC\b|\bOPTIONS\b|$)",
                                        "".join(outside), re.I | re.S)
                    udf.returns = returns.group(1).strip() if returns else None
                    start = m.end() - 1
                    udf.body = self.rewrite_calls(rest[start + 1:bu._scan_to_close(rest, start)].strip(), routine)
                    return udf
                outside.append(rest[i])
                i += 1
            udf.error = "no AS (...) body"
        except ValueError as error:
            udf.error = str(error)
        return udf


# --- UDF assertion cases -----------------------------------------------------------------------

@dataclass
class Case:
    id: str
    unit: str                 # the held-out unit: a UDF file or a test directory
    kind: str                 # "assertion", "xfail" or "query"
    assertion: str = ""
    bigquery: str = ""        # the query whose result the check reads
    expected: object = None   # query tests: the expected rows
    meta: dict = field(default_factory=dict)
    unsupported: tuple[str, str] | None = None


def held_out(unit: str) -> bool:
    return bu.held_out(unit)


def _calls(tree: exp.Expression):
    return [n for n in tree.find_all(exp.Anonymous) if str(n.this).lower().startswith(ASSERT_PREFIX)]


_TABLELESS = (exp.Distinct,)


def _assertion_name(node: exp.Anonymous) -> str:
    return str(node.this).lower()[len(ASSERT_PREFIX):]


def _inline_all(registry: Registry, tree: exp.Expression) -> exp.Expression:
    """``tree`` with every call of a repository SQL UDF inlined (a JavaScript UDF or unknown routine raises)."""

    for _ in range(40):
        node = next((n for n in tree.find_all(exp.Anonymous) if str(n.this).startswith(bu.UDF_PREFIX)), None)
        if node is None:
            return tree
        key = str(node.this)[len(bu.UDF_PREFIX):]
        if key.startswith("assert__"):
            raise bu.Unsupported("assertion nested in another expression", key)
        _capture_check(registry, key, list(node.expressions))
        new = bu.inline_call(registry.udfs, ETL, key, list(node.expressions))
        new = exp.Paren(this=new)
        if node is tree:
            tree = new
        else:
            node.replace(new)
    raise bu.Unsupported("UDF nesting too deep", "")


def _capture_check(registry: Registry, key: str, args: list[exp.Expression]) -> None:
    """Refuse inlining when a column of an argument could be captured by a name the UDF body introduces."""

    udf = registry.udfs.get((ETL, key))
    if udf is None or not udf.body:
        return
    columns = {c.name.lower() for a in args for c in a.find_all(exp.Column)} | {
        c.table.lower() for a in args for c in a.find_all(exp.Column) if c.table}
    if not columns:
        return
    try:
        body = bu._parse_expr(udf.body)
    except bu.Unsupported:
        return
    names = set()
    for node in body.find_all(exp.TableAlias):
        names.add(node.name.lower())
        names.update(c.name.lower() for c in node.args.get("columns") or [])
    for node in body.find_all(exp.Table, exp.CTE):
        names.add(node.alias_or_name.lower())
    for node in body.find_all(exp.Unnest):
        offset = node.args.get("offset")
        if isinstance(offset, exp.Expression):
            names.add(offset.name.lower())
    for node in body.find_all(exp.Lambda):
        names.update(i.name.lower() for i in node.expressions)
    if columns & names:
        raise bu.Unsupported("argument column may be captured by a name in the UDF body", key)


def build_assertion_cases(registry: Registry) -> list[Case]:
    cases = []
    for path, routine in registry.routines.items():
        for si, statement in enumerate(routine.tests):
            stem = f"{routine.unit}#{si}"
            xfail = "#xfail" in statement
            code = _code(statement)
            if not re.search(r"\bassert\s*\.", code, re.I):
                cases.append(Case(f"{stem}.0", routine.unit, "assertion",
                                  unsupported=("test statement has no assertion", code.strip()[:60])))
                continue
            text = registry.rewrite_calls(code, routine)
            text = re.sub(r"(?<![\w.`])(?:`?mozfun`?\.)?`?assert`?\.`?(\w+)`?(?=\s*\()", lambda m: ASSERT_PREFIX + m.group(1), text)
            try:
                tree = sqlglot.parse_one(text, read="bigquery")
            except sqlglot.errors.SqlglotError as error:
                cases.append(Case(f"{stem}.0", routine.unit, "assertion", unsupported=("does not parse", str(error)[:100])))
                continue
            if tree is None or isinstance(tree, exp.Command) or not isinstance(tree, exp.Select):
                cases.append(Case(f"{stem}.0", routine.unit, "assertion",
                                  unsupported=("test statement is not a plain SELECT", type(tree).__name__)))
                continue
            top = [e for e in tree.expressions if isinstance(e.unalias() if isinstance(e, exp.Alias) else e, exp.Anonymous)
                   and str((e.unalias() if isinstance(e, exp.Alias) else e).this).lower().startswith(ASSERT_PREFIX)]
            top_nodes = [e.unalias() if isinstance(e, exp.Alias) else e for e in top]
            nested = [c for c in _calls(tree) if not any(c is t for t in top_nodes)]
            for ai, node in enumerate(top_nodes):
                case = Case(f"{stem}.{ai}", routine.unit, "xfail" if xfail else "assertion", assertion=_assertion_name(node))
                if xfail and len(top_nodes) + len(nested) != 1:
                    case.unsupported = ("expected failure of several assertions", "")
                elif tree.args.get("distinct") or tree.args.get("group") or tree.args.get("qualify") or tree.args.get("limit"):
                    case.unsupported = ("assertion in a SELECT with DISTINCT, GROUP BY, QUALIFY or LIMIT", "")
                else:
                    try:
                        case.bigquery = _assertion_query(registry, tree, node)
                    except bu.Unsupported as error:
                        case.unsupported = (error.reason, error.detail)
                cases.append(case)
            for ni, node in enumerate(nested):
                cases.append(Case(f"{stem}.n{ni}", routine.unit, "assertion", assertion=_assertion_name(node),
                                  unsupported=("assertion not in the SELECT list", "")))
            if not top_nodes and not nested:
                cases.append(Case(f"{stem}.0", routine.unit, "assertion", unsupported=("no assertion call read", "")))
    return cases


def _assertion_query(registry: Registry, tree: exp.Select, node: exp.Anonymous) -> str:
    """The BigQuery query returning the arguments of ``node`` for every row the statement produces."""

    work = tree.copy()
    target = None
    for e in work.expressions:
        inner = e.unalias() if isinstance(e, exp.Alias) else e
        if isinstance(inner, exp.Anonymous) and str(inner.this).lower().startswith(ASSERT_PREFIX) and inner.sql() == node.sql():
            target = inner
            break
    if target is None:
        raise bu.Unsupported("assertion not found", "")
    name = _assertion_name(target)
    if name not in ARITY:
        raise bu.Unsupported("assertion kind not read", name)
    if len(target.expressions) != ARITY[name]:
        raise bu.Unsupported("assertion arguments", name)
    work.set("expressions", [exp.alias_(a.copy(), f"_a{i}") for i, a in enumerate(target.expressions)])
    for other in list(_calls(work)):
        raise bu.Unsupported("assertion nested in another expression", _assertion_name(other))
    work = _inline_all(registry, work)
    return work.sql(dialect="bigquery")


ARITY = {"equals": 2, "array_equals": 2, "array_equals_any_order": 2, "struct_equals": 2, "json_equals": 2,
         "map_equals": 2, "map_entries_equals": 2, "histogram_equals": 2, "approx_equals": 3, "sql_equals": 2,
         "null": 1, "not_null": 1, "true": 1, "false": 1, "array_empty": 1, "all_fields_null": 1}


# --- the assertions' predicates ----------------------------------------------------------------

def _pos(value):
    """STRUCT values (dicts) as tuples: BigQuery compares structs by position."""

    if isinstance(value, dict):
        return tuple(_pos(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return [_pos(v) for v in value]
    return value


def _eq(a, e, unordered=False) -> bool:
    return bu.same(_pos(a), _pos(e), True, unordered)


def _entries(value):
    if value is None:
        return {}
    return {(_freeze(entry["key"] if isinstance(entry, dict) else entry[0])): (entry["value"] if isinstance(entry, dict) else entry[1])
            for entry in value}


def _freeze(v):
    return tuple(_freeze(x) for x in v) if isinstance(v, (list, tuple)) else (tuple(v.items()) if isinstance(v, dict) else v)


def _map_equal(expected, actual) -> bool:
    e, a = _entries(expected), _entries(actual)
    for key in set(e) | set(a):
        x, y = e.get(key), a.get(key)
        if (x is None) != (y is None) or (x is not None and not _eq(y, x)):
            return False
    return True


def check(kind: str, v: list, unordered: bool = False) -> bool:
    """Whether the assertion ``kind`` holds for the argument values ``v`` (the predicate of mozfun's ``assert.*``)."""

    if kind == "null":
        return v[0] is None
    if kind == "not_null":
        return v[0] is not None
    if kind == "true":
        return v[0] is True
    if kind == "false":
        return v[0] is False
    if kind in ("equals", "struct_equals"):
        return _eq(v[1], v[0], unordered)
    if kind == "json_equals":
        return bu.same(_pos(v[1]), _pos(v[0]), True, unordered)
    if kind == "array_equals":
        a, e = v[1] or [], v[0] or []
        return _eq(list(a), list(e), unordered)
    if kind == "array_equals_any_order":
        return _eq(list(v[1] or []), list(v[0] or []), True)
    if kind == "array_empty":
        return v[0] is not None and len(v[0]) == 0
    if kind == "approx_equals":
        e, a, tol = v
        return e is not None and a is not None and abs(float(e) - float(a)) <= float(tol)
    if kind == "all_fields_null":
        return isinstance(v[0], dict) and all(x is None for x in v[0].values())
    if kind in ("map_equals", "map_entries_equals"):
        return _map_equal(v[0], v[1])
    if kind == "histogram_equals":
        e, a = v
        if e is None or a is None:
            return e is None and a is None
        return (_eq(a["bucket_count"], e["bucket_count"]) and _eq(a["sum"], e["sum"])
                and _eq(a["histogram_type"], e["histogram_type"])
                and _eq(list(a["range"] or []), list(e["range"] or [])) and _map_equal(e["values"], a["values"]))
    if kind == "sql_equals":
        norm = lambda s: re.sub(r"\s*", "", s).lower() if s is not None else None  # noqa: E731
        return norm(v[0]) == norm(v[1])
    raise ValueError(kind)


def _rows_hold(kind: str, rows, unordered=False) -> bool:
    return all(check(kind, list(r), unordered) for r in rows)


# --- query tests -------------------------------------------------------------------------------



def _yaml(text: str):
    return yaml.safe_load(text)


def _json_default(obj):
    """Mozilla's runner serialises dates and datetimes with ``isoformat`` (``default_encoding``)."""

    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    return obj


def _load_rows(text: str, ext: str):
    if ext == "ndjson":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if ext == "json":
        return json.loads(text)
    docs = [d for d in yaml.safe_load_all(text) if d is not None]
    if len(docs) != 1:
        raise bu.Unsupported("input file with several YAML documents", str(len(docs)))
    return docs[0]


@dataclass
class Column:
    name: str
    type: str          # BigQuery type, upper case, ``RECORD`` for structs
    repeated: bool
    fields: list = field(default_factory=list)


def read_schema(data) -> list[Column]:
    if isinstance(data, dict):
        data = data.get("fields", [])
    if len(data) == 1 and isinstance(data[0], dict) and "name" not in data[0] and "fields" in data[0]:
        data = data[0]["fields"]
    columns = []
    for f in data:
        kind = str(f.get("type", "STRING")).upper()
        kind = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "STRUCT": "RECORD"}.get(kind, kind)
        columns.append(Column(f["name"], kind, str(f.get("mode", "NULLABLE")).upper() == "REPEATED",
                              read_schema(f.get("fields", [])) if kind == "RECORD" else []))
    return columns


_SCALAR = {"STRING": "VARCHAR", "INT64": "BIGINT", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "TIMESTAMP": "TIMESTAMPTZ",
           "DATE": "DATE", "DATETIME": "TIMESTAMP", "TIME": "TIME", "NUMERIC": "DECIMAL(38,9)", "BYTES": "BLOB"}


def duck_type(column: Column) -> str:
    if column.type == "RECORD":
        base = "STRUCT(" + ", ".join(f'"{c.name}" {duck_type(c)}' for c in column.fields) + ")"
    elif column.type in _SCALAR:
        base = _SCALAR[column.type]
    else:
        raise bu.Unsupported("column type the harness cannot load", column.type)
    return base + ("[]" if column.repeated else "")


_TS = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|UTC|[+-]\d{2}(?::?\d{2})?)?$")


def _timestamp(value) -> str:
    """A BigQuery TIMESTAMP literal text (UTC) for a loaded value."""

    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat(sep=" ") + "+00"
    if isinstance(value, dt.date):
        return value.isoformat() + " 00:00:00+00"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=value)).isoformat(sep=" ") + "+00"
    if isinstance(value, str):
        m = _TS.match(value.strip())
        if not m:
            raise bu.Unsupported("timestamp text the harness does not read", value[:30])
        y, mo, d, h, mi, s, frac, zone = m.groups()
        text = f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h or 0):02d}:{mi or '00'}:{s or '00'}" + (f".{frac}" if frac else "")
        if zone and zone not in ("Z", "UTC"):
            sign, hh, mm = zone[0], zone[1:3], (zone[3:].lstrip(":") or "00")
            return f"{text}{sign}{hh}:{mm}"
        return text + "+00"
    raise bu.Unsupported("timestamp value the harness does not read", repr(value)[:30])


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def literal(value, column: Column, nested: bool = False) -> str:
    """A DuckDB literal for ``value`` read as ``column`` (BigQuery's JSON load rules for the types read)."""

    if column.repeated and not nested:
        items = [] if value is None else value
        if not isinstance(items, list):
            raise bu.Unsupported("a value that is not an array in a REPEATED column", column.name)
        return f"CAST([{', '.join(literal(v, column, True) for v in items)}] AS {duck_type(column)})"
    if value is None:
        return "NULL"
    kind = column.type
    if kind == "RECORD":
        if not isinstance(value, dict):
            raise bu.Unsupported("a value that is not a record in a RECORD column", column.name)
        names = {c.name.lower(): c for c in column.fields}
        extra = [k for k in value if k.lower() not in names]
        if extra:
            raise bu.Unsupported("a field the schema does not have", f"{column.name}.{extra[0]}")
        parts = []
        for c in column.fields:
            v = next((v for k, v in value.items() if k.lower() == c.name.lower()), None)
            parts.append(f"{_quote(c.name)}: {literal(v, c)}")
        return "{" + ", ".join(parts) + "}"
    if kind == "STRING":
        if isinstance(value, (dt.date, dt.datetime)):
            value = value.isoformat()
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            raise bu.Unsupported("a nested value in a STRING column", column.name)
        return f"CAST({_quote(str(value))} AS VARCHAR)"
    if kind == "INT64":
        if isinstance(value, bool) or isinstance(value, float) and value != int(value):
            raise bu.Unsupported("a value an INT64 column rejects", repr(value)[:20])
        return f"CAST({int(value)} AS BIGINT)" if not isinstance(value, str) else f"CAST({int(value.strip())} AS BIGINT)"
    if kind == "FLOAT64":
        return f"CAST({_quote(str(float(value)))} AS DOUBLE)"
    if kind == "BOOL":
        if isinstance(value, str):
            if value.lower() not in ("true", "false", "t", "f"):
                raise bu.Unsupported("a value a BOOL column rejects", value[:20])
            value = value.lower() in ("true", "t")
        return "TRUE" if value else "FALSE"
    if kind == "TIMESTAMP":
        return f"CAST({_quote(_timestamp(value))} AS TIMESTAMPTZ)"
    if kind == "DATE":
        if isinstance(value, dt.datetime):
            value = value.date()
        text = value.isoformat() if isinstance(value, dt.date) else str(value)
        return f"CAST({_quote(text)} AS DATE)"
    if kind == "DATETIME":
        text = value.isoformat(sep=" ") if isinstance(value, (dt.datetime, dt.date)) else str(value).replace("T", " ")
        return f"CAST({_quote(text)} AS TIMESTAMP)"
    if kind == "TIME":
        return f"CAST({_quote(str(value))} AS TIME)"
    if kind == "NUMERIC":
        number = decimal.Decimal(str(value))
        return f"CAST({_quote(str(number))} AS DECIMAL(38,9))"
    if kind == "BYTES":
        return f"CAST(from_base64({_quote(str(value))}) AS BLOB)"
    raise bu.Unsupported("column type the harness cannot load", kind)


def table_sql(name: str, columns: list[Column], rows: list) -> list[str]:
    ddl = f'CREATE TABLE {name} (' + ", ".join(f'"{c.name}" {duck_type(c)}' for c in columns) + ")"
    out = [ddl]
    for row in rows:
        if not isinstance(row, dict):
            raise bu.Unsupported("an input row that is not a mapping", repr(row)[:30])
        extra = [k for k in row if k.lower() not in {c.name.lower() for c in columns}]
        if extra:
            raise bu.Unsupported("an input field the schema does not have", extra[0])
        values = [literal(next((v for k, v in row.items() if k.lower() == c.name.lower()), None), c) for c in columns]
        out.append(f"INSERT INTO {name} VALUES ({', '.join(values)})")
    return out


_PARAM_TYPES = {"DATE": "DATE", "STRING": "STRING", "INT64": "INT64", "INTEGER": "INT64", "FLOAT64": "FLOAT64", "FLOAT": "FLOAT64",
                "BOOL": "BOOL", "BOOLEAN": "BOOL", "TIMESTAMP": "TIMESTAMP", "DATETIME": "DATETIME", "NUMERIC": "NUMERIC"}


def param_literal(param: dict) -> exp.Expression:
    kind = _PARAM_TYPES.get(str(param.get("type", param.get("type_", "STRING"))).upper())
    value = param.get("value")
    if kind is None or not set(param) <= {"name", "type", "type_", "value"}:
        raise bu.Unsupported("query parameter the harness does not read", param.get("name", "?"))
    if value is None:
        return exp.Cast(this=exp.Null(), to=exp.DataType.build(kind, dialect="bigquery"))
    if kind == "STRING":
        return exp.Literal.string(str(value))
    if kind in ("INT64", "FLOAT64", "NUMERIC"):
        return exp.Cast(this=exp.Literal.number(str(value)), to=exp.DataType.build(kind, dialect="bigquery"))
    if kind == "BOOL":
        return exp.Boolean(this=bool(value))
    if kind == "DATE":
        text = value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value)
        return exp.Cast(this=exp.Literal.string(text[:10]), to=exp.DataType.build("DATE", dialect="bigquery"))
    if kind == "TIMESTAMP":
        text = _timestamp(value).replace("+00", "+00:00")
        return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("TIMESTAMP", dialect="bigquery"))
    text = value.isoformat(sep=" ") if isinstance(value, (dt.date, dt.datetime)) else str(value)
    return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("DATETIME", dialect="bigquery"))


def render_template(text: str) -> str:
    try:
        import jinja2
    except ImportError as error:  # pragma: no cover
        raise bu.Unsupported("jinja2 is not installed", "") from error
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    try:
        return env.from_string(text).render(is_init=lambda: False)
    except jinja2.TemplateError as error:
        raise bu.Unsupported("template needs repository helpers", str(error)[:60]) from error


def _test_text(tree: Tree, test: str) -> tuple[str, str]:
    """(query text, source file) the way Mozilla's runner picks it for the test directory ``test``."""

    project, dataset, table, name = test.split("/")[2:]
    base = f"sql/{project}/{dataset}/{table}"
    if name in ("test_init", "test_script"):
        raise bu.Unsupported("init or script test", name)
    if tree.exists(f"{base}/view.sql"):
        text = re.sub("CREATE OR REPLACE VIEW.*?AS", "", render_template(tree.read(f"{base}/view.sql")), flags=re.DOTALL)
        return text, f"{base}/view.sql"
    if tree.exists(f"{base}/query.sql"):
        return render_template(tree.read(f"{base}/query.sql")), f"{base}/query.sql"
    raise bu.Unsupported("the query is not in the repository", base)


def _inputs(tree: Tree, test: str) -> dict:
    """``{full table name: (columns, rows)}`` for the input files of a test directory."""

    out = {}
    table_dir = test.rsplit("/", 1)[0]
    for name in tree.listdir(test):
        if "." not in name:
            continue
        stem, ext = name.rsplit(".", 1)
        if stem.endswith(".schema") or stem in ("expect", "query_params"):
            continue
        if ext not in ("yaml", "json", "ndjson"):
            raise bu.Unsupported("an input file the harness does not read", name)
        schema = next((tree.read(f"{table_dir}/{stem}.schema.{e}") for e in ("json", "yaml")
                       if tree.exists(f"{table_dir}/{stem}.schema.{e}")), None)
        if schema is not None:
            columns = read_schema(_yaml(schema))
        elif tree.exists("sql/" + stem.replace(".", "/") + "/schema.yaml"):
            columns = read_schema(_yaml(tree.read("sql/" + stem.replace(".", "/") + "/schema.yaml")))
        else:
            raise bu.Unsupported("an input table without a schema (BigQuery detects its types)", stem)
        out[stem.lower()] = (columns, _load_rows(tree.read(f"{test}/{name}"), ext))
    return out


def build_query_cases(tree: Tree, registry: Registry) -> list[Case]:
    cases = []
    for test in tree.test_dirs():
        project, dataset, table, name = test.split("/")[2:]
        case = Case(f"{project}/{dataset}/{table}/{name}", f"{project}/{dataset}/{table}", "query")
        try:
            _prepare_query(tree, registry, test, case)
        except bu.Unsupported as error:
            case.unsupported = (error.reason, error.detail)
        except (yaml.YAMLError, ValueError, KeyError, TypeError) as error:
            case.unsupported = ("test files the harness cannot read", f"{type(error).__name__}: {str(error)[:80]}")
        cases.append(case)
    return cases


def _prepare_query(tree: Tree, registry: Registry, test: str, case: Case) -> None:
    text, source = _test_text(tree, test)
    inputs = _inputs(tree, test)
    expect_name = next(n for n in tree.listdir(test) if n.startswith("expect."))
    expected = _load_rows(tree.read(f"{test}/{expect_name}"), expect_name.rsplit(".", 1)[1])
    expected = json.loads(json.dumps(expected, default=_json_default))
    params = []
    if tree.exists(f"{test}/query_params.yaml"):
        params = _yaml(tree.read(f"{test}/query_params.yaml")) or []
    statements = split_statements(text)
    local = Routine(source, "", "", "")
    definitions = []
    for statement in statements[:-1]:
        code = _code(statement)
        m = _DEFINITION.match(code)
        if not m:
            raise bu.Unsupported("a script, not a single query", code.strip()[:40])
        local.keys[m.group(3)] = f"{case.id.replace('/', '_')}__local__{_name_parts(m.group(3))[-1]}"
        definitions.append((statement, m))
    if not statements:
        raise bu.Unsupported("empty query", "")
    udfs = dict(registry.udfs)
    for statement, m in definitions:
        code = _code(statement)
        key = local.keys[m.group(3)]
        udf = registry._parse_definition(code, key, m, local)
        udfs[ETL, key] = udf
    query = registry.rewrite_calls(_code(statements[-1]), local)
    try:
        tree_ = sqlglot.parse_one(query, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        raise bu.Unsupported("does not parse", str(error)[:100]) from error
    if not isinstance(tree_, exp.Query):
        raise bu.Unsupported("not a query", type(tree_).__name__)
    by_name = {p["name"]: p for p in params}
    for node in list(tree_.find_all(exp.Parameter)):
        name = node.name
        if name not in by_name:
            raise bu.Unsupported("query parameter without a value", name)
        node.replace(param_literal(by_name[name]))
    cte_names = {c.alias.lower() for c in tree_.find_all(exp.CTE)}
    tables = {}
    for node in list(tree_.find_all(exp.Table)):
        if isinstance(node.this, exp.Func) or not node.name:
            continue
        full = ".".join(p for p in (node.catalog, node.db, node.name) if p).lower()
        if not node.db and node.name.lower() in cte_names:
            continue
        if "*" in node.name:
            raise bu.Unsupported("a wildcard table", full)
        if full not in inputs:
            raise bu.Unsupported("a table without input rows", full)
        alias = tables.setdefault(full, f"_in{len(tables)}")
        new = exp.Table(this=exp.to_identifier(alias), alias=node.args.get("alias"))
        node.replace(new)
    tree_ = _inline_all(SimpleNamespace(udfs=udfs), tree_)
    case.bigquery = tree_.sql(dialect="bigquery")
    case.expected = expected
    case.meta = {"tables": {alias: inputs[full] for full, alias in tables.items()}, "files": sorted(inputs)}


# --- reading results the way Mozilla's runner does ---------------------------------------------

def _decimal_text(value: decimal.Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def coerce(value, dtype):
    """A DuckDB value as Mozilla's ``coerce_result`` returns a BigQuery one (JSON-like, null columns omitted)."""

    if value is None:
        return None
    kind = dtype.id
    if kind == "struct":
        out = {}
        for (name, child_type), v in zip(dtype.children, value.values()):
            c = coerce(v, child_type)
            if c is not None and name not in ("created_at", "generated_time"):
                out[name] = c
        return out
    if kind == "list":
        child = dtype.children[0][1]
        return [coerce(v, child) for v in value]
    if kind == "timestamp with time zone":
        return value.replace(tzinfo=dt.timezone.utc).isoformat()
    if kind.startswith("timestamp") or kind == "date" or kind == "time":
        return value.isoformat()
    if kind == "decimal":
        return _decimal_text(value)
    if kind == "blob":
        return base64.encodebytes(bytes(value)).decode().strip()
    return value


def fetch_typed(con, duck_sql: str):
    """(rows as BigQuery-coerced dicts, DuckDB description); TIMESTAMPTZ read as UTC without ``pytz``."""

    con.execute(duck_sql)
    description = con.description
    names = [d[0] for d in description]
    types = [d[1] for d in description]
    if any("TIME ZONE" in str(t) for t in types):
        casts = ", ".join(f'CAST(#{i + 1} AS {str(t).replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")})' for i, t in enumerate(types))
        raw = con.execute(f"SELECT {casts} FROM ({duck_sql})").fetchall()
    else:
        raw = con.fetchall()
    rows = []
    for row in raw:
        out = {}
        for name, value, t in zip(names, row, types):
            c = coerce(value, t)
            if c is not None and name not in ("created_at", "generated_time"):
                out[name] = c
        rows.append(out)
    return rows


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def loose_equal(a, e, unordered: bool = False) -> bool:
    """Equality of two BigQuery-coerced values: exact, but numbers by value and timestamps by instant."""

    if isinstance(a, dict) and isinstance(e, dict):
        return a.keys() == e.keys() and all(loose_equal(a[k], e[k], unordered) for k in a)
    if isinstance(a, list) and isinstance(e, list):
        if len(a) != len(e):
            return False
        if unordered:
            rest = list(e)
            for x in a:
                i = next((i for i, y in enumerate(rest) if loose_equal(x, y, True)), None)
                if i is None:
                    return False
                rest.pop(i)
            return True
        return all(loose_equal(x, y) for x, y in zip(a, e))
    if isinstance(a, bool) or isinstance(e, bool):
        return a is e or a == e and type(a) is type(e)
    if isinstance(a, (int, float)) and isinstance(e, (int, float)):
        return a == e or (isinstance(a, float) or isinstance(e, float)) and bu.same(float(a), float(e))
    if isinstance(a, str) and isinstance(e, str):
        if a == e:
            return True
        if _ISO.match(a) and _ISO.match(e):
            try:
                x, y = dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(e)
                return x == y if (x.tzinfo is None) == (y.tzinfo is None) else False
            except ValueError:
                return False
        return False
    return a == e


def rows_equal(actual: list, expected: list, unordered: bool = False) -> bool:
    if len(actual) != len(expected):
        return False
    rest = list(expected)
    for row in actual:
        i = next((i for i, e in enumerate(rest) if loose_equal(row, e, unordered)), None)
        if i is None:
            return False
        rest.pop(i)
    return True


def evaluate_query(case: Case, con, translate, out: dict) -> dict:
    import duckdb

    try:
        for name, (columns, rows) in case.meta["tables"].items():
            for statement in table_sql(name, columns, rows):
                con.execute(statement)
    except bu.Unsupported as error:
        return _failed(out, "unsupported", error.reason, error.detail)
    except duckdb.Error as error:
        return _failed(out, "unsupported", "input table not loadable in DuckDB", str(error)[:120])
    try:
        duck = translate(case.bigquery)
    except UntranslatableError as error:
        return _failed(out, "unsupported", "KumoSQL declines to translate", str(error))
    except sqlglot.errors.SqlglotError as error:
        return _failed(out, "unsupported", "does not parse", str(error)[:120])
    out["duckdb"] = duck
    try:
        rows = fetch_typed(con, duck)
    except duckdb.Error as error:
        return _failed(out, "unsupported", "translation not executable in DuckDB", str(error)[:160])
    expected = case.expected
    unordered = bu.order_unspecified(case.bigquery)
    out["actual"] = repr(rows)[:300]
    out["expected"] = repr(expected)[:300]
    if rows_equal(rows, expected):
        out["outcome"] = "agree"
        return out
    con.execute("PRAGMA disable_optimizer")
    try:
        again = fetch_typed(con, duck)
    except duckdb.Error:
        again = None
    finally:
        con.execute("PRAGMA enable_optimizer")
    if again is not None and rows_equal(again, expected):
        return _failed(out, "unsupported", "DuckDB optimizer disagrees with itself")
    if unordered and rows_equal(rows, expected, True):
        return _failed(out, "unsupported", "element order not specified by GoogleSQL")
    out["outcome"] = "WRONG"
    return out


# --- running -----------------------------------------------------------------------------------

def translator(mode: str):
    """``mode``: "fixed" (``kumosql.bigquery_duckdb``), "baseline" (sqlglot alone) or "layer" (the layer the searches use)."""

    from kumosql import bigquery_duckdb

    if mode == "layer":
        from kumosql.counterexample import to_duckdb as layer

        return lambda sql: layer(sql, "bigquery")
    return lambda sql: bigquery_duckdb.to_duckdb(sql, fix=(mode == "fixed"))


def connect(mode: str):
    con = bu.connect()
    if mode == "layer":
        from kumosql import bigquery_on_duckdb

        bigquery_on_duckdb.configure(con)
    return con


def _failed(out: dict, outcome: str, reason: str, detail: str = "") -> dict:
    out.update(outcome=outcome, reason=reason, detail=detail)
    return out


def translate_and_run(case: Case, con, translate, out: dict, sql: str | None = None):
    """(duck_sql, rows, types), or None after recording why the case has no verdict in ``out``."""

    import duckdb

    sql = case.bigquery if sql is None else sql
    try:
        duck = translate(sql)
    except UntranslatableError as error:
        _failed(out, "unsupported", "KumoSQL declines to translate", str(error))
        return None
    except sqlglot.errors.SqlglotError as error:
        _failed(out, "unsupported", "does not parse", str(error)[:120])
        return None
    out["duckdb"] = duck
    try:
        rows, types = bu.run(con, duck)
    except duckdb.Error as error:
        _failed(out, "unsupported", "translation not executable in DuckDB", str(error)[:160])
        return None
    return duck, rows, types


def evaluate(case: Case, mode: str = "fixed", con=None) -> dict:
    out = {"id": case.id, "unit": case.unit, "kind": case.kind, "held_out": held_out(case.unit)}
    if case.unsupported:
        return _failed(out, "unsupported", *case.unsupported)
    own = con is None
    con = connect(mode) if own else con
    try:
        translate = translator(mode)
        if case.kind == "query":
            return evaluate_query(case, con, translate, out)
        got = translate_and_run(case, con, translate, out)
        if got is None:
            return out
        duck, rows, types = got
        if not rows:
            return _failed(out, "unsupported", "the statement returns no rows to check")
        holds = _rows_hold(case.assertion, rows)
        out["actual"] = repr(rows)[:300]
        if case.kind == "assertion":
            if holds:
                out["outcome"] = "agree"
                return out
            if _rows_hold(case.assertion, bu._rerun_unoptimized(con, duck)):
                return _failed(out, "unsupported", "DuckDB optimizer disagrees with itself")
            if bu.order_unspecified(case.bigquery) and _rows_hold(case.assertion, rows, unordered=True):
                return _failed(out, "unsupported", "element order not specified by GoogleSQL")
            out["outcome"] = "WRONG"
            return out
        # #xfail: BigQuery raised, so the assertion must not hold on our values
        if holds:
            if not _rows_hold(case.assertion, bu._rerun_unoptimized(con, duck)):
                return _failed(out, "unsupported", "DuckDB optimizer disagrees with itself")
            out["outcome"] = "WRONG"
        else:
            out["outcome"] = "agree"
        return out
    finally:
        if own:
            con.close()


def build_cases(tree: Tree, registry: Registry | None = None) -> list[Case]:
    registry = Registry(tree) if registry is None else registry
    cases = build_assertion_cases(registry)
    cases += build_query_cases(tree, registry)
    return cases


def run_all(cases: list[Case], mode: str = "fixed") -> list[dict]:
    return [evaluate(case, mode) for case in cases]


def summarize(results: list[dict]) -> dict:
    def part(rows):
        c = Counter(r["outcome"] for r in rows)
        return {"cases": len(rows), "agree": c["agree"], "wrong": c["WRONG"], "supported": c["agree"] + c["WRONG"],
                "unsupported": c["unsupported"]}

    def kind(name):
        return [r for r in results if r["kind"] == name]

    return {
        "all": part(results),
        "dev": part([r for r in results if not r["held_out"]]),
        "held_out": part([r for r in results if r["held_out"]]),
        "by_kind": {k: part(kind(k)) for k in ("assertion", "xfail", "query") if kind(k)},
        "units": len({r["unit"] for r in results}),
        "units_with_verdict": len({r["unit"] for r in results if r["outcome"] != "unsupported"}),
        "unsupported_reasons": dict(Counter(r["reason"] for r in results if r["outcome"] == "unsupported").most_common()),
        "declined": dict(Counter(r["detail"].split(":")[0][:60] for r in results
                                 if r.get("reason") == "KumoSQL declines to translate").most_common()),
        "not_executable": dict(Counter(r["detail"][:70] for r in results
                                       if r.get("reason", "").endswith("not executable in DuckDB")).most_common(25)),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", type=Path, help="a bigquery-etl checkout (default: fetch the pinned commit)")
    ap.add_argument("--check-sources", action="store_true", help="fetch the pin and compare every file's SHA-256")
    ap.add_argument("--write-manifest", action="store_true", help="re-pin the SHA-256 of every file read (a pin change)")
    ap.add_argument("--failures", action="store_true", help="print WRONG development cases (held-out cases stay hidden)")
    ap.add_argument("--unsupported", action="store_true", help="print unsupported development cases with their reason")
    ap.add_argument("--only", help="run the cases whose id contains this text")
    ap.add_argument("--mode", choices=["fixed", "baseline", "layer"], default="fixed")
    ap.add_argument("--include-held-out", action="store_true", help="also print held-out cases (final measurement only)")
    ap.add_argument("--write-results", action="store_true")
    ap.add_argument("--json", type=Path, help="write the outcome of every case that may be looked at here")
    ap.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check (development on a local clone)")
    args = ap.parse_args(argv)
    from bench_common import quiet

    quiet()
    tree = Tree(args.source) if args.source else fetch()
    if args.write_manifest:
        write_manifest(MANIFEST, checksums(tree))
    if args.check_sources:
        bad = check_sources(tree)
        print("sources match the pinned SHA-256" if not bad else f"{len(bad)} files differ: {bad[:5]}")
        return 1 if bad else 0
    bad = [] if args.no_verify else check_sources(tree)
    if bad:
        raise SystemExit(f"{len(bad)} files differ from {MANIFEST.name}, e.g. {bad[:3]}")
    cases = build_cases(tree)
    if args.only:
        cases = [c for c in cases if args.only in c.id]
    t0 = time.perf_counter()
    results = run_all(cases, args.mode)
    secs = time.perf_counter() - t0
    summary = summarize(results)
    shown = results if (args.only or args.include_held_out) else [r for r in results if not r["held_out"]]
    detail = summarize(shown)  # reasons are listed for the cases that may be looked at
    for key in ("units", "units_with_verdict", "unsupported_reasons", "declined", "not_executable"):
        summary[key] = detail[key]
    print(json.dumps(summary, indent=1))
    print(f"{len(results)} cases in {secs:.1f}s")
    if args.json:
        args.json.write_text(json.dumps(shown, indent=1, default=str), encoding="utf-8")
    for r in results:
        if r["held_out"] and not (args.only or args.include_held_out):
            continue
        if args.failures and r["outcome"] == "WRONG":
            print("WRONG", r["id"], "\n  duckdb:", r.get("duckdb"), "\n  actual:", r.get("actual"), "\n  expected:", r.get("expected"))
        if args.unsupported and r["outcome"] == "unsupported":
            print("unsupported", r["id"], r["reason"], "|", r.get("detail", "")[:160])
    return 1 if summary["all"]["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())
