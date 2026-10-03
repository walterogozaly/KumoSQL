"""Whole-pipeline equivalence eval: does a multi-model refactor keep every declared output?

Each case is a Dataform-style pipeline (several ``.sqlx`` models over declared source
tables), a refactor of it (a filter moved upstream, a shared model extracted, joins
staged differently, ...), and the list of *observable outputs*: the models consumers
read. An intermediate model that is exposed counts as an output; a hidden one does not.
Cases are generated in Python with a known answer (``equivalent`` or ``different``), at
sizes from a few models to dozens, with deliberately broken refactors next to the sound
ones. There is no LLM at evaluation time.

Three evidence levels are reported separately:

* ``proof``: ``kumosql.pipeline_equivalence.prove_models`` (unbounded; layer lemmas, then
  inlining) proves every output equal.
* ``bounded``: the prover's counterexample (found on a small database) refutes an output,
  and the counterexample replays in DuckDB through the whole pipeline.
* ``executed``: both pipelines are materialised in DuckDB on random source databases
  (NULLs, duplicates, empty tables) and each output is compared as a bag. This is also
  what validates the generator's known answers.

Scoring: a ``proved`` verdict on a ``different`` case or a ``different`` verdict on an
``equivalent`` case is wrong (must stay 0). ``unknown`` is never wrong.

    python tools/pipeline_bench.py                # development families
    python tools/pipeline_bench.py --held-out     # held-out families (run once, at the end)
    python tools/pipeline_bench.py --json out.json
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import re
import sys
import tempfile
import time

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PROJECT, DATASET = "proj", "an"
SOURCES = {
    "orders": ["id", "customer_id", "amount", "status", "region"],
    "customers": ["id", "region", "tier"],
    "items": ["order_id", "sku", "qty", "price"],
    "events_a": ["id", "kind", "amount"],
    "events_b": ["id", "kind", "amount"],
}
STATUSES = ["paid", "open", "void", None]
REGIONS = ["eu", "us", None]
TIERS = ["gold", "basic", None]
KINDS = ["buy", "refund", None]

HELD_OUT_FAMILIES = {"union_split", "latest_per_key"}


@dataclass
class Case:
    id: str
    family: str
    label: str  # "equivalent" or "different"
    before: dict[str, str]  # model -> SQL, models read each other as {{name}} and sources as {{raw.name}}
    after: dict[str, str]  # models that change or are added; all others are shared
    outputs: list[str]
    note: str = ""

    @property
    def held_out(self) -> bool:
        return self.family in HELD_OUT_FAMILIES

    @property
    def size(self) -> int:
        return len({*self.before, *self.after})


# ---------------------------------------------------------------- generators


def _passthrough_chain(prefix: str, depth: int, source: str, columns: str) -> dict[str, str]:
    models = {f"{prefix}1": f"SELECT {columns} FROM {{{{raw.{source}}}}}"}
    for i in range(2, depth + 1):
        models[f"{prefix}{i}"] = f"SELECT {columns} FROM {{{{{prefix}{i - 1}}}}}"
    return models


def filter_upstream(depth: int) -> list[Case]:
    """Move a filter from the consumer up to the first model of a chain."""

    cols = "id, customer_id, amount, status"
    chain = _passthrough_chain("stg", depth, "orders", cols)
    top = f"stg{depth}"
    cases = []

    def case(variant, label, final_before, final_after, first_after=None, outputs=("final",), upstream=None, note=""):
        before = {**chain, **(upstream or {}), "final": final_before}
        after = {"final": final_after}
        if first_after:
            after["stg1"] = first_after
        cases.append(Case(f"filter_upstream/{variant}/{depth}", "filter_upstream", label, before, after, list(outputs), note))

    agg = "SELECT customer_id, SUM(amount) AS total FROM {{%s}} %s GROUP BY customer_id"
    pushed_first = f"SELECT {cols} FROM {{{{raw.orders}}}} WHERE status = 'paid'"
    case("push_to_first", "equivalent", agg % (top, "WHERE status = 'paid'"), agg % (top, ""), pushed_first)
    case("exposed_intermediate", "different", agg % (top, "WHERE status = 'paid'"), agg % (top, ""), pushed_first,
         outputs=("final", "stg1"), note="the filter now also changes an exposed intermediate")
    case("exposed_top", "different", agg % (top, "WHERE status = 'paid'"), agg % (top, ""), pushed_first,
         outputs=("final", top), note="the filter now also changes the exposed last staging model")
    case("group_key_before_aggregate", "equivalent",
         "SELECT * FROM (SELECT customer_id, SUM(amount) AS total FROM {{%s}} GROUP BY customer_id) WHERE customer_id = 2" % top,
         "SELECT customer_id, SUM(amount) AS total FROM {{%s}} WHERE customer_id = 2 GROUP BY customer_id" % top)
    case("aggregate_filter_before_aggregate", "different",
         "SELECT customer_id, SUM(amount) AS total FROM {{%s}} GROUP BY customer_id HAVING SUM(amount) > 5" % top,
         "SELECT customer_id, SUM(amount) AS total FROM {{%s}} WHERE amount > 5 GROUP BY customer_id" % top)
    # outer joins: a filter on the preserved side moves up, one on the optional side does not
    join = "SELECT o.id, o.amount, c.tier FROM {{%s}} o LEFT JOIN {{customers_dim}} c ON o.customer_id = c.id %s"
    dim = {"customers_dim": "SELECT id, tier FROM {{raw.customers}}"}
    case("left_join_preserved_side", "equivalent", join % (top, "WHERE o.status = 'paid'"), join % (top, ""),
         pushed_first, upstream=dim)
    case("left_join_optional_side", "different", join % (top, "WHERE c.tier = 'gold'"), join % (top, ""), None,
         upstream=dim, outputs=("final",), note="")
    cases[-1].after["customers_dim"] = "SELECT id, tier FROM {{raw.customers}} WHERE tier = 'gold'"
    case("left_join_optional_side_null_safe", "different", join % (top, "WHERE c.tier IS NULL"), join % (top, ""), None,
         upstream=dim)
    cases[-1].after["customers_dim"] = "SELECT id, tier FROM {{raw.customers}} WHERE tier IS NULL"
    return cases


def extract_shared(consumers: int) -> list[Case]:
    """Two or more models repeat the same enrichment; extract it into one shared model."""

    enrich = ("SELECT o.id, o.customer_id, o.amount, c.tier FROM {{raw.orders}} o "
              "JOIN {{raw.customers}} c ON o.customer_id = c.id WHERE o.status = 'paid'")
    shapes = [
        "SELECT tier, SUM(amount) AS total FROM ({e}) GROUP BY tier",
        "SELECT customer_id, COUNT(*) AS n FROM ({e}) GROUP BY customer_id",
        "SELECT id, amount FROM ({e}) WHERE amount > 0",
        "SELECT tier, MAX(amount) AS biggest FROM ({e}) GROUP BY tier",
    ]
    consumers_before = {f"report{i}": shapes[i % len(shapes)].format(e=enrich) for i in range(consumers)}
    reads = {f"report{i}": shapes[i % len(shapes)].format(e="SELECT * FROM {{shared}}") for i in range(consumers)}
    reads = {k: v.replace("FROM (SELECT * FROM {{shared}})", "FROM {{shared}}") for k, v in reads.items()}
    outputs = list(consumers_before)
    base = {"before": consumers_before, "outputs": outputs}
    cases = []

    def add(variant, label, shared_sql, after_extra=None, outs=None, note=""):
        after = {"shared": shared_sql, **reads, **(after_extra or {})}
        cases.append(Case(f"extract_shared/{variant}/{consumers}", "extract_shared", label, dict(base["before"]), after,
                          outs or outputs, note))

    cols = "id, customer_id, amount, tier"
    add("faithful", "equivalent", enrich)
    add("extra_filter", "different", enrich + " AND o.amount > 0", note="only one consumer wanted the filter")
    add("left_join", "different", enrich.replace(" JOIN ", " LEFT JOIN ").replace("WHERE o.status = 'paid'", "AND o.status = 'paid'"),
        note="extracted with an outer join")
    add("distinct", "different", enrich.replace("SELECT o.id", "SELECT DISTINCT o.id"),
        note="DISTINCT cannot change a result whose key is id, but customers may repeat")
    # An extracted model that is also exposed to consumers
    cases.append(Case(f"extract_shared/exposed_shared/{consumers}", "extract_shared", "equivalent",
                      {**consumers_before, "shared": enrich}, {**{k: v for k, v in reads.items()}}, outputs + ["shared"],
                      "the shared model already existed and is exposed; consumers now read it"))
    return cases


def rollup(depth: int) -> list[Case]:
    """Aggregate once at fine grain and roll up, instead of scanning the raw table again."""

    fine = {"daily": "SELECT region, status, SUM(amount) AS total, COUNT(*) AS n, COUNT(amount) AS n_amount FROM {{raw.orders}} GROUP BY region, status"}
    cases = []
    stages = dict(fine)
    prev = "daily"
    for i in range(1, depth):  # extra pass-through layers between fine and coarse grain
        name = f"daily{i}"
        stages[name] = f"SELECT region, status, total, n, n_amount FROM {{{{{prev}}}}}"
        prev = name
    pieces = [
        ("sum_count", "equivalent",
         "SELECT region, SUM(amount) AS total, COUNT(*) AS n FROM {{raw.orders}} GROUP BY region",
         "SELECT region, SUM(total) AS total, SUM(n) AS n FROM {{%s}} GROUP BY region" % prev),
        ("average_of_averages", "different",
         "SELECT region, AVG(amount) AS mean FROM {{raw.orders}} GROUP BY region",
         "SELECT region, AVG(total / n) AS mean FROM {{%s}} GROUP BY region" % prev),
        ("average_from_sum_count", "equivalent",
         "SELECT region, AVG(amount) AS mean FROM {{raw.orders}} GROUP BY region",
         "SELECT region, SUM(total) / SUM(n_amount) AS mean FROM {{%s}} GROUP BY region" % prev),
        ("average_from_row_count", "different",
         "SELECT region, AVG(amount) AS mean FROM {{raw.orders}} GROUP BY region",
         "SELECT region, SUM(total) / SUM(n) AS mean FROM {{%s}} GROUP BY region" % prev),
        ("count_of_non_null_vs_star", "different",
         "SELECT region, COUNT(amount) AS n FROM {{raw.orders}} GROUP BY region",
         "SELECT region, SUM(n) AS n FROM {{%s}} GROUP BY region" % prev),
        ("count_of_non_null", "equivalent",
         "SELECT region, COUNT(amount) AS n FROM {{raw.orders}} GROUP BY region",
         "SELECT region, SUM(n_amount) AS n FROM {{%s}} GROUP BY region" % prev),
    ]
    for variant, label, before_sql, after_sql in pieces:
        cases.append(Case(f"rollup/{variant}/{depth}", "rollup", label, {**stages, "final": before_sql},
                          {"final": after_sql}, ["final"]))
    return cases


def join_staging(depth: int) -> list[Case]:
    """Split a three-way join into staged models (``depth`` pass-through layers on the first stage)."""

    first = "SELECT o.id AS id, o.customer_id AS customer_id, o.amount AS amount, i.sku AS sku, i.qty AS qty FROM {{raw.orders}} o %s {{raw.items}} i ON o.id = i.order_id"
    final = "SELECT x.id, x.sku, x.qty, c.tier FROM {{%s}} x %s {{raw.customers}} c ON x.customer_id = c.id"
    cases = []
    one = ("SELECT o.id AS id, i.sku AS sku, i.qty AS qty, c.tier AS tier FROM {{raw.orders}} o %s {{raw.items}} i ON o.id = i.order_id "
           "%s {{raw.customers}} c ON o.customer_id = c.id")

    def staged(first_join, second_join, name="stage"):
        models = {"stage": first % first_join}
        prev = "stage"
        for i in range(1, depth):
            models[f"stage{i}"] = f"SELECT id, customer_id, amount, sku, qty FROM {{{{{prev}}}}}"
            prev = f"stage{i}"
        models["final"] = final % (prev, second_join)
        return models

    for variant, label, jb, ja in [
        ("inner_inner", "equivalent", ("JOIN", "JOIN"), ("JOIN", "JOIN")),
        ("left_left", "equivalent", ("LEFT JOIN", "LEFT JOIN"), ("LEFT JOIN", "LEFT JOIN")),
        ("left_became_inner_first", "different", ("LEFT JOIN", "LEFT JOIN"), ("JOIN", "LEFT JOIN")),
        ("left_became_inner_second", "different", ("LEFT JOIN", "LEFT JOIN"), ("LEFT JOIN", "JOIN")),
        ("inner_became_left", "different", ("JOIN", "JOIN"), ("LEFT JOIN", "LEFT JOIN")),
    ]:
        before = {"final": one % jb}
        after = staged(*ja)
        cases.append(Case(f"join_staging/{variant}/{depth}", "join_staging", label, before, after, ["final"]))
    return cases


def rename_prune(width: int) -> list[Case]:
    """Rename or drop a column in an intermediate; consumers are adjusted."""

    cols = ["id", "customer_id", "amount", "status"]
    extra = [f"amount * {k + 2} AS metric{k}" for k in range(width)]
    stg = "SELECT " + ", ".join(cols + extra) + " FROM {{raw.orders}}"
    final = "SELECT customer_id, SUM(amount) AS total FROM {{stg}} WHERE status = 'paid' GROUP BY customer_id"
    cases = []
    renamed = "SELECT id, customer_id, amount AS paid_amount, status" + "".join(", " + e for e in extra) + " FROM {{raw.orders}}"
    cases.append(Case(f"rename_prune/rename_unexposed/{width}", "rename_prune", "equivalent", {"stg": stg, "final": final},
                      {"stg": renamed, "final": final.replace("SUM(amount)", "SUM(paid_amount)")}, ["final"]))
    cases.append(Case(f"rename_prune/rename_exposed/{width}", "rename_prune", "equivalent", {"stg": stg, "final": final},
                      {"stg": renamed, "final": final.replace("SUM(amount)", "SUM(paid_amount)")}, ["final", "stg"],
                      "a rename of an exposed column leaves the rows unchanged (columns compared by position)"))
    pruned = "SELECT id, customer_id, amount, status FROM {{raw.orders}}"
    cases.append(Case(f"rename_prune/prune_unexposed/{width}", "rename_prune", "equivalent", {"stg": stg, "final": final},
                      {"stg": pruned}, ["final"]))
    cases.append(Case(f"rename_prune/prune_exposed/{width}", "rename_prune", "different", {"stg": stg, "final": final},
                      {"stg": pruned}, ["final", "stg"], "an exposed intermediate lost columns"))
    swapped = final.replace("SUM(amount)", "SUM(metric0)")
    cases.append(Case(f"rename_prune/wrong_column/{width}", "rename_prune", "different", {"stg": stg, "final": final},
                      {"final": swapped}, ["final"], "the consumer reads a different column"))
    return cases


def union_split(depth: int) -> list[Case]:
    """(held out) Push a consumer filter into each branch of a UNION."""

    chain = {"a1": "SELECT id, kind, amount FROM {{raw.events_a}}", "b1": "SELECT id, kind, amount FROM {{raw.events_b}}"}
    for i in range(2, depth + 1):
        chain[f"a{i}"] = f"SELECT id, kind, amount FROM {{{{a{i - 1}}}}}"
        chain[f"b{i}"] = f"SELECT id, kind, amount FROM {{{{b{i - 1}}}}}"
    a, b = f"a{depth}", f"b{depth}"
    cases = []
    for variant, label, union_before, union_after, filt_before, filt_after in [
        ("all_push_filter", "equivalent", "UNION ALL", "UNION ALL", "kind = 'buy'", None),
        ("distinct_push_filter", "equivalent", "UNION DISTINCT", "UNION DISTINCT", "kind = 'buy'", None),
        ("all_to_distinct", "different", "UNION ALL", "UNION DISTINCT", None, None),
    ]:
        before_union = f"SELECT id, kind, amount FROM {{{{{a}}}}} {union_before} SELECT id, kind, amount FROM {{{{{b}}}}}"
        final_before = "SELECT id, amount FROM {{events}}" + (f" WHERE {filt_before}" if filt_before else "")
        before = {**chain, "events": before_union, "final": final_before}
        if filt_before:
            pushed = f"SELECT id, kind, amount FROM {{{{{a}}}}} WHERE {filt_before} {union_after} SELECT id, kind, amount FROM {{{{{b}}}}} WHERE {filt_before}"
            after = {"events": pushed, "final": "SELECT id, amount FROM {{events}}"}
            outs = ["final"]
        else:
            after = {"events": f"SELECT id, kind, amount FROM {{{{{a}}}}} {union_after} SELECT id, kind, amount FROM {{{{{b}}}}}"}
            outs = ["final"]
        cases.append(Case(f"union_split/{variant}/{depth}", "union_split", label, before, after, outs))
    # the same push but with the union exposed: events changes
    before = {**chain, "events": f"SELECT id, kind, amount FROM {{{{{a}}}}} UNION ALL SELECT id, kind, amount FROM {{{{{b}}}}}",
              "final": "SELECT id, amount FROM {{events}} WHERE kind = 'buy'"}
    after = {"events": f"SELECT id, kind, amount FROM {{{{{a}}}}} WHERE kind = 'buy' UNION ALL SELECT id, kind, amount FROM {{{{{b}}}}} WHERE kind = 'buy'",
             "final": "SELECT id, amount FROM {{events}}"}
    cases.append(Case(f"union_split/exposed_union/{depth}", "union_split", "different", before, after, ["final", "events"],
                      "the exposed union lost its non-buy rows"))
    return cases


def latest_per_key(depth: int) -> list[Case]:
    """(held out) Filters around a latest-row-per-key window."""

    chain = _passthrough_chain("src", depth, "orders", "id, customer_id, amount, status")
    top = f"src{depth}"
    latest = ("SELECT customer_id, id, amount FROM (SELECT customer_id, id, amount, ROW_NUMBER() OVER "
              "(PARTITION BY customer_id ORDER BY id DESC) AS rn FROM {{%s}} %s) WHERE rn = 1")
    cases = []
    cases.append(Case(f"latest_per_key/partition_key_filter/{depth}", "latest_per_key", "equivalent",
                      {**chain, "latest": latest % (top, ""), "final": "SELECT * FROM {{latest}} WHERE customer_id = 2"},
                      {"latest": latest % (top, "WHERE customer_id = 2"), "final": "SELECT * FROM {{latest}}"}, ["final"]))
    cases.append(Case(f"latest_per_key/value_filter/{depth}", "latest_per_key", "different",
                      {**chain, "latest": latest % (top, ""), "final": "SELECT * FROM {{latest}} WHERE amount > 0"},
                      {"latest": latest % (top, "WHERE amount > 0"), "final": "SELECT * FROM {{latest}}"}, ["final"],
                      "filtering before the window changes which row is the latest"))
    cases.append(Case(f"latest_per_key/exposed_latest/{depth}", "latest_per_key", "different",
                      {**chain, "latest": latest % (top, ""), "final": "SELECT * FROM {{latest}} WHERE customer_id = 2"},
                      {"latest": latest % (top, "WHERE customer_id = 2"), "final": "SELECT * FROM {{latest}}"}, ["final", "latest"],
                      "the exposed latest model now holds one customer"))
    return cases


FAMILIES = {
    "filter_upstream": (filter_upstream, [1, 2, 4, 8, 16, 30]),
    "extract_shared": (extract_shared, [2, 3, 5, 8]),
    "rollup": (rollup, [1, 3, 6]),
    "join_staging": (join_staging, [1, 3, 6]),
    "rename_prune": (rename_prune, [1, 4, 12]),
    "union_split": (union_split, [1, 4, 10]),
    "latest_per_key": (latest_per_key, [1, 5]),
}


def all_cases(held_out: bool = False) -> list[Case]:
    cases = []
    for name, (make, sizes) in FAMILIES.items():
        if (name in HELD_OUT_FAMILIES) == held_out:
            for size in sizes:
                cases.extend(make(size))
    return cases


# ------------------------------------------------------------ project builder

REF = re.compile(r"\{\{([a-z_0-9.]+)\}\}")


def _render(sql: str, rename: dict[str, str]) -> str:
    def ref(match: re.Match) -> str:
        name = match.group(1)
        if name.startswith("raw."):
            return '${ref("raw", "%s")}' % name[4:]
        return '${ref("%s")}' % rename.get(name, name)

    return REF.sub(ref, sql)


def _reads(sql: str) -> set[str]:
    return {m for m in REF.findall(sql) if not m.startswith("raw.")}


def world(case: Case) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """``(models, rename of after-world, sql of each model by project name)`` for the combined project."""

    changed = set(case.after)
    deps = {name: _reads(sql) for name, sql in {**case.before, **case.after}.items()}
    affected = set(changed)
    grew = True
    while grew:
        grew = False
        for name, reads in deps.items():
            if name not in affected and reads & affected:
                affected.add(name)
                grew = True
    rename = {name: f"{name}__after" for name in affected}
    models: dict[str, str] = {}
    for name, sql in case.before.items():
        models[name] = _render(sql, {})
    for name in affected:
        sql = case.after.get(name, case.before.get(name))
        models[f"{name}__after"] = _render(sql, rename)
    return models, rename, {**case.before, **{n: case.after.get(n, case.before.get(n)) for n in affected}}


def write_project(case: Case, root: Path) -> dict[str, str]:
    models, rename, _ = world(case)
    (root / "definitions").mkdir(parents=True, exist_ok=True)
    (root / "workflow_settings.yaml").write_text(f"defaultProject: {PROJECT}\ndefaultDataset: {DATASET}\n", encoding="utf-8")
    for source in SOURCES:
        (root / "definitions" / f"src_{source}.sqlx").write_text(
            f'config {{ type: "declaration", schema: "raw", name: "{source}" }}\n', encoding="utf-8")
    for name, sql in models.items():
        (root / "definitions" / f"{name}.sqlx").write_text(f'config {{ type: "table" }}\n{sql}\n', encoding="utf-8")
    return rename


# ------------------------------------------------------------------ execution


def random_database(rng: random.Random) -> dict[str, list[tuple]]:
    pools = {"status": STATUSES, "region": REGIONS, "tier": TIERS, "kind": KINDS}
    db = {}
    for table, columns in SOURCES.items():
        rows = []
        for _ in range(rng.choice([0, 1, 2, 3, 4, 5, 6, 8])):
            row = []
            for column in columns:
                if column in pools:
                    row.append(rng.choice(pools[column]))
                elif column in ("id", "customer_id", "order_id"):
                    row.append(rng.choice([None, 1, 2, 2, 3, 4]) if rng.random() < 0.15 else rng.randint(1, 4))
                else:
                    row.append(None if rng.random() < 0.15 else rng.randint(-2, 9))
            rows.append(tuple(row))
        if rows and rng.random() < 0.3:
            rows.append(rng.choice(rows))  # duplicates
        db[table] = rows
    return db


def _flat(tree: exp.Expression) -> exp.Expression:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in list(tree.find_all(exp.Table)):
        parts = [p.name.lower() for p in table.parts]
        if len(parts) == 1 and parts[0] in ctes:
            continue
        flat = "__".join(parts[-2:] if len(parts) >= 2 else parts)
        table.set("this", exp.to_identifier(flat, quoted=True))
        table.set("db", None)
        table.set("catalog", None)
    return tree


class Executor:
    """A pipeline materialised in DuckDB, one database after another on one connection.

    Each model is translated to DuckDB once, when first reached, and the source tables are created once; each
    database drops the last one's model tables, swaps the source rows and creates every model table again in
    dependency order, as a fresh connection would.
    """

    def __init__(self, pipeline):
        import duckdb

        from kumosql.bigquery_on_duckdb import configure

        self.pipeline = pipeline
        self.order = list(pipeline.topological_order())
        self.sql: dict[str, str] = {}
        self.created: list[str] = []
        self.con = duckdb.connect(":memory:")
        configure(self.con)  # the models are BigQuery: run them as BigQuery does, or fail where it fails
        types = {"status": "VARCHAR", "region": "VARCHAR", "tier": "VARCHAR", "kind": "VARCHAR"}
        for table, columns in SOURCES.items():
            self.con.execute(f'CREATE TABLE "raw__{table}" (' + ", ".join(f"{c} {types.get(c, 'BIGINT')}" for c in columns) + ")")

    def _model_sql(self, key: str) -> str:
        from kumosql.bigquery_on_duckdb import faithful

        if key not in self.sql:  # a model that cannot be translated raises here on every database that reaches it
            tree = _flat(sqlglot.parse_one(self.pipeline.models[key].sql, read="bigquery"))
            self.sql[key] = faithful(tree).sql(dialect="duckdb")
        return self.sql[key]

    def execute(self, db: dict[str, list[tuple]], outputs: list[str]) -> dict[str, Counter]:
        """Materialise every model on ``db`` and return each requested model as a bag."""

        from kumosql.bigquery_on_duckdb import bigquery_rows
        from kumosql.duckdb_load import insert_rows

        while self.created:  # the last database's model tables, so each CREATE TABLE starts as on a fresh connection
            self.con.execute(f'DROP TABLE "{self.created.pop()}"')
        for table in SOURCES:
            self.con.execute(f'DELETE FROM "raw__{table}"')
            insert_rows(self.con, f'"raw__{table}"', db[table])
        for key in self.order:
            name = "an__" + key.split(".")[-1]
            self.con.execute(f'CREATE TABLE "{name}" AS ' + self._model_sql(key))
            self.created.append(name)
        return {out: Counter(bigquery_rows(self.con.execute(f'SELECT * FROM "an__{out}"').fetchall())) for out in outputs}

    def close(self) -> None:
        self.con.close()


def execute(pipeline, db: dict[str, list[tuple]], outputs: list[str]) -> dict[str, Counter]:
    """Materialise every model in DuckDB on ``db`` and return each requested model as a bag."""

    executor = Executor(pipeline)
    try:
        return executor.execute(db, outputs)
    finally:
        executor.close()


def executed_check(pipeline, case: Case, rename: dict[str, str], trials: int, seed: int = 7, databases=None):
    """``(agree, example)``: whether every output matches on every database, else the first differing database."""

    rng = random.Random(seed)
    dbs = databases if databases is not None else [random_database(rng) for _ in range(trials)]
    from kumosql.bigquery_on_duckdb import is_bigquery_failure

    executor = None
    try:
        for db in dbs:
            names = [*case.outputs, *(rename[o] for o in case.outputs if o in rename)]
            try:
                executor = executor or Executor(pipeline)
                bags = executor.execute(db, names)
            except Exception as error:
                if is_bigquery_failure(error):
                    continue  # BigQuery fails on this database: it tells the pipelines apart nowhere
                raise
            for out in case.outputs:
                if bags[out] != bags[rename.get(out, out)]:
                    return False, {"database": db, "output": out}
        return True, None
    finally:
        if executor is not None:
            executor.close()


# --------------------------------------------------------------------- scoring


def source_schema():
    return {f"{PROJECT}.raw.{table}": {column: "STRING" for column in columns} for table, columns in SOURCES.items()}


def _prove_output(pipeline, out: str, rename: dict[str, str], timeout_ms: int, schema=None):
    from kumosql.pipeline_equivalence import prove_models

    if out not in rename:
        return "same", None
    started = time.time()
    result = prove_models(pipeline, out, rename[out], declared=[], schema=schema, timeout_ms=timeout_ms)
    seconds = time.time() - started
    if result.proven:
        return "proof", result
    reason = result.reason.lower()
    return ("timeout" if "timeout" in reason or "timed out" in reason else "unknown"), result


def _bounded_counterexample(pipeline, out: str, rename: dict[str, str], timeout_ms: int, schema=None):
    """The prover's counterexample for the two outputs inlined, if it finds one (a bounded refutation)."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.pipeline_equivalence import _inlined
    from kumosql.smt_equivalence import SmtStatus

    if out not in rename:
        return None
    flat = _inlined(pipeline, f"{PROJECT}.{DATASET}.{out}", [], None), _inlined(pipeline, f"{PROJECT}.{DATASET}.{rename[out]}", [], None)
    if not (flat[0] and flat[1]):
        return None
    result = prove_equivalent_algebraic(
        flat[0][0], flat[1][0], schema=(schema.columns or None) if schema else None, timeout_ms=timeout_ms)
    if result.status is SmtStatus.NOT_EQUIVALENT and result.counterexample is not None:
        return result.counterexample
    return None


def replay_counterexample(pipeline, case, rename, example) -> bool:
    """Whether the prover's counterexample really separates some output when run through the pipelines."""

    db = {table: [] for table in SOURCES}
    for name, rows in example.tables.items():
        table = name.replace("`", "").split(".")[-1].lower()
        if table in SOURCES:
            db[table] = [tuple(row.get(column) for column in SOURCES[table]) for row in rows]
    try:
        agree, _ = executed_check(pipeline, case, rename, 0, databases=[db])
    except Exception:
        return False
    return not agree


@dataclass
class CaseResult:
    id: str
    family: str
    label: str
    verdict: str  # equivalent, different, unknown, timeout, error, unsupported
    outputs: int = 0
    proven_outputs: int = 0
    changed_outputs: int = 0  # outputs whose model or upstream changed
    verified_changed_outputs: int = 0
    refuted_by: str = ""  # "bounded", "executed" or ""
    executed_agree: bool | None = None
    ground_truth_ok: bool = True
    seconds: float = 0.0
    detail: str = ""
    models: int = 0

    @property
    def wrong(self) -> bool:
        return (self.label == "different" and self.verdict == "equivalent") or (
            self.label == "equivalent" and self.verdict == "different")


def run_case(case: Case, trials: int = 40, timeout_ms: int = 5000, declared: bool = True) -> CaseResult:
    from kumosql import load_sqlx_project

    import contextlib
    import io

    started = time.time()
    result = CaseResult(case.id, case.family, case.label, "error", models=case.size)
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        root = Path(tmp)
        try:
            rename = write_project(case, root)
            pipeline = load_sqlx_project(root)
            schema = None
            if declared:
                from kumosql.prover_schema import from_pipeline

                pipeline.source_schema.update(source_schema())
                schema = from_pipeline(pipeline)
            result.outputs = len(case.outputs)
            result.changed_outputs = sum(1 for out in case.outputs if out in rename)
            # executed agreement validates the generator's known answer and gives the executed level
            agree, example = executed_check(pipeline, case, rename, trials)
            result.executed_agree = agree
            result.ground_truth_ok = agree == (case.label == "equivalent")
            statuses = []
            proven = 0
            for out in case.outputs:
                status, _ = _prove_output(pipeline, out, rename, timeout_ms, schema)
                statuses.append(status)
                if status in ("proof", "same"):
                    proven += 1
            result.proven_outputs = proven
            result.verified_changed_outputs = sum(1 for out, s in zip(case.outputs, statuses) if out in rename and s == "proof")
            if proven == len(case.outputs):
                result.verdict = "equivalent"
            else:
                counter = None
                for out, status in zip(case.outputs, statuses):
                    if status not in ("proof", "same"):
                        counter = _bounded_counterexample(pipeline, out, rename, timeout_ms, schema)
                        if counter is not None and replay_counterexample(pipeline, case, rename, counter):
                            result.refuted_by = "bounded"
                            break
                        counter = None
                if result.refuted_by:
                    result.verdict = "different"
                elif not agree:
                    result.verdict = "different"
                    result.refuted_by = "executed"
                elif "timeout" in statuses:
                    result.verdict = "timeout"
                else:
                    result.verdict = "unknown"
        except Exception as error:  # an error is its own outcome, never a pass
            result.verdict = "error"
            result.detail = f"{type(error).__name__}: {error}"[:300]
    result.seconds = time.time() - started
    return result


def run(held_out: bool = False, trials: int = 40, timeout_ms: int = 5000, only: str | None = None, declared: bool = True) -> dict:
    cases = [c for c in all_cases(held_out) if not only or only in c.id]
    results = [run_case(c, trials, timeout_ms, declared) for c in cases]
    return summarise(cases, results)


def summarise(cases: list[Case], results: list[CaseResult]) -> dict:
    by_id = {c.id: c for c in cases}
    verdicts = Counter(r.verdict for r in results)
    equiv = [r for r in results if r.label == "equivalent"]
    diff = [r for r in results if r.label == "different"]
    out = {
        "total": len(results),
        "equivalent_cases": len(equiv),
        "different_cases": len(diff),
        "proved": sum(1 for r in equiv if r.verdict == "equivalent"),
        "refuted": sum(1 for r in diff if r.verdict == "different"),
        "refuted_bounded": sum(1 for r in diff if r.verdict == "different" and r.refuted_by == "bounded"),
        "refuted_executed": sum(1 for r in diff if r.verdict == "different" and r.refuted_by == "executed"),
        "unknown": sum(1 for r in results if r.verdict == "unknown"),
        "timeout": verdicts["timeout"],
        "error": verdicts["error"],
        "unsupported": verdicts["unsupported"],
        "wrong": [r.id for r in results if r.wrong],
        "bad_ground_truth": [r.id for r in results if not r.ground_truth_ok and r.verdict != "error"],
        "changed_pipelines": sum(1 for c in cases if c.after),
        "changed_outputs": sum(r.changed_outputs for r in results),
        "verified_changed_outputs": sum(r.verified_changed_outputs for r in results),
        "executed_agree": sum(1 for r in equiv if r.executed_agree),
        "max_models": max((r.models for r in results), default=0),
        "seconds": round(sum(r.seconds for r in results), 1),
        "unproved": [r.id for r in equiv if r.verdict != "equivalent"],
        "unrefuted": [r.id for r in diff if r.verdict != "different"],
        "errors": {r.id: r.detail for r in results if r.verdict == "error"},
        "by_family": {},
    }
    families = sorted({r.family for r in results})
    for family in families:
        rs = [r for r in results if r.family == family]
        out["by_family"][family] = {
            "cases": len(rs),
            "proved": sum(1 for r in rs if r.label == "equivalent" and r.verdict == "equivalent"),
            "equivalent": sum(1 for r in rs if r.label == "equivalent"),
            "refuted": sum(1 for r in rs if r.label == "different" and r.verdict == "different"),
            "different": sum(1 for r in rs if r.label == "different"),
        }
    supported = [r for r in results if r.verdict not in ("error", "unsupported", "timeout")]
    out["supported"] = len(supported)
    out["supported_decided"] = sum(1 for r in supported if r.verdict in ("equivalent", "different") and not r.wrong)
    out["cases"] = [vars(r) | {"wrong": r.wrong} for r in results]
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--held-out", action="store_true", help="score the held-out families")
    parser.add_argument("--only", help="substring of a case id")
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--no-declared-columns", action="store_true", help="give the prover no source column lists")
    parser.add_argument("--json", metavar="FILE")
    args = parser.parse_args(argv)
    out = run(args.held_out, args.trials, args.timeout_ms, args.only, not args.no_declared_columns)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{'held-out' if args.held_out else 'development'}: {out['total']} cases "
          f"({out['equivalent_cases']} equivalent, {out['different_cases']} different), up to {out['max_models']} models")
    print(f"  proved {out['proved']}/{out['equivalent_cases']}, refuted {out['refuted']}/{out['different_cases']} "
          f"(bounded {out['refuted_bounded']}, executed {out['refuted_executed']}), unknown {out['unknown']}, "
          f"timeout {out['timeout']}, error {out['error']}, wrong {len(out['wrong'])}")
    print(f"  changed pipelines {out['changed_pipelines']}, changed outputs {out['changed_outputs']}, "
          f"verified changed outputs {out['verified_changed_outputs']}, {out['seconds']} s")
    for family, row in out["by_family"].items():
        print(f"  {family}: proved {row['proved']}/{row['equivalent']}, refuted {row['refuted']}/{row['different']}")
    if out["wrong"]:
        print("  WRONG:", out["wrong"])
    if out["bad_ground_truth"]:
        print("  BAD GROUND TRUTH:", out["bad_ground_truth"])
    if out["errors"]:
        print("  ERRORS:", {k: v[:120] for k, v in list(out["errors"].items())[:5]})
    return 1 if out["wrong"] or out["bad_ground_truth"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
