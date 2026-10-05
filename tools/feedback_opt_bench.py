"""Run the rewrites of a public feedback-driven SQL optimization artifact through KumoSQL's provers.

The artifact (Martin Kostov, https://github.com/KostovMartin/mk-feedback-driven-sql-optimization,
GPL-3.0-or-later) ships zipped run bundles in ``experiment-artifacts/``. Each bundle of a run on
PostgreSQL or DuckDB holds the workload's query templates (``query_templates.csv``), every
candidate rewrite that rules, seeded cases and a local language model produced for them
(``candidates.csv``) and the artifact's own result check of each candidate against its original
(``equivalence_checks.csv``: ``passed``, or a recorded reason such as ``row_multiset_mismatch``
with the rows that differ). The workloads are TPC-H at scale 1, ten hand-written "real-world"
queries over the same tables, drift variants of them and the 113 Join Order Benchmark queries on
IMDB. The licence is GPL, so nothing is copied into KumoSQL: the 24 bundles that hold result
checks are downloaded from a pinned commit into a cache folder and checked against SHA-256
digests.

Pairs are (template, candidate) texts, deduplicated across bundles, each with how the artifact's
result check went:

* ``equal``: the check passed on the artifact's data (three parameter sets for TPC-H, the fixed
  literals of JOB). This is agreement on one instance, not a proof.
* ``unequal``: both queries ran and a result row differed as a bag (``row_multiset_mismatch``).
* ``order``: the same row count but a different order (``ordered_rows_mismatch``), usually a tie in
  an ORDER BY. Kept apart: as bags the queries may well be equal.
* ``unchecked``: the check could not run (the candidate or the original raised an error).
* ``mixed``: passed in one run and showed different rows in another (not scored).

Every pair gets one verdict from the SQL and the TPC-H or JOB schema alone (nothing reads the
label while deciding): ``proven`` by the algebraic prover, ``refuted`` by a database on which
DuckDB returns different bags (confirmed with its optimizer off), or ``unknown``, ``unsupported``,
``timeout``, ``error``. A proof whose pair a replayed database separates is ``wrong``.

TPC-H templates take typed parameters (``$1::date``). For the prover each is a zero-argument
function (``kumo_param_1()``), a fixed but arbitrary value, so a proof holds for every parameter
value. For the database check each is bound to a TPC-H value (three bindings per pair).

    python tools/feedback_opt_bench.py                    # every pair
    python tools/feedback_opt_bench.py --sample 40        # a pinned stratified sample
    python tools/feedback_opt_bench.py --split dev        # without the held-out fifth
    python tools/feedback_opt_bench.py --check-sources    # download and verify every pinned file
    python tools/feedback_opt_bench.py --write-results    # feedback-opt-rewrites, feedback-opt-negatives
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import random
import re
import signal
import sys
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import quite_bench as qb  # noqa: E402  the replay machinery (schema model, DuckDB replayer, determinism, prover call)

COMMIT = "2d6de251f3befec6ef473b77196510243f59f2d5"
BASE = f"https://raw.githubusercontent.com/KostovMartin/mk-feedback-driven-sql-optimization/{COMMIT}/"
JOB_BASE = "https://raw.githubusercontent.com/gregrahn/join-order-benchmark/a39603662e023e449cb2121997a5034df9e02ebf/"
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "feedback-opt"
LICENCE = ("LICENSE", "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986")
JOB_SCHEMA = ("schema.sql", "1d10ba0c9a881e890aeb951c0960cc4e3472c9c9695ce1edb6493e9e4466d3d2")
# experiment-artifacts/<file>: SHA-256 at COMMIT (the bundles that hold at least one result check)
BUNDLES = {
    "drift-rules-m-20260612-221215-raw-run-data.zip": "bd04b3ccf12821b98dc76856c30143cfafd6cdc08dc9150c2d127addf25481be",
    "job-imdb-budget2-20260612-223257-raw-run-data.zip": "8095a1e335471bfe6cfef8ad09b8426aa8cd4d5c16a00717824a5e927237c423",
    "job-imdb-mixed-20260522-224026-raw-run-data.zip": "709a070f3716dc42bebfc6dd68048fffe8f12f48ba3d4e5ffb7d7d68a6701d9f",
    "job-imdb-mixed-20260523-101959-raw-run-data.zip": "8162f471f338aaceb1caa9c9b5150cde344d305ef4130ad6e9f51b387d3e8608",
    "job-imdb-mixed-20260524-090447-raw-run-data.zip": "0e1c27e51f1b97ca7a718d4bb2ab159dfc2ef829f9e54eb87a804d5ec0da2d74",
    "job-imdb-mixed-20260524-215655-raw-run-data.zip": "6ee188ca4923f0a96de468ae453136dbab84ccb42ed1a22f4d1e45b0a876a8b1",
    "job-imdb-mixed-duckdb-20260630-161415-raw-run-data.zip": "e9ef0ee1be53a3d78ed35be2673ec7eb40e40603bcbe50bb3ae5f7c2ab3e1208",
    "job-imdb-mixed-duckdb-unraid-20260702-125639-raw-run-data.zip": "33103ff0c82ce1bbc70820b7b21a25091f44f15cfaca60e64ddc271aa72aa201",
    "job-imdb-mixed-unraid-20260701-102515-raw-run-data.zip": "75679082000553e90ace1e43b81d69493183f60bcf04d199866dc29810be1734",
    "real-world-sf1-mixed-20260522-221945-raw-run-data.zip": "03568c90b734aa805d5cc2bdcfbc5e2cb20c7fcfd0bab7d75af48ae253df69ce",
    "real-world-sf1-mixed-20260523-100904-raw-run-data.zip": "fc923207cbc56b1bbdf9e2233d9dc4cc359006350cc1fb6760bfa78eb5a8d99c",
    "real-world-sf1-mixed-20260524-085401-raw-run-data.zip": "5938ce8c5f08469f8d64c5b8bde06beab86fc919478acd610a107b6f397df883",
    "real-world-sf1-mixed-20260524-214617-raw-run-data.zip": "45093fb64777560c41a23513a0d35160ab019647102b69e58e25f5299cbbb648",
    "real-world-sf1-mixed-duckdb-20260630-160514-raw-run-data.zip": "5df49e439afa6299d02488ebf40f73cf480a53089f93cb552b84aa4cf43d261f",
    "real-world-sf1-mixed-duckdb-unraid-20260701-090642-raw-run-data.zip": "9c0d3d60e4a8f13a479437fe882374be2cda94131826bd46dfa75e78f725b9be",
    "real-world-sf1-mixed-unraid-20260701-090642-raw-run-data.zip": "90f9d6f60012dfdc3e9145ffc320ac0f60012d45edc100c0a6ea98d80417b527",
    "tpch-sf1-mixed-20260522-213611-raw-run-data.zip": "7fffba90295ef46aeb5399a6b2971f7d0c5318639eb8453521110296f280365d",
    "tpch-sf1-mixed-20260523-093915-raw-run-data.zip": "d90cb4c454714b03fb0aa133d203f296ca6197383b287ebee4e2b967b8175849",
    "tpch-sf1-mixed-20260524-082213-raw-run-data.zip": "be311301c4396950dfe23bcf35b572b3fc57bf395bc1ad805dc2c7ceda6ff7b1",
    "tpch-sf1-mixed-20260524-210900-raw-run-data.zip": "192347067ba576a54b6c52e7389d70840182c8c6ee230d2940aafdb76f974565",
    "tpch-sf1-mixed-duckdb-20260630-153651-raw-run-data.zip": "73b4d7cd9359b854509824facd9cec76d06a5427a06b6aaa38293e0531b89d78",
    "tpch-sf1-mixed-duckdb-unraid-20260701-090642-raw-run-data.zip": "5a9f590717245cfc4898d967fb96187b6a6dbeffc4cf77ca83c90869b553abad",
    "tpch-sf1-mixed-unraid-20260701-090642-raw-run-data.zip": "a9943301a1aeffc87a52a1e4a08d29e08cc8f01158da58ee2f65a4834e8f0ba4",
}
# The TPC-H schema as the specification gives it (every column NOT NULL, the keys and foreign keys of clause 1.4).
TPCH_DDL = """
CREATE TABLE region (r_regionkey integer NOT NULL, r_name char(25) NOT NULL, r_comment varchar(152),
  PRIMARY KEY (r_regionkey));
CREATE TABLE nation (n_nationkey integer NOT NULL, n_name char(25) NOT NULL, n_regionkey integer NOT NULL,
  n_comment varchar(152), PRIMARY KEY (n_nationkey), FOREIGN KEY (n_regionkey) REFERENCES region (r_regionkey));
CREATE TABLE part (p_partkey integer NOT NULL, p_name varchar(55) NOT NULL, p_mfgr char(25) NOT NULL,
  p_brand char(10) NOT NULL, p_type varchar(25) NOT NULL, p_size integer NOT NULL, p_container char(10) NOT NULL,
  p_retailprice decimal(15,2) NOT NULL, p_comment varchar(23) NOT NULL, PRIMARY KEY (p_partkey));
CREATE TABLE supplier (s_suppkey integer NOT NULL, s_name char(25) NOT NULL, s_address varchar(40) NOT NULL,
  s_nationkey integer NOT NULL, s_phone char(15) NOT NULL, s_acctbal decimal(15,2) NOT NULL,
  s_comment varchar(101) NOT NULL, PRIMARY KEY (s_suppkey), FOREIGN KEY (s_nationkey) REFERENCES nation (n_nationkey));
CREATE TABLE partsupp (ps_partkey integer NOT NULL, ps_suppkey integer NOT NULL, ps_availqty integer NOT NULL,
  ps_supplycost decimal(15,2) NOT NULL, ps_comment varchar(199) NOT NULL, PRIMARY KEY (ps_partkey, ps_suppkey),
  FOREIGN KEY (ps_partkey) REFERENCES part (p_partkey), FOREIGN KEY (ps_suppkey) REFERENCES supplier (s_suppkey));
CREATE TABLE customer (c_custkey integer NOT NULL, c_name varchar(25) NOT NULL, c_address varchar(40) NOT NULL,
  c_nationkey integer NOT NULL, c_phone char(15) NOT NULL, c_acctbal decimal(15,2) NOT NULL,
  c_mktsegment char(10) NOT NULL, c_comment varchar(117) NOT NULL, PRIMARY KEY (c_custkey),
  FOREIGN KEY (c_nationkey) REFERENCES nation (n_nationkey));
CREATE TABLE orders (o_orderkey integer NOT NULL, o_custkey integer NOT NULL, o_orderstatus char(1) NOT NULL,
  o_totalprice decimal(15,2) NOT NULL, o_orderdate date NOT NULL, o_orderpriority char(15) NOT NULL,
  o_clerk char(15) NOT NULL, o_shippriority integer NOT NULL, o_comment varchar(79) NOT NULL,
  PRIMARY KEY (o_orderkey), FOREIGN KEY (o_custkey) REFERENCES customer (c_custkey));
CREATE TABLE lineitem (l_orderkey integer NOT NULL, l_partkey integer NOT NULL, l_suppkey integer NOT NULL,
  l_linenumber integer NOT NULL, l_quantity decimal(15,2) NOT NULL, l_extendedprice decimal(15,2) NOT NULL,
  l_discount decimal(15,2) NOT NULL, l_tax decimal(15,2) NOT NULL, l_returnflag char(1) NOT NULL,
  l_linestatus char(1) NOT NULL, l_shipdate date NOT NULL, l_commitdate date NOT NULL, l_receiptdate date NOT NULL,
  l_shipinstruct char(25) NOT NULL, l_shipmode char(10) NOT NULL, l_comment varchar(44) NOT NULL,
  PRIMARY KEY (l_orderkey, l_linenumber), FOREIGN KEY (l_orderkey) REFERENCES orders (o_orderkey),
  FOREIGN KEY (l_partkey, l_suppkey) REFERENCES partsupp (ps_partkey, ps_suppkey));
"""
PROVE_SECONDS = qb.PROVE_SECONDS
SAMPLE_SEED = 2026
BINDINGS = 3  # parameter bindings replayed per TPC-H pair


# --- data ----------------------------------------------------------------------------------


def _download(url: str, path: Path, digest: str) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic, so parallel tests never read half a file
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"{path} does not match the pinned version; delete it to download again")
    return path


def fetch(bundles: list[str] | None = None) -> Path:
    """The pinned bundles, the licence and JOB's schema, downloaded once into the cache folder."""

    _download(BASE + LICENCE[0], CACHE / LICENCE[0], LICENCE[1])
    _download(JOB_BASE + JOB_SCHEMA[0], CACHE / "job-schema.sql", JOB_SCHEMA[1])
    for name in bundles or BUNDLES:
        _download(BASE + f"experiment-artifacts/{name}", CACHE / name, BUNDLES[name])
    return CACHE


def _rows(archive: zipfile.ZipFile, member: str) -> list[dict]:
    import csv

    names = {name.replace("\\", "/"): name for name in archive.namelist()}
    csv.field_size_limit(1 << 30)
    return list(csv.DictReader(io.StringIO(archive.read(names[member]).decode("utf-8-sig"))))


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip())


def _kind(row: dict) -> str:
    """How the artifact's check of one candidate went: pass, rows, order, error or other."""

    if row["passed"] == "t":
        return "pass"
    try:
        reason = json.loads(row["mismatch_detail"] or "{}").get("reason", "")
    except ValueError:
        reason = ""
    return {"row_multiset_mismatch": "rows", "ordered_rows_mismatch": "order", "query_execution_error": "error"}.get(reason, "other")


@dataclass
class Pair:
    family: str  # tpch | real-world | drift | job
    original: str  # the template as the artifact stores it (typed ``$n::type`` parameters for TPC-H)
    rewritten: str
    sources: Counter = field(default_factory=Counter)  # who made the candidate: rule, llm, seeded
    runs: set = field(default_factory=set)
    checks: Counter = field(default_factory=Counter)  # the artifact's result checks: pass, rows, order, error, other
    engines: set = field(default_factory=set)

    @property
    def workload(self) -> str:
        return "job" if self.family == "job" else "tpch"

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.family}\n{self.original}\n{self.rewritten}".encode()).hexdigest()[:12]

    @property
    def case_id(self) -> str:
        return f"feedback-opt/{self.family}/{hashlib.sha1(_flat(self.original).encode()).hexdigest()[:10]}"

    @property
    def held_out(self) -> bool:
        """One query in five, by a hash of its id, is held out with every rewrite of it."""

        return int(hashlib.sha1(self.case_id.encode()).hexdigest(), 16) % 5 == 0

    @property
    def label(self) -> str:
        c = self.checks
        if c["rows"] and c["pass"]:
            return "mixed"
        if c["rows"]:
            return "unequal"
        if c["pass"]:
            return "equal"
        if c["order"]:
            return "order"
        return "unchecked"

    @property
    def parameters(self) -> bool:
        return bool(PARAM.search(self.original))


def load_pairs(root: Path | None = None) -> list[Pair]:
    """Distinct (family, template, candidate) texts in bundle order, with the artifact's checks merged."""

    root = root or fetch()
    pairs: dict[tuple[str, str, str], Pair] = {}
    for name in sorted(BUNDLES):
        family = "real-world" if name.startswith("real-world") else name.split("-")[0]
        engine = "duckdb" if "-duckdb" in name else "postgres"
        with zipfile.ZipFile(root / name) as archive:
            templates = {r["template_fingerprint"]: r["normalized_sql"] for r in _rows(archive, "query_templates.csv")}
            candidates = {r["id"]: r for r in _rows(archive, "candidates.csv")}
            checks = _rows(archive, "equivalence_checks.csv")
        for check in checks:
            candidate = candidates[check["candidate_id"]]
            original = templates[candidate["template_fingerprint"]]
            key = (family, _flat(original), _flat(candidate["sql_text"]))
            pair = pairs.get(key)
            if pair is None:
                pair = pairs[key] = Pair(family, original.strip().rstrip(";"), candidate["sql_text"].strip().rstrip(";"))
            pair.checks[_kind(check)] += 1
            pair.engines.add(engine)
            pair.runs.add(name.removesuffix("-raw-run-data.zip"))
            pair.sources[candidate["source_type"]] += 1
    return list(pairs.values())


# --- parameters ------------------------------------------------------------------------------

PARAM = re.compile(r"\$(\d+)(?:::(\w+))?")


def parameter_types(original: str) -> dict[int, str]:
    """``{n: type}`` from the casts the template writes (``$3::date``); a bare ``$n`` reads as text."""

    types = {int(n): "text" for n, _ in PARAM.findall(original)}
    for n, kind in PARAM.findall(original):
        if kind:
            types[int(n)] = kind.lower()
    return types


def with_functions(sql: str, types: dict[int, str]) -> str:
    """Each parameter as ``CAST(kumo_param_n() AS type)``: one fixed, arbitrary value that both queries share."""

    return PARAM.sub(lambda m: f"CAST(kumo_param_{m.group(1)}() AS {types.get(int(m.group(1)), 'text')})", sql)


_NATIONS = ["ALGERIA", "ARGENTINA", "BRAZIL", "CANADA", "EGYPT", "ETHIOPIA", "FRANCE", "GERMANY", "INDIA", "INDONESIA", "IRAN",
            "IRAQ", "JAPAN", "JORDAN", "KENYA", "MOROCCO", "MOZAMBIQUE", "PERU", "CHINA", "ROMANIA", "SAUDI ARABIA", "VIETNAM",
            "RUSSIA", "UNITED KINGDOM", "UNITED STATES"]
_TYPES1 = ["STANDARD", "SMALL", "MEDIUM", "LARGE", "ECONOMY", "PROMO"]
_TYPES2 = ["ANODIZED", "BURNISHED", "PLATED", "POLISHED", "BRUSHED"]
_TYPES3 = ["TIN", "NICKEL", "BRASS", "STEEL", "COPPER"]
_CONTAINERS = [f"{a} {b}" for a in ("SM", "LG", "MED", "JUMBO", "WRAP") for b in ("CASE", "BOX", "BAG", "JAR", "PKG", "PACK", "CAN", "DRUM")]
_COLORS = ["almond", "antique", "aquamarine", "azure", "beige", "bisque", "black", "blue", "blush", "brown", "burlywood", "chartreuse",
           "chiffon", "chocolate", "coral", "cornflower", "cream", "cyan", "forest", "frosted", "green", "honeydew", "ivory", "khaki"]
_DATES = [f"{y}-{m:02d}-01" for y in range(1993, 1998) for m in range(1, 13)]
# the domains the TPC-H specification gives each column a query can compare a parameter with
DOMAINS: dict[str, list] = {
    "r_name": ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"],
    "n_name": _NATIONS,
    "nation": _NATIONS,  # Q8 names its subquery column nation
    "c_mktsegment": ["AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY"],
    "o_orderpriority": ["1-URGENT", "2-HIGH", "3-MEDIUM", "4-NOT SPECIFIED", "5-LOW"],
    "o_orderstatus": ["F", "O", "P"],
    "o_clerk": [f"Clerk#{n:09d}" for n in (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)],
    "o_orderdate": _DATES,
    "l_shipdate": _DATES,
    "l_commitdate": _DATES,
    "l_receiptdate": _DATES,
    "l_returnflag": ["R", "A", "N"],
    "l_linestatus": ["O", "F"],
    "l_shipmode": ["REG AIR", "AIR", "RAIL", "SHIP", "TRUCK", "MAIL", "FOB"],
    "l_shipinstruct": ["DELIVER IN PERSON", "COLLECT COD", "NONE", "TAKE BACK RETURN"],
    "l_quantity": list(range(1, 51)),
    "l_discount": [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09],
    "l_tax": [0.0, 0.02, 0.04, 0.06, 0.08],
    "l_extendedprice": [1000, 5000, 10000, 30000, 50000],
    "ps_supplycost": [50, 100, 250, 500, 750, 900],
    "ps_availqty": [500, 2000, 5000, 9000],
    "o_totalprice": [10000, 50000, 100000, 200000, 350000],
    "o_custkey": [1, 7, 42, 100, 500, 1000],
    "p_partkey": [1, 10, 100, 1000],
    "p_brand": [f"Brand#{a}{b}" for a in range(1, 6) for b in range(1, 6)],
    "p_container": _CONTAINERS,
    "p_type": [f"{a} {b} {c}" for a in _TYPES1 for b in _TYPES2 for c in _TYPES3],
    "p_size": list(range(1, 51)),
    "p_name": _COLORS,
    "o_comment": ["special", "pending", "unusual", "express", "packages", "requests", "accounts", "deposits"],
    "s_comment": ["Customer", "Complaints", "furiously", "carefully"],
    "c_phone": ["13", "31", "23", "29", "30", "18", "17"],
    "c_acctbal": [0, 1000, 5000, 9000],
}
_FALLBACK = {"date": _DATES, "text": ["x", "AIR", "1"], "numeric": [0.0001, 0.01, 1, 10, 100], "integer": list(range(1, 11))}


def _contexts(original: str) -> dict[int, tuple[str | None, str]]:
    """``{n: (column the parameter is compared with, role)}``; the role is ``value``, ``word`` (inside a LIKE
    pattern that supplies its own ``%``) or ``pattern`` (the whole LIKE pattern)."""

    text = PARAM.sub(lambda m: f"kumo_param_{m.group(1)}()", original)
    try:
        tree = sqlglot.parse_one(text, read="postgres")
    except (sqlglot.errors.SqlglotError, ValueError):
        return {}
    out: dict[int, tuple[str | None, str]] = {}
    compare = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between, exp.In, exp.Like, exp.ILike)
    for node in tree.find_all(exp.Anonymous):
        name = str(node.name)
        if not name.startswith("kumo_param_"):
            continue
        n = int(name.rsplit("_", 1)[1])
        column, role, current, via = None, "value", node, []
        while current.parent is not None and column is None:
            parent = current.parent
            via.append(parent)
            if isinstance(parent, (exp.Select, exp.Subquery, exp.Mul, exp.Div)):
                break  # a scalar built from the parameter: no column to borrow a value from
            if isinstance(parent, compare):
                others = [a for a in (parent.this, parent.args.get("expression")) if a is not None and a is not current]
                if isinstance(parent, (exp.Between, exp.In)):
                    others = [parent.this]
                for other in others:
                    found = other.find(exp.Column)
                    if found is not None:
                        column = found.name.lower()
                if isinstance(parent, (exp.Like, exp.ILike)):
                    role = "word" if any(isinstance(v, exp.DPipe) for v in via) else "pattern"
            current = parent
        out[n] = (column, role)
    return out


def _pool(column: str | None, role: str, kind: str) -> list:
    pool = DOMAINS.get(column or "", _FALLBACK.get(kind, _FALLBACK["text"]))
    if role in ("word", "pattern") and column == "p_type":
        pool = sorted({word for value in pool for word in str(value).split()})
    if role == "pattern":
        pool = [f"{value}%" for value in pool]  # the parameter is the whole LIKE pattern: a prefix of a domain value
    return pool


def _orderable(values: list) -> bool:
    return all(isinstance(v, (int, float)) or re.fullmatch(r"\d{4}-\d\d-\d\d", str(v)) for v in values)


def bindings(original: str, count: int = BINDINGS) -> list[dict[int, str]]:
    """``count`` assignments of every parameter to a SQL literal, drawn from the domain of the column each is compared
    with. Parameters compared with the same column get distinct values, dates and numbers in ascending parameter
    order, so ``>= $1 AND < $2`` is a real range. Fixed by the template's text: the same on every run."""

    types = parameter_types(original)
    if not types:
        return []
    contexts = _contexts(original)
    groups: dict[tuple, list[int]] = {}
    for n in sorted(types):
        column, role = contexts.get(n, (None, "value"))
        groups.setdefault((column, role, types[n]), []).append(n)
    out = []
    for k in range(count):
        rng = random.Random(hashlib.sha1(f"{_flat(original)}|{k}".encode()).hexdigest())
        literals: dict[int, str] = {}
        for (column, role, kind), numbers in groups.items():
            pool = _pool(column, role, kind)
            drawn = rng.sample(pool, len(numbers)) if len(pool) >= len(numbers) else rng.choices(pool, k=len(numbers))
            if _orderable(drawn):
                drawn.sort()
            for n, value in zip(numbers, drawn):
                numeric = kind in ("integer", "int", "bigint", "numeric", "decimal", "double", "float") and not isinstance(value, str)
                literals[n] = repr(value) if numeric else "'" + str(value).replace("'", "''") + "'"
        out.append(literals)
    return out


def bound(sql: str, types: dict[int, str], literals: dict[int, str]) -> str:
    """The query with every parameter replaced by its typed literal."""

    return PARAM.sub(lambda m: f"CAST({literals[int(m.group(1))]} AS {types.get(int(m.group(1)), 'text')})", sql)


# --- schemas -------------------------------------------------------------------------------------


def schema_for(workload: str, root: Path | None = None) -> dict[str, qb.Table]:
    if workload == "tpch":
        return qb.load_schema(TPCH_DDL)
    path = fetch() / "job-schema.sql" if root is None else root / "job-schema.sql"
    return qb.load_schema(path.read_text(encoding="utf-8"))


# --- deciding a pair ---------------------------------------------------------------------------


def to_postgres(sql: str) -> tuple[str, bool]:
    """``(PostgreSQL text, read as DuckDB)``: the candidates of a DuckDB run may use DuckDB spellings."""

    try:
        return qb.adapt(sql)[0], False
    except (sqlglot.errors.SqlglotError, ValueError):
        tree = sqlglot.parse_one(sql.strip().rstrip(";"), read="duckdb")
        return qb.adapt(tree.sql(dialect="postgres"))[0], True


def retype(rows_by_table: dict, tables: dict[str, qb.Table]) -> dict:
    """The prover's counterexample with every value given the type its column has.

    The model names values with strings and booleans whatever the column's type. An injective map from those to
    integers keeps every equality and inequality the model relied on, and the replay decides whether the result
    still separates the queries, so a repaired database is a proposal, never evidence by itself.
    """

    ids: dict[tuple, int] = {}
    out = {}
    for name, rows in rows_by_table.items():
        table = tables.get(name.lower().split(".")[-1])
        columns = {c.lower(): qb._duck_type(t) for c, t in table.columns.items()} if table else {}
        fixed = []
        for row in rows:
            new = {}
            for column, value in row.items():
                kind = columns.get(str(column).lower(), "VARCHAR")
                if value is not None and (kind in ("BIGINT", "DOUBLE") or kind.startswith("DECIMAL")):
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        value = ids.setdefault((type(value).__name__, value), 1000 + len(ids))
                elif value is not None and kind == "VARCHAR" and not isinstance(value, str):
                    value = str(value)
                new[column] = value
            fixed.append(new)
        out[name] = fixed
    return out


class _Deadline(Exception):
    pass


def _alarm(signum, frame):
    raise _Deadline()


def search(left: str, right: str, tables: dict[str, qb.Table], replayer: qb.Replayer, kind: str, proposed: list, instance: Path | None, targeted: bool):
    """``(witness, source)`` of a replayed difference on one binding, or ``(None, "")``: the databases proposed
    (the prover's counterexample), the TPC-H instance, then KumoSQL's targeted databases."""

    duck_left, duck_right = qb.replay_sql(left), qb.replay_sql(right)
    for source, data in proposed:
        found = replayer.differs(duck_left, duck_right, data, kind)
        if found:
            return found, source
    if instance is not None and kind == "fixed":
        found = qb.instance_differs(instance, duck_left, duck_right)
        if found:
            return found, "TPC-H instance"
    if not targeted or os.environ.get("KUMOSQL_TARGETED", "1") == "0":
        return None, ""
    from kumosql.refute import find_targeted_difference
    from kumosql.result_equivalence import DataRules

    trees = [sqlglot.parse_one(sql, read="postgres") for sql in (left, right)]
    used = qb._used(trees, tables)
    schema = {t.name: {c: qb._bq_type(ty) for c, ty in t.columns.items()} for t in used.values()}
    rules = {t.name: DataRules(frozenset(t.not_null), tuple(t.keys)) for t in used.values()}
    single = [(t.name, c[0], p, pc[0]) for t in used.values() for c, p, pc in t.foreign if len(c) == 1 and p in used]
    fixed_left, fixed_right = (sqlglot.parse_one(d, read="duckdb").sql(dialect="postgres") for d in (duck_left, duck_right))
    try:
        found = find_targeted_difference(
            fixed_left, fixed_right, schema, rules, foreign_keys=single, dialect="postgres",
            settings=qb.DUCKDB_SETTINGS, budget=qb.SEARCH_SECONDS, timeout=qb.QUERY_SECONDS,
        )
    except Exception:  # noqa: BLE001 - no database is no evidence
        found = None
    if found is None:
        return None, ""
    data = {
        name.lower(): [tuple(qb._value(v, qb._duck_type(used[name.lower()].columns[c])) for (c, _), v in zip(t.columns, row)) for row in t.rows]
        for name, t in found.dataset.tables.items()
    }
    witness = replayer.differs(duck_left, duck_right, data, kind)
    return (witness, f"targeted: {found.label}") if witness else (None, "")


_REPLAYERS: dict[str, qb.Replayer] = {}
_TABLES: dict[str, dict[str, qb.Table]] = {}


def decide(pair: Pair, tables: dict[str, qb.Table], instance: Path | None = None) -> qb.Verdict:
    """Proven (then replayed), refuted (with a replayed database) or unknown. Never reads the label."""

    types = parameter_types(pair.original)
    try:
        plain_left, plain_right = pair.original, pair.rewritten
        left, right = (to_postgres(with_functions(sql, types))[0] for sql in (plain_left, plain_right))
        literal_sets = bindings(pair.original)
        bound_pairs = [tuple(to_postgres(bound(sql, types, lits))[0] for sql in (plain_left, plain_right)) for lits in literal_sets] or [(left, right)]
        trees = [sqlglot.parse_one(sql, read="postgres") for sql in (left, right)]
    except (sqlglot.errors.SqlglotError, ValueError, KeyError) as error:
        return qb.Verdict("unsupported", f"parse error: {str(error)[:200]}")
    kinds = {qb.determinism(tree) for tree in trees}
    kind = "random" if "random" in kinds else "ties" if "ties" in kinds else "fixed"
    replayer = _REPLAYERS.get(pair.workload)
    if replayer is None:
        replayer = _REPLAYERS[pair.workload] = qb.Replayer(tables)
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PROVE_SECONDS)
    crash = ""
    try:
        result = qb.prove(left, right, tables)
        proved, reason = result.proven, result.reason
        proposed = []
        counter = getattr(result, "counterexample", None)
        if counter is not None:
            data = qb.as_data(retype(counter.tables, tables), tables)
            if data is not None:
                proposed.append(("prover counterexample", data))
    except _Deadline:
        proved, reason, proposed, crash = False, "", [], "timeout"
    except Exception as error:  # noqa: BLE001 - a crash is a failure to prove, never a proof
        proved, reason, proposed, crash = False, "", [], f"{type(error).__name__}: {str(error)[:200]}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    witness, source = None, ""
    for index, (bound_left, bound_right) in enumerate(bound_pairs):
        try:
            witness, source = search(bound_left, bound_right, tables, replayer, kind, proposed if index == 0 else [], instance if pair.workload == "tpch" else None, targeted=index == 0)
        except (sqlglot.errors.SqlglotError, ValueError):
            continue
        if witness:
            break
    if proved:
        if witness:
            return qb.Verdict("wrong", "proved equivalent, but a replayed database separates the queries", witness, False, kind, source)
        return qb.Verdict("proven", reason[:200], None, False, kind)
    if witness:
        return qb.Verdict("refuted", "different bags on a replayed database", witness, False, kind, source)
    if crash == "timeout" or "timed out" in (reason or ""):
        return qb.Verdict("timeout", reason or f"no proof within {PROVE_SECONDS} s", None, False, kind)
    if crash:
        return qb.Verdict("error", crash, None, False, kind)
    return qb.Verdict("unknown", (reason or "no proof, no counterexample")[:200], None, False, kind)


def _decide_job(args):
    position, pair, instance = args
    tables = _TABLES.get(pair.workload)
    if tables is None:
        tables = _TABLES[pair.workload] = schema_for(pair.workload)
    try:
        return position, decide(pair, tables, instance)
    except Exception as error:  # noqa: BLE001 - one bad pair must not stop the run
        return position, qb.Verdict("error", f"{type(error).__name__}: {str(error)[:200]}")


# --- running and scoring -----------------------------------------------------------------------

OUTCOMES = qb.OUTCOMES
LABELS = ("equal", "unequal", "order", "unchecked", "mixed")
FAMILIES = ("tpch", "real-world", "drift", "job")


@dataclass
class Report:
    pairs: list[Pair]
    verdicts: dict[int, qb.Verdict] = field(default_factory=dict)
    seconds: float = 0.0

    def select(self, label: str | None = None, held_out: bool | None = None, family: str | None = None):
        for index, pair in enumerate(self.pairs):
            if label and pair.label != label:
                continue
            if held_out is not None and pair.held_out != held_out:
                continue
            if family and pair.family != family:
                continue
            yield pair, self.verdicts[index]

    def counts(self, **kw) -> Counter:
        return Counter(v.outcome for _, v in self.select(**kw))

    def equal_line(self, **kw) -> str:
        c = self.counts(label="equal", **kw)
        total = sum(c.values())
        return f"{c['proven']}/{total - c['refuted']} proved, {c['wrong']} wrong ({total} checked equal, {c['refuted']} refuted)"


def run(pairs: list[Pair], workers: int | None = None, instance: Path | None = None, log: Path | None = None) -> Report:
    """Decide every pair. With ``log``, each verdict is appended to it as it lands and pairs it already holds
    are not decided again, so a long run can be stopped and resumed."""

    import time

    report = Report(pairs)
    start = time.time()
    done: dict[str, qb.Verdict] = {}
    if log is not None and log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                key = row.pop("key")
                done[key] = qb.Verdict(**row)
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
    """A fixed sample, stratified so each family and label keeps its share."""

    rng = random.Random(SAMPLE_SEED)
    groups: dict[tuple[str, str], list[Pair]] = {}
    for pair in pairs:
        groups.setdefault((pair.family, pair.label), []).append(pair)
    chosen = []
    for key in sorted(groups):
        group = groups[key]
        share = max(1, round(size * len(group) / len(pairs)))
        chosen += rng.sample(group, min(share, len(group)))
    return chosen


def results_rows(report: Report, date: str) -> dict[str, dict]:
    """The two results files: proofs of the pairs whose check passed, and the pairs the artifact saw differ (never proved)."""

    eq = report.counts(label="equal")
    total_eq = sum(eq.values())
    held = report.counts(label="equal", held_out=True)
    un = report.counts(label="unequal")
    total_un = sum(un.values())
    held_un = report.counts(label="unequal", held_out=True)
    other = {label: sum(report.counts(label=label).values()) for label in ("order", "unchecked", "mixed")}
    wrong_all = sum(1 for v in report.verdicts.values() if v.outcome == "wrong")
    sources = Counter()
    for pair, _ in report.select(label="equal"):
        sources["rule" if pair.sources["rule"] and not pair.sources["llm"] else "llm" if pair.sources["llm"] else "seeded"] += 1
    common = {"docs": "docs/evals/feedback-optimization.md", "command": "python tools/feedback_opt_bench.py --write-results", "date": date}

    def coverage(c: Counter) -> dict:
        return {k: c[k] for k in OUTCOMES if k != "wrong" and (c[k] or k in ("proven", "refuted", "unknown"))}

    families = {f: sum(report.counts(label="equal", family=f).values()) for f in FAMILIES}
    equal = {
        "suite": "Feedback-driven optimization rewrites, check passed",
        "order": 372,
        "size": total_eq,
        "score": f"{eq['proven']}/{total_eq - eq['refuted']} proved, {eq['wrong']} wrong",
        "metric": (
            f"Distinct (template, candidate) pairs from {len(BUNDLES)} run bundles of a public feedback-driven SQL optimization artifact "
            f"({families['job']} on the Join Order Benchmark, {total_eq - families['job']} on TPC-H, its real-world and drift queries) "
            f"whose own result check passed on its data, proved equivalent as result bags for every parameter value; "
            f"{eq['refuted']} more are refuted by a replayed database (instance-label disagreements, left out of the denominator)."
        ),
        "evidence": "proof",
        "correctness": (
            f"{eq['wrong']} wrong: every proof is replayed on the prover's counterexample, a generated TPC-H instance at scale 0.01 "
            "(three parameter bindings per query) and KumoSQL's targeted databases; a difference counts only when DuckDB agrees with its "
            "optimizer off and, for order-sensitive queries, in three row orders"
        ),
        "coverage": coverage(eq),
        "held_out": f"{held['proven']}/{sum(held.values()) - held['refuted']} proved, {held['wrong']} wrong (a fifth of the queries, by hash, with all their rewrites)",
        "caveats": (
            "GPL-3.0-or-later bundles downloaded at a pinned commit, never committed. A passed check is agreement on the artifact's PostgreSQL "
            "or DuckDB data (three parameter sets for TPC-H, the fixed literals for JOB), so a refuted pair is an instance-label disagreement, not "
            f"an error. {other['unchecked']} pairs whose check could not run, {other['order']} that differed only in row order and {other['mixed']} "
            "that passed in one run and differed in another are not scored here. The JOB data (IMDB) is not available, so JOB pairs are refuted only "
            "on databases KumoSQL generates from the schema. Template parameters are fixed arbitrary values for the prover (a proof holds for every "
            "value) and TPC-H values for the replays. Tuned on test: no; no KumoSQL rule was changed for this eval."
        ),
        **common,
    }
    negatives = {
        "suite": "Feedback-driven optimization rewrites, rows differed",
        "order": 373,
        "size": total_un,
        "score": f"{un['refuted'] + un['wrong']}/{total_un} refuted, {un['proven']} proved, {un['wrong']} wrong",
        "metric": (
            f"Pairs whose own result check ran both queries and found different rows as a bag (a row only one side returns). "
            f"Refuted means DuckDB returns different bags on a replayed database that respects the schema; "
            f"a proof of a pair a replayed database separates would be wrong."
        ),
        "evidence": "executed",
        "correctness": f"{un['wrong']} wrong (a proof with a replayed difference); {un['proven']} of the {total_un} pairs are proved",
        "coverage": coverage(un),
        "held_out": f"{held_un['refuted'] + held_un['wrong']}/{sum(held_un.values())} refuted, {held_un['proven']} proved, {held_un['wrong']} wrong",
        "caveats": (
            "Few pairs: a model rewrite whose check found a differing row is rare in this artifact. The recorded rows come from the artifact's own "
            "data, which is not available, so a refutation comes from a database KumoSQL builds (TPC-H instance at scale 0.01, targeted databases). "
            f"All outcomes together: {wrong_all} wrong. Data downloaded at a pinned commit. Tuned on test: no."
        ),
        **common,
    }
    return {"feedback-opt-rewrites": equal, "feedback-opt-negatives": negatives}


def print_report(report: Report) -> None:
    pairs = report.pairs
    print(f"{len(pairs)} distinct pairs in {report.seconds:.0f}s")
    print(f"{'family':11} {'label':10} {'pairs':>6} " + " ".join(f"{o:>11}" for o in OUTCOMES))
    for family in FAMILIES:
        for label in LABELS:
            c = report.counts(label=label, family=family)
            if sum(c.values()):
                print(f"{family:11} {label:10} {sum(c.values()):6} " + " ".join(f"{c[o]:11}" for o in OUTCOMES))
    print("check passed:", report.equal_line())
    print("  held out:  ", report.equal_line(held_out=True))
    for label in ("unequal", "order", "unchecked", "mixed"):
        c = report.counts(label=label)
        print(f"{label:9}: {sum(c.values())} pairs, {dict(c)}")
    print("all:", dict(Counter(v.outcome for v in report.verdicts.values())))


def check_sources() -> int:
    """Download every pinned file and verify its digest."""

    fetch()
    print(f"{len(BUNDLES)} bundles, the licence and JOB's schema match the pinned digests ({CACHE})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--family", choices=FAMILIES, action="append")
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--sample", type=int, help="a pinned stratified sample of this many pairs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--no-instance", action="store_true", help="skip the generated TPC-H instance")
    parser.add_argument("--json", type=Path, help="write per-pair verdicts here")
    parser.add_argument("--write-results", action="store_true", help="write benchmarks/results/feedback-opt-*.json (whole corpus only)")
    parser.add_argument("--log", type=Path, help="append each verdict here as it lands; a rerun resumes from it")
    parser.add_argument("--show", choices=OUTCOMES, action="append", help="print the pairs with this outcome")
    parser.add_argument("--check-sources", action="store_true", help="download and verify every pinned file, then stop")
    args = parser.parse_args(argv)
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    if args.check_sources:
        return check_sources()
    pairs = load_pairs()
    if args.family:
        pairs = [p for p in pairs if p.family in args.family]
    if args.split != "all":
        pairs = [p for p in pairs if p.held_out == (args.split == "held-out")]
    if args.sample:
        pairs = sample(pairs, args.sample)
    pairs = pairs[: args.limit]
    instance = None if args.no_instance else qb.tpch_instance()
    report = run(pairs, args.workers, instance, args.log)
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps([
            {"key": p.key, "case": p.case_id, "family": p.family, "label": p.label, "held_out": p.held_out, "checks": dict(p.checks),
             "sources": dict(p.sources), "outcome": v.outcome, "detail": v.detail, "kind": v.kind, "source": v.source, "witness": v.witness,
             "original": p.original, "rewritten": p.rewritten}
            for p, v in ((p, report.verdicts[i]) for i, p in enumerate(pairs))
        ], indent=1, default=str))
    if args.write_results:
        if args.family or args.split != "all" or args.sample or args.limit:
            parser.error("--write-results needs the whole corpus")
        from bench_common import today, write_results

        for name, row in results_rows(report, today()).items():
            write_results(name, row)
    for outcome in args.show or ():
        for index, pair in enumerate(pairs):
            verdict = report.verdicts[index]
            if verdict.outcome == outcome:
                print(f"\n#{pair.key} {pair.case_id} [{pair.label}] {verdict.outcome}: {verdict.detail} {verdict.source}")
                print("  O:", _flat(pair.original)[:400])
                print("  R:", _flat(pair.rewritten)[:400])
                if verdict.witness:
                    print("  witness:", json.dumps(verdict.witness, default=str)[:600])
    return 1 if any(v.outcome == "wrong" for v in report.verdicts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
