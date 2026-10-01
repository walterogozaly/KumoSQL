"""Duplicate-detection and shared-model extraction benchmark, generated in Python, no language model.

A seeded generator writes Dataform-style projects of a few to thousands of models around known
*families* of SELECTs and records the truth for every copy:

* ``equiv``: the same query in different clothes. Tier A rewrites are purely textual (aliases,
  conjunct order, flipped comparisons, CTE names, layout); tier B rewrites are semantic
  (``IN`` against ``OR``, ``BETWEEN`` against a range, ``IFNULL`` against ``COALESCE``).
* ``near``: the family with a different literal, an extra filter or an extra column.
* ``decoy``: deceptively similar but *not* equivalent (``>`` against ``>=``, ``AND`` against
  ``OR``, ``INNER`` against ``LEFT`` join, ``SUM`` against ``AVG``, a dropped ``DISTINCT``,
  a negated filter). Every decoy is checked on random DuckDB data to really differ.

Scoring keeps four things apart:

1. **correctness**: unsafe refactors offered as ready (must be 0) and decoys grouped as exact
   duplicates (must be 0);
2. **analysis quality**: pair precision/recall of exact duplicates (fingerprints), of near-duplicate
   clusters and of the "repeated work" report;
3. **usefulness**: shared-model refactors that are proposed *and* verified ready;
4. **performance**: seconds to analyse.

Each proposal's refactor is applied to every copy and judged at three evidence levels, never mixed:
``proof`` (the prover proves the model unchanged), ``bounded`` (equal on every database of a
small exhaustive domain, DuckDB) and ``executed`` (equal on random DuckDB databases). Families
whose id is divisible by ``HOLDOUT_EVERY`` are held out: they never influence any rule and are
scored separately.

    python tools/dup_bench.py                     # sizes 6, 60, 600 models
    python tools/dup_bench.py --sizes 6,60,600,3000 --seed 1 --json out.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import itertools
import json
import logging
from pathlib import Path
import random
import sys
import time

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

HOLDOUT_EVERY = 5
TABLE_COLUMNS = {"id": "int", "k": "int", "amt": "int", "qty": "int", "status": "str", "region": "str"}
DIM_COLUMNS = {"id": "int", "label": "str", "tier": "str", "score": "int"}
STATUSES = ["paid", "open", "void", "late", "hold"]
REGIONS = ["eu", "us", "apac", "latam"]
TIERS = ["gold", "silver", "bronze"]
NUMERIC_OPS = [">", ">=", "<", "<=", "="]
FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "="}
NEXT_OP = {">": ">=", ">=": ">", "<": "<=", "<=": "<"}


# ------------------------------------------------------------------ the spec


@dataclass(frozen=True)
class Cond:
    col: str  # "amt" or "d.tier": a column of the fact table or, with "d.", of the dimension
    kind: str  # "cmp", "in", "notnull", "between", "ne"
    op: str = ""
    value: tuple = ()

    def columns(self) -> tuple[str, ...]:
        return (self.col,)


@dataclass(frozen=True)
class Spec:
    table: str
    join: str  # "", "inner" or "left"
    cols: tuple[str, ...]
    conds: tuple[Cond, ...]
    agg: str = ""  # "", "sum", "avg", "count"
    group: str = ""
    distinct: bool = False
    ifnull_cols: tuple[str, ...] = ()
    extra_or: tuple[int, int] | None = None  # conds i and j are joined by OR instead of AND
    negated: int | None = None  # index of a cond rendered as NOT (...)

    def semantic_key(self) -> tuple:
        return (self.table, self.join, self.cols, frozenset(self.conds), self.agg, self.group,
                self.distinct, self.ifnull_cols, self.extra_or, self.negated)


def _lit(value) -> str:
    return str(value) if isinstance(value, int) else "'" + str(value) + "'"


def render(spec: Spec, rng: random.Random | None, *, tier_a: bool = False, tier_b: bool = False,
           project: str | None = "proj", dataset: str = "raw") -> str:
    """The spec as SQL. With ``rng`` the rewrites are chosen at random; without it, canonically."""

    pick = rng or random.Random(0)
    fa, da = (pick.choice(["o", "t", "f", "x1", "base_t"]), pick.choice(["d", "c", "dim", "y2"])) if tier_a else ("o", "d")
    qualified = {c: (f"{da}.{c[2:]}" if c.startswith("d.") else f"{fa}.{c}") for c in set(
        [*spec.cols, *(c.col for c in spec.conds), *([spec.group] if spec.group else [])])}

    def ref(name: str) -> str:
        return f"`{project}.{dataset}.{name}`" if project else name

    def cond_sql(c: Cond) -> str:
        col = qualified[c.col]
        if c.kind == "cmp":
            if tier_a and rng and rng.random() < 0.5:
                return f"{_lit(c.value[0])} {FLIP[c.op]} {col}"
            return f"{col} {c.op} {_lit(c.value[0])}"
        if c.kind == "ne":
            return f"{col} {'!=' if tier_a and rng and rng.random() < 0.5 else '<>'} {_lit(c.value[0])}"
        if c.kind == "notnull":
            return f"{col} IS NOT NULL"
        if c.kind == "in":
            values = list(c.value)
            if tier_a and rng:
                rng.shuffle(values)
            if tier_b and rng and rng.random() < 0.7:
                return "(" + " OR ".join(f"{col} = {_lit(v)}" for v in values) + ")"
            return f"{col} IN ({', '.join(_lit(v) for v in values)})"
        if c.kind == "between":
            low, high = c.value
            if tier_b and rng and rng.random() < 0.7:
                return f"({col} >= {low} AND {col} <= {high})"
            return f"{col} BETWEEN {low} AND {high}"
        raise ValueError(c.kind)

    def col_sql(c: str) -> str:
        if c in spec.ifnull_cols:
            fn = "COALESCE" if tier_b and rng and rng.random() < 0.7 else "IFNULL"
            return f"{fn}({qualified[c]}, 0) AS {c.replace('d.', '')}"
        return qualified[c]

    parts: list[str] = []
    for i, c in enumerate(spec.conds):
        text = cond_sql(c)
        if spec.negated == i:
            text = f"NOT ({text})"
        parts.append((i, text))
    if spec.extra_or is not None:
        i, j = spec.extra_or
        merged = f"({dict(parts)[i]} OR {dict(parts)[j]})"
        parts = [(k, t) for k, t in parts if k not in (i, j)] + [(min(i, j), merged)]
    order = [t for _, t in sorted(parts)]
    if tier_a and rng:
        rng.shuffle(order)
    if spec.agg:
        measure = {"sum": f"SUM({fa}.amt) AS total", "avg": f"AVG({fa}.amt) AS total", "count": f"COUNT({fa}.amt) AS total"}[spec.agg]
        select = f"{qualified[spec.group]}, {measure}"
    else:
        select = ", ".join(col_sql(c) for c in spec.cols)
    if spec.distinct:
        select = "DISTINCT " + select
    sql = f"SELECT {select} FROM {ref(spec.table)} AS {fa}"
    if spec.join:
        kind = {"inner": "JOIN" if not tier_a or not rng or rng.random() < 0.5 else "INNER JOIN", "left": "LEFT JOIN"}[spec.join]
        sql += f" {kind} {ref('d' + spec.table)} AS {da} ON {fa}.k = {da}.id"
    if order:
        sql += " WHERE " + " AND ".join(order)
    if spec.agg:
        sql += f" GROUP BY {qualified[spec.group]}"
    return sql


# ---------------------------------------------------------------- generation


@dataclass
class Site:
    model: str
    family: int
    kind: str  # equiv_a, equiv_b, near_literal, near_filter, near_cols, decoy_<what>, base
    spec: Spec
    class_id: tuple
    placement: str
    holdout: bool


@dataclass
class Project:
    graph: dict
    sites: list[Site]
    families: dict[int, list[Site]]
    noise: int
    tables: list[str]
    seed: int
    models: int = 0
    downstream: dict[str, list[str]] = field(default_factory=dict)


def _random_cond(rng: random.Random, table_cols: list[str], join: bool, used: set[str]) -> Cond:
    options = [c for c in table_cols if c not in ("id", "k") and c not in used]
    if join:
        options += ["d.tier", "d.score"]
        options = [c for c in options if c not in used]
    col = rng.choice(options)
    bare = col.replace("d.", "")
    if bare in ("amt", "qty", "score"):
        kind = rng.choice(["cmp", "cmp", "between", "ne", "notnull"])
        if kind == "cmp":
            return Cond(col, "cmp", rng.choice(NUMERIC_OPS), (rng.randint(2, 40),))
        if kind == "between":
            low = rng.randint(1, 20)
            return Cond(col, "between", value=(low, low + rng.randint(3, 15)))
        if kind == "ne":
            return Cond(col, "ne", value=(rng.randint(2, 40),))
        return Cond(col, "notnull")
    pool = {"status": STATUSES, "region": REGIONS, "tier": TIERS}[bare]
    kind = rng.choice(["cmp", "in", "ne", "notnull"])
    if kind == "cmp":
        return Cond(col, "cmp", "=", (rng.choice(pool),))
    if kind == "in":
        return Cond(col, "in", value=tuple(rng.sample(pool, 2)))
    if kind == "ne":
        return Cond(col, "ne", value=(rng.choice(pool),))
    return Cond(col, "notnull")


def _skeleton(spec: Spec) -> frozenset:
    return frozenset([("t", spec.table), ("j", spec.join), ("g", spec.group), ("a", spec.agg),
                      *(("c", c) for c in spec.cols), *(("w", c.col) for c in spec.conds)])


def _family_spec(rng: random.Random, table: str, skeletons: list[frozenset]) -> Spec:
    cols_pool = [c for c in TABLE_COLUMNS if c != "k"]
    for _ in range(200):
        join = rng.choice(["", "inner", "inner", "left"])
        agg = rng.choice(["", "", "sum", "count"])
        cols = tuple(rng.sample(cols_pool, rng.randint(3, 5)))
        if join:
            cols += tuple(rng.sample(["d.label", "d.tier"], 1))
        used: set[str] = set()
        conds = []
        for _ in range(rng.randint(2, 3)):
            cond = _random_cond(rng, list(TABLE_COLUMNS), bool(join), used)
            used.add(cond.col)
            conds.append(cond)
        group = rng.choice([c for c in cols if c not in ("amt",)]) if agg else ""
        spec = Spec(table, join, cols, tuple(conds), agg, group,
                    ifnull_cols=(("amt",) if "amt" in cols and not agg and rng.random() < 0.4 else ()))
        shape = _skeleton(spec)
        if all(len(shape ^ other) >= 6 for other in skeletons):
            skeletons.append(shape)
            return spec
    skeletons.append(shape)
    return spec


def _decoy(rng: random.Random, spec: Spec) -> tuple[str, Spec]:
    options = []
    cmps = [i for i, c in enumerate(spec.conds) if c.kind == "cmp" and c.op in NEXT_OP]
    if cmps:
        options.append("decoy_boundary")
    options.append("decoy_literal")
    if len(spec.conds) >= 2:
        options.append("decoy_or")
    options.append("decoy_not")
    if spec.join == "inner":
        options.append("decoy_join")
    if spec.agg == "sum":
        options.append("decoy_agg")
    if not spec.agg and not spec.distinct:
        options.append("decoy_distinct")
    kind = rng.choice(options)
    conds = list(spec.conds)
    if kind == "decoy_boundary":
        i = rng.choice(cmps)
        conds[i] = Cond(conds[i].col, "cmp", NEXT_OP[conds[i].op], conds[i].value)
        return kind, _replace(spec, conds=tuple(conds))
    if kind == "decoy_literal":
        i = rng.randrange(len(conds))
        c = conds[i]
        if c.kind in ("cmp", "ne"):
            value = c.value[0]
            new = value + 1 if isinstance(value, int) else rng.choice([v for v in STATUSES + REGIONS + TIERS if v != value])
            conds[i] = Cond(c.col, c.kind, c.op, (new,))
        elif c.kind == "in":
            conds[i] = Cond(c.col, "in", value=(c.value[0],))
        elif c.kind == "between":
            conds[i] = Cond(c.col, "between", value=(c.value[0] + 1, c.value[1]))
        else:
            conds[i] = Cond(c.col, "cmp", ">", (3,)) if c.col in ("amt", "qty", "d.score") else c
        return kind, _replace(spec, conds=tuple(conds))
    if kind == "decoy_or":
        return kind, _replace(spec, extra_or=(0, 1))
    if kind == "decoy_not":
        return kind, _replace(spec, negated=rng.randrange(len(conds)))
    if kind == "decoy_join":
        return kind, _replace(spec, join="left")
    if kind == "decoy_agg":
        return kind, _replace(spec, agg="avg")
    return kind, _replace(spec, distinct=True)


def _replace(spec: Spec, **changes) -> Spec:
    data = {**spec.__dict__, **changes}
    return Spec(**data)


def _near(rng: random.Random, spec: Spec) -> tuple[str, Spec]:
    choice = rng.choice(["near_literal", "near_filter", "near_cols"])
    if choice == "near_literal":
        conds = []
        changed = False
        for c in spec.conds:
            if not changed and c.kind == "cmp" and c.op == "=" and isinstance(c.value[0], str):
                pool = STATUSES if c.col == "status" else REGIONS if c.col == "region" else TIERS
                conds.append(Cond(c.col, "cmp", "=", (rng.choice([v for v in pool if v != c.value[0]]),)))
                changed = True
            elif not changed and c.kind == "cmp" and isinstance(c.value[0], int):
                conds.append(Cond(c.col, "cmp", c.op, (c.value[0] + rng.randint(1, 9),)))
                changed = True
            else:
                conds.append(c)
        if changed:
            return choice, _replace(spec, conds=tuple(conds))
        choice = "near_filter"
    if choice == "near_filter" and not spec.agg:
        used = {c.col for c in spec.conds}
        free = [c for c in ("amt", "qty", "region", "status") if c not in used]
        if free:
            extra = _random_cond(rng, free, False, used)
            if extra.col in free:
                return choice, _replace(spec, conds=(*spec.conds, extra))
    free_cols = [c for c in TABLE_COLUMNS if c not in spec.cols and c != "k"] if not spec.agg else []
    if free_cols:
        return "near_cols", _replace(spec, cols=(*spec.cols, rng.choice(free_cols)))
    bumped = tuple(
        Cond(c.col, c.kind, c.op, (c.value[0] + 1,)) if c.kind == "cmp" and isinstance(c.value[0], int) else c
        for c in spec.conds
    )
    if bumped != spec.conds:
        return "near_literal", _replace(spec, conds=bumped)
    return "base", spec


def generate(models: int, seed: int = 1) -> Project:
    """A project of about ``models`` models: families of copies, downstream readers and unrelated noise."""

    rng = random.Random(seed)
    table_count = max(3, models // 25)
    tables = [f"t{i:03d}" for i in range(table_count)]
    skeletons_by_table: dict[str, list[frozenset]] = defaultdict(list)
    sites: list[Site] = []
    families: dict[int, list[Site]] = {}
    tails = ["", ""]
    graph_tables = []
    family_count = max(1, int(models * 0.75) // 5)
    index = 0

    def add_model(name: str, query: str) -> None:
        graph_tables.append({"target": {"database": "proj", "schema": "mart", "name": name}, "type": "table",
                             "query": query, "fileName": f"definitions/{name}.sqlx"})

    placements = ["cte", "cte", "whole", "subquery"]
    for family in range(family_count):
        table = tables[family % len(tables)]
        base = _family_spec(rng, table, skeletons_by_table[table])
        holdout = family % HOLDOUT_EVERY == 0
        variants: list[tuple[str, Spec, dict]] = [("base", base, {})]
        for _ in range(rng.randint(1, 2)):
            variants.append(("equiv_a", base, {"tier_a": True}))
        if rng.random() < 0.7:
            variants.append(("equiv_b", base, {"tier_a": True, "tier_b": True}))
        for _ in range(rng.randint(0, 1)):
            kind, spec = _near(rng, base)
            if kind != "base":
                variants.append((kind, spec, {"tier_a": True}))
        for _ in range(rng.randint(1, 2)):
            kind, spec = _decoy(rng, base)
            variants.append((kind, spec, {"tier_a": True}))
        for kind, spec, options in variants:
            name = f"m{index:05d}_f{family}"
            index += 1
            body = render(spec, rng, **options)
            placement = rng.choice(placements)
            cols = [c.replace("d.", "") for c in spec.cols] if not spec.agg else [spec.group.replace("d.", ""), "total"]
            if placement == "cte":
                cte = rng.choice(["base", "src", "prepared", "scoped"])
                query = f"WITH {cte} AS ({body}) SELECT {', '.join(cols[:2])} FROM {cte} WHERE {cols[0]} IS NOT NULL"
            elif placement == "subquery":
                query = f"SELECT {cols[0]} FROM ({body}) AS s WHERE {cols[0]} IS NOT NULL"
            else:
                query = body
            add_model(name, query)
            site = Site(name, family, kind, spec,
                        (family, spec.semantic_key()), placement, holdout)
            sites.append(site)
            families.setdefault(family, []).append(site)

    downstream: dict[str, list[str]] = {}
    for site in rng.sample(sites, k=min(len(sites), max(1, len(sites) // 4))):
        child = f"down_{site.model}"
        add_model(child, f"SELECT COUNT(*) AS n FROM `proj.mart.{site.model}`")
        downstream.setdefault(site.model, []).append(child)
    noise = max(0, models - len(graph_tables))
    for i in range(noise):
        table = rng.choice(tables)
        add_model(f"noise_{i:05d}", (f"SELECT {rng.choice(['id', 'k', 'qty'])}, COUNT(*) AS n{i % 3} FROM `proj.raw.{table}` "
                                     f"WHERE {rng.choice(['qty', 'id', 'amt'])} > {rng.randint(1, 99)} GROUP BY 1"))
    declarations = [{"target": {"database": "proj", "schema": "raw", "name": t}} for t in tables]
    declarations += [{"target": {"database": "proj", "schema": "raw", "name": "d" + t}} for t in tables]
    return Project({"tables": graph_tables, "declarations": declarations}, sites, families, noise, tables, seed,
                   models=len(graph_tables), downstream=downstream)


# ------------------------------------------------------------------ DuckDB


def make_database(tables: list[str], seed: int, rows: int = 40):
    """A DuckDB connection with random data: small value domains, some NULLs, so filters bite."""

    import duckdb

    rng = random.Random(seed)
    con = duckdb.connect()

    def value(kind: str, column: str):
        if rng.random() < 0.12 and column not in ("id",):
            return None
        if kind == "int":
            return rng.randint(0, 45)
        return rng.choice({"status": STATUSES, "region": REGIONS, "tier": TIERS}.get(column, ["a", "b", "c"]))

    for table in tables:
        for name, columns in ((table, TABLE_COLUMNS), ("d" + table, DIM_COLUMNS)):
            ddl = ", ".join(f"{c} {'INTEGER' if k == 'int' else 'VARCHAR'}" for c, k in columns.items())
            con.execute(f"CREATE TABLE {name} ({ddl})")
            data = []
            for i in range(rows if name == table else 12):
                row = []
                for c, k in columns.items():
                    if c == "id":
                        row.append(i % 12 if name != table else i)
                    elif c == "k":
                        row.append(rng.choice([None] + list(range(0, 14))))
                    else:
                        row.append(value(k, c))
                data.append(row)
            con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(columns))})", data)
    return con


def to_duckdb(sql: str) -> str:
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="bigquery")
    for table in tree.find_all(exp.Table):
        table.set("db", None)
        table.set("catalog", None)
    return tree.sql(dialect="duckdb")


def rows_of(con, sql: str) -> list[tuple] | None:
    try:
        rows = con.execute(to_duckdb(sql)).fetchall()
    except Exception:
        return None
    return sorted(rows, key=lambda r: tuple((x is None, str(x)) for x in r))


def agree(sql_a: str, sql_b: str, tables: list[str], databases: int = 6, seed: int = 0) -> bool | None:
    """Whether two queries return the same rows (as a bag, by position) on every random database."""

    for i in range(databases):
        con = make_database(tables, seed * 1000 + i)
        left, right = rows_of(con, sql_a), rows_of(con, sql_b)
        if left is None or right is None:
            return None
        if left != right:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", default="6,60,600")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dup_bench_run

    report = dup_bench_run.run_sizes([int(s) for s in args.sizes.split(",")], args.seed)
    print(dup_bench_run.format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
