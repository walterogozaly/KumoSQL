"""Fetch and read public analytical SQL benchmark corpora (none of them are checked in).

Corpora:

* ``sqlstorm/<dataset>``: SQLStorm v1.0 (https://github.com/SQL-Storm/SQLStorm, MIT licence,
  Schmidt et al., "SQLStorm: Taking Database Benchmarking into the LLM Era", VLDB 2025):
  LLM-generated analytical queries over ``stackoverflow``, ``tpch``, ``tpcds`` and ``job``.
* ``sqlstorm-v0/<dataset>``: SQLStorm v0.0, the official parameterised TPC-H, TPC-DS and JOB
  queries produced by each benchmark's own generator.
* ``dsb``: DSB (https://github.com/microsoft/dsb, MIT licence, Ding et al., "DSB: A Decision
  Support Benchmark for Workload Optimization", VLDB 2021): its 52 TPC-DS-derived templates,
  instantiated locally with the bundled ``dsqgen`` (needs ``make`` and a C compiler).

Every query is written for Postgres (SQLStorm also runs them on DuckDB and Umbra), so
:func:`to_bigquery` converts each one with sqlglot before KumoSQL sees it.

``schema(corpus)`` returns ``{table: {column: BigQuery type}}`` for the corpus's database, which
the execution checks use to generate synthetic tables.

    python tools/benchmark_corpora.py fetch sqlstorm dsb    # into $KUMOSQL_BENCH_DIR
    python tools/benchmark_corpora.py fetch tpch-data tpcds-data   # real data for the transformation bench
    python tools/benchmark_corpora.py list                  # corpora and their sizes

Data lives in ``$KUMOSQL_BENCH_DIR`` (default ``~/.cache/kumosql-bench``), outside git.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import sqlglot
from sqlglot import exp

BENCH_DIR = Path(os.environ.get("KUMOSQL_BENCH_DIR", Path.home() / ".cache" / "kumosql-bench"))
SQLSTORM_URL = "https://github.com/SQL-Storm/SQLStorm.git"
DSB_URL = "https://github.com/microsoft/dsb.git"
JOB_URL = "https://github.com/gregrahn/join-order-benchmark.git"
# Pinned source versions: results are only comparable on the same queries.
PINS = {
    SQLSTORM_URL: "b3bb0b96794a6afe9bb8f3ff2b243562b779c40d",
    DSB_URL: "ec9a156ceee923db1114cbe388f6183b53d49787",
    JOB_URL: "a39603662e023e449cb2121997a5034df9e02ebf",
}
SQLSTORM_DATASETS = ("stackoverflow", "tpch", "tpcds", "job")
DSB_SEEDS = (1, 2, 3)


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _clone(url: str, dest: Path, sparse: list[str] | None = None) -> None:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if sparse:
            _run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, str(dest)])
            # Non-cone mode so single files can be listed; a leading slash anchors each path at the root.
            _run(["git", "sparse-checkout", "set", "--no-cone", *("/" + p.lstrip("/") for p in sparse)], cwd=dest)
        else:
            _run(["git", "clone", "-q", "--depth", "1", url, str(dest)])
    pin = PINS.get(url)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
    if pin and head != pin:
        _run(["git", "fetch", "-q", "--depth", "1", "origin", pin], cwd=dest)
        _run(["git", "checkout", "-q", pin], cwd=dest)


def source_versions() -> dict[str, str]:
    """The commit each fetched corpus is at (it should match ``PINS``)."""

    out = {}
    for name in ("SQLStorm", "dsb", "join-order-benchmark"):
        path = BENCH_DIR / name
        if path.exists():
            out[name] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    return out


def held_out(query_id: str) -> bool:
    """One query in five, chosen by a hash of its id, is held out for final evaluation only."""

    import hashlib

    return int(hashlib.sha1(query_id.encode()).hexdigest(), 16) % 5 == 0


def fetch_sqlstorm() -> Path:
    dest = BENCH_DIR / "SQLStorm"
    paths = [f"v1.0/{d}/queries" for d in SQLSTORM_DATASETS] + [f"v0.0/{d}/queries" for d in ("tpch", "tpcds", "job")]
    _clone(SQLSTORM_URL, dest, paths + ["v1.0/stackoverflow/schema.sql", "LICENSE"])
    _clone(JOB_URL, BENCH_DIR / "join-order-benchmark")
    return dest


def fetch_dsb() -> Path:
    """Clone DSB, build its query generator and instantiate every template with three seeds."""

    dest = BENCH_DIR / "dsb"
    _clone(DSB_URL, dest)
    tools = dest / "code" / "tools"
    if not (tools / "dsqgen").exists():
        _run(["make", "-s", "-j4"], cwd=tools)
    out = BENCH_DIR / "dsb-queries"
    out.mkdir(exist_ok=True)
    for kind_dir in sorted((dest / "query_templates_pg").iterdir()):
        for template in sorted(kind_dir.glob("query*.tpl")):
            for seed in DSB_SEEDS:
                target = out / f"{kind_dir.name}_{template.stem}_{seed}.sql"
                if target.exists():
                    continue
                result = subprocess.run(
                    ["./dsqgen", "-DIRECTORY", str(kind_dir), "-TEMPLATE", template.name, "-DIALECT", "postgres",
                     "-RNGSEED", str(seed), "-SCALE", "1", "-FILTER", "Y", "-QUALIFY", "N"],
                    cwd=tools, capture_output=True, text=True,
                )
                if result.returncode == 0:
                    target.write_text(_strip_qgen_noise(result.stdout))
    return out


def fetch_tpch_data(scale: float = 0.1) -> Path:
    """TPC-H tables at ``scale`` in a DuckDB file, generated by ``tpchgen-cli`` (pip install tpchgen-cli)."""

    import duckdb

    target = BENCH_DIR / "tpch.duckdb"
    if target.exists():
        return target
    out = BENCH_DIR / "tpch-parquet"
    out.mkdir(parents=True, exist_ok=True)
    _run(["tpchgen-cli", "-s", str(scale), "--format", "parquet", "--output-dir", str(out)])
    con = duckdb.connect(str(target))
    for parquet in sorted(out.glob("*.parquet")):
        con.execute(f"CREATE TABLE {parquet.stem} AS SELECT * FROM '{parquet}'")
    con.close()
    return target


def fetch_tpcds_data(scale: int = 1) -> Path:
    """TPC-DS tables at ``scale`` (1 is the smallest) in a DuckDB file, generated by DSB's ``dsdgen``."""

    import duckdb

    target = BENCH_DIR / "tpcds.duckdb"
    if target.exists():
        return target
    fetch_dsb()
    tools = BENCH_DIR / "dsb" / "code" / "tools"
    raw = BENCH_DIR / "tpcds-raw"
    raw.mkdir(parents=True, exist_ok=True)
    _run(["./dsdgen", "-SCALE", str(scale), "-DIR", str(raw), "-FORCE", "-QUIET", "Y"], cwd=tools)
    con = duckdb.connect(str(target))
    for statement in (tools / "tpcds.sql").read_text().split(";"):
        if "create table" in statement.lower():
            con.execute(re.sub(r",\s*primary key[^)]*\)\s*\)\s*$", ")", statement.strip(), flags=re.I | re.S))
    for data in sorted(raw.glob("*.dat")):
        con.execute(f"COPY {data.stem} FROM '{data}' (DELIMITER '|', HEADER false, NULL '')")
    con.close()
    for data in raw.glob("*"):
        data.unlink()
    return target


def _strip_qgen_noise(text: str) -> str:
    # dsqgen echoes its arguments ("name DIRECTORY, param ...") and the template's comment lines.
    return "\n".join(l for l in text.splitlines() if not l.startswith("name ") and not l.startswith("-- ")).strip() + "\n"


# ---------------------------------------------------------------- corpora


def corpora() -> list[str]:
    names = []
    if (BENCH_DIR / "SQLStorm").exists():
        names += [f"sqlstorm/{d}" for d in SQLSTORM_DATASETS] + [f"sqlstorm-v0/{d}" for d in ("tpch", "tpcds", "job")]
    if (BENCH_DIR / "dsb-queries").exists():
        names.append("dsb")
    return names


def _query_dir(corpus: str) -> Path:
    if corpus == "dsb":
        return BENCH_DIR / "dsb-queries"
    family, dataset = corpus.split("/")
    version = "v1.0" if family == "sqlstorm" else "v0.0"
    return BENCH_DIR / "SQLStorm" / version / dataset / "queries"


def _natural(path: Path) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path.name))


def queries(corpus: str) -> Iterator[tuple[str, str]]:
    """``(query id, Postgres SQL)`` for every query of a corpus, in a stable order."""

    root = _query_dir(corpus)
    for path in sorted(root.rglob("*.sql"), key=lambda p: (str(p.parent), _natural(p))):
        yield f"{corpus}/{path.relative_to(root).with_suffix('')}", path.read_text(errors="replace")


def to_bigquery(sql: str, read: str = "postgres") -> str:
    """Convert one benchmark query to BigQuery SQL; raises when sqlglot cannot convert it."""

    statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    if len(statements) != 1:
        raise ValueError(f"expected one statement, found {len(statements)}")
    return statements[0].sql(dialect="bigquery", unsupported_level=sqlglot.ErrorLevel.RAISE)


# ---------------------------------------------------------------- schemas

_TYPES = (
    (r"^(tiny|small|big)?int(eger)?\b|^serial|^identifier", "INT64"),
    (r"^(decimal|numeric)", "NUMERIC"),
    (r"^(float|double|real)", "FLOAT64"),
    (r"^bool", "BOOL"),
    (r"^date$", "DATE"),
    (r"^(timestamp|datetime|time)", "TIMESTAMP"),
)


def _bq_type(type_sql: str) -> str:
    lowered = type_sql.lower()
    for pattern, bq in _TYPES:
        if re.search(pattern, lowered):
            return bq
    return "STRING"


def _schema_from_ddl(text: str, read: str = "postgres") -> dict[str, dict[str, str]]:
    schema: dict[str, dict[str, str]] = {}
    for statement in sqlglot.parse(text, read=read):
        if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
            continue
        table = statement.this.this.name
        columns = {
            col.name: _bq_type(col.args["kind"].sql(dialect="postgres"))
            for col in statement.this.expressions
            if isinstance(col, exp.ColumnDef) and col.args.get("kind") is not None
        }
        if columns:
            schema[table] = columns
    return schema


TPCH_DDL = """
create table nation (n_nationkey integer, n_name char(25), n_regionkey integer, n_comment varchar(152));
create table region (r_regionkey integer, r_name char(25), r_comment varchar(152));
create table part (p_partkey integer, p_name varchar(55), p_mfgr char(25), p_brand char(10), p_type varchar(25),
  p_size integer, p_container char(10), p_retailprice decimal(15,2), p_comment varchar(23));
create table supplier (s_suppkey integer, s_name char(25), s_address varchar(40), s_nationkey integer,
  s_phone char(15), s_acctbal decimal(15,2), s_comment varchar(101));
create table partsupp (ps_partkey integer, ps_suppkey integer, ps_availqty integer, ps_supplycost decimal(15,2),
  ps_comment varchar(199));
create table customer (c_custkey integer, c_name varchar(25), c_address varchar(40), c_nationkey integer,
  c_phone char(15), c_acctbal decimal(15,2), c_mktsegment char(10), c_comment varchar(117));
create table orders (o_orderkey integer, o_custkey integer, o_orderstatus char(1), o_totalprice decimal(15,2),
  o_orderdate date, o_orderpriority char(15), o_clerk char(15), o_shippriority integer, o_comment varchar(79));
create table lineitem (l_orderkey integer, l_partkey integer, l_suppkey integer, l_linenumber integer,
  l_quantity decimal(15,2), l_extendedprice decimal(15,2), l_discount decimal(15,2), l_tax decimal(15,2),
  l_returnflag char(1), l_linestatus char(1), l_shipdate date, l_commitdate date, l_receiptdate date,
  l_shipinstruct char(25), l_shipmode char(10), l_comment varchar(44));
"""


@lru_cache(maxsize=None)
def schema(corpus: str) -> dict[str, dict[str, str]] | None:
    """The database schema a corpus's queries run against, or None when it is not available."""

    dataset = "tpcds" if corpus == "dsb" else corpus.split("/")[-1]
    path = None
    if dataset == "tpch":
        return _schema_from_ddl(TPCH_DDL)
    if dataset == "tpcds":
        path = BENCH_DIR / "dsb" / "code" / "tools" / "tpcds.sql"
    elif dataset == "stackoverflow":
        path = BENCH_DIR / "SQLStorm" / "v1.0" / "stackoverflow" / "schema.sql"
    elif dataset == "job":
        path = BENCH_DIR / "join-order-benchmark" / "schema.sql"
    if path is None or not path.exists():
        return None
    return _schema_from_ddl(path.read_text())


FETCHERS = {"sqlstorm": fetch_sqlstorm, "dsb": fetch_dsb, "tpch-data": fetch_tpch_data, "tpcds-data": fetch_tpcds_data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("which", nargs="+", choices=tuple(FETCHERS))
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        for which in args.which:
            print(f"{which}: {FETCHERS[which]()}")
    else:
        for corpus in corpora():
            print(f"{corpus}: {sum(1 for _ in queries(corpus))} queries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
