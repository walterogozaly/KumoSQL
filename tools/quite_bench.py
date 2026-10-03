"""Run the QUITE paper's published LLM query rewrites through KumoSQL's provers.

QUITE (Song et al., "QUITE: A Query Rewrite System Beyond Rules with LLM Agents",
https://github.com/Yuyang-Song/QUITE) publishes, in ``experiments_results/``, the rewrites that
13 configurations (QUITE, QUITE with hints, LLM-R2, R-Bot and a plain LLM agent on several
models, and LearnedRewrite) produced for 63 TPC-H, 156 DSB, 58 Calcite and 43 SQLStorm
(StackOverflow) queries: 4,160 rewrites. Each carries an ``equivalence`` flag from running both
queries on the authors' PostgreSQL instance of the benchmark. A fourteenth file per benchmark
(``EXP_QUITE_*``) holds QUITE's raw output without a flag and is not used. The repository has
no licence, so the files are downloaded from a pinned commit into a cache folder, checked
against their SHA-256 digests, and never stored in this repository.

The rewrites are deduplicated on their exact text (original, rewrite), keeping how many systems
produced each pair. Every distinct pair then gets one verdict, decided from the SQL and the
published schema (``dataset/schemas``: types, primary keys, NOT NULL, foreign keys):

* ``proven``: KumoSQL's algebraic prover proved the pair equivalent (as result bags). Every
  proof is replayed on the databases below; one that separates the queries makes it ``wrong``.
* ``refuted``: a database that respects the schema on which DuckDB returns different bags,
  confirmed with DuckDB's optimizer off (``kumosql.duckdb_load.run_unoptimized``).
* ``unknown``, ``unsupported`` (the SQL does not parse), ``timeout`` and ``error`` (a crash).

Databases come from the prover's own counterexample, KumoSQL's targeted suite
(``kumosql.refute``) and, for TPC-H when ``tpchgen-cli`` is installed, a generated TPC-H
instance at scale factor 0.01. Queries are PostgreSQL; DuckDB runs them with PostgreSQL's
integer division and NULL ordering, and ``now()``/``current_date`` read one fixed instant on
both sides. A difference is only counted when it cannot come from tie-breaking: queries with
``random()`` are never refuted, and when a query's rows can depend on input order (a LIMIT that
may cut ties, ROW_NUMBER, LAG, STRING_AGG, DISTINCT ON ...) both sides must return the same bag
with every table loaded in three row orders.

The flags are not semantic labels: equal on one instance does not mean equal on every database
(the repository documents one such TPC-H Q2 rewrite), and a pair is flagged unequal also when a
query timed out (300 s) or the rewrite failed to run. So:

* flagged-equal pairs score ``proved / (pairs - refuted)``; a refuted one is an instance-label
  disagreement, reported with its counterexample, not a wrong answer;
* flagged-unequal pairs must never be proved. Each is classified by why it was flagged:
  ``rows`` (both queries ran and the results differed), ``error`` (the rewrite failed to run:
  its recorded time is the original's) or ``timeout``. Only replayed differences count as
  negatives, and a proof of a pair whose results differ on a replayed database is ``wrong``.

One query in five (by SHA-1 of ``quite/<benchmark>/<query id>``) is held out with all its
rewrites.

    python tools/quite_bench.py                         # every distinct pair
    python tools/quite_bench.py --benchmark tpch        # one benchmark
    python tools/quite_bench.py --sample 60             # the pinned sample the test runs
    python tools/quite_bench.py --write-results         # benchmarks/results/quite-*.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import datetime
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

COMMIT = "0cffd7ce412dcd5c46bf272cd6464a00120db91c"
BASE = f"https://raw.githubusercontent.com/Yuyang-Song/QUITE/{COMMIT}/"
BENCHMARKS = ("tpch", "dsb", "calcite", "sqlstorm")
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "quite"
TIMEOUT_S = 300  # the authors' statement timeout: a recorded time of exactly 300 s is a timeout
SCHEMAS = {
    "calcite": ("dataset/schemas/calcite_schemas.sql", "aa14a9893edc839872fdde0270edd37819f091053aee73e530c19d0f8da4dad5"),
    "dsb": ("dataset/schemas/dsb_schemas.sql", "2e69b84187f31d78f561d681b0074a7b3f59a88f37e13f270685bfbcc3c8f698"),
    "sqlstorm": ("dataset/schemas/sqlstorm_schemas.sql", "cc398f148f81e2e4ca1c2b181a2cb94c9b8b08f24427b14f048da14b06262236"),
    "tpch": ("dataset/schemas/tpch_schemas.sql", "18df4138b5863e4e29e50a0863ad0a6e94f6c94a74694b6e5e1376021c9d1429"),
}
# experiments_results/<file>: SHA-256 at COMMIT (the 13 flagged files per benchmark)
RESULT_FILES = {
    "calcite/LLM_Agent_Claude3.7_calcite_58queries.json": "a43ffc0be3a67f8409e11ba02d0e4e16edabda3ff21bccf89c74af2db935c766",
    "calcite/LLM_Agent_DeepSeekR1_calcite_58queries.json": "2c1018113f3320bb8ffa51ca67b4ac007bb90324efeba117a37c40b6bad26eb4",
    "calcite/LLM_Agent_DeepSeekV3_calcite_58queries.json": "79b4d4659f873e10360fb7915787eff8d8ce4a1f7fc14b3712f634d516d8a93c",
    "calcite/LLM_Agent_GPT4o_calcite_58queries.json": "5d5a93e1353824f60154abaa90f10907fa9f63a4ff547aeaf70b90132320d228",
    "calcite/LLM_R2_Claude3.7_calcite_58queries.json": "c5e7a660694e359d830d8696cb7c5fee8459830a8a9d50f3b7a049c29c9c81a3",
    "calcite/LLM_R2_DeepSeekR1_calcite_58queries.json": "03c20c75a5006b1f9a8700b37889b6b45ef93b86acdc17ea6d074601f31cc495",
    "calcite/LLM_R2_GPT4o_calcite_58queries.json": "d44af8998a18341a2f24a5163d7e306d0198ee1907cf45b9ef25ad581ce499af",
    "calcite/LR_calcite_58queries.json": "285df3dbcf6674ca6bc3c32db7037b2f7fd5df64ebc3a6dad37449b0bec15df2",
    "calcite/QUITE_calcite_58queries.json": "146d05e2b5d3eaa12e85ae81bc243fe9d6e755b21a0f764b7926a98724421c79",
    "calcite/QUITE_hint_calcite_58queries.json": "7ee45ae81f85577bd3acb0149cd4724828d59ff28b2988e76b43a7d5982a7f15",
    "calcite/RBot_Claude3.7_calcite_58queries.json": "25777ec10f0ed1296ffa4f2367858aba4ad71e3792d70513a4cd9a93717f0058",
    "calcite/RBot_DeepSeekR1_calcite_58queries.json": "d148b30b58704cb3556405ff9d859a663a0436ef255997671224da5ed580f8c2",
    "calcite/RBot_GPT4o_calcite_58queries.json": "569f0b8d462f4f7cd2a3a2c5cfac227bda30914ce17291427d0129386c1f1397",
    "dsb/LLM_Agent_Claude3.7_dsb_156queries.json": "f52bbf4064275095638cb8113e9f7e1068bb7c6cd1bcfdebd0d6ebce52c98544",
    "dsb/LLM_Agent_DeepSeekR1_dsb_156queries.json": "b3617627e4d117320c829c762afd7fc11a5e94b354d2ca7b96fa43c8bacc0bd7",
    "dsb/LLM_Agent_DeepSeekV3_dsb_156queries.json": "0c0fccba0a018ae3e8dfd20f7cc78d76697a44dfd4cd69198e0be03dc2b2583d",
    "dsb/LLM_Agent_GPT4o_dsb_156queries.json": "7028ba7b78f9fae97ff662357abf7443268d78591378a13d6d21dc27044f8890",
    "dsb/LLM_R2_Claude3.7_dsb_156queries.json": "bb29a3bbf86c6ae3d84a9ec405eeabf5bc4cb3aa22b2b2d000a552540f7511c8",
    "dsb/LLM_R2_DeepSeekR1_dsb_156queries.json": "0a0c78d6b1f29d47aad07396827ebf9f0d7c5ddb55bbaf06138319f278a36e71",
    "dsb/LLM_R2_GPT4o_dsb_156queries.json": "032939e13bf0a7862b7ac7126c17640e78bbde6bfff41cdaf2e124244a18d025",
    "dsb/LR_dsb_156queries.json": "9e1a7f95cb25417fbb13151e3418a20f1e36c7b55d6799a605eef6d9a31aee2d",
    "dsb/QUITE_dsb_156queries.json": "0a7961cfcf81de5c4a67edc9f703a4916df6c66e27257967481b2c4f28578514",
    "dsb/QUITE_hint_dsb_156queries.json": "2e800a0e6efe41cacf3bcbcf49e0a1e2c3c2982e9a32e260cd5409aac9036583",
    "dsb/RBot_Claude3.7_dsb_156queries.json": "def1d1a675c2b66c5fe48d8625cd4d38da8b44546e90c9b4b6b2d8dce4774385",
    "dsb/RBot_DeepSeekR1_dsb_156queries.json": "6d776b71c5dcf7213892aa4f20881d62285fc173e71c526f333659c512ffd591",
    "dsb/RBot_GPT4o_dsb_156queries.json": "c96d1363d6cf7acfa7e2657f75ae56dce81e02f2cd03d0b750cb642687ee82d8",
    "sqlstorm/LLM_Agent_Claude3.7_sqlstorm_43queries.json": "581e742207c1b56b3fc357e58977f3d5dc525f4cb08b8b2a6853421dd792039c",
    "sqlstorm/LLM_Agent_DeepSeekR1_sqlstorm_43queries.json": "01b4d69c2ebe99e301d87763013f91f2ff1ccfcc6a0d05b649c4ce16f0e6605d",
    "sqlstorm/LLM_Agent_DeepSeekV3_sqlstorm_43queries.json": "44e65496ca3b6751bf9d322988cab3c5c0e70763b45912fa09105ef575f29925",
    "sqlstorm/LLM_Agent_GPT4o_sqlstorm_43queries.json": "e31ce9c756291a01113fa0e0dee23af9d89a3d87e01f667ae6bd9c482a14e9bd",
    "sqlstorm/LLM_R2_Claude3.7_sqlstorm_43queries.json": "cea9d3dddaefc4c0598c652381550a95041d98b01947fbbf85119e4f47a9860e",
    "sqlstorm/LLM_R2_DeepSeekR1_sqlstorm_43queries.json": "16041ef8b7d2f7406ebe81aa2bc77875c1d98005a7b1e48a9d590695bd5e3b38",
    "sqlstorm/LLM_R2_GPT4o_sqlstorm_43queries.json": "d65e6880aa7a5bb2b64a383c57fecf33738262498b88c2c2be06f6446d8663c4",
    "sqlstorm/LR_sqlstorm_43queries.json": "8906d15471399624eae367ca95a002459e2be20d908c055f65f35872c85e43e6",
    "sqlstorm/QUITE_hint_sqlstorm_43queries.json": "4b41000b1eadd0bc014f4e8f11663695516c736019fbf3f95192aa056196368d",
    "sqlstorm/QUITE_sqlstorm_43queries.json": "7e077060cb1a8612658df3960f0888524538ce305d4fa0064ea89c79eb99fea1",
    "sqlstorm/RBot_Claude3.7_sqlstorm_43queries.json": "cf80c51b4436806f11b65efca97f04868607520f015f5b60db51008573d9bebd",
    "sqlstorm/RBot_DeepSeekR1_sqlstorm_43queries.json": "ee2f136e3383e9426d86d422f4375d6df882e481691f893fcc67b5decdc10e30",
    "sqlstorm/RBot_GPT4o_sqlstorm_43queries.json": "ab7604beab2acf85594b6795ee3d26298aae2a95a1f0a6a5f375a08eb6a3d998",
    "tpch/LLM_Agent_Claude3.7_tpch_63queries.json": "dba2d9a46647aa151e5cbd630710e494e0ab33a381ed963a018f18fc8fe8797c",
    "tpch/LLM_Agent_DeepSeekR1_tpch_63queries.json": "e732f10c72b5faca9256146f1c406711d61dc61a4fda3581d3017af2b2586293",
    "tpch/LLM_Agent_DeepSeekV3_tpch_63queries.json": "0b80e4e0a381cdf05a33954719a95fab09f7f606636bff3b772f1be46068bcb0",
    "tpch/LLM_Agent_GPT4o_tpch_63queries.json": "a2d36954307484d6834f89e4ca9608314281e48cce27b44857729e8e0d43f5ae",
    "tpch/LLM_R2_Claude3.7_tpch_63queries.json": "a280ac3d6d79fabdc3f11fc6768eb03859c1587d2c29b7017e442d310da577e2",
    "tpch/LLM_R2_DeepSeekR1_tpch_63queries.json": "466ee51d565a80236a58c26b3a478018a438d102a6f4ce7e899817721af07e31",
    "tpch/LLM_R2_GPT4o_tpch_63queries.json": "9d6d2745e8a99fddc7c315306f91c3d6f0b350b357c73d1111274c03365871a7",
    "tpch/LR_tpch_63queries.json": "075c5584ad3cd5440fdac7cb5567b3c796dc8ac6cec23be659df316f5c0aedd4",
    "tpch/QUITE_hint_tpch_63queries.json": "83b2ca47f93021530e9654fceca276f1abf1b866a1726af8111409dd63d87441",
    "tpch/QUITE_tpch_63queries.json": "d1a234a6f51efdc19959a92095a762f165d73c1d68d99fc3eb90f7aacde6013f",
    "tpch/RBot_Claude3.7_tpch_63queries.json": "5d464b9ed678d43522bcc50d87633c1e992e885573efc1e17ef4d4bef9e0805b",
    "tpch/Rbot_DeepSeekR1_tpch_63queries.json": "44db4429ffca5ac2b68e502ffd2b4646af053ea6a95ab746236e6ae5fdb064f1",
    "tpch/Rbot_GPT4o_tpch_63queries.json": "fb6386f883a1867be32d45faae86a60f7508b1d5a0bb4ebd71451a82581e88a6",
}

# The one rewrite the authors document as equal on their instance but not semantically equal (TPC-H Q2,
# documents/equivalence_definition_and_known_cases.md); they set its flag to false by hand.
DOCUMENTED = {("tpch", "QUITE", "4"), ("tpch", "QUITE_hint", "4")}

# DuckDB with PostgreSQL's integer division and NULL ordering; one thread so runs repeat exactly.
DUCKDB_SETTINGS = (
    "SET integer_division = true",
    "SET default_null_order = 'nulls_last_on_asc_first_on_desc'",
    "SET threads = 1",
)
PROVE_SECONDS = 60  # hard cap on one proof attempt, normalization included
SEARCH_SECONDS = 8.0  # budget of the targeted-database search per pair
QUERY_SECONDS = 10.0  # one query on one replayed database
NOW = "2024-06-01 12:00:00"  # the instant now() and current_date read during a replay
SAMPLE_SEED = 2026
FETCH_AS_LIMIT = True  # False reproduces the baseline run, before the harness read FETCH FIRST as LIMIT


# --- data ---------------------------------------------------------------------------


def _download(relative: str, digest: str) -> Path:
    path = CACHE / relative
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE + relative, timeout=60) as response:
            data = response.read()
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic, so parallel tests never read half a file
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"{path} does not match the pinned version; delete it to download again")
    return path


def fetch() -> Path:
    """Every flagged result file and the four schemas, downloaded once into the cache folder."""

    for relative, digest in SCHEMAS.values():
        _download(relative, digest)
    for name, digest in RESULT_FILES.items():
        _download(f"experiments_results/{name}", digest)
    return CACHE


@dataclass
class Rewrite:
    benchmark: str
    system: str
    qid: str
    original: str
    rewritten: str
    flag: bool
    original_seconds: float | None
    rewrite_seconds: float | None

    @property
    def why(self) -> str:
        """How the authors' run went: ``timeout`` (either query hit 300 s), ``error`` (the rewrite failed
        and the original's time was recorded for it) or ``rows`` (both ran; the results were compared)."""

        if (self.benchmark, self.system, self.qid) in DOCUMENTED:
            return "documented"
        if TIMEOUT_S in (self.original_seconds, self.rewrite_seconds):
            return "timeout"
        if self.original_seconds == self.rewrite_seconds:
            return "error"
        return "rows"


def _system(name: str) -> str:
    return re.sub(r"_(tpch|dsb|calcite|sqlstorm)_\d+queries\.json$", "", name).replace("Rbot", "RBot")


def load_rewrites(root: Path | None = None) -> list[Rewrite]:
    root = root or fetch()
    out = []
    for name in sorted(RESULT_FILES):
        benchmark = name.split("/")[0]
        for row in json.loads((root / "experiments_results" / name).read_text(encoding="utf-8")):
            if not isinstance(row, dict) or "original_query" not in row:
                continue  # the per-file timing summary
            out.append(Rewrite(
                benchmark, _system(name.split("/")[1]), str(row["id"]), row["original_query"].strip(),
                row["rewritten_query"].strip(), bool(row["equivalence"]), row.get("original_execution_time"),
                row.get("rewrite_execution_time", row.get("rewritten_execution_time")),
            ))
    return out


@dataclass
class Pair:
    benchmark: str
    query: str  # the original in a canonical spelling: the files spell one query several ways and number it differently
    original: str
    rewritten: str
    systems: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)  # "equal" / "unequal" -> rewrites
    why: Counter = field(default_factory=Counter)  # how the flagged-unequal rewrites ran

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.benchmark}\n{self.original}\n{self.rewritten}".encode()).hexdigest()[:12]

    @property
    def case_id(self) -> str:
        return f"quite/{self.benchmark}/{hashlib.sha1(self.query.encode()).hexdigest()[:10]}"

    @property
    def held_out(self) -> bool:
        """One query in five, by a hash of its id, is held out with every rewrite of it."""

        return int(hashlib.sha1(self.case_id.encode()).hexdigest(), 16) % 5 == 0

    @property
    def label(self) -> str:
        if self.flags["equal"] and self.flags["unequal"]:
            return "mixed"
        return "equal" if self.flags["equal"] else "unequal"

    @property
    def reason(self) -> str:
        """Why a flagged-unequal pair was flagged: ``documented`` (the authors' known case), ``rows`` if any
        run compared rows, else ``error``, else ``timeout``."""

        for reason in ("documented", "rows", "error", "timeout"):
            if self.why[reason]:
                return reason
        return ""

    @property
    def identical(self) -> bool:
        return _strip(self.original) == _strip(self.rewritten)


def _strip(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip())


def canonical(sql: str) -> str:
    """One spelling per query: parsed, identifiers folded and unquoted, layout dropped (text only if it does not parse)."""

    try:
        tree = sqlglot.parse_one(sql.strip().rstrip(";"), read="postgres")
        for identifier in tree.find_all(exp.Identifier):
            identifier.set("this", identifier.this.lower())
            identifier.set("quoted", False)
        sql = tree.sql(dialect="postgres")
    except (sqlglot.errors.SqlglotError, ValueError, AttributeError):
        pass
    return fingerprint(sql)


def load_pairs(root: Path | None = None) -> list[Pair]:
    """Distinct (benchmark, original, rewrite) texts, in first-seen order, with per-system counts."""

    pairs: dict[tuple[str, str, str], Pair] = {}
    queries: dict[str, str] = {}
    for rewrite in load_rewrites(root):
        key = (rewrite.benchmark, rewrite.original, rewrite.rewritten)
        pair = pairs.get(key)
        if pair is None:
            query = queries.get(rewrite.original)
            if query is None:
                query = queries[rewrite.original] = canonical(rewrite.original)
            pair = pairs[key] = Pair(rewrite.benchmark, query, rewrite.original, rewrite.rewritten)
        pair.systems[rewrite.system] += 1
        pair.flags["equal" if rewrite.flag else "unequal"] += 1
        if not rewrite.flag:
            pair.why[rewrite.why] += 1
    return list(pairs.values())


# --- schema ---------------------------------------------------------------------------


@dataclass
class Table:
    name: str
    columns: dict[str, str]  # column -> PostgreSQL type, upper case
    not_null: set[str] = field(default_factory=set)
    keys: list[tuple[str, ...]] = field(default_factory=list)
    foreign: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)


def load_schema(text: str) -> dict[str, Table]:
    """Tables, NOT NULL columns, primary keys and foreign keys of a PostgreSQL DDL file (names lower-cased)."""

    tables: dict[str, Table] = {}
    alters = re.findall(r"alter\s+table\s+(\w+)\s+add\s+constraint\s+\w+\s+primary\s+key\s*\(([^)]*)\)", text, re.I)
    text = re.sub(r"alter\s+table[^;]*;", "", text, flags=re.I)
    for statement in sqlglot.parse(text, read="postgres"):
        if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
            continue
        table = Table(statement.this.this.name.lower(), {})
        for item in statement.this.expressions:
            if isinstance(item, exp.ColumnDef):
                name = item.name.lower()
                table.columns[name] = item.args["kind"].sql(dialect="postgres").upper()
                for constraint in item.args.get("constraints") or []:
                    kind = constraint.args.get("kind")
                    if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get("allow_null"):
                        table.not_null.add(name)
                    elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        table.keys.append((name,))
                        table.not_null.add(name)
            elif isinstance(item, exp.PrimaryKey):
                key = tuple(e.name.lower() for e in item.expressions)
                table.keys.append(key)
                table.not_null.update(key)
            elif isinstance(item, exp.ForeignKey) and item.args.get("reference") is not None:
                parent = item.args["reference"].this
                if isinstance(parent, exp.Schema):
                    table.foreign.append((
                        tuple(e.name.lower() for e in item.expressions),
                        parent.this.name.lower(),
                        tuple(e.name.lower() for e in parent.expressions),
                    ))
        tables[table.name] = table
    for name, columns in alters:
        key = tuple(c.strip().lower() for c in columns.split(","))
        tables[name.lower()].keys.append(key)
        tables[name.lower()].not_null.update(key)
    return tables


def schema_for(benchmark: str, root: Path | None = None) -> dict[str, Table]:
    root = root or CACHE
    relative, digest = SCHEMAS[benchmark]
    path = root / relative if root != CACHE else _download(relative, digest)
    return load_schema(path.read_text(encoding="utf-8"))


def _duck_type(pg_type: str) -> str:
    base = pg_type.split("(")[0].strip()
    if base in {"INT", "INTEGER", "SMALLINT", "BIGINT", "INT4", "INT8", "INT2", "SERIAL", "BIGSERIAL"}:
        return "BIGINT"
    if base in {"DECIMAL", "NUMERIC"}:
        return pg_type.replace(" ", "") if "(" in pg_type else "DECIMAL(38,9)"
    if base in {"DOUBLE PRECISION", "DOUBLE", "FLOAT", "REAL", "FLOAT8", "FLOAT4"}:
        return "DOUBLE"
    if base == "DATE":
        return "DATE"
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return "TIMESTAMP"
    if base in {"BOOLEAN", "BOOL"}:
        return "BOOLEAN"
    return "VARCHAR"


_BQ_TYPES = {"BIGINT": "INT64", "DOUBLE": "FLOAT64", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "BOOLEAN": "BOOL", "VARCHAR": "STRING"}


def _bq_type(pg_type: str) -> str:
    duck = _duck_type(pg_type)
    return "NUMERIC" if duck.startswith("DECIMAL") else _BQ_TYPES[duck]


# --- queries ----------------------------------------------------------------------------


def adapt(sql: str) -> tuple[str, bool]:
    """``(PostgreSQL text, adapted)``: identifiers folded to lower case, as PostgreSQL folds unquoted ones.

    ``adapted`` is true when a quoted identifier with upper-case letters was folded too (the Calcite
    queries quote ``"EMP"`` against tables created as ``emp``), which PostgreSQL itself would not do.
    """

    tree = sqlglot.parse_one(sql.strip().rstrip(";"), read="postgres")
    adapted = False
    for identifier in tree.find_all(exp.Identifier):
        name = identifier.this
        if identifier.quoted and name != name.lower():
            adapted = True
        identifier.set("this", name.lower())
    # FETCH FIRST n ROWS ONLY is PostgreSQL's standard spelling of LIMIT n; the prover reads only LIMIT.
    for fetch in list(tree.find_all(exp.Fetch)) if FETCH_AS_LIMIT else ():
        count = fetch.args.get("count")
        options = fetch.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            continue
        fetch.replace(exp.Limit(expression=count.copy() if count is not None else exp.Literal.number(1)))
    return tree.sql(dialect="postgres"), adapted


_TIE_FUNCTIONS = {"ROW_NUMBER", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "NTILE",
                  "GROUP_CONCAT", "STRING_AGG", "ARRAY_AGG", "JSON_AGG", "JSONB_AGG", "XMLAGG"}
_RANDOM_FUNCTIONS = {"RANDOM", "RAND", "UUID", "GEN_RANDOM_UUID", "SETSEED"}


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    return node.sql_name().upper() if isinstance(node, exp.Func) else ""


def _fully_ordered(select: exp.Expression) -> bool:
    """A LIMIT whose ORDER BY sorts on every output column keeps the same rows however ties are broken."""

    order = select.args.get("order") if isinstance(select, exp.Select) else None
    if order is None or any(isinstance(e, exp.Star) for e in select.expressions):
        return False
    keys = set()
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and not key.is_string and key.name.isdigit():
            keys.add(("position", int(key.name) - 1))
        else:
            keys.add(("text", key.sql().lower()))
            if isinstance(key, exp.Column):
                keys.add(("name", key.name.lower()))
    for position, projection in enumerate(select.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if not ({("position", position), ("text", inner.sql().lower()), ("name", projection.alias_or_name.lower())} & keys):
            return False
    return True


def determinism(tree: exp.Expression) -> str:
    """``random`` (never refuted), ``ties`` (rows may depend on input order) or ``fixed``."""

    names = {_function_name(node) for node in tree.find_all(exp.Func)}
    if names & _RANDOM_FUNCTIONS or any(tree.find_all(exp.Rand)):
        return "random"
    if names & _TIE_FUNCTIONS or any(tree.find_all(exp.RowNumber, exp.Lag, exp.Lead, exp.FirstValue, exp.LastValue, exp.NthValue, exp.GroupConcat, exp.ArrayAgg)):
        return "ties"
    for spec in tree.find_all(exp.WindowSpec):
        if spec.args.get("kind") and str(spec.args["kind"]).upper() == "ROWS":
            return "ties"
    for select in tree.find_all(exp.Select):
        if select.args.get("distinct") is not None and select.args["distinct"].args.get("on") is not None:
            return "ties"
    for node in tree.find_all(exp.Limit, exp.Fetch, exp.Offset):
        parent = node.parent
        if isinstance(node, exp.Limit) and isinstance(node.expression, exp.Literal) and node.expression.name == "0":
            continue
        if not _fully_ordered(parent):
            return "ties"
    return "fixed"


def replay_sql(sql: str) -> str:
    """DuckDB text of a (folded) PostgreSQL query, with now() and current_date read as one fixed instant."""

    tree = sqlglot.parse_one(sql, read="postgres")

    def fix(node):
        if isinstance(node, exp.CurrentDate):
            return exp.cast(exp.Literal.string(NOW[:10]), "date")
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentTime)) or (isinstance(node, exp.Anonymous) and str(node.name).upper() in {"NOW", "CLOCK_TIMESTAMP", "TRANSACTION_TIMESTAMP", "STATEMENT_TIMESTAMP", "LOCALTIMESTAMP"}):
            return exp.cast(exp.Literal.string(NOW), "timestamp")
        return node

    return tree.transform(fix).sql(dialect="duckdb")


# --- replaying databases ---------------------------------------------------------------------


def _bag(rows) -> Counter:
    def cell(value):
        if isinstance(value, (float, Decimal)):
            return round(float(value), 6)
        if isinstance(value, str):
            return value.rstrip(" ")  # PostgreSQL's char(n) pads with spaces; DuckDB's does not
        return value

    return Counter(tuple(cell(v) for v in row) for row in rows)


Data = dict[str, list[tuple]]


def legal(data: Data, tables: dict[str, Table]) -> bool:
    """Whether every row respects NOT NULL, the keys and the foreign keys."""

    for name, rows in data.items():
        table = tables[name]
        columns = list(table.columns)
        for row in rows:
            if any(row[columns.index(c)] is None for c in table.not_null):
                return False
        for key in table.keys:
            seen = [tuple(row[columns.index(c)] for c in key) for row in rows]
            if len(seen) != len(set(seen)):
                return False
        for child, parent, parent_columns in table.foreign:
            parent_rows = data.get(parent, [])
            parent_index = [list(tables[parent].columns).index(c) for c in parent_columns]
            allowed = {tuple(r[i] for i in parent_index) for r in parent_rows}
            for row in rows:
                value = tuple(row[columns.index(c)] for c in child)
                if None not in value and value not in allowed:
                    return False
    return True


def repair(data: Data, tables: dict[str, Table]) -> Data:
    """Point a child row with no parent at a parent row (or NULL, or drop it), then drop repeated keys."""

    data = {name: [list(row) for row in rows] for name, rows in data.items()}
    for name, table in tables.items():
        if table.foreign and name in data:
            for _, parent, _ in table.foreign:
                data.setdefault(parent, [])
    for _ in range(3):
        for name, rows in data.items():
            table = tables[name]
            columns = list(table.columns)
            for child, parent, parent_columns in table.foreign:
                parent_index = [list(tables[parent].columns).index(c) for c in parent_columns]
                options = [tuple(r[i] for i in parent_index) for r in data.get(parent, [])]
                allowed = set(options)
                kept = []
                for row in rows:
                    value = tuple(row[columns.index(c)] for c in child)
                    if None in value or value in allowed:
                        kept.append(row)
                    elif options:
                        for c, v in zip(child, options[0]):
                            row[columns.index(c)] = v
                        kept.append(row)
                    elif not set(child) & table.not_null:
                        for c in child:
                            row[columns.index(c)] = None
                        kept.append(row)
                rows[:] = kept
            for key in table.keys:
                seen, kept = set(), []
                for row in rows:
                    value = tuple(row[columns.index(c)] for c in key)
                    if value not in seen:
                        seen.add(value)
                        kept.append(row)
                rows[:] = kept
    return {name: [tuple(row) for row in rows] for name, rows in data.items()}


_DEFAULTS = {"BIGINT": 0, "DOUBLE": 0.0, "DATE": datetime.date(2000, 1, 1), "TIMESTAMP": datetime.datetime(2000, 1, 1), "BOOLEAN": False, "VARCHAR": ""}


def _value(value, duck_type: str):
    if value is None:
        return None
    try:
        if duck_type == "BIGINT":
            return int(value)
        if duck_type.startswith("DECIMAL"):
            return Decimal(str(value))
        if duck_type == "DOUBLE":
            return float(value)
        if duck_type == "DATE" and isinstance(value, str):
            return datetime.date.fromisoformat(value[:10])
        if duck_type == "TIMESTAMP" and isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        if duck_type == "TIMESTAMP" and isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            return datetime.datetime(value.year, value.month, value.day)
        if duck_type == "VARCHAR" and not isinstance(value, str):
            return str(value)
    except (ValueError, ArithmeticError, TypeError):
        return value
    return value


def as_data(tables_rows: dict[str, list[dict]], tables: dict[str, Table]) -> Data | None:
    """Rows given as ``{table: [{column: value}]}`` (a counterexample) as full rows of the schema's tables."""

    data: Data = {}
    for name, rows in tables_rows.items():
        table = tables.get(name.lower().split(".")[-1])
        if table is None:
            return None
        full = []
        for row in rows:
            lowered = {str(k).lower(): v for k, v in row.items()}
            values = []
            for column, pg_type in table.columns.items():
                duck = _duck_type(pg_type)
                if column in lowered:
                    values.append(_value(lowered[column], duck))
                elif column not in table.not_null:  # a column the queries never read: any legal value
                    values.append(None)
                else:
                    values.append(Decimal(0) if duck.startswith("DECIMAL") else _DEFAULTS[duck])
            full.append(tuple(values))
        data[table.name] = full
    return data


class Replayer:
    """One DuckDB database holding a benchmark's tables, refilled for every replayed database."""

    def __init__(self, tables: dict[str, Table]):
        import duckdb

        self.tables = tables
        self.db = duckdb.connect(":memory:")
        for setting in DUCKDB_SETTINGS:
            self.db.execute(setting)
        for table in tables.values():
            columns = ", ".join(f'"{c}" {_duck_type(t)}' for c, t in table.columns.items())
            self.db.execute(f'CREATE TABLE "{table.name}" ({columns})')

    def close(self) -> None:
        self.db.close()

    def _load(self, data: Data) -> None:
        from kumosql.duckdb_load import insert_rows

        for name in self.tables:
            self.db.execute(f'DELETE FROM "{name}"')
            insert_rows(self.db, f'"{name}"', data.get(name, []))

    def _run(self, sql: str) -> Counter:
        timer = threading.Timer(QUERY_SECONDS, self.db.interrupt)
        timer.start()
        try:
            return _bag(self.db.execute(sql).fetchall())
        finally:
            timer.cancel()

    def differs(self, left: str, right: str, data: Data, kind: str) -> dict | None:
        """The two bags when the (DuckDB) queries differ on ``data``, confirmed; else ``None``."""

        import duckdb

        from kumosql.duckdb_load import run_unoptimized

        if kind == "random":
            return None
        data = repair(data, self.tables)
        if not legal(data, self.tables):
            return None
        try:
            self._load(data)
            a, b = self._run(left), self._run(right)
            if a == b:
                return None
            if [_bag(rows) for rows in run_unoptimized(self.db, left, right)] != [a, b]:
                return None  # DuckDB's optimizer disagrees with its unoptimized plan: not evidence
            if kind == "ties":
                rng = random.Random(7)
                for order in ("reversed", "shuffled"):
                    permuted = {}
                    for name, rows in data.items():
                        rows = list(reversed(rows)) if order == "reversed" else rng.sample(rows, len(rows))
                        permuted[name] = rows
                    self._load(permuted)
                    if (self._run(left), self._run(right)) != (a, b):
                        return None
        except duckdb.Error:
            return None
        return {
            "tables": {name: rows for name, rows in data.items() if rows},
            "left": sorted(map(str, a.elements()))[:20],
            "right": sorted(map(str, b.elements()))[:20],
        }


def documented_database(tables: dict[str, Table]) -> Data | None:
    """The database the authors describe for their TPC-H Q2 case: a supplier outside EUROPE ties the
    EUROPE minimum supply cost of a size-6 NICKEL part (columns the case does not name get legal values)."""

    return as_data({
        "region": [{"r_regionkey": 1, "r_name": "EUROPE"}, {"r_regionkey": 2, "r_name": "ASIA"}],
        "nation": [{"n_nationkey": 1, "n_name": "FRANCE", "n_regionkey": 1}, {"n_nationkey": 2, "n_name": "JAPAN", "n_regionkey": 2}],
        "supplier": [{"s_suppkey": 1, "s_name": "Supplier#1", "s_nationkey": 1, "s_acctbal": 10},
                     {"s_suppkey": 2, "s_name": "Supplier#2", "s_nationkey": 2, "s_acctbal": 20}],
        "part": [{"p_partkey": 1, "p_size": 6, "p_type": "STANDARD POLISHED NICKEL"}],
        "partsupp": [{"ps_partkey": 1, "ps_suppkey": 1, "ps_supplycost": 100}, {"ps_partkey": 1, "ps_suppkey": 2, "ps_supplycost": 100}],
    }, tables)


# --- the TPC-H instance --------------------------------------------------------------------


def tpch_instance() -> Path | None:
    """A TPC-H database at scale factor 0.01 (``tpchgen-cli``), or ``None`` when the generator is missing."""

    target = CACHE / "tpch-sf0.01.duckdb"
    if target.exists():
        return target
    if shutil.which("tpchgen-cli") is None or os.environ.get("KUMOSQL_QUITE_INSTANCE", "1") == "0":
        return None
    import duckdb

    work = CACHE / f"tpch-parquet.{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tpchgen-cli", "-s", "0.01", "--format", "parquet", "--output-dir", str(work)], check=True, capture_output=True)
    partial = CACHE / f"tpch-sf0.01.{os.getpid()}.duckdb"
    con = duckdb.connect(str(partial))
    for parquet in sorted(work.glob("*.parquet")):
        con.execute(f"CREATE TABLE {parquet.stem} AS SELECT * FROM '{parquet}'")
    con.close()
    shutil.rmtree(work)
    partial.replace(target)
    return target


def instance_differs(path: Path, left: str, right: str) -> dict | None:
    """Different bags on the TPC-H instance, confirmed with the optimizer off; ``None`` otherwise."""

    import duckdb

    from kumosql.duckdb_load import run_unoptimized

    con = duckdb.connect(str(path), read_only=True)
    try:
        for setting in DUCKDB_SETTINGS:
            con.execute(setting)
        results = []
        for sql in (left, right):
            timer = threading.Timer(QUERY_SECONDS * 3, con.interrupt)
            timer.start()
            try:
                results.append(_bag(con.execute(sql).fetchall()))
            finally:
                timer.cancel()
        if results[0] == results[1]:
            return None
        if [_bag(rows) for rows in run_unoptimized(con, left, right)] != results:
            return None
        return {"tables": "TPC-H instance, scale factor 0.01 (tpchgen-cli)", "left": sorted(map(str, (results[0] - results[1]).elements()))[:5], "right": sorted(map(str, (results[1] - results[0]).elements()))[:5]}
    except duckdb.Error:
        return None
    finally:
        con.close()


# --- deciding a pair -------------------------------------------------------------------------


@dataclass
class Verdict:
    outcome: str  # proven | refuted | unknown | unsupported | timeout | error | wrong
    detail: str = ""
    witness: dict | None = None
    adapted: bool = False
    kind: str = "fixed"  # determinism of the pair
    source: str = ""  # where a replayed difference came from


class _Deadline(Exception):
    pass


def _alarm(signum, frame):
    raise _Deadline()


def _constraints(tables: dict[str, Table]):
    from kumosql.smt_equivalence import TableConstraints

    return {
        t.name: TableConstraints(not_null=frozenset(t.not_null), keys=tuple(t.keys), foreign_keys=tuple(t.foreign))
        for t in tables.values()
    }


def prove(left: str, right: str, tables: dict[str, Table]):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    return prove_equivalent_algebraic(
        left, right,
        schema={t.name: list(t.columns) for t in tables.values()},
        constraints=_constraints(tables),
        types={t.name: dict(t.columns) for t in tables.values()},
        compare_names=False, dialect="postgres", exact_arithmetic=True, timeout_ms=10000,
    )


def _used(trees, tables: dict[str, Table]) -> dict[str, Table]:
    names = {t.name.lower() for tree in trees for t in tree.find_all(exp.Table)}
    used = {n: tables[n] for n in names if n in tables}
    for table in list(used.values()):  # parents of foreign keys, so generated rows can point somewhere
        for _, parent, _ in table.foreign:
            used.setdefault(parent, tables[parent])
    return used


def search(left: str, right: str, tables: dict[str, Table], replayer: Replayer, kind: str, proposed: list[Data], instance: Path | None):
    """``(witness, source)`` of a replayed difference, or ``(None, "")``."""

    duck_left, duck_right = replay_sql(left), replay_sql(right)
    for source, data in proposed:
        found = replayer.differs(duck_left, duck_right, data, kind)
        if found:
            return found, source
    if instance is not None and kind == "fixed":
        found = instance_differs(instance, duck_left, duck_right)
        if found:
            return found, "TPC-H instance"
    if os.environ.get("KUMOSQL_TARGETED", "1") == "0":
        return None, ""
    from kumosql.refute import find_targeted_difference
    from kumosql.result_equivalence import DataRules

    trees = [sqlglot.parse_one(sql, read="postgres") for sql in (left, right)]
    used = _used(trees, tables)
    schema = {t.name: {c: _bq_type(ty) for c, ty in t.columns.items()} for t in used.values()}
    rules = {t.name: DataRules(frozenset(t.not_null), tuple(t.keys)) for t in used.values()}
    single = [(t.name, c[0], p, pc[0]) for t in used.values() for c, p, pc in t.foreign if len(c) == 1 and p in used]
    fixed_left, fixed_right = (sqlglot.parse_one(d, read="duckdb").sql(dialect="postgres") for d in (duck_left, duck_right))
    try:
        found = find_targeted_difference(
            fixed_left, fixed_right, schema, rules, foreign_keys=single, dialect="postgres",
            settings=DUCKDB_SETTINGS, budget=SEARCH_SECONDS, timeout=QUERY_SECONDS,
        )
    except Exception:  # noqa: BLE001 - no database is no evidence
        found = None
    if found is None:
        return None, ""
    data = {name.lower(): [tuple(_value(v, _duck_type(used[name.lower()].columns[c])) for (c, _), v in zip(t.columns, row)) for row in t.rows] for name, t in found.dataset.tables.items()}
    witness = replayer.differs(duck_left, duck_right, data, kind)
    return (witness, f"targeted: {found.label}") if witness else (None, "")


_REPLAYERS: dict[str, Replayer] = {}


def decide(pair: Pair, tables: dict[str, Table], instance: Path | None = None) -> Verdict:
    """Proven (then replayed), refuted (with a replayed database) or unknown. Never reads the flag."""

    try:
        left, adapted_left = adapt(pair.original)
        right, adapted_right = adapt(pair.rewritten)
        trees = [sqlglot.parse_one(sql, read="postgres") for sql in (left, right)]
    except (sqlglot.errors.SqlglotError, ValueError) as error:
        return Verdict("unsupported", f"parse error: {str(error)[:200]}")
    adapted = adapted_left or adapted_right
    kinds = {determinism(tree) for tree in trees}
    kind = "random" if "random" in kinds else "ties" if "ties" in kinds else "fixed"
    replayer = _REPLAYERS.get(pair.benchmark)
    if replayer is None:
        replayer = _REPLAYERS[pair.benchmark] = Replayer(tables)
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PROVE_SECONDS)
    crash = ""
    try:
        result = prove(left, right, tables)
        proved, reason = result.proven, result.reason
        proposed = []
        counter = getattr(result, "counterexample", None)
        if counter is not None:
            data = as_data(counter.tables, tables)
            if data is not None:
                proposed.append(("prover counterexample", data))
    except _Deadline:
        proved, reason, proposed, crash = False, "", [], "timeout"
    except Exception as error:  # a crash is a failure to prove, never a proof
        proved, reason, proposed, crash = False, "", [], f"{type(error).__name__}: {str(error)[:200]}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    use_instance = instance if pair.benchmark == "tpch" else None
    if pair.benchmark == "tpch":
        proposed.append(("the database of the authors' documented TPC-H Q2 case", documented_database(tables)))
    witness, source = search(left, right, tables, replayer, kind, proposed, use_instance)
    if proved:
        if witness:
            return Verdict("wrong", "proved equivalent, but a replayed database separates the queries", witness, adapted, kind, source)
        return Verdict("proven", reason[:200], None, adapted, kind)
    if witness:
        return Verdict("refuted", "different bags on a replayed database", witness, adapted, kind, source)
    if crash == "timeout" or "timed out" in (reason or ""):
        return Verdict("timeout", reason or f"no proof within {PROVE_SECONDS} s", None, adapted, kind)
    if crash:
        return Verdict("error", crash, None, adapted, kind)
    if (reason or "").startswith(("unsupported", "parse error")):
        return Verdict("unknown", reason[:200], None, adapted, kind)
    return Verdict("unknown", (reason or "no proof, no counterexample")[:200], None, adapted, kind)


_TABLES: dict[str, dict[str, Table]] = {}


def _decide_job(args):
    position, pair, instance = args
    tables = _TABLES.get(pair.benchmark)
    if tables is None:
        tables = _TABLES[pair.benchmark] = schema_for(pair.benchmark)
    try:
        return position, decide(pair, tables, instance)
    except Exception as error:  # noqa: BLE001 - one bad pair must not stop the run
        return position, Verdict("error", f"{type(error).__name__}: {str(error)[:200]}")


# --- overlap with other evals ---------------------------------------------------------------------


def fingerprint(sql: str, shape: bool = False) -> str:
    """The query lower-cased, unquoted and without layout; ``shape`` also masks every literal.

    Plain text processing rather than parsing, so the 80,000 SQLStorm queries take seconds.
    """

    text = re.sub(r"--[^\n]*", " ", sql).lower().replace('"', "").replace("`", "")
    if shape:
        text = re.sub(r"'[^']*'", "?", text)
        text = re.sub(r"(?<![\w.])\d+(\.\d+)?(?![\w.])", "?", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([(),=<>+\-*/;|])\s*", r"\1", text)
    return text.strip().rstrip(";")


def _overlap_corpora() -> dict[str, list[tuple[str, str]]]:
    """``{name: [(benchmark it can overlap, SQL)]}`` for the other evals' queries found locally."""

    import csv

    import benchmark_corpora as corpora

    found: dict[str, list[tuple[str, str]]] = {}
    fixtures = ROOT / "tests" / "fixtures"
    found["rbot-calcite"] = [("calcite", json.loads(line)["input_sql"]) for line in (fixtures / "rbot" / "calcite.jsonl").read_text().splitlines() if line.strip()]
    for suite, benchmark in (("calcite", "calcite"), ("tpch", "tpch")):
        lines = [line for line in (fixtures / "sqlsolver" / f"{suite}_pairs.txt").read_text().splitlines() if line.strip()]
        found[f"sqlsolver-{suite}"] = [(benchmark, line) for line in lines]
    queries = corpora.BENCH_DIR / "llm-r2" / "data" / "data_llmr2" / "queries"
    if queries.exists():
        csv.field_size_limit(1 << 30)
        for split in ("train", "test"):
            rows = []
            for dataset in ("tpch", "dsb"):
                with (queries / f"queries_{dataset}_{split}.csv").open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.reader(handle)
                    header = next(reader)
                    column = header.index("original_sql")
                    rows += [(dataset, row[column]) for row in reader if row and row[column].strip()]
            found[f"llm-r2 {split}"] = rows
    if (corpora.BENCH_DIR / "dsb-queries").exists():
        found["dsb (analytical coverage)"] = [("dsb", sql) for _, sql in corpora.queries("dsb")]
    if (corpora.BENCH_DIR / "SQLStorm").exists():
        found["sqlstorm (analytical coverage)"] = [("sqlstorm", sql) for _, sql in corpora.queries("sqlstorm/stackoverflow")]
        found["sqlstorm-tpch (analytical coverage)"] = [("tpch", sql) for _, sql in corpora.queries("sqlstorm/tpch")]
        found["sqlstorm-v0-tpch (analytical coverage)"] = [("tpch", sql) for _, sql in corpora.queries("sqlstorm-v0/tpch")]
    return found


def overlap(pairs: list[Pair]) -> dict:
    """How many distinct QUITE originals each other eval also holds, as the same text or the same shape."""

    sys.path.insert(0, str(ROOT / "tools"))
    prints: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for pair in pairs:  # per query: the texts its rewrites start from (some differ in layout or quoting)
        prints.setdefault(pair.benchmark, {}).setdefault(pair.query, set()).add((fingerprint(pair.original), fingerprint(pair.original, shape=True)))
    out = {}
    for name, rows in _overlap_corpora().items():
        exact: dict[str, set] = {}
        shapes: dict[str, set] = {}
        sizes: Counter = Counter()
        for benchmark, sql in rows:
            exact.setdefault(benchmark, set()).add(fingerprint(sql))
            shapes.setdefault(benchmark, set()).add(fingerprint(sql, shape=True))
            sizes[benchmark] += 1
        for benchmark in sorted(exact):
            if benchmark not in prints:
                continue
            mine = prints[benchmark]
            out[f"{name} / {benchmark}"] = {
                "quite_queries": len(mine),
                "same_text": sum(1 for texts in mine.values() if any(text in exact[benchmark] for text, _ in texts)),
                "same_shape": sum(1 for texts in mine.values() if any(shape in shapes[benchmark] for _, shape in texts)),
                "their_queries": sizes[benchmark],
            }
    return out


# --- running and scoring ---------------------------------------------------------------------


OUTCOMES = ("proven", "refuted", "unknown", "unsupported", "timeout", "error", "wrong")


@dataclass
class Report:
    pairs: list[Pair]
    verdicts: dict[int, Verdict] = field(default_factory=dict)
    seconds: float = 0.0

    def select(self, label: str | None = None, held_out: bool | None = None, benchmark: str | None = None, reason: str | None = None):
        for index, pair in enumerate(self.pairs):
            if label and pair.label != label:
                continue
            if held_out is not None and pair.held_out != held_out:
                continue
            if benchmark and pair.benchmark != benchmark:
                continue
            if reason and pair.reason != reason:
                continue
            yield pair, self.verdicts[index]

    def counts(self, **kw) -> Counter:
        return Counter(v.outcome for _, v in self.select(**kw))

    def equal_line(self, **kw) -> str:
        c = self.counts(label="equal", **kw)
        total = sum(c.values())
        return f"{c['proven']}/{total - c['refuted']} proved, {c['wrong']} wrong ({total} flagged equal, {c['refuted']} refuted)"

    def negatives(self, **kw) -> tuple[int, int, int]:
        """(real negatives: flagged-unequal pairs with a replayed difference, proofs of flagged-unequal pairs, wrong)."""

        c = self.counts(label="unequal", **kw)
        return c["refuted"] + c["wrong"], c["proven"], c["wrong"]


def run(pairs: list[Pair], workers: int | None = None, instance: Path | None = None, log: Path | None = None) -> Report:
    """Decide every pair. With ``log``, each verdict is appended to it as it lands and pairs it already
    holds are not decided again, so a long run can be stopped and resumed."""

    report = Report(pairs)
    start = time.time()
    done: dict[str, Verdict] = {}
    if log is not None and log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                key = row.pop("key")
                done[key] = Verdict(**row)
    jobs = []
    for position, pair in enumerate(pairs):
        if pair.key in done:
            report.verdicts[position] = done[pair.key]
        else:
            jobs.append((position, pair, instance))
    handle = log.open("a", encoding="utf-8") if log is not None else None
    try:
        with ProcessPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 1)) as pool:
            for position, verdict in pool.map(_decide_job, jobs, chunksize=1):
                report.verdicts[position] = verdict
                if handle is not None:
                    handle.write(json.dumps({"key": pairs[position].key, **vars(verdict)}, default=str) + "\n")
                    handle.flush()
    finally:
        if handle is not None:
            handle.close()
    report.seconds = time.time() - start
    return report


def sample(pairs: list[Pair], size: int) -> list[Pair]:
    """A fixed sample, stratified so each benchmark and flag keeps its share."""

    rng = random.Random(SAMPLE_SEED)
    groups: dict[tuple[str, str], list[Pair]] = {}
    for pair in pairs:
        groups.setdefault((pair.benchmark, pair.label), []).append(pair)
    chosen = []
    for key in sorted(groups):
        group = groups[key]
        share = max(1, round(size * len(group) / len(pairs)))
        chosen += rng.sample(group, min(share, len(group)))
    return chosen


def summary(report: Report) -> dict:
    out = {"pairs": len(report.pairs), "seconds": round(report.seconds), "benchmarks": {}}
    for benchmark in BENCHMARKS + (None,):
        name = benchmark or "all"
        entry = {}
        for label in ("equal", "unequal", "mixed"):
            entry[label] = dict(report.counts(label=label, benchmark=benchmark))
        for reason in ("rows", "error", "timeout"):
            entry[f"unequal_{reason}"] = dict(report.counts(label="unequal", reason=reason, benchmark=benchmark))
        out["benchmarks"][name] = entry
    return out


def results_rows(report: Report, date: str) -> dict[str, dict]:
    """The two results files: proofs of the flagged-equal pairs, and the flagged-unequal pairs that must not be proved."""

    eq = report.counts(label="equal")
    total_eq = sum(eq.values())
    held = report.counts(label="equal", held_out=True)
    un = report.counts(label="unequal")
    total_un = sum(un.values())
    reasons = {r: sum(report.counts(label="unequal", reason=r).values()) for r in ("rows", "error", "timeout", "documented")}
    refuted_by = {r: report.counts(label="unequal", reason=r)["refuted"] for r in reasons}
    tie_proofs = sum(1 for _, v in report.select(label="unequal") if v.outcome == "proven" and v.kind == "ties")
    held_un = report.counts(label="unequal", held_out=True)
    mixed = sum(report.counts(label="mixed").values())
    rewrites = sum(sum(p.systems.values()) for p in report.pairs)
    common = {"docs": "docs/evals/quite.md", "command": "python tools/quite_bench.py --write-results", "date": date}

    def coverage(c: Counter) -> dict:
        return {k: c[k] for k in OUTCOMES if k != "wrong" and (c[k] or k in ("proven", "refuted", "unknown"))}

    equal = {
        "suite": "QUITE LLM rewrites, flagged equal",
        "order": 36,
        "size": total_eq,
        "score": f"{eq['proven']}/{total_eq - eq['refuted']} proved, {eq['wrong']} wrong",
        "metric": (
            f"Distinct (query, rewrite) pairs, from {rewrites:,} rewrites by 13 systems of TPC-H, DSB, Calcite and SQLStorm "
            f"queries, that the authors' PostgreSQL run flagged equal, proved equivalent as result bags; "
            f"{eq['refuted']} more are refuted by a replayed database (instance-label disagreements, left out of the denominator)."
        ),
        "evidence": "proof",
        "correctness": (
            f"{eq['wrong']} wrong: every proof is replayed on the prover's counterexample, KumoSQL's targeted databases and, "
            "for TPC-H, a generated scale-0.01 instance; a difference counts only when DuckDB agrees with its optimizer off "
            "and, for order-sensitive queries, in three row orders"
        ),
        "coverage": coverage(eq),
        "held_out": f"{held['proven']}/{sum(held.values()) - held['refuted']} proved, {held['wrong']} wrong (a fifth of the queries, by hash, with all their rewrites)",
        "caveats": (
            "Data downloaded at a pinned commit (no licence; not bundled). Flags come from one PostgreSQL instance, so a "
            "refuted pair is a label disagreement, not an error. Quoted upper-case identifiers are folded to lower case "
            "(the Calcite queries quote tables created in lower case) and FETCH FIRST n ROWS ONLY is read as LIMIT n. "
            f"{mixed} pairs flagged both ways are not scored. No KumoSQL rule was changed for this eval."
        ),
        **common,
    }
    negatives = {
        "suite": "QUITE LLM rewrites, flagged unequal",
        "order": 37,
        "size": total_un,
        "score": f"{un['refuted'] + un['wrong']}/{total_un} refuted, {un['proven']} proved, {un['wrong']} wrong",
        "metric": (
            f"Pairs the authors' run flagged unequal: {reasons['rows']} because the results differed, {reasons['error']} because "
            f"the rewrite failed to run, {reasons['timeout']} because a query timed out, and the {reasons['documented']} documented "
            "TPC-H Q2 rewrites. Refuted means DuckDB returns different bags on a replayed database that respects the schema "
            f"(by flag reason: {refuted_by['rows']} rows, {refuted_by['error']} error, {refuted_by['timeout']} timeout, "
            f"{refuted_by['documented']} documented). A proof with a replayed difference would be wrong."
        ),
        "evidence": "executed",
        "correctness": (
            f"{un['wrong']} wrong (a proof with a replayed difference). Of the {un['proven']} proofs, {tie_proofs} rely on the "
            "prover's stated assumption that rows tied on ORDER BY are cut by LIMIT alike, where the authors' run saw "
            "tie-breaking; the others were flagged for a timeout or a failed run, not for different rows"
        ),
        "coverage": coverage(un),
        "held_out": f"{held_un['refuted'] + held_un['wrong']}/{sum(held_un.values())} refuted, {held_un['proven']} proved, {held_un['wrong']} wrong",
        "caveats": (
            "A flag of unequal is not a semantic label: timeouts and rewrites that failed to run are flagged too, so only "
            "replayed differences count as negatives. Refutations come from KumoSQL's own databases (the authors' instances "
            "are not public; TPC-H also runs on a generated scale-0.01 instance). Data downloaded at a pinned commit."
        ),
        **common,
    }
    return {"quite-rewrites": equal, "quite-negatives": negatives}


def print_report(report: Report) -> None:
    pairs = report.pairs
    rewrites = sum(sum(p.systems.values()) for p in pairs)
    print(f"{len(pairs)} distinct pairs from {rewrites} rewrites in {report.seconds:.0f}s")
    print(f"{'benchmark':10} {'flag':8} {'pairs':>6} " + " ".join(f"{o:>11}" for o in OUTCOMES))
    for benchmark in BENCHMARKS:
        for label in ("equal", "unequal", "mixed"):
            c = report.counts(label=label, benchmark=benchmark)
            if sum(c.values()):
                print(f"{benchmark:10} {label:8} {sum(c.values()):6} " + " ".join(f"{c[o]:11}" for o in OUTCOMES))
    print("flagged equal:", report.equal_line())
    print("  held out:   ", report.equal_line(held_out=True))
    for reason in ("rows", "error", "timeout"):
        c = report.counts(label="unequal", reason=reason)
        print(f"flagged unequal ({reason:7}): {sum(c.values())} pairs, {dict(c)}")
    negatives, proofs, wrong = report.negatives()
    print(f"flagged unequal: {negatives} refuted by a replayed database, {proofs} proved, {wrong} wrong")
    adapted = Counter(v.outcome for v in report.verdicts.values() if v.adapted)
    print("adapted (quoted upper-case identifiers folded):", dict(adapted))
    total = Counter(v.outcome for v in report.verdicts.values())
    print("all:", dict(total))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--benchmark", choices=BENCHMARKS, action="append")
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--sample", type=int, help="a pinned stratified sample of this many pairs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--no-instance", action="store_true", help="skip the generated TPC-H instance")
    parser.add_argument("--json", type=Path, help="write per-pair verdicts here")
    parser.add_argument("--write-results", action="store_true", help="write benchmarks/results/quite-*.json (whole corpus only)")
    parser.add_argument("--log", type=Path, help="append each verdict here as it lands; a rerun resumes from it")
    parser.add_argument("--show", choices=OUTCOMES, action="append", help="print the pairs with this outcome")
    parser.add_argument("--overlap", action="store_true", help="only report which originals other evals also hold "
                        "(fetch them first: python tools/benchmark_corpora.py fetch llm-r2 sqlstorm dsb)")
    args = parser.parse_args(argv)
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    pairs = load_pairs()
    if args.overlap:
        print(f"{'corpus / benchmark':45} {'QUITE queries':>13} {'same text':>10} {'same shape':>11} {'their queries':>14}")
        for name, row in overlap(pairs).items():
            print(f"{name:45} {row['quite_queries']:13} {row['same_text']:10} {row['same_shape']:11} {row['their_queries']:14}")
        return 0
    if args.benchmark:
        pairs = [p for p in pairs if p.benchmark in args.benchmark]
    if args.split != "all":
        pairs = [p for p in pairs if p.held_out == (args.split == "held-out")]
    if args.sample:
        pairs = sample(pairs, args.sample)
    pairs = pairs[: args.limit]
    instance = None if args.no_instance else tpch_instance()
    report = run(pairs, args.workers, instance, args.log)
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps([
            {"key": p.key, "case": p.case_id, "benchmark": p.benchmark, "label": p.label, "reason": p.reason,
             "held_out": p.held_out, "systems": dict(p.systems), "identical": p.identical,
             "outcome": v.outcome, "detail": v.detail, "adapted": v.adapted, "kind": v.kind, "source": v.source,
             "witness": v.witness, "original": p.original, "rewritten": p.rewritten}
            for p, v in ((p, report.verdicts[i]) for i, p in enumerate(pairs))
        ], indent=1, default=str))
    if args.write_results:
        if args.benchmark or args.split != "all" or args.sample or args.limit:
            parser.error("--write-results needs the whole corpus")
        sys.path.insert(0, str(ROOT / "tools"))
        from bench_common import today, write_results

        for name, row in results_rows(report, today()).items():
            write_results(name, row)
    for outcome in args.show or ():
        for index, pair in enumerate(pairs):
            verdict = report.verdicts[index]
            if verdict.outcome == outcome:
                print(f"\n#{pair.key} {pair.case_id} [{pair.label}/{pair.reason}] {verdict.outcome}: {verdict.detail} {verdict.source}")
                print("  O:", _strip(pair.original)[:400])
                print("  R:", _strip(pair.rewritten)[:400])
                if verdict.witness:
                    print("  witness:", json.dumps(verdict.witness, default=str)[:600])
    return 1 if any(v.outcome == "wrong" for v in report.verdicts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
