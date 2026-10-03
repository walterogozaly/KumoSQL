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
    python tools/googlesql_conformance.py --mismatches       # list every mismatch
    python tools/googlesql_conformance.py --file strings     # one test file
    python tools/googlesql_conformance.py --split heldout    # measured once, never developed on
    python tools/googlesql_conformance.py --harvest DIR      # rebuild the fixture from testdata/

The fixture (``tests/fixtures/googlesql_conformance``) keeps every case of every ``.test`` file at
the pinned commit, split by file into dev and held-out (a quarter of the files, chosen by a hash of
the file name before any function was written). The claimed subset is decided here, from each
case's declared features, not from how the evaluator does on it.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
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
        for case in entry["cases"]:
            options = {**entry["defaults"], **case["options"]}
            if "default required_features" in options and "required_features" not in options:
                options["required_features"] = options["default required_features"]
            expected = case["expected"][0] if case["expected"] else ""
            claimed, reason = claim(entry["file"], case["sql"], options)
            out.append(Case(entry["file"], case["name"], case["sql"], options, expected, entry["prepare"], claimed, reason))
    return out
