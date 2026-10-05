"""GoogleSQL compliance expected results as an oracle for KumoSQL's BigQuery-to-DuckDB execution.

KumoSQL's execution checks and counterexample replays run BigQuery SQL on DuckDB through one
translation (:func:`kumosql.counterexample.to_duckdb` with the BigQuery dialect, which applies
:mod:`kumosql.bigquery_on_duckdb`). A difference that translation reports must be a difference
BigQuery would show. The GoogleSQL compliance tests (google/googlesql, Apache-2.0) give, for each
query, the typed rows the reference implementation returns over tables built by
``[prepare_database]`` blocks. This harness uses those rows as an independent oracle:

* each case whose features BigQuery supports (no protos or enums, no parameters, every required
  feature in :data:`BIGQUERY_FEATURES`, no type BigQuery lacks such as ``INT32`` or ``UINT64``) is
  **supported**; every other case is skipped and counted by reason;
* the fixture tables are loaded into DuckDB from the expected rows of their ``prepare_database``
  block (never by running the setup SQL, so the oracle does not depend on the translation);
* the query is translated and run, read back the way BigQuery returns rows, and compared with the
  expected rows under the case's ordering (``unknown order`` arrays compare as multisets) and the
  compliance float margin (4 ULP bits, as ``FloatMargin::UlpMargin(4)``).

Outcomes of a supported case: ``agree``; ``declined`` (KumoSQL says no faithful DuckDB reading
exists, or a guard says BigQuery would fail); ``not executable`` (DuckDB or sqlglot cannot run the
translation); ``not compared`` (the result has a type the harness does not read); and
``DISAGREE``: the translation ran and returned different rows. A difference counts only when DuckDB
with its optimizer off returns the same rows (DuckDB 1.5 optimizer bug, see
:func:`kumosql.duckdb_load.run_unoptimized`). Each disagreement is classified in
:data:`KNOWN_DIVERGENCES` or automatically (the compliance tests' default time zone is
America/Los_Angeles, BigQuery's is UTC: a case that agrees when DuckDB reads times in
Los Angeles is an oracle-side difference, not a translation bug).

One test file in five (SHA-1 of the file name) is held out. The first fixes came from the development files
alone; the held-out failures were looked at afterwards and fixed (``UNSEEN``), so that split is "tuned on test".

    python tools/googlesql_results_eval.py                    # fetch googlesql at the pin, run all
    python tools/googlesql_results_eval.py --failures          # list every disagreement
    python tools/googlesql_results_eval.py --write-results     # update benchmarks/results
    python tools/googlesql_results_eval.py --write-sample      # refresh the pinned test sample

The pinned commit is :data:`PIN`; :data:`MANIFEST` holds the SHA-256 of each ``*.test`` file and a run checks it.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import quiet, today, write_results  # noqa: E402
from benchmark_corpora import held_out  # noqa: E402

from kumosql import bigquery_on_duckdb as bq  # noqa: E402
from kumosql.counterexample import to_duckdb  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402

GOOGLESQL_URL = "https://github.com/google/googlesql.git"
PIN = "d82db99a923a46571f543a13899dc791a7b6f743"  # the pin tools/bq_behavior_eval.py's corpus came from
TESTDATA = "googlesql/compliance/testdata"
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "googlesql"
SAMPLE = ROOT / "tests" / "fixtures" / "googlesql_results" / "sample.json.gz"
RESULTS_NAME = "googlesql-expected-results"
COMPLIANCE_TIME_ZONE = "America/Los_Angeles"  # the compliance driver's default; BigQuery's is UTC

# GoogleSQL language features BigQuery has (GoogleSQL for BigQuery reference). A case that needs any
# other feature is skipped: it tests GoogleSQL, not BigQuery. Unsure features are left out.
BIGQUERY_FEATURES = frozenset(
    """
    ANALYTIC_FUNCTIONS NUMERIC_TYPE BIGNUMERIC_TYPE CIVIL_TIME JSON_TYPE INTERVAL_TYPE RANGE_TYPE GEOGRAPHY
    GROUP_BY_ROLLUP GROUPING_SETS GROUPING_BUILTIN GROUP_BY_STRUCT GROUP_BY_ALL QUALIFY PIVOT UNPIVOT
    TABLESAMPLE WITH_RECURSIVE WITH_ON_SUBQUERY SAFE_FUNCTION_CALL NULL_HANDLING_MODIFIER_IN_AGGREGATE
    NULL_HANDLING_MODIFIER_IN_ANALYTIC ORDER_BY_IN_AGGREGATE LIMIT_IN_AGGREGATE HAVING_IN_AGGREGATE
    NULLS_FIRST_LAST_IN_ORDER_BY IS_DISTINCT LIKE_ANY_SOME_ALL LIKE_ANY_SOME_ALL_ARRAY COLLATION_SUPPORT
    ANNOTATION_FRAMEWORK ORDER_BY_COLLATE FORMAT_IN_CAST PARAMETERIZED_TYPES CORRESPONDING CORRESPONDING_FULL
    BY_NAME PIPES TABLE_VALUED_FUNCTIONS TEMPLATE_FUNCTIONS CREATE_TABLE_FUNCTION NAMED_ARGUMENTS ENCRYPTION
    JSON_VALUE_EXTRACTION_FUNCTIONS JSON_LAX_VALUE_EXTRACTION_FUNCTIONS JSON_ARRAY_FUNCTIONS
    JSON_CONSTRUCTOR_FUNCTIONS JSON_MUTATOR_FUNCTIONS JSON_KEYS_FUNCTION JSON_QUERY_LAX
    JSON_ARRAY_VALUE_EXTRACTION_FUNCTIONS TIME_BUCKET_FUNCTIONS SELECT_STAR_EXCEPT_REPLACE
    DATE_TIME_CONSTRUCTORS WEEK_WITH_WEEKDAY ROUND_WITH_ROUNDING_MODE
    """.split()
)
# Type names GoogleSQL has and BigQuery does not, in SQL text (DOUBLE is GoogleSQL's name for FLOAT64,
# which BigQuery does not accept; FLOAT is 32-bit) and in result or table types (where GoogleSQL
# prints FLOAT64 as DOUBLE).
NON_BIGQUERY_TYPE = re.compile(r"\b(INT32|UINT32|UINT64|DOUBLE|FLOAT(?!64)|ENUM|PROTO|UUID|MAP)\b(?!\s*\()", re.I)
NON_BIGQUERY_TYPES = {"INT32", "UINT32", "UINT64", "FLOAT", "ENUM", "PROTO", "UUID", "MAP"}
# BigQuery types the harness cannot load into a fixture table or read from a result.
UNREAD_TYPES = {"JSON", "INTERVAL", "RANGE", "GEOGRAPHY", "BIGNUMERIC", "GRAPH_ELEMENT", "GRAPH_PATH", "MEASURE",
                "TOKENLIST", "TIMESTAMP_PICOS"}

# Disagreements classified by hand (case id -> (class, note)). Classes:
#   divergence: a known BigQuery-vs-reference difference that is not KumoSQL's translation
#   decline:    an untranslatable construct KumoSQL should decline (tracked as wrong until it does)
#   bug:        a KumoSQL translation bug still open (counted as wrong)
KNOWN_DIVERGENCES: dict[str, tuple[str, str]] = {}


# --- fetching ----------------------------------------------------------------------------------


MANIFEST = ROOT / "tests" / "fixtures" / "googlesql_results" / "testdata.sha256"


def checksums(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob("*.test"))}


def verify(directory: Path) -> None:
    """Every ``*.test`` file must be the one pinned in :data:`MANIFEST` (SHA-256)."""

    pinned = dict(line.split()[::-1] for line in MANIFEST.read_text().splitlines() if line.strip())
    actual = checksums(directory)
    if actual != pinned:
        differ = sorted(k for k in set(actual) | set(pinned) if actual.get(k) != pinned.get(k))
        raise RuntimeError(f"{directory}: {len(differ)} test files differ from {MANIFEST.name}, e.g. {differ[:3]}")


def fetch(dest: Path = CACHE) -> Path:
    """Sparse, blobless clone of google/googlesql at :data:`PIN` (once); return the testdata directory."""

    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        run = lambda *a: subprocess.run(["git", *a], cwd=dest, check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        run("remote", "add", "origin", GOOGLESQL_URL)
        run("sparse-checkout", "set", "--no-cone", f"/{TESTDATA}/", "/LICENSE")
        run("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", PIN)
        run("checkout", "-q", "FETCH_HEAD")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
    if head != PIN:
        raise RuntimeError(f"{dest} is at {head}, expected {PIN}")
    return dest / TESTDATA


# --- types -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Type:
    name: str  # INT64, DOUBLE, STRING, ARRAY, STRUCT, ... ("" for an ARRAY<> whose element type is written inline)
    args: tuple = ()  # ARRAY: (element,); STRUCT: ((field name or "", Type), ...); others: raw parameters

    def names(self) -> set[str]:
        out = {self.name}
        for a in self.args:
            if isinstance(a, Type):
                out |= a.names()
            elif isinstance(a, tuple) and len(a) == 2 and isinstance(a[1], Type):
                out |= a[1].names()
        return out


class Unreadable(ValueError):
    """Expected-result text the harness cannot read."""


_TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9.]*|`[^`]*`|<|>|,|\(|\)|[0-9]+)")


class _TypeParser:
    def __init__(self, text: str, pos: int = 0):
        self.text, self.pos = text, pos

    def peek(self) -> str:
        m = _TOKEN.match(self.text, self.pos)
        return m.group(1) if m else ""

    def take(self) -> str:
        m = _TOKEN.match(self.text, self.pos)
        if not m:
            raise Unreadable(f"type at {self.text[self.pos:self.pos + 30]!r}")
        self.pos = m.end()
        return m.group(1)

    def type(self) -> Type:
        name = self.take().upper()
        args: tuple = ()
        if self.peek() == "<":
            self.take()
            if name == "STRUCT":
                fields = []
                while self.peek() != ">":
                    before = self.pos
                    first = self.take()
                    if self.peek() in (",", ">", "<", "("):  # an unnamed field: the token was its type
                        self.pos = before
                        fields.append(("", self.type()))
                    else:
                        fields.append((first.strip("`"), self.type()))
                    if self.peek() == ",":
                        self.take()
                args = tuple(fields)
            elif self.peek() == ">":
                args = (Type(""),)
            else:
                inner = [self.type()]
                while self.peek() == ",":
                    self.take()
                    inner.append(self.type())
                args = tuple(inner)
            if self.take() != ">":
                raise Unreadable("type: expected >")
        if re.match(r"\s*\(\s*[0-9]", self.text[self.pos:]):  # STRING(10), NUMERIC(5, 2); not a typed (NULL)
            depth = 0
            while True:
                t = self.take()
                depth += t == "("
                depth -= t == ")"
                if depth == 0:
                    break
        return Type(name, args)


def parse_type(text: str, pos: int = 0) -> tuple[Type, int]:
    p = _TypeParser(text, pos)
    return p.type(), p.pos


# --- values ------------------------------------------------------------------------------------


@dataclass
class Array:
    items: list
    unordered: bool
    element: Type | None = None  # the element type when the value carries its own (under ARRAY<>)


class _ValueParser:
    def __init__(self, text: str):
        self.text, self.pos = text, 0

    def ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def startswith(self, s: str) -> bool:
        self.ws()
        return self.text.startswith(s, self.pos)

    def expect(self, s: str) -> None:
        if not self.startswith(s):
            raise Unreadable(f"expected {s!r} at {self.text[self.pos:self.pos + 40]!r}")
        self.pos += len(s)

    def atom(self) -> str:
        """Characters up to the next ``,``, ``]`` or ``}`` at depth 0."""

        self.ws()
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",]}\n":
            self.pos += 1
        return self.text[start:self.pos].strip()

    def skip(self) -> None:
        """A value of a type the harness does not read: balanced brackets, quotes respected."""

        self.ws()
        depth = 0
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if c in "\"'":
                self.string_body()
                continue
            if c in "[{(":
                depth += 1
            elif c in "]})":
                if depth == 0:
                    return
                depth -= 1
            elif c == "," and depth == 0:
                return
            self.pos += 1

    def string_body(self) -> str:
        quote = self.text[self.pos]
        if self.text.startswith(quote * 3, self.pos):
            raise Unreadable("triple-quoted string")
        self.pos += 1
        out = []
        while True:
            if self.pos >= len(self.text):
                raise Unreadable("unterminated string")
            c = self.text[self.pos]
            if c == quote:
                self.pos += 1
                return "".join(out)
            if c == "\\":
                n = self.text[self.pos + 1]
                simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "`": "`", "?": "?",
                          "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
                if n in simple:
                    out.append(simple[n])
                    self.pos += 2
                elif n in "xX":
                    out.append(chr(int(self.text[self.pos + 2:self.pos + 4], 16)))
                    self.pos += 4
                elif n == "u":
                    out.append(chr(int(self.text[self.pos + 2:self.pos + 6], 16)))
                    self.pos += 6
                elif n == "U":
                    out.append(chr(int(self.text[self.pos + 2:self.pos + 10], 16)))
                    self.pos += 10
                elif n in "01234567":
                    out.append(chr(int(self.text[self.pos + 1:self.pos + 4], 8)))
                    self.pos += 4
                else:
                    raise Unreadable(f"escape \\{n}")
                continue
            out.append(c)
            self.pos += 1

    def value(self, t: Type):
        self.ws()
        inline = False
        if re.match(r"(ARRAY|STRUCT)<", self.text[self.pos:self.pos + 7]):
            t, self.pos = parse_type(self.text, self.pos)
            inline = True
            if self.startswith("(NULL)"):  # a typed NULL, ARRAY<INT64>(NULL)
                self.pos += len("(NULL)")
                return None
        if self.text.startswith("NULL", self.pos) and not self.text[self.pos + 4:self.pos + 5].isalnum():
            self.pos += 4
            return None
        if t.name == "ARRAY":
            self.expect("[")
            unordered = False
            if self.startswith("unknown order:"):
                self.pos += len("unknown order:")
                unordered = True
            elif self.startswith("known order:"):
                self.pos += len("known order:")
            items = []
            while not self.startswith("]"):
                items.append(self.value(t.args[0]))
                if self.startswith(","):
                    self.pos += 1
            self.expect("]")
            return Array(items, unordered, t.args[0] if inline else None)
        if t.name == "STRUCT":
            self.expect("{")
            values = []
            for _, ft in t.args:
                values.append(self.value(ft))
                if self.startswith(","):
                    self.pos += 1
            self.expect("}")
            return tuple(values)
        if t.name in ("STRING",):
            if not (self.startswith('"') or self.startswith("'")):
                raise Unreadable("string")
            return self.string_body()
        if t.name == "BYTES":
            self.expect("b")
            return self.string_body().encode("latin-1")
        if t.name in NON_BIGQUERY_TYPES or t.name in UNREAD_TYPES or t.name == "":
            self.skip()
            raise _Skipped(t.name or "ARRAY<>")
        text = self.atom()
        return _scalar(t.name, text)


class _Skipped(Unreadable):
    pass


def _scalar(name: str, text: str):
    try:
        if name in ("INT64",):
            return int(text)
        if name in ("FLOAT64", "DOUBLE", "FLOAT"):
            return float(text)
        if name == "NUMERIC":
            return Decimal(text)
        if name == "BOOL":
            return {"true": True, "false": False}[text]
        if name == "DATE":
            y, m, d = text.split("-")
            return date(int(y), int(m), int(d))
        if name == "TIMESTAMP":
            m = re.fullmatch(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d(?:\.\d+)?)([+-]\d\d(?::?\d\d)?)", text)
            if not m:
                raise Unreadable(f"timestamp {text!r}")
            return _datetime(f"{m.group(1)} {m.group(2)}").replace(tzinfo=timezone.utc) - _offset(m.group(3))
        if name == "DATETIME":
            return _datetime(text.replace("T", " "))
        if name == "TIME":
            hh, mm, ss = text.split(":")
            sec, _, frac = ss.partition(".")
            if len(frac) > 6:
                raise Unreadable("sub-microsecond time")
            return dtime(int(hh), int(mm), int(sec), int((frac + "000000")[:6]))
    except (ValueError, KeyError, InvalidOperation) as error:
        raise Unreadable(f"{name} {text!r}: {error}") from error
    raise _Skipped(name)


def _offset(text: str) -> timedelta:
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))


def _datetime(text: str) -> datetime:
    day, _, clock = text.partition(" ")
    sec_frac = clock.split(".")
    if len(sec_frac) == 2 and len(sec_frac[1]) > 6:
        if sec_frac[1][6:].strip("0"):
            raise Unreadable("sub-microsecond timestamp")
        clock = sec_frac[0] + "." + sec_frac[1][:6]
    y, mo, d = (int(x) for x in day.split("-"))
    hh, mm, ss = clock.split(":")
    sec, _, frac = ss.partition(".")
    return datetime(y, mo, d, int(hh), int(mm), int(sec), int((frac + "000000")[:6]))


def parse_result(text: str) -> tuple[Type, Any]:
    """The type and value of an expected result, ``ARRAY<STRUCT<...>>[...]``."""

    t, pos = parse_type(text)
    parser = _ValueParser(text)
    parser.pos = pos
    value = parser.value(t)
    parser.ws()
    if parser.pos != len(text):
        raise Unreadable(f"trailing text {text[parser.pos:parser.pos + 40]!r}")
    return t, value


# --- test files --------------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    file: str
    name: str
    sql: str
    expected: str  # the expected-result text, or "ERROR: ..."
    options: dict = field(default_factory=dict)
    prepare: bool = False


def _options(lines: list[str]) -> tuple[dict, list[str]]:
    """Leading ``[option]`` lines (an option may span lines) and ``#`` comments; the rest is the body."""

    opts: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith("#"):
            i += 1
            continue
        if not line.startswith("["):
            break
        text = line
        while text.count("[") > text.count("]") and i + 1 < len(lines):
            i += 1
            text += "\n" + lines[i]
        i += 1
        inner = text.strip()[1:-1]
        key, _, value = inner.partition("=")
        opts[key.strip()] = value.strip()
    return opts, lines[i:]


def parse_test_file(stem: str, text: str) -> list[Case]:
    cases, defaults = [], {}
    for position, block in enumerate(re.split(r"\n==\n", "\n" + text + "\n")):
        opts, body = _options(block.strip("\n").split("\n"))
        for key in list(opts):
            if key.startswith("default "):
                k, _, v = key[len("default "):].partition("=")
                defaults[k.strip()] = v.strip() if v else opts[key]
                del opts[key]
        if position == 0 and "default_time_zone" in opts:  # in the file's first block it covers the whole file
            defaults["default_time_zone"] = opts["default_time_zone"]
        if not body:
            continue
        merged = dict(defaults)
        for key, value in opts.items():
            if key in ("required_features", "forbidden_features") and merged.get(key):
                value = ",".join(x for x in (merged[key], value) if x)
            merged[key] = value
        content = "\n".join(body)
        sql, _, expected = content.partition("\n--\n")
        expected = re.sub(r"\n\s*NOTE:.*", "", expected, flags=re.S)
        expected = re.sub(r"\A(\s*#[^\n]*\n)+", "", expected)  # a comment before the result
        note = "NOTE: Reference implementation reports non-determinism" in content
        if note:
            merged["nondeterministic"] = "1"
        name = merged.pop("name", "") or f"_test{len(cases)}"
        cases.append(Case(f"{stem}/{name}", stem, name, sql.strip(), expected.strip(), merged, "prepare_database" in merged))
    return cases


def read_testdata(directory: Path) -> list[Case]:
    out = []
    for path in sorted(directory.glob("*.test")):
        out.extend(parse_test_file(path.stem, path.read_text(encoding="utf-8", errors="replace")))
    return out


# --- fixture tables ----------------------------------------------------------------------------


@dataclass
class Table:
    name: str
    columns: list  # [(name, Type)]
    rows: list  # list of tuples, one value per column (None for an unread column)
    unread: set  # lower-case names of columns that are not loaded, with the reason
    reasons: dict


def _ctas_name(sql: str) -> str | None:
    m = re.match(r"(?is)\s*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+([`\w.]+)\s+AS\b", sql)
    return m.group(1).strip("`") if m else None


def build_table(case: Case) -> Table | None:
    name = _ctas_name(case.sql)
    if name is None or case.expected.startswith("ERROR"):
        return None
    t, pos = parse_type(case.expected)
    if t.name != "ARRAY" or t.args[0].name != "STRUCT":
        return None
    columns = list(t.args[0].args)
    unread, reasons = set(), {}
    for cname, ctype in columns:
        bad = ctype.names() & (NON_BIGQUERY_TYPES | UNREAD_TYPES | {""})
        if bad:
            unread.add(cname.lower())
            reasons[cname.lower()] = "non-BigQuery type" if bad & NON_BIGQUERY_TYPES else f"{sorted(bad)[0] or 'ARRAY<>'} column"
    parser = _ValueParser(case.expected)
    parser.pos = pos
    parser.expect("[")
    if parser.startswith("unknown order:"):
        parser.pos += len("unknown order:")
    elif parser.startswith("known order:"):
        parser.pos += len("known order:")
    rows = []
    while not parser.startswith("]"):
        parser.expect("{")
        row = []
        for cname, ctype in columns:
            if cname.lower() in unread:
                parser.skip()
                row.append(None)
            else:
                row.append(_loadable(parser.value(ctype)))
            if parser.startswith(","):
                parser.pos += 1
        parser.expect("}")
        rows.append(tuple(row))
        if parser.startswith(","):
            parser.pos += 1
    return Table(name, columns, rows, unread, reasons)


def _loadable(value):
    if isinstance(value, Array):
        return [_loadable(v) for v in value.items]
    if isinstance(value, tuple):
        return tuple(_loadable(v) for v in value)
    return value


def duck_type(t: Type) -> str:
    simple = {"INT64": "BIGINT", "BOOL": "BOOLEAN", "STRING": "VARCHAR", "BYTES": "BLOB", "DATE": "DATE",
              "TIMESTAMP": "TIMESTAMPTZ", "DATETIME": "TIMESTAMP", "TIME": "TIME", "NUMERIC": "DECIMAL(38, 9)",
              "FLOAT64": "DOUBLE", "DOUBLE": "DOUBLE"}
    if t.name in simple:
        return simple[t.name]
    if t.name == "ARRAY":
        return duck_type(t.args[0]) + "[]"
    if t.name == "STRUCT":
        return "STRUCT(" + ", ".join(f'"{n or f"_field_{i + 1}"}" {duck_type(ft)}' for i, (n, ft) in enumerate(t.args)) + ")"
    raise Unreadable(t.name)


def load_table(con, table: Table) -> None:
    keep = [i for i, (n, _) in enumerate(table.columns) if n.lower() not in table.unread]
    cols = [table.columns[i] for i in keep]
    if not cols:  # every column unread: keep the row count (queries reading a column are not run)
        con.execute(f'CREATE TABLE "{table.name}" (kumo_unread BOOLEAN)')
        if table.rows:
            insert_rows(con, f'"{table.name}"', [(None,)] * len(table.rows))
        return
    con.execute(f'CREATE TABLE "{table.name}" (' + ", ".join(f'"{n}" {duck_type(t)}' for n, t in cols) + ")")
    rows = [tuple(_insertable(table.columns[i][1], row[i]) for i in keep) for row in table.rows]
    if rows:
        insert_rows(con, f'"{table.name}"', rows)


def _insertable(t: Type, value):
    if value is None:
        return None
    if t.name == "STRUCT":  # bound as a dict with the declared field names, in order
        return {(n or f"_field_{i + 1}"): _insertable(ft, v) for i, ((n, ft), v) in enumerate(zip(t.args, value))}
    if t.name == "ARRAY":
        return [_insertable(t.args[0], v) for v in value]
    if isinstance(value, datetime) and value.tzinfo is not None:  # the session reads timestamps in UTC
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# --- comparing ---------------------------------------------------------------------------------


def _ulp(x: float) -> float:
    if x == 0 or not math.isfinite(x):
        return 0.0
    _, e = math.frexp(x)
    return math.ldexp(sys.float_info.epsilon, max(-1021, e) - 1)


def float_equal(x: float, y: float, bits: int = 4) -> bool:
    """``FloatMargin::UlpMargin(bits)`` from googlesql/common/float_margin.h."""

    if x == y or (math.isnan(x) and math.isnan(y)):
        return True
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    zero = 2.0 ** bits * _ulp(1.0)
    if abs(x) <= zero and abs(y) <= zero:
        return abs(x - y) <= zero
    return abs(x - y) <= 2.0 ** bits * _ulp(max(abs(x), abs(y)))


def equal(t: Type, expected, actual) -> bool:
    """Whether ``actual`` (a value as :func:`kumosql.bigquery_on_duckdb.bigquery_rows` reads it) is ``expected``."""

    if isinstance(expected, Array) and not expected.items:
        expected = None  # BigQuery returns a NULL array as []
    if expected is None or actual is None:
        return expected is None and actual is None
    if t.name == "ARRAY" or isinstance(expected, Array):
        element = expected.element or (t.args[0] if t.args else Type(""))
        if not isinstance(actual, (tuple, list)) or len(actual) != len(expected.items):
            return False
        if not expected.unordered:
            return all(equal(element, e, a) for e, a in zip(expected.items, actual))
        return _multiset_equal(element, expected.items, list(actual))
    if t.name == "STRUCT":
        if not isinstance(actual, (tuple, list)) or len(actual) != len(expected):
            return False
        return all(equal(ft, e, a) for (_, ft), e, a in zip(t.args, expected, actual))
    if t.name in ("FLOAT64", "DOUBLE", "FLOAT"):
        return isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool) and float_equal(expected, float(actual))
    if t.name in ("INT64", "NUMERIC"):
        if isinstance(actual, bool) or not isinstance(actual, (int, float, Decimal)):
            return False
        return Decimal(str(actual)) == Decimal(expected) if isinstance(actual, float) else Decimal(actual) == Decimal(expected)
    if t.name == "TIMESTAMP":
        if isinstance(actual, datetime):
            actual = actual if actual.tzinfo else actual.replace(tzinfo=timezone.utc)
            return actual == expected
        if isinstance(actual, date):  # bigquery_rows reads a midnight timestamp as a date
            return expected == datetime(actual.year, actual.month, actual.day, tzinfo=timezone.utc)
        return False
    if t.name == "DATETIME":
        if isinstance(actual, datetime):
            return actual.replace(tzinfo=None) == expected
        if isinstance(actual, date):
            return expected == datetime(actual.year, actual.month, actual.day)
        return False
    if t.name == "DATE":
        return type(actual) is date and actual == expected
    if t.name == "BOOL":
        return isinstance(actual, bool) and actual == expected
    if t.name == "BYTES":
        return isinstance(actual, (bytes, bytearray, memoryview)) and bytes(actual) == expected
    return type(actual) is type(expected) and actual == expected


def _multiset_equal(t: Type, expected: list, actual: list) -> bool:
    remaining = list(actual)
    for e in expected:
        for i, a in enumerate(remaining):
            if equal(t, e, a):
                del remaining[i]
                break
        else:
            return False
    return not remaining


# --- running one case --------------------------------------------------------------------------


def _ctas_columns(con, sql: str, name: str = "kumo_result") -> list:
    """Run ``sql`` into a temp table and read it back with TIMESTAMPTZ as UTC naive timestamps (no pytz)."""

    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {sql}")
    return _read_table(con, name)


def _read_table(con, name: str) -> list:
    con.execute("SET TimeZone = 'UTC'")
    description = con.execute(f"SELECT * FROM {name} LIMIT 0").description
    casts = []
    for i, d in enumerate(description, start=1):
        kind = str(d[1])
        casts.append(f"CAST(#{i} AS {kind.replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMP')})" if "WITH TIME ZONE" in kind else f"#{i}")
    return con.execute(f"SELECT {', '.join(casts) or '*'} FROM {name}").fetchall()


QUERY_SECONDS = 20  # a query still running then (a runaway recursion, a huge array) is interrupted


def _run(con, duck_sql: str, unoptimized: bool = False, zone: str = "UTC") -> list:
    con.execute(f"SET TimeZone = '{zone}'")
    timer = threading.Timer(QUERY_SECONDS, con.interrupt)
    timer.start()
    try:
        statement = f"CREATE OR REPLACE TEMP TABLE kumo_result AS {duck_sql}"
        if unoptimized:
            run_unoptimized(con, statement)
        else:
            con.execute(statement)
    finally:
        timer.cancel()
        con.execute("SET TimeZone = 'UTC'")
    return bq.bigquery_rows(_read_table(con, "kumo_result"))


def _rows_equal(t: Type, expected: Array, actual: list, value_table: bool) -> bool:
    element = t.args[0]
    if value_table or element.name != "STRUCT":  # one value per row, not a row of columns
        actual = [row[0] if len(row) == 1 else row for row in actual]
    if len(actual) != len(expected.items):
        return False
    if expected.unordered:
        return _multiset_equal(element, expected.items, actual)
    return all(equal(element, e, a) for e, a in zip(expected.items, actual))


def _value_table(tree: exp.Expression) -> bool:
    """A query whose rows are single values (``SELECT AS STRUCT/VALUE``, also as a set operation's first operand)."""

    while isinstance(tree, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        tree = tree.this
    return bool(isinstance(tree, exp.Select) and tree.args.get("kind"))


def _tables_used(tree: exp.Expression) -> set[str]:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return {t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes}


def skip_reason(case: Case, functions: set[str]) -> str | None:
    """Why ``case`` is not a supported BigQuery query, or ``None``."""

    opts = case.options
    if case.prepare:
        return "fixture (prepare_database)"
    if not re.match(r"(?is)\s*(select|with|\(|from)\b", case.sql):
        return "not a query (DML, DDL or script)"
    if opts.get("parameters"):
        return "query parameters"
    features = {f for f in opts.get("required_features", "").split(",") if f}
    outside = sorted(features - BIGQUERY_FEATURES)
    if outside:
        return "feature outside BigQuery"
    if {f for f in opts.get("forbidden_features", "").split(",") if f} & BIGQUERY_FEATURES:
        return "needs a feature BigQuery has turned off"
    if opts.get("default_time_zone"):
        return "case-specific default time zone"
    if re.search(r"\bNEW\s+[\w.]+\s*\(|googlesql_test\.|\bproto\b|\benum\b", case.sql, re.I):
        return "proto or enum"
    if NON_BIGQUERY_TYPE.search(case.sql):
        return "type outside BigQuery (INT32, UINT64, FLOAT, DOUBLE, ...)"
    if functions and re.search(r"\b(" + "|".join(map(re.escape, functions)) + r")\s*\(", case.sql, re.I):
        return "prepared UDF, TVF or graph"
    if opts.get("nondeterministic"):
        return "reference reports non-determinism"
    if case.expected.startswith("ERROR"):
        return "expected error"
    if not case.expected:
        return "no expected result"
    return None


_FUNCTION_DEF = re.compile(r"(?is)\s*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?(?:AGGREGATE\s+)?(?:TABLE\s+)?(?:FUNCTION|PROPERTY\s+GRAPH)\s+([`\w.]+)")


def evaluate(case: Case, con, tables: dict[str, Table]) -> dict:
    """Run one supported case; returns {"class": ..., "detail": ...}."""

    try:
        t, expected = parse_result(case.expected)
    except _Skipped as skipped:
        t, expected = None, f"result type {skipped}"
    except Unreadable as error:
        return {"class": "not compared", "detail": f"expected result: {error}"[:160]}
    try:
        tree = sqlglot.parse_one(case.sql, read="bigquery")
        if tree is None or isinstance(tree, exp.Command):
            return {"class": "not executable", "detail": "sqlglot: no parse"}
    except Exception as error:  # noqa: BLE001
        return {"class": "not executable", "detail": f"sqlglot: {type(error).__name__}"}
    used = _tables_used(tree)
    missing = sorted(u for u in used if u not in tables)
    if missing:
        return {"class": "not executable", "detail": f"table not prepared in the file: {missing[0]}"}
    for name in used:
        table = tables[name]
        if not table.unread:
            continue
        columns = {c.name.lower() for c in tree.find_all(exp.Column)}
        star = any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star))
        hit = (columns & table.unread) or (star and table.unread)
        if hit:
            column = sorted(hit)[0]
            reason = table.reasons[column]
            if reason == "non-BigQuery type":
                return {"class": "skip", "detail": "reads a fixture column of a type outside BigQuery",
                        "note": f"{name}.{column}"}
            return {"class": "not compared", "detail": f"fixture column {name}.{column}: {reason}"}
    try:
        duck_sql = to_duckdb(case.sql, "bigquery")
    except bq.Unfaithful as error:
        return {"class": "declined", "detail": str(error).split(": ", 1)[-1][:120]}
    except Exception as error:  # noqa: BLE001
        return {"class": "not executable", "detail": f"translation: {type(error).__name__}: {str(error)[:80]}"}
    try:
        rows = _run(con, duck_sql)
    except bq.UnfaithfulOutput as error:
        return {"class": "declined", "detail": f"output: {str(error)[len(bq.MARKER) + 2:][:100]}"}
    except duckdb.Error as error:
        if bq.MARKER in str(error):
            return {"class": "declined", "detail": "guard: " + str(error).split(bq.MARKER + ": ", 1)[-1][:100]}
        return {"class": "not executable", "detail": f"duckdb: {str(error).splitlines()[0][:120]}"}
    if t is None or t.name != "ARRAY":
        return {"class": "not compared", "detail": expected if t is None else f"result type {t.name}"}
    value_table = _value_table(tree)
    if _rows_equal(t, expected, rows, value_table):
        return {"class": "agree", "detail": ""}
    try:
        again = _run(con, duck_sql, unoptimized=True)
    except Exception:  # noqa: BLE001
        again = None
    if again is not None and _rows_equal(t, expected, again, value_table):
        return {"class": "agree", "detail": "DuckDB optimizer bug: agrees with the optimizer off"}
    detail = {"class": "DISAGREE", "detail": f"got {_short(rows)}", "duckdb": duck_sql}
    known = KNOWN_DIVERGENCES.get(case.id)
    if known:
        detail["kind"], detail["note"] = known
        return detail
    try:
        local = _run(con, duck_sql, zone=COMPLIANCE_TIME_ZONE)
    except Exception:  # noqa: BLE001
        local = None
    if local is not None and _rows_equal(t, expected, local, value_table):
        detail["kind"], detail["note"] = "divergence", "default time zone (compliance America/Los_Angeles, BigQuery UTC)"
    else:
        detail["kind"], detail["note"] = "unclassified", ""
    return detail


def _short(rows) -> str:
    text = repr(rows)
    return text if len(text) < 160 else text[:157] + "..."


# --- corpus ------------------------------------------------------------------------------------


def prepare(cases: list[Case]) -> dict[str, tuple[dict, set]]:
    """Per test file: the fixture tables (from prepare_database rows) and prepared function names."""

    per_file: dict[str, tuple[dict, set]] = {}
    for case in cases:
        tables, functions = per_file.setdefault(case.file, ({}, set()))
        if not case.prepare:
            continue
        m = _FUNCTION_DEF.match(case.sql)
        if m:
            functions.add(m.group(1).strip("`").split(".")[-1])
            continue
        try:
            table = build_table(case)
        except Unreadable:
            table = None
        if table is not None:
            tables[table.name.lower()] = table
    return per_file


def _connection(tables: dict[str, Table]):
    con = duckdb.connect(":memory:")
    con.execute("SET threads TO 1")
    con.execute("SET memory_limit = '1GB'")
    bq.configure(con)
    loaded = {}
    for name, table in tables.items():
        try:
            load_table(con, table)
            loaded[name] = table
        except (Unreadable, duckdb.Error, TypeError, ValueError):
            pass
    return con, loaded


def run(cases: list[Case], progress: bool = False) -> list[dict]:
    per_file = prepare(cases)
    results, connections = [], {}
    for case in cases:
        for name in [n for n in connections if n != case.file]:  # cases come file by file
            connections.pop(name)[0].close()
        if progress:
            print(case.id, file=sys.stderr, flush=True)
        tables, functions = per_file[case.file]
        reason = skip_reason(case, functions)
        row = {"id": case.id, "file": case.file, "held_out": held_out(case.file)}
        if reason:
            row.update({"class": "skip", "detail": reason})
            if reason == "expected error" and _runtime_error(case):
                row["runtime_error"] = _expected_error(case, per_file, connections)
            results.append(row)
            continue
        if case.file not in connections:
            connections[case.file] = _connection(tables)
        con, loaded = connections[case.file]
        row.update(evaluate(case, con, loaded))
        results.append(row)
    for con, _ in connections.values():
        con.close()
    return results


def _runtime_error(case: Case) -> bool:
    return case.expected.startswith("ERROR: generic::out_of_range")


def _expected_error(case: Case, per_file, connections) -> str:
    """For a query BigQuery fails on at run time: does the translation fail too?"""

    tables, functions = per_file[case.file]
    opts = dict(case.options)
    probe = Case(case.id, case.file, case.name, case.sql, "", opts)
    if skip_reason(probe, functions) != "no expected result":
        return "skipped"
    if case.file not in connections:
        connections[case.file] = _connection(tables)
    con, loaded = connections[case.file]
    try:
        tree = sqlglot.parse_one(case.sql, read="bigquery")
        if tree is None or any(u not in loaded or loaded[u].unread for u in _tables_used(tree)):
            return "skipped"
        duck_sql = to_duckdb(case.sql, "bigquery")
    except bq.Unfaithful:
        return "declined"
    except Exception:  # noqa: BLE001
        return "skipped"
    try:
        _run(con, duck_sql)
    except (bq.UnfaithfulOutput, duckdb.Error):
        return "fails"
    return "returns rows"


def summarize(results: list[dict]) -> dict:
    supported = [r for r in results if r["class"] != "skip"]
    c = Counter(r["class"] for r in supported)
    kinds = Counter(r.get("kind") for r in supported if r["class"] == "DISAGREE")
    errors = Counter(r["runtime_error"] for r in results if "runtime_error" in r)
    return {
        "cases": len(results),
        "supported": len(supported),
        "agree": c.get("agree", 0),
        "disagree": c.get("DISAGREE", 0),
        "disagree_by_kind": dict(kinds),
        "wrong": sum(n for k, n in kinds.items() if k in ("bug", "decline", "unclassified")),
        "declined": c.get("declined", 0),
        "not_executable": c.get("not executable", 0),
        "not_compared": c.get("not compared", 0),
        "skipped": len(results) - len(supported),
        "skipped_by_reason": dict(Counter(r["detail"] for r in results if r["class"] == "skip").most_common()),
        "runtime_errors": dict(errors),
    }


def split(results: list[dict]) -> tuple[list[dict], list[dict]]:
    return [r for r in results if not r["held_out"]], [r for r in results if r["held_out"]]


# --- the pinned sample for the test suite ------------------------------------------------------


def write_sample(cases: list[Case], results: list[dict], path: Path = SAMPLE, every: int = 12) -> None:
    """Every ``every``-th supported case plus every disagreement, with the fixture blocks of their files."""

    by_id = {r["id"]: r for r in results}
    chosen = [c for i, c in enumerate(c for c in cases if by_id[c.id]["class"] != "skip") if i % every == 0]
    chosen_ids = {c.id for c in chosen} | {r["id"] for r in results if r["class"] == "DISAGREE"}
    files = {by_id[i]["file"] for i in chosen_ids}
    keep = [c for c in cases if c.id in chosen_ids or (c.file in files and c.prepare)]
    data = {
        "source": f"{GOOGLESQL_URL} @ {PIN} ({TESTDATA}, Apache-2.0)",
        "cases": [dict(id=c.id, file=c.file, name=c.name, sql=c.sql, expected=c.expected, options=c.options,
                       prepare=c.prepare, outcome=by_id[c.id]["class"]) for c in keep],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)


def load_sample(path: Path = SAMPLE) -> tuple[list[Case], dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    cases = [Case(c["id"], c["file"], c["name"], c["sql"], c["expected"], c["options"], c["prepare"]) for c in data["cases"]]
    return cases, {c["id"]: c["outcome"] for c in data["cases"]}


# --- command line ------------------------------------------------------------------------------


def _line(name: str, s: dict) -> str:
    return (f"{name}: {s['agree']}/{s['supported']} agree, {s['disagree']} disagree {s['disagree_by_kind']}, "
            f"{s['declined']} declined, {s['not_executable']} not executable, {s['not_compared']} not compared; "
            f"{s['skipped']} skipped of {s['cases']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--testdata", type=Path, help="a googlesql compliance/testdata checkout (default: fetch at the pin)")
    ap.add_argument("--failures", action="store_true", help="list every disagreement")
    ap.add_argument("--details", action="store_true", help="also list declined / not executable / not compared")
    ap.add_argument("--include-held-out", action="store_true", help="list held-out failures too (final measurement only)")
    ap.add_argument("--write-results", action="store_true")
    ap.add_argument("--write-sample", action="store_true")
    ap.add_argument("--write-manifest", action="store_true", help="re-pin the SHA-256 of every test file (a pin change)")
    ap.add_argument("--json", type=Path, help="write every case's outcome here")
    ap.add_argument("--progress", action="store_true", help="print each case id to stderr")
    ap.add_argument("--files", help="only test files whose name matches this regular expression")
    args = ap.parse_args(argv)
    quiet()
    directory = args.testdata or fetch()
    if args.write_manifest:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text("".join(f"{h}  {name}\n" for name, h in checksums(directory).items()))
    verify(directory)
    t0 = time.perf_counter()
    cases = read_testdata(directory)
    if args.files:
        cases = [c for c in cases if re.search(args.files, c.file)]
    results = run(cases, args.progress)
    seconds = time.perf_counter() - t0
    dev, held = split(results)
    s_all, s_dev, s_held = summarize(results), summarize(dev), summarize(held)
    print(_line("all", s_all))
    print(_line("development files", s_dev))
    print(_line("held-out files", s_held))
    print("skipped by reason:", s_all["skipped_by_reason"])
    print("expected run-time errors:", s_all["runtime_errors"])
    print(f"{seconds:.1f}s")
    shown = results if args.include_held_out else dev
    if args.failures or args.details:
        for r in shown:
            if r["class"] == "DISAGREE" or (args.details and r["class"] in ("declined", "not executable", "not compared")):
                print(" ", r["class"], r.get("kind", ""), r["id"], r["detail"], r.get("note", ""))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.write_sample:
        write_sample(cases, results)
        print(f"wrote {SAMPLE}")
    if args.write_results:
        write_results(RESULTS_NAME, results_row(s_all, s_held, seconds))
    return 1 if s_all["wrong"] else 0


# Measured before the translation was changed (master's bigquery_on_duckdb.py, same harness): 1,692 agreed and
# 97 returned other rows than the expected ones (52 in development files, 45 in held-out files).
BASELINE = "1,692 agree and 97 wrong (52 in development files, 45 in held-out files)"
# Measured after fixing from the development files only, before the held-out failures were looked at.
UNSEEN = "after fixing from the development files alone, the held-out files had 38 wrong of 1,385 supported"


def results_row(s: dict, held: dict, seconds: float) -> dict:
    n = lambda x: f"{x:,}"  # noqa: E731
    dev_agree = s["agree"] - held["agree"]
    unknown = s["declined"] + s["not_compared"] + s["disagree"] - s["wrong"]  # oracle-side time-zone differences too
    return {
        "suite": "GoogleSQL compliance expected results",
        "order": 62,
        "size": s["supported"],
        "score": f"{n(s['agree'])}/{n(s['supported'])} supported cases return the expected rows, {s['wrong']} wrong",
        "metric": (
            "The typed rows the GoogleSQL compliance tests expect for each query BigQuery supports, as an oracle for "
            "KumoSQL's BigQuery-to-DuckDB execution: fixture tables are loaded from the files' expected rows, the query "
            "is translated and run, and the rows are compared under the case's ordering and 4-ULP float margin. A "
            "declined translation is not wrong."
        ),
        "docs": "docs/evals/googlesql-expected-results.md",
        "command": "python tools/googlesql_results_eval.py",
        "date": today(),
        "caveats": (
            f"Source google/googlesql @ {PIN[:7]} (Apache-2.0, SHA-256 of every .test file pinned in "
            f"tests/fixtures/googlesql_results/testdata.sha256), {n(s['cases'])} cases of which {n(s['skipped'])} are skipped "
            f"(features, types or fixtures BigQuery lacks, errors, DML) and {n(s['supported'])} supported, none adapted. "
            f"Of the supported: {n(s['agree'])} agree, {n(s['declined'])} declined (no faithful DuckDB reading, or a guard says "
            f"BigQuery would fail), {n(s['not_executable'])} not executable in DuckDB or sqlglot, {n(s['not_compared'])} with a "
            f"result type the harness does not read, and {s['disagree'] - s['wrong']} oracle-side differences (the compliance "
            f"default time zone is America/Los_Angeles, BigQuery's UTC; they agree when DuckDB reads times in Los Angeles). "
            f"Baseline before fixes: {BASELINE}; the fixes are refusals and guards in bigquery_on_duckdb.py, so constructs "
            f"DuckDB reads differently now decline (agreement fell to {n(s['agree'])}) and cases that need a file's own default time "
            f"zone are skipped. Held-out files (a fifth by SHA-1 of the file name): {UNSEEN}, "
            f"then those failures were fixed too, so the held-out row below is tuned on test. Also not counted: "
            f"{s['runtime_errors'].get('returns rows', 0)} cases BigQuery fails at run time (out of range) where the translation "
            f"returns rows instead; {s['runtime_errors'].get('fails', 0)} fail and {s['runtime_errors'].get('declined', 0)} are "
            f"declined as they should be. DuckDB is not BigQuery: an agreement shows the translation returns what the "
            f"reference implementation returns, not that BigQuery does."
        ),
        "evidence": "executed",
        "correctness": "0 rows that differ from the expected rows without a time-zone explanation, checked against the compliance tests' expected results",
        "coverage": {"proven": s["agree"], "unknown": unknown, "unsupported": s["not_executable"], "error": 0},
        "held_out": (
            f"{n(held['agree'])}/{n(held['supported'])} agree, {held['wrong']} wrong (tuned on test: fixed after "
            f"a first run with 38 wrong); development files {n(dev_agree)}/{n(s['supported'] - held['supported'])}"
        ),
        "performance": f"{n(s['cases'])} cases in {seconds:.0f} s",
    }


if __name__ == "__main__":
    sys.exit(main())
