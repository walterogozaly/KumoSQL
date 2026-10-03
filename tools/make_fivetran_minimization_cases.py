#!/usr/bin/env python3
"""Turn Fivetran dbt packages into table-minimization cases (case format v1).

Output goes to benchmarks/table_minimization/sourced-fivetran.jsonl (and sourced-jaffle.jsonl
with --repos jaffle_shop_duckdb). Needs dbt-core and dbt-duckdb, which are not KumoSQL dependencies;
the eval itself only reads the JSON.

Usage:
    python tools/make_fivetran_minimization_cases.py --clones CLONES --work WORK --out sourced-fivetran.jsonl \
        [--repos dbt_github dbt_asana ...] [--skip-build]

CLONES holds git clones (nothing is downloaded): the fivetran/dbt_<x> packages plus
dbt_fivetran_utils (v0.4.x), dbt-utils (1.3.x) and spark-utils (0.3.x). Each package is copied to
WORK/<repo> (the clone is not touched), its packages.yml files are pointed at the local clones,
every model is forced to `materialized: table`, and its integration_tests project is built on DuckDB
(`dbt seed` + `dbt run`, source schema var = main). The manifest's compiled SQL is then converted:
relation names made bare, now()/current_* fixed to 2026-01-01, transpiled duckdb -> bigquery, and
every table checked by transpiling back to duckdb and comparing bags with what dbt built.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kumosql import formatting  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402

FIXED_TS = "TIMESTAMP '2026-01-01 00:00:00'"
FIXED_DATE = "DATE '2026-01-01'"
UTIL_DIRS = {"fivetran_utils": "dbt_fivetran_utils", "dbt_utils": "dbt-utils", "spark_utils": "spark-utils"}
N_RANDOM = 50
# Projects that are not Fivetran packages: the clone is the dbt project itself (no integration_tests),
# its own models are the pipeline, and the end models are named here.
GENERIC = {
    "jaffle_shop_duckdb": {"source": "dbt-labs/jaffle_shop_duckdb", "end": ["customers", "orders"],
                           "ids": "jaffle"},
}
RANDOM_SEED = 42


# ---------------------------------------------------------------------------------------------
# Build


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def packages_yml(names: list[str], util_root: Path, extra: str = "") -> str:
    lines = ["packages:"]
    if extra:
        lines.append(extra)
    for name in names:
        lines.append(f"  - local: {util_root / UTIL_DIRS[name]}")
    return "\n".join(lines) + "\n"


def package_deps(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"package:\s*([\w-]+)/([\w-]+)", text):
        name = m.group(2).replace("-", "_")
        if name == "dbt_utils" or name == "fivetran_utils" or name == "spark_utils":
            out.append(name)
        else:
            raise SystemExit(f"unsupported dependency {m.group(0)}")
    return out


def force_tables(root: Path) -> None:
    proj = root / "dbt_project.yml"
    proj.write_text(re.sub(r"(\+?materialized:\s*)['\"]?(ephemeral|view|incremental)['\"]?",
                           r"\1table", proj.read_text()))
    for sql in (root / "models").rglob("*.sql"):
        text = sql.read_text()
        new = re.sub(r"(materialized\s*=\s*)(['\"])(ephemeral|view|incremental)\2", r"\1\2table\2", text)
        if new != text:
            sql.write_text(new)


def prepare_utils(clones: Path, work: Path) -> None:
    """Copy the utility packages next to the builds and point fivetran_utils at the local dbt_utils."""

    utils = work / "utils"
    if utils.exists():
        shutil.rmtree(utils)
    utils.mkdir(parents=True)
    for d in UTIL_DIRS.values():
        shutil.copytree(clones / d, utils / d, ignore=shutil.ignore_patterns(".git"))
    (utils / "dbt_fivetran_utils" / "packages.yml").write_text(packages_yml(["dbt_utils"], utils))


def build_generic(repo: str, dest: Path, work: Path) -> Path:
    """A plain dbt project (seeds + models, no packages): force tables, seed and run it."""

    if (dest / "packages.yml").exists() and "package:" in (dest / "packages.yml").read_text():
        raise SystemExit(f"{repo}: generic projects with hub packages are not supported")
    force_tables(dest)
    profile = re.search(r"(?m)^profile:\s*['\"]?([\w-]+)", (dest / "dbt_project.yml").read_text()).group(1)
    db = work / (repo + ".duckdb")
    if db.exists():
        db.unlink()
    (dest / "profiles.yml").write_text(
        f"{profile}:\n  target: duckdb\n  outputs:\n    duckdb:\n      type: duckdb\n      path: {db}\n      threads: 4\n")
    log = []
    for cmd in (["dbt", "seed"], ["dbt", "run"]):
        r = sh(cmd + ["--profiles-dir", "."], dest)
        log.append(f"$ {' '.join(cmd)}\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}")
        if r.returncode != 0:
            (work / (repo + ".build.log")).write_text("\n".join(log))
            raise SystemExit(f"{repo}: {' '.join(cmd)} failed; see {work / (repo + '.build.log')}")
    (work / (repo + ".build.log")).write_text("\n".join(log))
    return dest


def build(repo: str, clones: Path, work: Path) -> Path:
    """Copy, patch and build one package; returns the integration_tests dir."""

    utils = work / "utils"
    dest = work / repo
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(clones / repo, dest, ignore=shutil.ignore_patterns(".git", "dbt_packages", "target", "logs"))
    if repo in GENERIC:
        return build_generic(repo, dest, work)
    deps = package_deps((dest / "packages.yml").read_text())
    if "fivetran_utils" in deps and "dbt_utils" not in deps:
        deps.append("dbt_utils")
    (dest / "packages.yml").write_text(packages_yml(deps, utils))
    force_tables(dest)
    it = dest / "integration_tests"
    it_deps = (it / "packages.yml").read_text()
    extra = [d for d in package_deps(re.sub(r"local:.*", "", it_deps))] if "package:" in it_deps else []
    (it / "packages.yml").write_text(packages_yml(sorted(set(deps) | set(extra)), utils, "  - local: ../"))
    proj = it / "dbt_project.yml"
    proj.write_text(re.sub(r"(?m)^(\s+\w+_schema:\s*).*$", r"\1main", proj.read_text()))
    (it / "profiles.yml").write_text(
        "integration_tests:\n  target: duckdb\n  outputs:\n    duckdb:\n      type: duckdb\n"
        f"      path: {work / (repo + '.duckdb')}\n      threads: 4\n")
    db = work / (repo + ".duckdb")
    if db.exists():
        db.unlink()
    schema_vars = sorted(set(re.findall(r"var\(\s*['\"](\w+_schema)['\"]", "".join(
        p.read_text() for p in (dest / "models").rglob("*.yml")))))
    vars_ = json.dumps({v: "main" for v in schema_vars})
    log = []
    for cmd in (["dbt", "deps"], ["dbt", "seed", "--vars", vars_], ["dbt", "run", "--vars", vars_]):
        r = sh(cmd + ["--profiles-dir", "."], it)
        log.append(f"$ {' '.join(cmd)}\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}")
        if r.returncode != 0 and cmd[1] != "run":
            (work / (repo + ".build.log")).write_text("\n".join(log))
            raise SystemExit(f"{repo}: {' '.join(cmd)} failed; see {work / (repo + '.build.log')}")
    (work / (repo + ".build.log")).write_text("\n".join(log))
    return it




# ---------------------------------------------------------------------------------------------
# Types, values and bags

TO_CASE = {"BIGINT": "INT64", "INTEGER": "INT64", "SMALLINT": "INT64", "TINYINT": "INT64", "HUGEINT": "INT64",
           "VARCHAR": "STRING", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "DATE": "DATE",
           "TIMESTAMP": "TIMESTAMP"}
TO_DUCK = {"INT64": "BIGINT", "STRING": "VARCHAR", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "DATE": "DATE",
           "TIMESTAMP": "TIMESTAMP", "NUMERIC": "DECIMAL(38, 9)"}


def case_type(duck: str) -> str:
    if duck.startswith("DECIMAL"):
        return "NUMERIC"
    if duck not in TO_CASE:
        raise ValueError(f"unmapped seed type {duck}")
    return TO_CASE[duck]


def norm(v, digits: int = 12):
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, (float, decimal.Decimal)):
        f = float(v)
        if f != f:
            return "NaN"
        return float(f"{f:.{digits}g}") + 0.0
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return v
    if isinstance(v, (list, tuple)):
        return tuple(norm(x, digits) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, norm(x, digits)) for k, x in v.items()))
    return v


def bag(cols, rows, digits=12):
    return (tuple(cols), Counter(tuple(norm(v, digits) for v in r) for r in rows))


def json_value(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    return v


# ---------------------------------------------------------------------------------------------
# Running a pipeline on DuckDB (optimizer off)


def topo(tables: dict[str, str], deps: dict[str, set[str]]) -> list[str]:
    order, seen = [], set()

    def visit(t):
        if t in seen:
            return
        seen.add(t)
        for d in sorted(deps[t]):
            if d in tables:
                visit(d)
        order.append(t)

    for t in sorted(tables):
        visit(t)
    return order


def connect_with(sources: dict, rows: dict) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 1")
    con.execute("SET TimeZone = 'UTC'")
    for name, spec in sources.items():
        cols = ", ".join(f'"{c}" {TO_DUCK[t]}' for c, t in spec["columns"].items())
        con.execute(f'CREATE TABLE "{name}" ({cols})')
        insert_rows(con, f'"{name}"', rows.get(name, []))
    return con


def run_pipeline(sources, rows, duck_tables: dict[str, str], order: list[str], want, digits=12):
    """{table: bag} for `want`, building every table in `order` with the optimizer off."""

    con = connect_with(sources, rows)
    try:
        for t in order:
            run_unoptimized(con, f'CREATE TABLE "{t}" AS {duck_tables[t]}')
        out = {}
        for t in want:
            cur = con.execute(f'SELECT * FROM "{t}"')
            out[t] = bag([d[0] for d in cur.description], cur.fetchall(), digits)
        return out
    finally:
        con.close()


def to_duck(bq_sql: str) -> str:
    return sqlglot.transpile(bq_sql, read="bigquery", write="duckdb")[0]


# ---------------------------------------------------------------------------------------------
# Conversion of one built package


def fixed_time(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    hit = [False]

    def swap(node):
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDatetime)) or (
                isinstance(node, exp.Anonymous) and str(node.this).lower() in ("now", "get_current_timestamp")):
            hit[0] = True
            return sqlglot.parse_one(FIXED_TS, read="duckdb")
        if isinstance(node, exp.CurrentDate):
            hit[0] = True
            return sqlglot.parse_one(FIXED_DATE, read="duckdb")
        return node

    return tree.transform(swap), hit[0]


def ordered_aggregates(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    """Give every STRING_AGG / ARRAY_AGG without ORDER BY an order on its own argument, so the output no
    longer depends on the order rows arrive in (ties are equal values, so the result is fixed)."""

    hit = False
    for node in list(tree.find_all(exp.GroupConcat, exp.ArrayAgg)):
        inner = node.this
        if isinstance(inner, exp.Order):
            continue
        key = inner.expressions[0] if isinstance(inner, exp.Distinct) else inner
        node.set("this", exp.Order(this=inner, expressions=[exp.Ordered(this=key.copy(), desc=False, nulls_first=True)]))
        hit = True
    return tree, hit


def is_cte_ref(table: exp.Table) -> bool:
    """Whether a bare table name refers to a CTE in scope (a CTE's own body sees only earlier CTEs)."""

    if table.db:
        return False
    name = table.name
    child, node = table, table.parent
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with") if isinstance(node, exp.Expression) else None
        if isinstance(with_, exp.With):
            ctes = list(with_.expressions)
            if child is with_:
                # inside one of this WITH's CTE bodies: only the CTEs before it are visible
                inner = next((c for c in ctes if c is _ancestor_in(table, ctes)), None)
                visible = ctes[:ctes.index(inner)] if inner is not None else ctes
            else:
                visible = ctes
            if any(c.alias_or_name == name for c in visible):
                return True
        child, node = node, node.parent
    return False


def _ancestor_in(node: exp.Expression, candidates: list) -> exp.Expression | None:
    ids = {id(c) for c in candidates}
    while node is not None:
        if id(node) in ids:
            return node
        node = node.parent
    return None


def table_refs(bq_sql: str) -> set[str]:
    tree = sqlglot.parse_one(bq_sql, read="bigquery")
    return {t.name for t in tree.find_all(exp.Table) if not t.db and not is_cte_ref(t)}


def unordered_aggregates(bq_sql: str) -> bool:
    """STRING_AGG / ARRAY_AGG without ORDER BY: the result depends on input row order."""

    tree = sqlglot.parse_one(bq_sql, read="bigquery")
    return any(not isinstance(n.this, exp.Order) for n in tree.find_all(exp.GroupConcat, exp.ArrayAgg))


def downstream(of: set[str], deps: dict[str, set[str]]) -> set[str]:
    out = set(of)
    changed = True
    while changed:
        changed = False
        for t, ds in deps.items():
            if t not in out and ds & out:
                out.add(t)
                changed = True
    return out


def pipeline_complexity(tables: dict[str, str]) -> dict:
    fn = getattr(formatting, "pipeline_complexity", None)
    if fn is not None:
        got = fn(tables)
        return got if isinstance(got, dict) else got.to_json()
    structural = sum(formatting.complexity(sql).score for sql in tables.values())
    return {"score": round(structural + len(tables), 1), "structural": round(structural, 1), "tables": len(tables)}


class Package:
    def __init__(self, repo: str, it: Path, clone: Path, work: Path):
        self.repo = repo
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()
        self.tag = subprocess.run(["git", "describe", "--tags"], cwd=clone, capture_output=True, text=True).stdout.strip()
        manifest = json.loads((it / "target" / "manifest.json").read_text())
        root = manifest["metadata"]["project_name"]
        self.db_path = work / (repo + ".duckdb")
        dbt_db = duckdb.connect(str(self.db_path), read_only=True)
        nodes = manifest["nodes"]
        self.generic = GENERIC.get(repo)
        self.origin = self.generic["source"] if self.generic else f"fivetran/{repo}"
        models = {k: n for k, n in nodes.items() if n["resource_type"] == "model"
                  and (n["package_name"] == root if self.generic else n["package_name"] != root)}
        self.pkg = {n["package_name"] for n in models.values()}.pop()
        self.n_models = len(models)
        # sources = the seeds
        self.sources, self.seed_rows, seed_rel = {}, {}, {}
        seeds = sorted((n for n in nodes.values() if n["resource_type"] == "seed"), key=lambda n: n["name"])
        for n in seeds:
            _, schema, name = [p.strip('"') for p in n["relation_name"].split(".")]
            seed_rel[(schema.lower(), name.lower())] = name.lower()
            info = dbt_db.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = "
                                  "? AND table_name = ? ORDER BY ordinal_position", [schema, name]).fetchall()
            self.sources[name.lower()] = {"columns": {c.lower(): case_type(t) for c, t in info}}
            sel = ", ".join(f'"{c}"' for c, _ in info)
            rows = dbt_db.execute(f'SELECT {sel} FROM "{schema}"."{name}"').fetchall()
            f32 = [t == "FLOAT" for _, t in info]
            self.seed_rows[name.lower()] = [
                [float(f"{v:.7g}") if (is32 and v is not None) else v for v, is32 in zip(r, f32)] for r in rows]
        by_rel = {}
        for k, n in models.items():
            _, schema, ident = [p.strip('"') for p in n["relation_name"].split(".")]
            by_rel[(schema.lower(), ident.lower())] = n["name"]
        self.dropped: dict[str, str] = {}
        self.bq: dict[str, str] = {}
        self.native: dict[str, str] = {}
        self.uses_now: set[str] = set()
        self.ordered: set[str] = set()
        self.path = {n["name"]: n["path"] for n in models.values()}
        for k, n in models.items():
            name = n["name"]
            if name in self.sources:
                raise SystemExit(f"model {name} clashes with a source")
            try:
                tree = sqlglot.parse_one(n["compiled_code"], read="duckdb")
                for t in list(tree.find_all(exp.Table)):
                    if t.catalog:
                        key = (t.db.lower(), t.name.lower())
                        if key in by_rel:
                            new = by_rel[key]
                        elif key in seed_rel:
                            new = seed_rel[key]
                        else:
                            raise ValueError(f"unknown relation {t.sql('duckdb')}")
                        t.replace(exp.Table(this=exp.to_identifier(new),
                                            alias=t.args.get("alias") or None))
                tree, hit = fixed_time(tree)
                if hit:
                    self.uses_now.add(name)
                tree, hit = ordered_aggregates(tree)
                if hit:
                    self.ordered.add(name)
                self.native[name] = tree.sql(dialect="duckdb", comments=False)
                self.bq[name] = tree.sql(dialect="bigquery", comments=False)
            except Exception as e:  # noqa: BLE001
                self.dropped[name] = f"sqlglot could not convert: {str(e).splitlines()[0][:200]}"
        self.deps = {}
        for name in list(self.bq):
            refs = table_refs(self.bq[name])
            unknown = refs - set(self.bq) - set(self.sources) - set(self.dropped)
            if unknown:
                self.dropped[name] = f"reads unknown relations {sorted(unknown)}"
            self.deps[name] = refs
        for name in self.dropped:
            self.deps.setdefault(name, set())
        self.dbt_tables = {}
        for n in models.values():
            _, schema, ident = [p.strip('"') for p in n["relation_name"].split(".")]
            cur = dbt_db.execute(f'SELECT * FROM "{schema}"."{ident}"')
            self.dbt_tables[n["name"]] = ([d[0] for d in cur.description], cur.fetchall())
        dbt_db.close()
        self.end = sorted(self.generic["end"]) if self.generic else sorted(
            n["name"] for n in models.values()
            if not re.match(r"(stg|int)_", n["name"]) and not re.search(r"(staging|intermediate|tmp)", n["path"]))
        self.check()

    def drop_with_downstream(self, name: str, why: str) -> None:
        self.dropped.setdefault(name, why)
        for d in downstream({name}, self.deps) - {name}:
            self.dropped.setdefault(d, f"downstream of dropped {name}")

    def alive(self) -> list[str]:
        return [t for t in self.bq if t not in self.dropped]

    def check(self) -> None:
        """Round trip each table bq -> duckdb on the seed data and compare with dbt's table."""

        for name in list(self.dropped):
            self.drop_with_downstream(name, self.dropped[name])
        for name in self.alive():
            try:
                c = formatting.complexity(self.bq[name])
                assert c is not None
            except Exception as e:  # noqa: BLE001
                self.drop_with_downstream(name, f"sqlfluff (bigquery) cannot score it: {str(e)[:120]}")
        self.time_dependent = downstream(self.uses_now | self.ordered, self.deps)
        self.loose = []
        con_rt = connect_with(self.sources, self.seed_rows)
        con_nat = connect_with(self.sources, self.seed_rows)
        self.rt = {}
        for name in topo({t: 1 for t in self.alive()}, self.deps):
            if name in self.dropped:
                continue
            try:
                self.rt[name] = to_duck(self.bq[name])
                run_unoptimized(con_rt, f'CREATE TABLE "{name}" AS {self.rt[name]}')
                run_unoptimized(con_nat, f'CREATE TABLE "{name}" AS {self.native[name]}')
            except Exception as e:  # noqa: BLE001
                self.drop_with_downstream(name, f"fails to run on DuckDB: {str(e).splitlines()[0][:200]}")
                continue
            cur = con_rt.execute(f'SELECT * FROM "{name}"')
            raw = ([d[0] for d in cur.description], cur.fetchall())
            got = bag(*raw)
            cur = con_nat.execute(f'SELECT * FROM "{name}"')
            nat = bag([d[0] for d in cur.description], cur.fetchall())
            if got != nat:
                self.drop_with_downstream(name, "bigquery round trip changes its rows")
                continue
            if name in self.time_dependent:
                continue
            if got != bag(*self.dbt_tables[name]):
                if bag(*raw, digits=6) == bag(*self.dbt_tables[name], digits=6):
                    self.loose.append(name)
                else:
                    self.drop_with_downstream(name, "rows differ from the table dbt built")
        con_rt.close()
        con_nat.close()
        self.end = [t for t in self.end if t not in self.dropped]
        self.columns = {}
        con = connect_with(self.sources, self.seed_rows)
        for name in topo({t: 1 for t in self.alive()}, self.deps):
            run_unoptimized(con, f'CREATE TABLE "{name}" AS {self.rt[name]}')
            self.columns[name] = [d[0] for d in con.execute(f'SELECT * FROM "{name}" LIMIT 0').description]
        con.close()
        for s, spec in self.sources.items():
            self.columns[s] = list(spec["columns"])


# ---------------------------------------------------------------------------------------------
# Mechanical reference: dead tables, then pass-through tables


def needed(tables: dict[str, str], protected, deps) -> set[str]:
    keep, todo = set(), list(protected)
    while todo:
        t = todo.pop()
        if t in keep or t not in tables:
            continue
        keep.add(t)
        todo.extend(deps[t])
    return keep


def passthrough_target(sql: str, columns: dict[str, list[str]]) -> str | None:
    """x when `sql` returns exactly the table x: SELECT * FROM x, or its columns in order, no casts."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    if not isinstance(tree, exp.Select):
        return None
    ctes = {c.alias: c.this for c in (tree.args.get("with_") or tree.args.get("with") or exp.With()).expressions} \
        if (tree.args.get("with_") or tree.args.get("with")) else {}

    def resolve(select) -> str | None:
        if not isinstance(select, exp.Select):
            return None
        for arg in ("joins", "where", "group", "having", "qualify", "order", "limit", "offset", "distinct",
                    "laterals", "windows"):
            if select.args.get(arg):
                return None
        if select is not tree and (select.args.get("with_") or select.args.get("with")):
            return None
        src = select.args.get("from_") or select.args.get("from")
        if src is None or not isinstance(src.this, exp.Table) or src.this.db:
            return None
        name = src.this.name
        if name in ctes and select is not ctes.get(name):
            base = resolve(ctes[name])
            if base is None:
                return None
            cols = columns.get(base)
        else:
            base = name
            cols = columns.get(name)
        if cols is None:
            return None
        projs = select.expressions
        if len(projs) == 1 and isinstance(projs[0], exp.Star):
            return base
        if len(projs) == 1 and isinstance(projs[0], exp.Column) and isinstance(projs[0].this, exp.Star):
            return base
        names = []
        for p in projs:
            col = p.this if isinstance(p, exp.Alias) else p
            if not isinstance(col, exp.Column) or isinstance(col.this, exp.Star):
                return None
            if p.alias_or_name != col.name:
                return None
            names.append(col.name)
        return base if [n.lower() for n in names] == [c.lower() for c in cols] else None

    return resolve(tree)


def redirect(sql: str, old: str, new: str) -> str:
    tree = sqlglot.parse_one(sql, read="bigquery")
    for t in list(tree.find_all(exp.Table)):
        if not t.db and t.name == old and not is_cte_ref(t):
            alias = t.args.get("alias")
            t.set("this", exp.to_identifier(new))
            if is_cte_ref(t):
                raise ValueError("new name shadowed by a CTE")
            if alias is None:
                t.set("alias", exp.TableAlias(this=exp.to_identifier(old)))
    return tree.sql(dialect="bigquery", comments=False)


def mechanical_reference(tables, protected, deps, columns, passthrough=True):
    tables = {t: tables[t] for t in needed(tables, protected, deps)}
    deps = {t: set(deps[t]) for t in tables}
    removed_pt = []
    changed = passthrough
    while changed:
        changed = False
        for t in sorted(tables):
            if t in protected:
                continue
            x = passthrough_target(tables[t], columns)
            if x is None or x == t:
                continue
            readers = [r for r in tables if t in deps[r]]
            try:
                new = {r: redirect(tables[r], t, x) for r in readers}
            except ValueError:
                continue
            for r in readers:
                tables[r] = new[r]
                deps[r] = (deps[r] - {t}) | {x}
            del tables[t]
            del deps[t]
            removed_pt.append(f"{t} -> {x}")
            changed = True
            break
    return tables, removed_pt


# ---------------------------------------------------------------------------------------------
# Random databases


def domains(pkg: "Package", rng: random.Random) -> dict:
    out = {}
    for s, spec in pkg.sources.items():
        rows = pkg.seed_rows[s]
        for i, (c, t) in enumerate(spec["columns"].items()):
            seen = sorted({r[i] for r in rows if r[i] is not None}, key=repr)
            pick = rng.sample(seen, min(3, len(seen)))
            extra = {
                "INT64": [1, 2, 3], "FLOAT64": [0.0, 1.5, -2.25], "NUMERIC": [0, 1.5, 2],
                "BOOL": [True, False], "STRING": [] if pick else ["a", "b"],
                "DATE": [dt.date(2020, 1, 1), dt.date(2020, 1, 3)],
                "TIMESTAMP": [dt.datetime(2020, 1, 1, 0, 0), dt.datetime(2020, 1, 2, 12, 30)],
            }[t]
            out[(s, c)] = pick + extra
    return out


def random_db(pkg: "Package", rng: random.Random) -> dict:
    dom = domains(pkg, rng)
    db = {}
    for s, spec in pkg.sources.items():
        n = rng.choice([0, 1, 2, 3, 4, 5, 6, 8])
        rows = []
        for _ in range(n):
            if rows and rng.random() < 0.15:
                rows.append(list(rng.choice(rows)))  # exact duplicate
                continue
            rows.append([None if rng.random() < 0.15 else rng.choice(dom[(s, c)]) for c in spec["columns"]])
        db[s] = rows
    return db


# ---------------------------------------------------------------------------------------------
# Verification and cases


class Oracle:
    """The original pipeline's outputs on the seed database and on random databases."""

    def __init__(self, pkg: "Package", want_random: int = N_RANDOM, max_tries: int = 400):
        self.pkg = pkg
        self.tables = {t: pkg.bq[t] for t in pkg.alive()}
        self.duck = {t: pkg.rt[t] for t in self.tables}
        self.order = topo(self.tables, pkg.deps)
        self.seed_out = run_pipeline(pkg.sources, pkg.seed_rows, self.duck, self.order, pkg.end)
        rev = run_pipeline(pkg.sources, {s: list(reversed(r)) for s, r in pkg.seed_rows.items()},
                           self.duck, self.order, pkg.end)
        self.seed_stable = {t for t in pkg.end if rev[t] == self.seed_out[t]}
        rng = random.Random(f"{RANDOM_SEED}-{pkg.repo}")
        self.dbs, self.outs, self.stable = [], [], []
        self.errors = 0
        tries = 0
        def short():
            return any(sum(t in st for st in self.stable) < want_random for t in pkg.end)

        while (len(self.dbs) < want_random or short()) and tries < max_tries:
            tries += 1
            db = random_db(pkg, rng)
            try:
                out = run_pipeline(pkg.sources, db, self.duck, self.order, pkg.end)
                rev = run_pipeline(pkg.sources, {s: list(reversed(r)) for s, r in db.items()},
                                   self.duck, self.order, pkg.end)
            except Exception:  # noqa: BLE001
                self.errors += 1
                continue
            self.dbs.append(db)
            self.outs.append(out)
            self.stable.append({t for t in pkg.end if out[t] == rev[t]})
        self.tries = tries
        self.stable_count = {t: sum(t in s for s in self.stable) for t in pkg.end}
        self.nonempty_count = {t: sum(bool(o[t][1]) for o in self.outs) for t in pkg.end}

    def verify(self, tables: dict[str, str], protected: list[str]) -> tuple[bool, str]:
        deps = {t: table_refs(sql) for t, sql in tables.items()}
        duck = {t: to_duck(sql) for t, sql in tables.items()}
        order = topo(tables, deps)
        try:
            got = run_pipeline(self.pkg.sources, self.pkg.seed_rows, duck, order, protected)
        except Exception as e:  # noqa: BLE001
            return False, f"seed database: fails ({str(e)[:100]})"
        for p in protected:
            if got[p] != self.seed_out[p]:
                return False, f"seed database: {p} differs"
        for i, db in enumerate(self.dbs):
            check = [p for p in protected if p in self.stable[i]]
            if not check:
                continue
            try:
                got = run_pipeline(self.pkg.sources, db, duck, order, check)
            except Exception as e:  # noqa: BLE001
                return False, f"random database {i}: fails ({str(e)[:100]})"
            for p in check:
                if got[p] != self.outs[i][p]:
                    return False, f"random database {i}: {p} differs"
        return True, ""


def split_of(case_id: str) -> str:
    return "held_out" if int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 5 == 0 else "dev"


def make_case(pkg: "Package", oracle: Oracle, case_id: str, protected: list[str], stats: dict) -> dict | None:
    tables = oracle.tables
    alive_deps = {t: pkg.deps[t] for t in tables}
    attempts = []
    ref, removed_pt = mechanical_reference(tables, set(protected), alive_deps, pkg.columns, passthrough=True)
    kind = "dead tables and pass-through tables removed"
    ok, why = oracle.verify(ref, protected)
    if not ok:
        attempts.append(f"dead+passthrough reference failed: {why}")
        ref, removed_pt = mechanical_reference(tables, set(protected), alive_deps, pkg.columns, passthrough=False)
        kind = "dead tables removed"
        ok, why = oracle.verify(ref, protected)
    if not ok:
        attempts.append(f"dead-table reference failed: {why}")
        ref, removed_pt, kind = dict(tables), [], "the original (nothing verified to remove)"
    dead = sorted(set(tables) - needed(tables, set(protected), alive_deps))
    families = []
    if dead and set(dead).isdisjoint(ref):
        families.append("dead_tables")
    if removed_pt:
        families.append("passthrough_chain")
    if ref == tables:
        kind = "nothing removable mechanically: reference = original"
    orig_c = pipeline_complexity(tables)
    ref_c = pipeline_complexity(ref)
    time_dep = sorted(set(protected) & pkg.time_dependent)
    stable = {p: oracle.stable_count[p] for p in protected}
    notes = [
        f"Adapted from {pkg.origin}@{pkg.sha[:12]}, built with dbt-core 1.12 + dbt-duckdb 1.11 with every model "
        "forced to materialized=table (view -> table)." if pkg.generic else
        f"Adapted from fivetran/{pkg.repo} {pkg.tag} integration_tests, built with dbt-core 1.12 + dbt-duckdb 1.11 "
        "(fivetran_utils v0.4.13, dbt_utils 1.3.0) with every package model forced to materialized=table "
        "(ephemeral/view/incremental -> table) and the source schema vars set to the seed schema.",
        "Compiled DuckDB SQL: relation names made bare (models by name, sources by seed table name), comments "
        "dropped, transpiled duckdb -> bigquery with sqlglot "
        f"{sqlglot.__version__}; each table transpiled back to duckdb reproduces dbt's rows on the seed data "
        "(time-dependent tables: reproduces the duckdb compiled SQL with the same fixed time).",
        "Sources are the project's seeds; seed types mapped INTEGER/BIGINT->INT64, VARCHAR->STRING, "
        "FLOAT/DOUBLE->FLOAT64 (FLOAT seeds rounded to 7 significant digits), DECIMAL->NUMERIC. No keys declared.",
    ]
    if pkg.uses_now:
        notes.append(f"now()/current_timestamp replaced by {FIXED_TS} and current_date by {FIXED_DATE} in: "
                     + ", ".join(sorted(pkg.uses_now)) + ".")
    if pkg.ordered:
        notes.append("STRING_AGG/ARRAY_AGG without ORDER BY given ORDER BY its own argument (ASC NULLS FIRST) so "
                     "the output does not depend on row order (these tables, and tables reading them, are checked "
                     "against the duckdb compiled SQL with the same change rather than dbt's table): "
                     + ", ".join(sorted(pkg.ordered)) + ".")
    if pkg.loose:
        notes.append("Matched dbt's table only at 6 significant digits (float32 seed columns read as FLOAT64): "
                     + ", ".join(pkg.loose) + ".")
    if pkg.dropped:
        notes.append(f"{len(pkg.dropped)} models dropped (failed the round trip or downstream of one): "
                     + ", ".join(sorted(pkg.dropped)) + ".")
    notes.append(f"Reference ({kind}): verified equal on the seed database and {len(oracle.dbs)} random databases "
                 "(DuckDB, optimizer off, bags with column names in order, floats compared at 12 significant "
                 "digits); a random database counts for a protected table only when the original gives the "
                 "same output with every source's rows reversed (order-sensitive outputs skipped: "
                 + ", ".join(f"{p} {len(oracle.dbs) - stable[p]}" for p in protected) + ").")
    order_dep = sorted(t for t in needed(tables, set(protected), alive_deps) if unordered_aggregates(tables[t]))
    if order_dep:
        notes.append("Caution: STRING_AGG/ARRAY_AGG without ORDER BY (output depends on row order) in tables the "
                     "protected ones read: " + ", ".join(order_dep) + ".")
    if removed_pt:
        notes.append("Pass-through tables replaced by what they read: " + "; ".join(removed_pt) + ".")
    if attempts:
        notes.append(" ".join(attempts))
    case = {
        "id": case_id,
        "source": f"{pkg.origin}@{pkg.sha}",
        "families": families,
        "split": split_of(case_id),
        "dialect": "bigquery",
        "sources": pkg.sources,
        "tables": tables,
        "protected": protected,
        "original": {"complexity": orig_c},
        "reference": {"tables": ref, "complexity": ref_c},
        "reference_kind": "mechanical",
        "traps": [],
        "verification": {"engine": f"duckdb {duckdb.__version__}, optimizer off", "databases": 1 + len(oracle.dbs),
                         "seed": RANDOM_SEED, "proved": {p: False for p in protected},
                         "order_sensitive_skipped": {p: len(oracle.dbs) - stable[p] for p in protected}},
        "data": {s: [[json_value(v) for v in r] for r in rows] for s, rows in pkg.seed_rows.items()},
        "note": " ".join(notes),
    }
    stats.setdefault("cases", []).append({
        "id": case_id, "split": case["split"], "protected": protected, "families": families, "kind": kind,
        "original": orig_c, "reference": ref_c, "removed_passthrough": removed_pt,
        "dead": len(dead), "attempts": attempts, "time_dependent_protected": time_dep,
        "unordered_aggregates": order_dep})
    return case


def convert(repo: str, clones: Path, work: Path, skip_build: bool) -> tuple[list[dict], dict]:
    it = work / repo if repo in GENERIC else work / repo / "integration_tests"
    if not skip_build:
        it = build(repo, clones, work)
    pkg = Package(repo, it, clones / repo, work)
    stats = {"repo": repo, "sha": pkg.sha, "tag": pkg.tag, "models": pkg.n_models, "alive": len(pkg.alive()),
             "dropped": pkg.dropped, "end": pkg.end, "uses_now": sorted(pkg.uses_now),
             "time_dependent": sorted(pkg.time_dependent), "loose": pkg.loose,
             "seed_rows": {s: len(r) for s, r in pkg.seed_rows.items()},
             "end_seed_rows": {}}
    oracle = Oracle(pkg)
    stats.update({"random_dbs": len(oracle.dbs), "random_tries": oracle.tries, "random_errors": oracle.errors,
                  "stable": oracle.stable_count, "seed_stable": sorted(oracle.seed_stable), "nonempty": oracle.nonempty_count,
                  "end_seed_rows": {t: sum(oracle.seed_out[t][1].values()) for t in pkg.end}})
    cases = []
    if not pkg.end:
        return cases, stats
    short = pkg.pkg
    if len(oracle.dbs) < N_RANDOM:
        stats["skipped"] = f"only {len(oracle.dbs)} random databases ran without error"
        return cases, stats
    usable = [t for t in pkg.end if oracle.stable_count[t] >= N_RANDOM and t in oracle.seed_stable]
    stats["unusable_end"] = sorted(set(pkg.end) - set(usable))
    if pkg.generic:  # numbered ids: <prefix>-01 protects every end model, then one per end model
        ids = [f"{pkg.generic['ids']}-{i + 1:02d}" for i in range(len(usable) + 1)]
    else:
        ids = [f"fivetran-{short}-all"] + [f"fivetran-{short}-{t}" for t in usable]
    if len(usable) > 1:
        cases.append(make_case(pkg, oracle, ids[0], usable, stats))
    for case_id, t in zip(ids[1:], usable):
        cases.append(make_case(pkg, oracle, case_id, [t], stats))
    return cases, stats


def _sqlx(sql: str, rename: dict[str, str], sources) -> str:
    """The table's SQL as Dataform SQLX: bare names become ${ref(...)} (renamed ones to their copy)."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    for t in list(tree.find_all(exp.Table)):
        if t.db or is_cte_ref(t):
            continue
        old = t.name
        alias = t.args.get("alias")
        t.set("this", exp.to_identifier(f"__REF__{rename.get(old, old)}__FER__"))
        if alias is None:
            t.set("alias", exp.TableAlias(this=exp.to_identifier(old)))
    text = tree.sql(dialect="bigquery", comments=False)

    def ref(m):
        name = m.group(1)
        return '${ref("raw", "%s")}' % name if name in sources else '${ref("%s")}' % name

    return re.sub(r"`?__REF__(\w+?)__FER__`?", ref, text)


def prove_case(case: dict, timeout_ms: int = 5000) -> dict[str, bool]:
    """Per protected table: did KumoSQL's prove_models show reference == original?

    Both pipelines go into one Dataform project (reference tables that differ, or read one that
    differs, get a `__ref` copy) and `prove_models` compares each protected table with its copy.
    """

    import contextlib
    import io
    import tempfile

    from kumosql import load_sqlx_project
    from kumosql.pipeline_equivalence import prove_models
    from kumosql.prover_schema import from_pipeline

    orig, ref = case["tables"], case["reference"]["tables"]
    ref_deps = {t: table_refs(sql) for t, sql in ref.items()}
    changed = {t for t in ref if ref[t] != orig.get(t)}
    changed = downstream(changed, ref_deps) & set(ref)
    rename = {t: f"{t}__ref" for t in changed}
    sources = set(case["sources"])
    out = {}
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        root = Path(tmp)
        (root / "definitions").mkdir()
        (root / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: an\n")
        for s in sources:
            (root / "definitions" / f"src_{s}.sqlx").write_text(
                f'config {{ type: "declaration", schema: "raw", name: "{s}" }}\n')
        for t, sql in orig.items():
            (root / "definitions" / f"{t}.sqlx").write_text('config { type: "table" }\n' + _sqlx(sql, {}, sources) + "\n")
        for t in changed:
            (root / "definitions" / f"{rename[t]}.sqlx").write_text(
                'config { type: "table" }\n' + _sqlx(ref[t], rename, sources) + "\n")
        pipeline = load_sqlx_project(root)
        pipeline.source_schema.update({f"proj.raw.{s}": dict(spec["columns"]) for s, spec in case["sources"].items()})
        schema = from_pipeline(pipeline)
        for p in case["protected"]:
            try:
                result = prove_models(pipeline, p, rename.get(p, p), declared=[], schema=schema, timeout_ms=timeout_ms)
                out[p] = bool(result.proven)
            except Exception:  # noqa: BLE001
                out[p] = False
    return out


def _prove_worker(args):
    line, timeout_s = args
    import multiprocessing as mp

    case = json.loads(line)
    with mp.get_context("fork").Pool(1) as pool:
        job = pool.apply_async(prove_case, (case,))
        try:
            return case["id"], job.get(timeout=timeout_s)
        except Exception:  # noqa: BLE001  (timeout or crash: not proved)
            return case["id"], {p: False for p in case["protected"]}


def prove_file(path: Path, timeout_s: int, jobs: int) -> None:
    """Fill verification.proved in a cases file (each case gets `timeout_s` seconds)."""

    from concurrent.futures import ProcessPoolExecutor

    lines = path.read_text().splitlines()
    with ProcessPoolExecutor(jobs) as ex:
        results = dict(ex.map(_prove_worker, [(line, timeout_s) for line in lines]))
    cases = [json.loads(line) for line in lines]
    for c in cases:
        c["verification"]["proved"] = results[c["id"]]
        c["verification"]["proved_timeout_s"] = timeout_s
    path.write_text("".join(json.dumps(c) + "\n" for c in cases))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clones", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stats", type=Path)
    ap.add_argument("--repos", nargs="+", default=["dbt_asana", "dbt_intercom", "dbt_iterable", "dbt_klaviyo",
                                                    "dbt_mailchimp", "dbt_recurly"])
    ap.add_argument("--skip-build", action="store_true", help="reuse WORK/<repo> from an earlier run")
    ap.add_argument("--prove-only", action="store_true", help="only fill verification.proved in --out")
    ap.add_argument("--prove", action="store_true", help="also try KumoSQL's prove_models on every case")
    ap.add_argument("--prove-timeout", type=int, default=300)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    if args.prove_only:
        prove_file(args.out, args.prove_timeout, args.jobs)
        return 0
    clones, work = args.clones.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    prepare_utils(clones, work)
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(args.jobs) as ex:
        results = list(ex.map(convert, args.repos, [clones] * len(args.repos), [work] * len(args.repos),
                              [args.skip_build] * len(args.repos)))
    all_cases, all_stats = [], []
    for repo, (cases, stats) in zip(args.repos, results):
        print(f"{repo}: {len(cases)} cases, {stats['alive']}/{stats['models']} models kept", flush=True)
        all_cases += cases
        all_stats.append(stats)
    with args.out.open("w") as f:
        for c in all_cases:
            f.write(json.dumps(c) + "\n")
    if args.prove:
        prove_file(args.out, args.prove_timeout, args.jobs)
        proved = {json.loads(line)["id"]: json.loads(line)["verification"]["proved"]
                  for line in args.out.read_text().splitlines()}
        for stats in all_stats:
            for c in stats.get("cases", []):
                c["proved"] = proved[c["id"]]
    if args.stats:
        args.stats.write_text(json.dumps(all_stats, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
