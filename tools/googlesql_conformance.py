"""GoogleSQL conformance eval for KumoSQL's pure-Python evaluator (:mod:`kumosql.gsql_eval`).

The GoogleSQL compliance tests (google/googlesql, formerly ZetaSQL, Apache-2.0) give a query, the
tables it reads and the result the reference implementation returns. Each case in the claimed subset
is run on the evaluator and scored:

* **exact**: the same result (rows, column types, order where the result is ordered) or the same
  kind of runtime error, compared the way the compliance driver compares (floats within 4 ULPs,
  rows and arrays of unknown order as multisets);
* **unsupported**: the evaluator declined (:class:`kumosql.gsql_eval.Unsupported`);
* **mismatch**: anything else. This must stay at 0: the evaluator either matches or declines.

    python tools/googlesql_conformance.py                    # dev split, summary
    python tools/googlesql_conformance.py --mismatches       # every mismatch, with expected, actual and traceback
    python tools/googlesql_conformance.py --failures         # one line per case that is not exact
    python tools/googlesql_conformance.py --file strings     # one test file (repeatable; a name or a prefix*)
    python tools/googlesql_conformance.py --by-file          # per-file breakdown
    python tools/googlesql_conformance.py --reasons 60       # more of the unsupported reasons
    python tools/googlesql_conformance.py --json             # everything as JSON on stdout
    python tools/googlesql_conformance.py --split heldout    # measured once, never developed on
    python tools/googlesql_conformance.py --harvest DIR      # rebuild the fixture from testdata/

Exit status is 1 when there is any mismatch (or an expected result this runner cannot read), else 0.

The fixture (``tests/fixtures/googlesql_conformance``) keeps every case of every ``.test`` file at
the pinned commit, split by file into dev and held-out (a quarter of the files, chosen by a hash of
the file name before any function was written). The claimed subset is decided here, from each
case's declared features, not from how the evaluator does on it.
"""

from __future__ import annotations

import argparse
import gzip
import multiprocessing
import os
import signal
import traceback
import hashlib
import json
import math
import re
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = ROOT / "tests" / "fixtures" / "googlesql_conformance"
SOURCE = "google/googlesql @ d82db99 (googlesql/compliance/testdata/*.test, Apache-2.0)"
SPLIT_SALT = "googlesql-conformance:"


# --- harvesting ----------------------------------------------------------------------------------


def split_of(stem: str) -> str:
    """``heldout`` for a quarter of the files (by a hash of the name), ``dev`` for the rest."""

    digest = int(hashlib.sha256((SPLIT_SALT + stem).encode()).hexdigest(), 16)
    return "heldout" if digest % 4 == 0 else "dev"


def _options(lines: list[str]) -> tuple[dict[str, str], int]:
    """The ``[key=value]`` options at the top of a block (an option may span lines) and where the body starts."""

    options: dict[str, str] = {}
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
        for match in re.finditer(r"\[([^\[\]=]*)(?:=((?:[^\[\]]|\[[^\[\]]*\])*))?\]", text, re.S):
            options[match.group(1).strip()] = (match.group(2) or "").strip()
    return options, i


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("\\") else line


def parse_test_file(text: str) -> dict:
    """A compliance ``.test`` file as ``{"defaults", "prepare", "cases"}``, keeping the raw text of each part."""

    defaults: dict[str, str] = {}
    prepare: list[dict] = []
    cases: list[dict] = []
    for block in re.split(r"\n==\n", "\n" + text.replace("\r\n", "\n") + "\n"):
        lines = block.strip("\n").split("\n")
        options, start = _options(lines)
        body = [_unescape(line) for line in lines[start:] if not line.startswith("#")]
        if not body or not any(line.strip() for line in body):
            for key, value in options.items():
                if key.startswith("default "):
                    defaults[key[len("default "):].strip()] = value
                elif key.startswith("default"):
                    defaults[key[len("default"):].strip()] = value
            continue
        sections: list[list[str]] = [[]]
        for line in body:
            if line == "--":
                sections.append([])
            else:
                sections[-1].append(line)
        sql = "\n".join(sections[0]).strip()
        expected = ["\n".join(s).strip() for s in sections[1:]]
        entry = {"options": options, "sql": sql, "expected": expected}
        if "prepare_database" in options:
            prepare.append(entry)
        else:
            entry["name"] = options.get("name", "")
            cases.append(entry)
    return {"defaults": defaults, "prepare": prepare, "cases": cases}


def harvest(directory: Path, out: Path = FIXTURE) -> dict[str, int]:
    files = {"dev": [], "heldout": []}
    for path in sorted(directory.glob("*.test")):
        parsed = parse_test_file(path.read_text(encoding="utf-8", errors="replace"))
        parsed["file"] = path.stem
        files[split_of(path.stem)].append(parsed)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split, entries in files.items():
        with gzip.open(out / f"{split}.json.gz", "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump({"source": SOURCE, "split": split, "files": entries}, handle, separators=(",", ":"), sort_keys=True)
        counts[split] = sum(len(f["cases"]) for f in entries)
    return counts


def load(split: str = "dev") -> list[dict]:
    with gzip.open(FIXTURE / f"{split}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)["files"]


# --- the claimed subset --------------------------------------------------------------------------
#
# Fixed before the evaluator had any functions: the BigQuery language. A case is in the subset when it
# is a query (not DML, DDL or a graph query), every feature it requires is one BigQuery has, it names
# no GoogleSQL-only type, and its file is not about a GoogleSQL-only topic. Cases in the subset that
# the evaluator declines count as unsupported, never as excluded.

CLAIMED_FEATURES = frozenset(
    """
    ANALYTIC_FUNCTIONS NUMERIC_TYPE BIGNUMERIC_TYPE CIVIL_TIME INTERVAL_TYPE HAVING_IN_AGGREGATE
    SAFE_FUNCTION_CALL ORDER_BY_IN_AGGREGATE LIMIT_IN_AGGREGATE NULL_HANDLING_MODIFIER_IN_AGGREGATE
    NULL_HANDLING_MODIFIER_IN_ANALYTIC NULLS_FIRST_LAST_IN_ORDER_BY WITH_RECURSIVE WITH_ON_SUBQUERY
    GROUP_BY_STRUCT GROUP_BY_ARRAY GROUPING_SETS GROUP_BY_ROLLUP GROUPING_BUILTIN GROUP_BY_ALL
    MULTI_GROUPING_SETS QUALIFY PIVOT UNPIVOT BY_NAME CORRESPONDING CORRESPONDING_FULL
    LIKE_ANY_SOME_ALL LIKE_ANY_SOME_ALL_ARRAY ENFORCE_CONDITIONAL_EVALUATION IS_DISTINCT
    """.split()
)

# Topics BigQuery does not share with GoogleSQL (or that only exist as engine extensions there).
EXCLUDED_FILES = re.compile(
    r"^(dml_|graph_|json_|proto|match_recognize|pipe_|anonymization|differential_privacy|aggregation_threshold|kll_|"
    r"analytic_kll|approx_|analytic_approx|analytic_hll|hll_|map_|vector|uuid|new_uuid|generate_uuid|rand$|tablesample|"
    r"range|orderby_range|compression|keys|aead|typeof|call_sql_|hop_tvf|tumble_tvf|align_operator|pipe_align|"
    r"authorization_|collation|orderby_collate|elementwise_|apply_lambda|array_functions_with_lambda|filter_fields|"
    r"replace_fields|multi_level_aggregation|group_rows|pico_timestamp|nano_timestamp|unnest_multiway|lateral_join|"
    r"enum_|cast_function_to_json|ai_functions|measure|top_level_table_statement|generalized_statement|invoke_view|"
    r"hints|no_tests)"
)

# Type names BigQuery does not have (sqlglot reads some of them as BigQuery types, e.g. INT32 as INT64).
GOOGLESQL_ONLY_TYPES = re.compile(
    r"(?i)\b(int32|uint32|uint64|float32|float|proto|enum|new|map\s*<|range\s*<|uuid|graph_table|graph|json|"
    r"googlesql_test|kitchensinkpb|tokenlist|vector)\b"
)
QUERY_START = re.compile(r"(?is)^\s*(\(\s*)*(select|with)\b")


@dataclass
class Case:
    file: str
    name: str
    sql: str
    options: dict
    expected: str
    tables: list[dict]  # the file's prepare_database blocks
    claimed: bool
    reason: str = ""


def _features(options: dict, key: str) -> set[str]:
    return {x.strip() for x in options.get(key, "").split(",") if x.strip()}


def claim(file: str, sql: str, options: dict) -> tuple[bool, str]:
    """Whether a case is in the claimed subset, and why not."""

    if EXCLUDED_FILES.match(file):
        return False, "GoogleSQL-only topic"
    if not QUERY_START.match(sql):
        return False, "not a query"
    missing = _features(options, "required_features") - CLAIMED_FEATURES
    if missing:
        return False, "feature " + ",".join(sorted(missing))
    if _features(options, "forbidden_features") & CLAIMED_FEATURES:
        return False, "result holds only without a claimed feature"
    if GOOGLESQL_ONLY_TYPES.search(sql) or GOOGLESQL_ONLY_TYPES.search(options.get("parameters", "")):
        return False, "GoogleSQL-only type"
    if not options.get("name"):
        return False, "unnamed"
    return True, ""


def cases(split: str = "dev", only_file: str | None = None) -> list[Case]:
    out = []
    for entry in load(split):
        if only_file and entry["file"] != only_file:
            continue
        # A ``default <key>`` option applies to the rest of its file, whichever block carries it; the
        # file-wide ``default_time_zone`` lives on a prepare block.
        carried = dict(entry["defaults"])
        for block in entry["prepare"]:
            if "default_time_zone" in block["options"]:
                carried["default_time_zone"] = block["options"]["default_time_zone"]
        for case in entry["cases"]:
            for key, value in case["options"].items():
                if key.startswith("default ") and key != "default global_labels":
                    carried[key[len("default "):].strip()] = value
            options = {**carried, **case["options"]}
            if "required_features" in carried and "required_features" not in case["options"]:
                options["required_features"] = carried["required_features"]
            expected = case["expected"][0] if case["expected"] else ""
            claimed, reason = claim(entry["file"], case["sql"], options)
            out.append(Case(entry["file"], case["name"], case["sql"], options, expected, entry["prepare"], claimed, reason))
    return out


# --- expected results ----------------------------------------------------------------------------
#
# The compliance files print a result as ``ARRAY<STRUCT<a INT64, b STRING>>[known order:{1, "x"}, ...]``:
# the type of the rows, then the rows. Values follow the header's types (a nested array repeats its own
# type, ``ARRAY<INT64>[unknown order:1, 2]``, because the header only says ``ARRAY<>``). Payloads are the
# ones ``kumosql.gsql_eval.values`` defines.

from kumosql.gsql_eval import types as T  # noqa: E402
from kumosql.gsql_eval import values as V  # noqa: E402


class ExpectedError(Exception):
    """The expected-result text is not in a form this runner reads."""


class NanoPrecision(ExpectedError):
    """The expected result has sub-microsecond digits (GoogleSQL's nanosecond types); BigQuery has none."""


class Raw(str):
    """The printed text of a value of a type the evaluator does not model (a protocol buffer, a range...)."""

    __slots__ = ()


@dataclass
class Expected:
    kind: str  # "rows" or "error"
    type: T.Type | None = None  # the type of one row (a STRUCT) or of one value (a value table)
    rows: list = field(default_factory=list)
    unordered: bool = False  # the printed rows say ``unknown order``
    code: str = ""  # an error's status code (``out_of_range``)
    message: str = ""
    nondeterministic: bool = False  # the reference reported non-determinism: the rows are one possible answer


_PREFIX = re.compile(r"(ARRAY|STRUCT|PROTO|ENUM|RANGE|MAP)\s*<")
_OPENERS = "([{"
_CLOSERS = ")]}"
_PLACEHOLDER = "?"


def _placeholder(t: T.Type) -> bool:
    return t.kind == "OTHER" and t.label == _PLACEHOLDER


def has_placeholder(t: T.Type) -> bool:
    if _placeholder(t):
        return True
    if t.kind == "ARRAY":
        return has_placeholder(t.elem)
    if t.kind == "STRUCT":
        return any(has_placeholder(ft) for _, ft in t.fields)
    return False


def merge_type(known: T.Type, seen: T.Type) -> T.Type:
    """``known`` with its ``ARRAY<>`` placeholders filled in from ``seen`` (a type read from a value)."""

    if not has_placeholder(known):
        return known
    if _placeholder(known):
        return seen
    if known.kind == "ARRAY" and seen.kind == "ARRAY":
        return T.Type("ARRAY", elem=merge_type(known.elem, seen.elem))
    if known.kind == "STRUCT" and seen.kind == "STRUCT" and len(known.fields) == len(seen.fields):
        return T.struct((n, merge_type(a, b)) for (n, a), (_, b) in zip(known.fields, seen.fields))
    return known


def parse_header_type(text: str) -> T.Type:
    """A type as the compliance printer writes it (``STRUCT<a INT64, ARRAY<>>``; a bare ``ARRAY<>`` is a placeholder)."""

    pos = 0
    n = len(text)

    def fail(message: str):
        raise ExpectedError(f"type {text[:80]!r}: {message}")

    def skip():
        nonlocal pos
        while pos < n and text[pos].isspace():
            pos += 1

    def word() -> str:
        nonlocal pos
        skip()
        start = pos
        while pos < n and (text[pos].isalnum() or text[pos] in "_.$"):
            pos += 1
        return text[start:pos]

    def balanced() -> str:
        """The raw text of ``<...>`` starting at ``pos`` (which is the ``<``)."""

        nonlocal pos
        start = pos
        depth = 0
        while pos < n:
            depth += {"<": 1, ">": -1}.get(text[pos], 0)
            pos += 1
            if depth == 0:
                return text[start:pos]
        fail("unbalanced <>")

    def one() -> T.Type:
        nonlocal pos
        name = word()
        if not name:
            fail(f"a type name expected at {pos}")
        upper = name.upper()
        skip()
        has_args = pos < n and text[pos] == "<"
        if upper == "ARRAY" and has_args:
            pos += 1
            skip()
            if pos < n and text[pos] == ">":
                pos += 1
                return T.Type("ARRAY", elem=T.other(_PLACEHOLDER))
            elem = one()
            skip()
            if pos >= n or text[pos] != ">":
                fail("'>' expected")
            pos += 1
            return T.Type("ARRAY", elem=elem)
        if upper == "STRUCT" and has_args:
            pos += 1
            fields = []
            while True:
                skip()
                if pos >= n:
                    fail("unterminated STRUCT")
                if text[pos] == ">":
                    pos += 1
                    break
                save = pos
                first = word()
                skip()
                if first and pos < n and text[pos] not in ",><":
                    fields.append((first, one()))  # a name, then its type
                else:
                    pos = save
                    fields.append((None, one()))
                skip()
                if pos < n and text[pos] == ",":
                    pos += 1
            return T.struct(fields)
        if has_args:  # PROTO<..>, ENUM<..>, RANGE<..>, MAP<..>: kept as text
            return T.other(name.upper() + balanced())
        if upper in T.BY_NAME:
            return T.BY_NAME[upper]
        return T.other(upper)

    result = one()
    skip()
    if pos != n:
        fail(f"text after the type at {pos}")
    return result


_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"',
            "?": "?", "`": "`"}


def unescape_literal(body: str, is_bytes: bool) -> str | bytes:
    """The text between the quotes of a GoogleSQL string or bytes literal, escapes decoded."""

    out: list[int] = []  # code points (strings) or byte values (bytes)
    i = 0
    n = len(body)
    while i < n:
        char = body[i]
        if char != "\\":
            if is_bytes:
                out.extend(char.encode("utf-8"))
            else:
                out.append(ord(char))
            i += 1
            continue
        i += 1
        if i >= n:
            raise ExpectedError("literal ends with a backslash")
        char = body[i]
        if char in _ESCAPES:
            out.append(ord(_ESCAPES[char]))
            i += 1
        elif char in "xX":
            digits = body[i + 1:i + 3]
            if not re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                raise ExpectedError("bad \\x escape")
            out.append(int(digits, 16))
            i += 3
        elif char in "uU" and not is_bytes:
            width = 4 if char == "u" else 8
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise ExpectedError("bad unicode escape")
            out.append(int(digits, 16))
            i += 1 + width
        elif char in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i:]).group(0)
            out.append(int(digits, 8))
            i += len(digits)
        else:
            raise ExpectedError(f"unknown escape \\{char}")
    if is_bytes:
        return bytes(out)
    return "".join(map(chr, out))


_DATETIME_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d+))?$")
_TIMESTAMP_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d(?::?\d\d)?)?$")
_TIME_TEXT = re.compile(r"^(\d\d):(\d\d):(\d\d)(?:\.(\d+))?$")
_INTERVAL_TEXT = re.compile(r"^(-?)(\d+)-(\d+) (-?\d+) (-?)(\d+):(\d+):(\d+)(?:\.(\d+))?$")


def _micros(fraction: str | None) -> int:
    if not fraction:
        return 0
    if len(fraction) > 6 and fraction[6:].strip("0"):
        raise NanoPrecision("sub-microsecond value")
    return int(fraction[:6].ljust(6, "0"))


def scalar_value(kind: str, token: str) -> Any:
    """A printed scalar of type ``kind`` as its payload."""

    try:
        if kind in ("INT64", "INT32", "UINT32", "UINT64"):
            return int(token)
        if kind in ("FLOAT64", "FLOAT32"):
            return float(token)
        if kind in ("NUMERIC", "BIGNUMERIC"):
            value = Decimal(token)
            if not value.is_finite():
                raise ValueError(token)
            return value
        if kind == "BOOL":
            if token not in ("true", "false"):
                raise ValueError(token)
            return token == "true"
        if kind == "DATE":
            return date.fromisoformat(token)
        if kind == "TIME":
            m = _TIME_TEXT.match(token)
            return dtime(int(m.group(1)), int(m.group(2)), int(m.group(3)), _micros(m.group(4)))
        if kind == "DATETIME":
            m = _DATETIME_TEXT.match(token)
            y, mo, d, h, mi, s = map(int, m.groups()[:6])
            return datetime(y, mo, d, h, mi, s, _micros(m.group(7)))
        if kind == "TIMESTAMP":
            m = _TIMESTAMP_TEXT.match(token)
            y, mo, d, h, mi, s = map(int, m.groups()[:6])
            civil = datetime(y, mo, d, h, mi, s, _micros(m.group(7)))
            offset = m.group(8)
            minutes = 0
            if offset and offset != "Z":
                digits = offset[1:].replace(":", "")
                minutes = int(digits[:2]) * 60 + (int(digits[2:]) if len(digits) > 2 else 0)
                minutes = -minutes if offset[0] == "-" else minutes
            return V.utc_to_micros(civil - timedelta(minutes=minutes))
        if kind == "INTERVAL":
            m = _INTERVAL_TEXT.match(token)
            sign_ym, years, months, days, sign_t, hours, minutes, seconds, fraction = m.groups()
            total_months = int(years) * 12 + int(months)
            micros = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1_000_000 + _micros(fraction)
            return V.Interval(-total_months if sign_ym else total_months, int(days), -micros if sign_t else micros)
    except (ValueError, AttributeError, ArithmeticError, TypeError):
        raise ExpectedError(f"bad {kind} value {token[:60]!r}") from None
    raise ExpectedError(f"no reader for a {kind} value")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def fail(self, message: str):
        raise ExpectedError(f"{message} at {self.pos}: {self.text[max(0, self.pos - 20):self.pos + 30]!r}")

    def ws(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n and text[self.pos].isspace():
            self.pos += 1

    def read_type(self) -> T.Type:
        text = self.text
        start = self.pos
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "<":
                depth += 1
            elif text[i] == ">":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        else:
            self.fail("unterminated type")
        self.pos = i + 1
        return parse_header_type(text[start:i + 1])

    def at_null(self) -> bool:
        text, pos = self.text, self.pos
        return text.startswith("NULL", pos) and (pos + 4 >= len(text) or text[pos + 4] in ",}])" or text[pos + 4].isspace())

    def raw(self) -> str:
        """The text of one value of an unmodelled form: up to the next top-level ``,`` ``}`` ``]`` or ``)``."""

        text, n = self.text, len(self.text)
        start = self.pos
        depth = 0
        i = start
        while i < n:
            char = text[i]
            if char in "\"'`":
                i = self.skip_quoted(i)
                continue
            if char in _OPENERS:
                depth += 1
            elif char in _CLOSERS:
                if depth == 0:
                    break
                depth -= 1
            elif char == "," and depth == 0:
                break
            i += 1
        self.pos = i
        return text[start:i].strip()

    def skip_quoted(self, i: int) -> int:
        text = self.text
        quote = text[i]
        if text.startswith(quote * 3, i):
            end = text.find(quote * 3, i + 3)
            if end < 0:
                self.fail("unterminated string")
            return end + 3
        i += 1
        while i < len(text) and text[i] != quote:
            i += 2 if text[i] == "\\" else 1
        if i >= len(text):
            self.fail("unterminated string")
        return i + 1

    def literal(self, is_bytes: bool) -> str | bytes:
        text = self.text
        if is_bytes:
            if text[self.pos] in "bB":
                self.pos += 1
            else:
                self.fail("bytes literal expected")
        if self.pos >= len(text) or text[self.pos] not in "\"'":
            self.fail("string literal expected")
        start = self.pos
        end = self.skip_quoted(start)
        self.pos = end
        quote = text[start]
        width = 3 if text.startswith(quote * 3, start) else 1
        return unescape_literal(text[start + width:end - width], is_bytes)

    def value(self, hint: T.Type | None) -> tuple[T.Type, Any]:
        self.ws()
        text = self.text
        if _PREFIX.match(text, self.pos):
            hint = self.read_type()
            self.ws()
            if text.startswith("(NULL)", self.pos):
                self.pos += 6
                return hint, None
        if self.at_null():
            self.pos += 4
            return (hint or T.other(_PLACEHOLDER)), None
        if hint is None:
            self.fail("a value with no type")
        kind = hint.kind
        if kind == "ARRAY":
            if not text.startswith("[", self.pos):
                self.fail("array expected")
            return self.array(hint)
        if kind == "STRUCT":
            if not text.startswith("{", self.pos):
                self.fail("struct expected")
            return self.struct(hint)
        if kind == "OTHER" or kind == "JSON":
            return hint, Raw(self.raw())
        if kind == "STRING":
            return hint, self.literal(False)
        if kind == "BYTES":
            return hint, self.literal(True)
        token = self.raw()
        return hint, scalar_value(kind, token)

    def array(self, hint: T.Type) -> tuple[T.Type, Any]:
        text = self.text
        self.pos += 1
        self.ws()
        unordered = False
        for marker in ("unknown order:", "known order:"):
            if text.startswith(marker, self.pos):
                self.pos += len(marker)
                unordered = marker.startswith("unknown")
                break
        elem = hint.elem
        items = []
        while True:
            self.ws()
            if self.pos >= len(text):
                self.fail("unterminated array")
            if text[self.pos] == "]":
                self.pos += 1
                break
            seen, item = self.value(hint.elem)
            elem = merge_type(elem, seen)
            items.append(item)
            self.ws()
            if self.pos < len(text) and text[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(text) and text[self.pos] != "]":
                self.fail("',' or ']' expected")
        payload = V.UnorderedArray(items) if unordered else tuple(items)
        return T.Type("ARRAY", elem=elem), payload

    def struct(self, hint: T.Type) -> tuple[T.Type, Any]:
        text = self.text
        self.pos += 1
        fields, items = [], []
        for index, (name, field_type) in enumerate(hint.fields):
            self.ws()
            seen, item = self.value(field_type)
            fields.append((name, merge_type(field_type, seen)))
            items.append(item)
            self.ws()
            if index + 1 < len(hint.fields):
                if text[self.pos:self.pos + 1] != ",":
                    self.fail("',' expected between struct fields")
                self.pos += 1
        self.ws()
        if text[self.pos:self.pos + 1] != "}":
            self.fail("'}' expected")
        self.pos += 1
        return T.struct(fields), tuple(items)


def parse_expected(text: str) -> Expected:
    """The result a compliance case expects: an error (``ERROR: generic::out_of_range: ...``) or typed rows."""

    text = text.strip()
    if text.startswith("ERROR"):
        match = re.match(r"ERROR:\s*(?:generic::)?(\w+)?:?\s*(.*)", text, re.S)
        return Expected("error", code=match.group(1) or "", message=match.group(2))
    note = re.search(r"\n\s*NOTE:[^\n]*$", text)
    if note:
        text = text[:note.start()].rstrip()
    parser = _Parser(text)
    parser.ws()
    if not _PREFIX.match(text, parser.pos):
        raise ExpectedError("result does not start with a type")
    result_type, payload = parser.value(None)
    parser.ws()
    if parser.pos != len(text):
        parser.fail("text after the result")
    if result_type.kind != "ARRAY" or not isinstance(payload, tuple):
        raise ExpectedError(f"result is a {result_type}, not an array of rows")
    return Expected("rows", result_type.elem, list(payload), isinstance(payload, V.UnorderedArray), nondeterministic=bool(note))


# --- comparison ----------------------------------------------------------------------------------


def _float_order(x: float) -> int:
    i = struct.unpack("<q", struct.pack("<d", x))[0]
    return i if i >= 0 else -(2**63) - i


def float_close(a: float, b: float, ulps: int = 4) -> bool:
    """Equal as the compliance driver compares doubles: NaN equals NaN, otherwise within ``ulps`` representable steps."""

    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if a == b:
        return True
    if math.isinf(a) or math.isinf(b):
        return False
    return abs(_float_order(a) - _float_order(b)) <= ulps


def types_match(expected: T.Type, actual: T.Type) -> bool:
    """Equal types, field names included; an ``ARRAY<>`` the printer left bare matches any array."""

    if expected.kind == "ARRAY" and actual.kind == "ARRAY":
        return _placeholder(expected.elem) or types_match(expected.elem, actual.elem)
    if expected.kind == "STRUCT" and actual.kind == "STRUCT":
        return len(expected.fields) == len(actual.fields) and all(
            (n1 or None) == (n2 or None) and types_match(t1, t2) for (n1, t1), (n2, t2) in zip(expected.fields, actual.fields)
        )
    return expected == actual


def _key(t: T.Type, value: Any) -> Any:
    """A hashable form that is equal for values that are exactly equal (arrays of unknown order sorted)."""

    if value is None:
        return None
    kind = t.kind
    if kind in ("FLOAT64", "FLOAT32"):
        return "nan" if math.isnan(value) else value + 0.0
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return str(value.normalize()) if value else "0"
    if kind == "ARRAY":
        keys = tuple(_key(t.elem, v) for v in value)
        return ("u",) + tuple(sorted(keys, key=repr)) if isinstance(value, V.UnorderedArray) else ("a",) + keys
    if kind == "STRUCT":
        return ("s",) + tuple(_key(ft, v) for (_, ft), v in zip(t.fields, value))
    if kind == "INTERVAL":
        return ("i", value.months, value.days, value.micros)
    if kind in ("BOOL",):
        return ("b", value)
    return value


def values_equal(t: T.Type, expected: Any, actual: Any, strict: bool = True) -> bool:
    """Whether two payloads of type ``t`` are the same value.

    ``expected``'s arrays of unknown order compare as multisets. With ``strict=False`` the same goes for
    ``actual``'s (the evaluator said it does not know their order).
    """

    if expected is None or actual is None:
        return expected is None and actual is None
    kind = t.kind
    if kind in ("FLOAT64", "FLOAT32"):
        return isinstance(actual, float) and float_close(expected, actual)
    if kind == "ARRAY":
        if not isinstance(actual, tuple) or len(expected) != len(actual):
            return False
        if isinstance(expected, V.UnorderedArray) or (not strict and isinstance(actual, V.UnorderedArray)):
            return multiset_equal(t.elem, expected, actual, strict)
        return all(values_equal(t.elem, x, y, strict) for x, y in zip(expected, actual))
    if kind == "STRUCT":
        if not isinstance(actual, tuple) or len(expected) != len(actual) or len(expected) != len(t.fields):
            return False
        return all(values_equal(ft, x, y, strict) for (_, ft), x, y in zip(t.fields, expected, actual))
    if kind == "BOOL":
        return isinstance(actual, bool) and expected == actual
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return isinstance(actual, Decimal) and expected == actual
    if kind == "INT64":
        return isinstance(actual, int) and not isinstance(actual, bool) and expected == actual
    if kind in ("OTHER", "JSON"):
        return str(expected).split() == str(actual).split()
    return type(expected) is type(actual) and expected == actual


def multiset_difference(t: T.Type, expected, actual, strict: bool = True) -> tuple[list, list]:
    """The values only ``expected`` has and the values only ``actual`` has (floats compared within ULPs)."""

    pending = Counter(_key(t, v) for v in expected)
    pending.subtract(Counter(_key(t, v) for v in actual))
    if not any(pending.values()):
        return [], []
    # drop what matches exactly, then pair the rest value by value
    surplus = {k: c for k, c in pending.items() if c > 0}
    missing = {k: -c for k, c in pending.items() if c < 0}
    rest_left, rest_right = [], []
    for v in expected:
        k = _key(t, v)
        if surplus.get(k, 0) > 0:
            surplus[k] -= 1
            rest_left.append(v)
    for v in actual:
        k = _key(t, v)
        if missing.get(k, 0) > 0:
            missing[k] -= 1
            rest_right.append(v)
    only_expected = []
    for v in rest_left:
        for index, w in enumerate(rest_right):
            if values_equal(t, v, w, strict):
                del rest_right[index]
                break
        else:
            only_expected.append(v)
    return only_expected, rest_right


def multiset_equal(t: T.Type, expected, actual, strict: bool = True) -> bool:
    if len(expected) != len(actual):
        return False
    left, right = multiset_difference(t, expected, actual, strict)
    return not left and not right


def rows_equal(t: T.Type, expected_rows, actual_rows, unordered: bool, strict: bool = True) -> bool:
    if len(expected_rows) != len(actual_rows):
        return False
    if unordered:
        return multiset_equal(t, expected_rows, actual_rows, strict)
    return all(values_equal(t, x, y, strict) for x, y in zip(expected_rows, actual_rows))


def result_shape(result) -> tuple[T.Type, list]:
    """The result as the compliance printer sees it: the row type and the row payloads."""

    columns = [(name or None, t) for name, t in result.columns]
    if result.value_table:
        if len(columns) != 1:
            raise ExpectedError("a value table with several columns")
        return columns[0][1], [row[0] for row in result.rows]
    return T.struct(columns), [tuple(row) for row in result.rows]


def show(value: Any, limit: int = 400) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def compare(expected: Expected, result) -> tuple[str, str]:
    """``(verdict, reason)`` for a result the evaluator returned: exact, unsupported (flagged undetermined) or mismatch."""

    if expected.kind == "error":
        return "mismatch", f"expected error {expected.code}, got {len(result.rows)} rows"
    row_type, rows = result_shape(result)
    if not types_match(expected.type, row_type):
        return "mismatch", f"column types: expected {expected.type}, got {row_type}"
    ordered = getattr(result, "ordered", True)
    # a known-order answer is only matched when the evaluator itself determined the order
    determined = expected.unordered or ordered or len(rows) <= 1
    if determined and rows_equal(expected.type, expected.rows, rows, expected.unordered, strict=True):
        return "exact", ""
    if rows_equal(expected.type, expected.rows, rows, expected.unordered or not ordered, strict=False):
        return "unsupported", "order undetermined (flagged by the evaluator)"
    if not getattr(result, "deterministic", True):
        return "unsupported", "nondeterministic result (flagged by the evaluator) differs"
    return "mismatch", "rows differ: " + describe_difference(expected, rows, ordered)


def describe_difference(expected: Expected, rows: list, ordered: bool) -> str:
    t = expected.type
    head = f"{len(expected.rows)} rows expected, {len(rows)} got"
    if not expected.unordered and ordered:
        for index, (x, y) in enumerate(zip(expected.rows, rows)):
            if not values_equal(t, x, y):
                return f"{head}; first difference at row {index}: expected {show(x, 300)}, got {show(y, 300)}"
        return f"{head}; the extra rows are {show((expected.rows if len(expected.rows) > len(rows) else rows)[min(len(rows), len(expected.rows)):][:3], 300)}"
    left, right = multiset_difference(t, expected.rows, rows)
    return f"{head}; only in expected: {show(left[:3], 300)}; only in actual: {show(right[:3], 300)}"


# --- the fixture's tables, constants and parameters ------------------------------------------------

DEFAULT_TIME_ZONE = "America/Los_Angeles"  # the compliance driver's default
_CREATE_TABLE = re.compile(
    r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?!FUNCTION\b)(?:IF\s+NOT\s+EXISTS\s+)?([\w.`]+)", re.I
)
_CREATE_CONSTANT = re.compile(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?CONSTANT\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.`]+)\s*=\s*(.*?);?\s*$", re.I | re.S)


def _plain(t: T.Type, value: Any) -> Any:
    """A loaded value with every array an ordinary ordered tuple (a stored array has the order it was stored in)."""

    if value is None:
        return None
    if t.kind == "ARRAY":
        return tuple(_plain(t.elem, v) for v in value)
    if t.kind == "STRUCT":
        return tuple(_plain(ft, v) for (_, ft), v in zip(t.fields, value))
    return value


def load_table(text: str):
    """A table from the result a ``CREATE TABLE ... AS SELECT`` block prints: the columns and their rows."""

    from kumosql.gsql_eval import Table

    expected = parse_expected(text)
    if expected.kind != "rows":
        raise ExpectedError("the table's rows are an error")
    row_type = expected.type
    if row_type.kind != "STRUCT":
        raise ExpectedError(f"a value table of {row_type}")
    if has_placeholder(row_type):
        raise ExpectedError("a column whose type the printed rows leave open")
    columns = [(name or f"_{i}", t) for i, (name, t) in enumerate(row_type.fields)]
    return Table(columns, [_plain(row_type, row) for row in expected.rows])


class FileContext:
    """What a test file's ``[prepare_database]`` blocks define: tables, plus names that cannot be loaded."""

    def __init__(self, prepare: list[dict]):
        from kumosql.gsql_eval import Database

        self.database = Database()
        self.unloadable: dict[str, str] = {}  # table or constant name (lower case) -> why it cannot be used
        self.constants: dict[str, str] = {}
        self._blocked: re.Pattern | None = None
        for block in prepare:
            sql = block["sql"]
            match = _CREATE_TABLE.match(sql)
            if match:
                name = match.group(1).replace("`", "")
                try:
                    if not block["expected"]:
                        raise ExpectedError("no printed rows")
                    self.database.add(name, load_table(block["expected"][0]))
                except ExpectedError as error:
                    self.unloadable[name.lower()] = f"table {name} cannot be loaded from its printed rows: {error}"
                continue
            match = _CREATE_CONSTANT.match(sql)
            if match:
                name = match.group(1).replace("`", "")
                if hasattr(self.database, "add_constant"):
                    try:
                        self.database.add_constant(name, *constant_value(match.group(2)))
                        continue
                    except Exception as error:  # noqa: BLE001
                        self.unloadable[name.lower()] = f"constant {name}: {type(error).__name__}: {str(error)[:80]}"
                        continue
                self.constants[name.lower()] = match.group(2)
                self.unloadable[name.lower()] = f"constant {name} (the evaluator's Database has no constants)"

    def blocked(self, sql: str) -> str | None:
        """Why a query cannot run: it names a table or constant this fixture could not provide."""

        if not self.unloadable:
            return None
        if self._blocked is None:
            names = sorted(self.unloadable, key=len, reverse=True)
            self._blocked = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.I)
        found = self._blocked.search(sql)
        return self.unloadable[found.group(1).lower()] if found else None


def constant_value(expression: str):
    from kumosql.gsql_eval import evaluate

    result = evaluate("SELECT " + expression, None, DEFAULT_TIME_ZONE, None, "googlesql")
    return result.columns[0][1], result.rows[0][0]


def split_top_level(text: str) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        char = text[i]
        if char in "\"'`":
            quote = char
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def query_parameters(option: str | None, time_zone: str) -> dict:
    """``{name: (type, payload)}`` for a case's ``parameters`` option (``2 as lmt, cast(NULL as string) as sep``)."""

    params: dict = {}
    for entry in split_top_level(option or ""):
        match = re.match(r"^(.*\S)\s+as\s+(\w+)$", entry, re.I | re.S)
        if not match:
            raise ExpectedError(f"query parameter {entry[:40]!r}")
        from kumosql.gsql_eval import evaluate

        result = evaluate("SELECT " + match.group(1), None, time_zone, None, "googlesql")
        params[match.group(2).lower()] = (result.columns[0][1], result.rows[0][0])
    return params


# --- running a case ------------------------------------------------------------------------------


class CaseTimeout(BaseException):
    """Raised by the per-case alarm; a ``BaseException`` so an evaluator's ``except Exception`` cannot swallow it."""


def _alarm(signum, frame):
    raise CaseTimeout()


@dataclass
class Outcome:
    file: str
    name: str
    verdict: str  # exact | unsupported | mismatch | unscored
    reason: str = ""
    detail: str = ""  # expected and actual, or a traceback (mismatches)
    code_diff: str = ""  # an exact error case whose status code differs: "expected out_of_range, got invalid_argument"
    seconds: float = 0.0
    timeout: bool = False


def case_time_zone(options: dict) -> str:
    return options.get("default_time_zone") or options.get("_time_zone") or options.get("time_zone") or DEFAULT_TIME_ZONE


def run_case(case: Case, context: FileContext, timeout: float = 30.0) -> Outcome:
    from kumosql.gsql_eval import AnalysisError, EvalError, Unsupported, evaluate

    started = time.perf_counter()

    def done(verdict: str, reason: str = "", detail: str = "", **extra) -> Outcome:
        return Outcome(case.file, case.name, verdict, reason, detail, seconds=time.perf_counter() - started, **extra)

    blocked = context.blocked(case.sql)
    if blocked:
        return done("unsupported", "fixture: " + blocked)
    tz = case_time_zone(case.options)
    armed = hasattr(signal, "setitimer") and timeout > 0
    result = error = None
    try:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            params = query_parameters(case.options.get("parameters"), tz)
        except (Unsupported, AnalysisError, EvalError, ExpectedError) as problem:
            return done("unsupported", f"fixture: query parameter: {type(problem).__name__}: {str(problem)[:100]}")
        result = evaluate(case.sql, context.database, tz, params, "googlesql")
    except Unsupported as problem:
        return done("unsupported", str(problem).strip().splitlines()[0] if str(problem).strip() else "Unsupported")
    except (AnalysisError, EvalError) as problem:
        error = problem
    except CaseTimeout:
        return done("mismatch", f"timeout after {timeout:g}s", timeout=True)
    except RecursionError:
        return done("mismatch", "RecursionError", traceback.format_exc(limit=-12))
    except Exception as problem:  # noqa: BLE001  anything else is the evaluator's bug
        return done("mismatch", f"{type(problem).__name__}: {str(problem)[:160]}", traceback.format_exc())
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        expected = parse_expected(case.expected)
    except NanoPrecision as problem:
        if error is not None:
            return done("mismatch", f"expected a result with sub-microsecond digits, got {type(error).__name__}")
        return done("mismatch", "expected result has sub-microsecond digits that BigQuery cannot represent")
    except ExpectedError as problem:
        return done("unscored", f"expected result not readable: {str(problem)[:140]}", case.expected[:300])
    if error is not None:
        if expected.kind == "error":
            code = getattr(error, "code", "")
            diff = f"expected {expected.code}, got {code}" if expected.code and code != expected.code else ""
            return done("exact", code_diff=diff)
        return done("mismatch", f"unexpected {type(error).__name__}: {str(error)[:140]}", f"expected {show(expected.rows)}")
    verdict, reason = compare(expected, result)
    if verdict == "mismatch":
        return done("mismatch", "rows differ" if reason.startswith("rows differ") else reason, reason)
    return done(verdict, reason)


# Everything a worker needs, inherited through fork: {file: [Case]} and the per-case timeout.
_WORK: dict = {"cases": {}, "timeout": 30.0}


def _run_file(name: str) -> list[Outcome]:
    sys.setrecursionlimit(4000)
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
    cases_here = _WORK["cases"][name]
    context = FileContext(cases_here[0].tables)
    return [run_case(case, context, _WORK["timeout"]) for case in cases_here]


def run_all(selected: list[Case], jobs: int, timeout: float) -> list[Outcome]:
    by_file: dict[str, list[Case]] = {}
    for case in selected:
        by_file.setdefault(case.file, []).append(case)
    _WORK["cases"] = by_file
    _WORK["timeout"] = timeout
    names = sorted(by_file, key=lambda n: -len(by_file[n]))
    outcomes: list[Outcome] = []
    if jobs > 1 and len(names) > 1 and "fork" in multiprocessing.get_all_start_methods():
        with multiprocessing.get_context("fork").Pool(jobs) as pool:
            for batch in pool.imap_unordered(_run_file, names):
                outcomes.extend(batch)
    else:
        for name in names:
            outcomes.extend(_run_file(name))
    order = {(c.file, c.name): i for i, c in enumerate(selected)}
    outcomes.sort(key=lambda o: order.get((o.file, o.name), 0))
    return outcomes


# --- reporting -----------------------------------------------------------------------------------


def reason_group(reason: str) -> str:
    """An unsupported reason with its numbers and quoted names folded, so the same missing feature groups together."""

    text = " ".join(reason.split())
    text = re.sub(r"(?<![A-Za-z_\d])\d+(?:\.\d+)?", "#", text)
    return text[:110]


_FAMILY = re.compile(r"^((?:window |aggregate |analytic |table )?(?:function|operator|aggregate|window function))\s+(\S+)", re.I)


def reason_family(group: str) -> tuple[str, str]:
    """``("function", "ArraySize")`` for a reason that names a missing function; otherwise the reason itself."""

    match = _FAMILY.match(group)
    if match:
        return match.group(1).lower() + " <name>", match.group(2)
    return group, ""


def summarize(split: str, selected: list[Case], total: int, outcomes: list[Outcome], seconds: float) -> dict:
    counts = Counter(o.verdict for o in outcomes)
    reasons: dict[str, dict] = {}
    for o in outcomes:
        if o.verdict == "unsupported":
            group = reasons.setdefault(reason_group(o.reason), {"count": 0, "files": Counter(), "examples": []})
            group["count"] += 1
            group["files"][o.file] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(f"{o.file}/{o.name}")
    families: dict[str, tuple[int, Counter]] = {}
    for group, data in reasons.items():
        family, name = reason_family(group)
        count, names = families.get(family, (0, Counter()))
        names[name] += data["count"]
        families[family] = (count + data["count"], names)
    by_file: dict[str, Counter] = {}
    for o in outcomes:
        by_file.setdefault(o.file, Counter())[o.verdict] += 1
    return {
        "split": split,
        "cases": total,
        "claimed": len(selected),
        "counts": {k: counts.get(k, 0) for k in ("exact", "unsupported", "mismatch", "unscored")},
        "timeouts": sum(1 for o in outcomes if o.timeout),
        "error_code_differences": [f"{o.file}/{o.name}: {o.code_diff}" for o in outcomes if o.code_diff],
        "unsupported_families": [
            {"family": family, "count": n, "names": len(names), "top_names": [f"{name} {c}" for name, c in names.most_common(8) if name]}
            for family, (n, names) in sorted(families.items(), key=lambda kv: -kv[1][0])
        ],
        "unsupported_reasons": [
            {"reason": r, "count": g["count"], "files": dict(g["files"].most_common(5)), "examples": g["examples"]}
            for r, g in sorted(reasons.items(), key=lambda kv: -kv[1]["count"])
        ],
        "by_file": {f: {k: c.get(k, 0) for k in ("exact", "unsupported", "mismatch", "unscored")} for f, c in sorted(by_file.items())},
        "mismatches": [
            {"case": f"{o.file}/{o.name}", "reason": o.reason, "detail": o.detail} for o in outcomes if o.verdict in ("mismatch", "unscored")
        ],
        "seconds": round(seconds, 1),
    }


def render(summary: dict, outcomes: list[Outcome], args) -> str:
    out: list[str] = []
    c = summary["counts"]
    claimed = summary["claimed"] or 1
    out.append(
        f"GoogleSQL conformance, {summary['split']} split: {summary['claimed']} claimed cases of {summary['cases']} "
        f"in {len(summary['by_file'])} files ({summary['seconds']}s)"
    )
    for key in ("exact", "unsupported", "mismatch", "unscored"):
        out.append(f"  {key:<12}{c[key]:>6}  {100 * c[key] / claimed:5.1f}%")
    if summary["timeouts"]:
        out.append(f"  ({summary['timeouts']} of the mismatches are timeouts)")
    if summary["error_code_differences"]:
        out.append(f"  {len(summary['error_code_differences'])} exact error cases differ in status code (use --failures to list)")
    if args.by_file:
        out.append("")
        out.append(f"  {'file':<56}{'exact':>7}{'unsup':>7}{'mism':>6}")
        for name, row in summary["by_file"].items():
            out.append(f"  {name:<56}{row['exact']:>7}{row['unsupported']:>7}{row['mismatch'] + row['unscored']:>6}")
    reasons = summary["unsupported_reasons"]
    if reasons:
        out.append("")
        out.append(f"unsupported, by reason ({len(reasons)} distinct; top {min(args.reasons, len(reasons))}):")
        for group in reasons[:args.reasons]:
            files = ", ".join(f"{f} {n}" for f, n in list(group["files"].items())[:3])
            out.append(f"  {group['count']:>5}  {group['reason']}   [{files}]")
        rest = sum(g["count"] for g in reasons[args.reasons:])
        if rest:
            out.append(f"  {rest:>5}  ... {len(reasons) - args.reasons} more reasons (--reasons N, or --json)")
    families = [f for f in summary["unsupported_families"] if f["top_names"] or f["count"] > 20]
    if families:
        out.append("")
        out.append("unsupported, by family (what is still missing):")
        for fam in families[:12]:
            names = f"   {fam['names']} names: " + ", ".join(fam["top_names"][:6]) if fam["top_names"] else ""
            out.append(f"  {fam['count']:>5}  {fam['family'][:60]}{names}")
    if args.failures:
        out.append("")
        out.append("not exact:")
        for o in outcomes:
            if o.verdict != "exact":
                out.append(f"  {o.verdict:<11} {o.file}/{o.name}: {o.reason}")
        for line in summary["error_code_differences"]:
            out.append(f"  code        {line}")
    if args.mismatches:
        out.append("")
        bad = [o for o in outcomes if o.verdict in ("mismatch", "unscored")]
        out.append(f"{len(bad)} mismatches:")
        sql_of = {(case.file, case.name): case.sql for case in _WORK["selected"]}
        for o in bad:
            out.append(f"--- {o.verdict}: {o.file}/{o.name}: {o.reason}")
            out.append("    sql: " + sql_of.get((o.file, o.name), "").strip().replace("\n", "\n         ")[:600])
            if o.detail:
                out.append("    " + o.detail.rstrip().replace("\n", "\n    "))
    return "\n".join(out)


def select(split: str, files: list[str], name: str | None) -> tuple[list[Case], int]:
    import fnmatch

    everything = cases(split)
    selected = [c for c in everything if c.claimed]
    if files:
        selected = [c for c in selected if any(fnmatch.fnmatch(c.file, f) for f in files)]
    if name:
        selected = [c for c in selected if name in c.name]
    return selected, len(everything)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", default="dev", choices=["dev", "heldout"], help="which part of the fixture to run (heldout: measure once)")
    parser.add_argument("--file", action="append", default=[], help="only this test file (a name or a glob such as 'analytic_*'; repeatable)")
    parser.add_argument("--name", help="only cases whose name contains this text")
    parser.add_argument("--mismatches", action="store_true", help="list every mismatch with its SQL, expected and actual rows or traceback")
    parser.add_argument("--failures", action="store_true", help="list every case that is not exact, one line each")
    parser.add_argument("--by-file", action="store_true", help="per-file counts")
    parser.add_argument("--reasons", type=int, default=25, help="how many unsupported reasons to list (default 25)")
    parser.add_argument("--json", action="store_true", help="print the whole summary as JSON")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="worker processes (default min(4, cpus))")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per case before it counts as a (timeout) mismatch")
    parser.add_argument("--write-results", metavar="PATH", help="also write the summary JSON to PATH")
    parser.add_argument("--harvest", metavar="DIR", help="rebuild the fixture from a googlesql compliance testdata directory and exit")
    args = parser.parse_args(argv)
    if args.harvest:
        print(json.dumps(harvest(Path(args.harvest))))
        return 0
    started = time.perf_counter()
    selected, total = select(args.split, args.file, args.name)
    _WORK["selected"] = selected
    outcomes = run_all(selected, max(1, args.jobs), args.timeout)
    summary = summarize(args.split, selected, total, outcomes, time.perf_counter() - started)
    if args.write_results:
        Path(args.write_results).write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        print(render(summary, outcomes, args))
    return 1 if summary["counts"]["mismatch"] or summary["counts"]["unscored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
