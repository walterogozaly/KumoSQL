"""Jaffle Shop eval: a real dbt project, built in DuckDB and loaded whole into KumoSQL.

The source is dbt Labs' ``jaffle_shop_duckdb`` (``duckdb`` branch, Apache-2.0), copied at a pinned commit
into ``tests/fixtures/jaffle_shop/`` with its licence: three seed CSVs, five models (three staging views,
two marts) written in dbt Jinja (``ref`` and a loop over payment methods), and the YAML tests that come
with them (``unique``, ``not_null``, ``accepted_values``, ``relationships``). There is no LLM at eval time.

Adaptations, recorded here and on the docs page (the upstream files are kept unchanged):

* dbt is not run. A small renderer below handles exactly the Jinja the project uses (``{# #}`` comments,
  ``{% set %}`` of a list of strings, ``{% for %}``, ``{{ ref('x') }}``, ``{{ name }}`` and the ``-`` whitespace
  controls); when ``jinja2`` is installed the rendered text is checked to be identical to Jinja's own.
* Seeds are loaded with the types dbt infers for them (integers, a date, strings). The dbt build is the
  rendered models created in dependency order as views (staging) and tables (marts), as
  ``dbt_project.yml`` configures them.
* For KumoSQL the rendered models are written as a Dataform project: ``{{ ref('x') }}`` becomes
  ``${ref("x")}`` and each seed becomes a declaration with its columns. The project is loaded with
  ``load_sqlx_project`` like any user project.

Tracks, scored separately:

1. **dbt build**: the 20 upstream YAML tests pass on the dbt build (expectations, checking the harness).
2. **graph**: KumoSQL's model graph equals dbt's ``ref`` graph (8 edges).
3. **lineage**: every output column (27) has the sources, transform and seed sources of a hand-written
   expectation (``expected_lineage.json``).
4. **loader round trip**: every model, rebuilt in DuckDB from the SQL KumoSQL loaded, returns the rows
   of the dbt build, on the seeds and on random databases.
5. **rewrites**: every registered rewrite rule, and the whole cleanup in canonical order, applied to each
   model; the pipeline is rebuilt with the rewritten model and every model's output compared with the
   original build on the seeds and on random databases. Any difference is wrong.
6. **output comparison**: ``plan_output_comparison`` (joined query, per-side snapshots, keyed
   drill-down) reports no difference for the unchanged pipeline built twice, and for the dbt build
   against KumoSQL's.
7. **authored refactors** (kept apart from the upstream cases): equivalent and breaking refactors of
   the project, written for this eval. ``prove_models`` must prove the equivalent ones or say unknown;
   a breaking one is refuted by the prover's counterexample (searched on databases built for the
   inlined queries) replayed through both pipelines, or by random-database execution. Every label is
   checked by execution; the output-comparison API must agree with the direct comparison on the seeds.

Held-out cases: a fifth of each track by SHA-1 of the case id; nothing in KumoSQL was tuned for this eval.

    python tools/jaffle_shop_bench.py [--trials 10] [--refactor-trials 30] [--json out.json] [--write-results]
"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import csv
from dataclasses import dataclass
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "jaffle_shop"
PROJECT, DATASET = "jaffle", "main"

# The types dbt infers for the seed CSVs, in BigQuery's names (KumoSQL reads BigQuery).
SEEDS = {
    "raw_customers": {"id": "INT64", "first_name": "STRING", "last_name": "STRING"},
    "raw_orders": {"id": "INT64", "user_id": "INT64", "order_date": "DATE", "status": "STRING"},
    "raw_payments": {"id": "INT64", "order_id": "INT64", "payment_method": "STRING", "amount": "INT64"},
}
DUCK_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR", "DATE": "DATE"}
FLOAT_DIGITS = 6  # floats are compared after rounding; SUM over FLOAT64 has no fixed order in BigQuery either


def held_out(case_id: str) -> bool:
    return int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 5 == 0


def key(name: str) -> str:
    return f"{PROJECT}.{DATASET}.{name}"


# ------------------------------------------------------------------ source pin


def source() -> dict:
    return json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))


def verify_pin() -> list[str]:
    """Files whose SHA-256 differs from the pin (empty when the copy is the pinned one)."""

    bad = []
    for path, digest in source()["files"].items():
        data = (FIXTURE / path).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            bad.append(path)
    return bad


# ------------------------------------------------------------------ dbt Jinja

_TAG = re.compile(r"(\{\{-?|\{%-?|\{#-?)(.*?)(-?\}\}|-?%\}|-?#\})", re.S)


def render(text: str, ref) -> str:
    """Render the Jinja subset this project uses; ``ref(name)`` gives the text that replaces ``{{ ref('name') }}``.

    Raises ``ValueError`` on anything outside the subset, so a new construct is never silently dropped.
    """

    parts: list[tuple] = []
    pos = 0
    for match in _TAG.finditer(text):
        parts.append(("text", text[pos:match.start()], ""))
        parts.append((match.group(1), match.group(2).strip(), match.group(3)))
        pos = match.end()
    parts.append(("text", text[pos:], ""))
    tokens = []
    for i, part in enumerate(parts):
        if part[0] != "text":
            tokens.append(part)
            continue
        body = part[1]
        if i > 0 and parts[i - 1][2].startswith("-"):
            body = body.lstrip()
        if i + 1 < len(parts) and parts[i + 1][0].endswith("-"):
            body = body.rstrip()
        tokens.append(("text", body, ""))

    def value(expression: str, env: dict):
        called = re.fullmatch(r"ref\(\s*'([A-Za-z0-9_]+)'\s*\)", expression)
        if called:
            return ref(called.group(1))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expression) and expression in env:
            return env[expression]
        raise ValueError(f"unsupported Jinja expression: {expression}")

    def run(tokens: list[tuple], env: dict) -> str:
        out: list[str] = []
        i = 0
        while i < len(tokens):
            kind, body, _ = tokens[i]
            if kind == "text":
                out.append(body)
            elif kind.startswith("{{"):
                out.append(str(value(body, env)))
            elif kind.startswith("{%"):
                assign = re.fullmatch(r"set\s+([A-Za-z_]\w*)\s*=\s*\[(.*)\]", body, re.S)
                loop = re.fullmatch(r"for\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_]\w*)", body)
                if assign:
                    items = [item.strip() for item in assign.group(2).split(",") if item.strip()]
                    if not all(re.fullmatch(r"'[^']*'", item) for item in items):
                        raise ValueError(f"unsupported Jinja list: {body}")
                    env[assign.group(1)] = [item[1:-1] for item in items]
                elif loop:
                    depth, end = 1, i + 1
                    while depth:
                        if end >= len(tokens):
                            raise ValueError("unclosed for loop")
                        if tokens[end][0].startswith("{%"):
                            if tokens[end][1].startswith("for "):
                                depth += 1
                            elif tokens[end][1] == "endfor":
                                depth -= 1
                        end += 1
                    for item in value(loop.group(2), env):
                        out.append(run(tokens[i + 1:end - 1], {**env, loop.group(1): item}))
                    i = end - 1
                else:
                    raise ValueError(f"unsupported Jinja statement: {body}")
            i += 1
        return "".join(out)

    rendered = run(tokens, {})
    # Jinja drops one trailing newline by default (keep_trailing_newline=False).
    return rendered[:-1] if rendered.endswith("\n") else rendered


def render_with_jinja2(text: str, ref) -> str | None:
    """Jinja's own rendering, as a check on :func:`render` (None when jinja2 is not installed)."""

    try:
        import jinja2
    except ImportError:
        return None
    return jinja2.Environment().from_string(text).render(ref=ref)


def refs(text: str) -> set[str]:
    """The models and seeds a dbt model names with ``ref`` (dbt's own dependency graph)."""

    found = set()
    for match in _TAG.finditer(text):
        if match.group(1).startswith("{{"):
            found.update(re.findall(r"ref\(\s*'([A-Za-z0-9_]+)'\s*\)", match.group(2)))
    return found


@dataclass
class DbtModel:
    name: str
    path: str
    text: str
    materialized: str


def dbt_models() -> dict[str, DbtModel]:
    """The upstream models, with the materialization ``dbt_project.yml`` gives each folder."""

    import yaml

    config = yaml.safe_load((FIXTURE / "dbt_project.yml").read_text(encoding="utf-8"))["models"]["jaffle_shop"]
    models = {}
    for path in sorted((FIXTURE / "models").rglob("*.sql")):
        relative = path.relative_to(FIXTURE / "models")
        materialized = config.get("+materialized", "view")
        node = config
        for folder in relative.parts[:-1]:
            node = node.get(folder, {})
            materialized = node.get("+materialized", materialized)
        models[path.stem] = DbtModel(path.stem, f"models/{relative.as_posix()}", path.read_text(encoding="utf-8"), materialized)
    return models


def dbt_order(texts: dict[str, str]) -> list[str]:
    """Models in dependency order (ref graph), names sorted within a level so the order is stable."""

    order: list[str] = []
    pending = dict(texts)
    while pending:
        ready = sorted(name for name, text in pending.items() if not (refs(text) & set(pending)))
        if not ready:
            raise ValueError("ref cycle")
        order.extend(ready)
        for name in ready:
            del pending[name]
    return order


# ------------------------------------------------------------------ dbt tests


@dataclass
class DbtTest:
    id: str
    model: str
    column: str
    kind: str
    values: tuple[str, ...] = ()
    to: str = ""
    field: str = ""


def dbt_tests() -> list[DbtTest]:
    """The generic tests of both ``schema.yml`` files."""

    import yaml

    tests = []
    for path in sorted((FIXTURE / "models").rglob("schema.yml")):
        for model in yaml.safe_load(path.read_text(encoding="utf-8"))["models"]:
            for column in model.get("columns", []):
                for test in column.get("tests", []) or []:
                    if isinstance(test, str):
                        tests.append(DbtTest(f"{test}/{model['name']}.{column['name']}", model["name"], column["name"], test))
                        continue
                    (kind, spec), = test.items()
                    args = spec.get("arguments", spec)
                    to = ""
                    if kind == "relationships":
                        to = re.fullmatch(r"ref\('([A-Za-z0-9_]+)'\)", args["to"]).group(1)
                    tests.append(DbtTest(f"{kind}/{model['name']}.{column['name']}", model["name"], column["name"], kind,
                                         tuple(args.get("values", ())), to, args.get("field", "")))
    return tests


def dbt_test_sql(test: DbtTest, relation) -> str:
    """The rows that fail ``test`` (dbt's generic test SQL); ``relation(name)`` places a model."""

    table, column = relation(test.model), test.column
    if test.kind == "not_null":
        return f"SELECT {column} FROM {table} WHERE {column} IS NULL"
    if test.kind == "unique":
        return f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL GROUP BY {column} HAVING COUNT(*) > 1"
    if test.kind == "accepted_values":
        values = ", ".join("'" + v.replace("'", "''") + "'" for v in test.values)
        return f"SELECT {column} FROM {table} GROUP BY {column} HAVING {column} NOT IN ({values})"
    if test.kind == "relationships":
        return (f"SELECT child.{column} FROM {table} AS child LEFT JOIN {relation(test.to)} AS parent "
                f"ON child.{column} = parent.{test.field} WHERE child.{column} IS NOT NULL AND parent.{test.field} IS NULL")
    raise ValueError(f"unknown dbt test {test.kind}")


# ------------------------------------------------------------------ databases


def seed_rows() -> dict[str, list[tuple]]:
    """The seed CSVs as typed rows."""

    convert = {"INT64": int, "STRING": str, "DATE": datetime.date.fromisoformat}
    data = {}
    for table, columns in SEEDS.items():
        with (FIXTURE / "seeds" / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == list(columns), (table, reader.fieldnames)
            data[table] = [tuple(None if row[c] == "" else convert[t](row[c]) for c, t in columns.items()) for row in reader]
    return data


STATUSES = ["placed", "shipped", "completed", "return_pending", "returned", None]
METHODS = ["credit_card", "coupon", "bank_transfer", "gift_card", "cash", None]
DATES = [datetime.date(2018, 1, 1), datetime.date(2018, 1, 2), datetime.date(2018, 2, 1), None]


def random_rows(rng: random.Random) -> dict[str, list[tuple]]:
    """A small database over the seed tables: NULLs, duplicate and dangling keys, empty tables."""

    def ident(top: int):
        return None if rng.random() < 0.12 else rng.randint(1, top)

    def count():
        return rng.choice([0, 1, 2, 3, 4, 5, 6, 8])

    data = {
        "raw_customers": [(ident(4), rng.choice(["Ann", "Bo", None]), rng.choice(["P.", None])) for _ in range(count())],
        "raw_orders": [(ident(5), ident(4), rng.choice(DATES), rng.choice(STATUSES)) for _ in range(count())],
        "raw_payments": [(ident(6), ident(5), rng.choice(METHODS), rng.choice([None, 0, 100, 250, 1000, -50]))
                         for _ in range(count())],
    }
    for rows in data.values():
        if rows and rng.random() < 0.3:
            rows.append(rng.choice(rows))
    return data


def edge_rows() -> dict[str, list[tuple]]:
    """A hand-written database for the NULL corners no seed row and few random rows reach, so no refactor's label rests on
    a timed search: an order whose payments of one method all have NULL amounts, a payment of a missing order, an order
    without a customer id and a repeated order id."""

    day = DATES[0]
    return {
        "raw_customers": [(1, "Ann", "P."), (2, "Bo", None), (None, None, None)],
        "raw_orders": [(1, 1, day, "placed"), (2, 1, day, "shipped"), (2, 1, day, "shipped"), (3, None, day, "returned"),
                       (None, 2, day, None)],
        "raw_payments": [(1, 1, "credit_card", 100), (2, 1, "credit_card", None), (3, 2, "coupon", None),
                         (4, 2, "coupon", None), (5, 9, "cash", 250), (6, None, "gift_card", -50), (7, 3, None, 0)],
    }


def databases(trials: int, seed: int = 11, edge: bool = False) -> list[tuple[str, dict[str, list[tuple]]]]:
    """The seeds, ``trials`` random databases and, with ``edge``, the hand-written corner-case database last."""

    rng = random.Random(seed)
    dbs = [("seeds", seed_rows())] + [(f"random-{i}", random_rows(rng)) for i in range(trials)]
    return dbs + [("edge", edge_rows())] if edge else dbs


def connect(data: dict[str, list[tuple]]):
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH ':memory:' AS {PROJECT}")
    con.execute(f"USE {PROJECT}")
    con.execute("CREATE MACRO farm_fp(x) AS CAST(hash(x) AS HUGEINT)")
    for table, columns in SEEDS.items():
        con.execute(f"CREATE TABLE {DATASET}.{table} (" + ", ".join(f"{c} {DUCK_TYPES[t]}" for c, t in columns.items()) + ")")
        if data[table]:
            con.executemany(f"INSERT INTO {DATASET}.{table} VALUES ({', '.join('?' * len(columns))})", data[table])
    return con


def _value(v):
    if isinstance(v, float):
        v = round(v, FLOAT_DIGITS)
        return 0.0 if v == 0 else v
    return v


def bag(rows) -> Counter:
    return Counter(tuple(_value(v) for v in row) for row in rows)


def build_dbt(con, models: dict[str, DbtModel], schema: str = DATASET) -> None:
    """The dbt build: rendered models in ref order, as views or tables."""

    if schema != DATASET:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    texts = {name: model.text for name, model in models.items()}
    for name in dbt_order(texts):
        model = models[name]
        sql = render(model.text, lambda ref: f"{DATASET if ref in SEEDS else schema}.{ref}")
        kind = "VIEW" if model.materialized == "view" else "TABLE"
        con.execute(f"CREATE {kind} {schema}.{name} AS {sql}")


def duckdb_sql(pipeline, sql: str, schema: str) -> str:
    """A model's BigQuery SQL as DuckDB SQL, reading the pipeline's models from ``schema``."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in list(tree.find_all(exp.Table)):
        if not table.args.get("db") and table.name.lower() in ctes:
            continue  # a WITH table of the query, not a model with the same name
        resolved = pipeline.resolve(table)
        if resolved is None:
            continue
        name = resolved.split(".")[-1]
        place = DATASET if resolved not in pipeline.models else schema
        table.set("catalog", exp.to_identifier(PROJECT))
        table.set("db", exp.to_identifier(place))
        table.set("this", exp.to_identifier(name))
    return tree.sql(dialect="duckdb")


def build_pipeline(con, pipeline, schema: str, override: dict[str, str] | None = None) -> None:
    """Materialise every model KumoSQL loaded into ``schema``; ``override`` replaces a model's SQL."""

    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for model_key in pipeline.topological_order():
        model = pipeline.models.get(model_key)
        if model is None or not model.is_query:
            continue
        sql = (override or {}).get(model_key, model.sql)
        con.execute(f"CREATE TABLE {schema}.{model.target.name} AS {duckdb_sql(pipeline, sql, schema)}")


def read_bags(con, schema: str, names) -> dict[str, Counter]:
    return {name: bag(con.execute(f"SELECT * FROM {schema}.{name}").fetchall()) for name in names}


def columns_of(con, schema: str, name: str) -> list[str]:
    return [row[0] for row in con.execute(f"DESCRIBE {schema}.{name}").fetchall()]


# ------------------------------------------------------------------ the KumoSQL project


def sqlx_models(models: dict[str, DbtModel], edits: dict[str, str] | None = None, rename: dict[str, str] | None = None):
    """``{file name: SQLX text}``: each model rendered with ``${ref("x")}`` (refs renamed by ``rename``)."""

    rename = rename or {}
    files = {}
    for table, columns in SEEDS.items():
        files[table] = f'config {{ type: "declaration", name: "{table}" }}\n'
    texts = {name: model.text for name, model in models.items()}
    texts.update(edits or {})
    for name, text in texts.items():
        kind = models[name].materialized if name in models else "table"
        body = render(text, lambda ref: '${ref("%s")}' % rename.get(ref, ref))
        files[rename.get(name, name)] = f'config {{ type: "{kind}" }}\n{body}\n'
    return files


def source_schema() -> dict[str, dict[str, str]]:
    return {key(table): dict(columns) for table, columns in SEEDS.items()}


def load(files: dict[str, str]):
    """Load SQLX files as a Dataform project, the way a user loads one."""

    from kumosql import load_sqlx_project

    with tempfile.TemporaryDirectory(prefix="jaffle-") as tmp:
        root = Path(tmp)
        (root / "definitions").mkdir()
        (root / "workflow_settings.yaml").write_text(f"defaultProject: {PROJECT}\ndefaultDataset: {DATASET}\n", encoding="utf-8")
        for name, text in files.items():
            (root / "definitions" / f"{name}.sqlx").write_text(text, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            return load_sqlx_project(root, source_schema=source_schema())


# ------------------------------------------------------------------ tracks


def track_build(models: dict[str, DbtModel]) -> dict:
    """Render every model, check against Jinja, build on the seeds and run the upstream tests."""

    out = {"rendered_same_as_jinja2": None, "tests": [], "passed": 0, "failed": []}
    checks = []
    for model in models.values():
        theirs = render_with_jinja2(model.text, lambda ref: ref)
        if theirs is not None:
            checks.append(render(model.text, lambda ref: ref) == theirs)
    out["rendered_same_as_jinja2"] = all(checks) if checks else None
    con = connect(seed_rows())
    build_dbt(con, models)
    for test in dbt_tests():
        failures = len(con.execute(dbt_test_sql(test, lambda name: f"{DATASET}.{name}")).fetchall())
        out["tests"].append({"id": test.id, "failures": failures, "held_out": held_out(test.id)})
        if failures:
            out["failed"].append(test.id)
        else:
            out["passed"] += 1
    out["total"] = len(out["tests"])
    out["rows"] = {name: con.execute(f"SELECT COUNT(*) FROM {DATASET}.{name}").fetchone()[0] for name in models}
    return out


def track_graph(models: dict[str, DbtModel], pipeline) -> dict:
    expected = {(key(name), key(ref)) for name, model in models.items() for ref in refs(model.text)}
    found = {(child, parent) for child, parents in pipeline.upstream.items() for parent in parents}
    order = pipeline.topological_order()
    return {
        "expected": len(expected),
        "found": len(expected & found),
        "missing": sorted(f"{c} <- {p}" for c, p in expected - found),
        "extra": sorted(f"{c} <- {p}" for c, p in found - expected),
        "order_respects_refs": all(order.index(p) < order.index(c) for c, p in expected if p in order),
        "complete": pipeline.completeness()["complete"],
    }


def track_lineage(pipeline, built_columns: dict[str, list[str]]) -> dict:
    from kumosql.pipeline_types import ColumnRef

    expected = json.loads((FIXTURE / "expected_lineage.json").read_text(encoding="utf-8"))["columns"]
    records = pipeline.explain_lineage()
    short = lambda ref: f"{ref.table.split('.')[-1]}.{ref.column}"  # noqa: E731
    found_columns = {short(ref) for ref in records}
    built = {f"{model}.{column}" for model, columns in built_columns.items() for column in columns}
    rows = []
    for column, want in expected.items():
        model, name = column.split(".")
        ref = ColumnRef(key(model), name)
        record = records.get(ref)
        got = {"status": None}
        if record is not None:
            trace = pipeline.trace_column(ref)
            got = {
                "status": record.status,
                "sources": sorted(short(s) for s in record.sources),
                "transform": record.transform,
                "seed_sources": sorted(short(s) for s in trace.sources),
            }
        ok = (got["status"] == "traced" and got["sources"] == sorted(want["sources"])
              and got["transform"] == want["transform"] and got["seed_sources"] == sorted(want["seed_sources"]))
        rows.append({"column": column, "ok": ok, "held_out": held_out(f"lineage/{column}"), "got": got})
    return {
        "columns": len(rows),
        "correct": sum(r["ok"] for r in rows),
        "wrong": [r["column"] for r in rows if not r["ok"]],
        "expected_matches_built_columns": set(expected) == built,
        "kumosql_columns_match_built_columns": found_columns == built,
        "held_out": [sum(r["ok"] for r in rows if r["held_out"]), sum(r["held_out"] for r in rows)],
        "rows": rows,
    }


def _differs_unoptimized(make_con, build_left, build_right, names) -> bool:
    """Rebuild both sides with DuckDB's optimizer off; True when an output still differs."""

    from kumosql.duckdb_load import run_unoptimized

    con = make_con()
    con.execute("PRAGMA disable_optimizer")
    try:
        build_left(con)
        build_right(con)
    finally:
        con.execute("PRAGMA enable_optimizer")
    for name in names:
        left, right = run_unoptimized(con, f"SELECT * FROM l.{name}", f"SELECT * FROM r.{name}")
        if bag(left) != bag(right):
            return True
    return False


def track_round_trip(models: dict[str, DbtModel], pipeline, dbs) -> dict:
    """Every model rebuilt from KumoSQL's SQL returns the dbt build's rows."""

    names = list(models)
    differing = []
    for label, data in dbs:
        con = connect(data)
        build_dbt(con, models, "l")
        build_pipeline(con, pipeline, "r")
        dbt_bags, kumo_bags = read_bags(con, "l", names), read_bags(con, "r", names)
        for name in names:
            if dbt_bags[name] != kumo_bags[name] and _differs_unoptimized(
                    lambda: connect(data), lambda c: build_dbt(c, models, "l"), lambda c: build_pipeline(c, pipeline, "r"), [name]):
                differing.append(f"{label}:{name}")
    return {"databases": len(dbs), "models": len(names), "differing": differing}


def transformations() -> list[str]:
    from kumosql.rewrite import available_rules

    return [*sorted(name for name, rule in available_rules().items() if not rule.opt_in), "pipeline"]


def apply_transformation(name: str, sql: str):
    from kumosql.rewrite import apply_rule, apply_rules, canonical_rule_order

    if name == "pipeline":
        return apply_rules(canonical_rule_order(), sql)
    return apply_rule(name, sql)


def _same_tree(a: str, b: str) -> bool:
    try:
        return sqlglot.parse_one(a, read="bigquery") == sqlglot.parse_one(b, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return False


def track_rewrites(pipeline, dbs) -> dict:
    """Each transformation on each model; the pipeline rebuilt and every output compared."""

    names = [pipeline.models[k].target.name for k in pipeline.topological_order() if k in pipeline.models]
    baselines = {}
    for label, data in dbs:
        con = connect(data)
        build_pipeline(con, pipeline, "l")
        baselines[label] = read_bags(con, "l", names)
    rows = []
    for model_key in pipeline.topological_order():
        if model_key not in pipeline.models:
            continue
        original = pipeline.models[model_key].sql
        for name in transformations():
            case_id = f"{model_key.split('.')[-1]}/{name}"
            row = {"id": case_id, "held_out": held_out(case_id), "status": "", "detail": ""}
            started = time.perf_counter()
            try:
                result = apply_transformation(name, original)
            except Exception as error:  # noqa: BLE001 - a crash is its own outcome
                row.update(status="error", detail=f"{type(error).__name__}: {str(error)[:120]}")
                rows.append(row)
                continue
            row["seconds"] = round(time.perf_counter() - started, 3)
            row["verification"] = result.verification.status.value
            if result.sql == original:
                row["status"] = "declined"
                rows.append(row)
                continue
            if _same_tree(result.sql, original):  # only the layout changed: the same query
                row.update(status="kept", changed="layout")
                rows.append(row)
                continue
            row["changed"] = "tree"
            row["status"] = "kept"
            override = {model_key: result.sql}
            for label, data in dbs:
                con = connect(data)
                try:
                    build_pipeline(con, pipeline, "r", override)
                except Exception as error:  # noqa: BLE001 - a rewrite that no longer runs is wrong
                    row.update(status="wrong", detail=f"{label}: no longer runs: {str(error)[:120]}")
                    break
                after = read_bags(con, "r", names)
                diff = [n for n in names if after[n] != baselines[label][n]]
                if diff and _differs_unoptimized(lambda: connect(data), lambda c: build_pipeline(c, pipeline, "l"),
                                                 lambda c: build_pipeline(c, pipeline, "r", override), diff):
                    row.update(status="wrong", detail=f"{label}: {', '.join(diff)} changed")
                    break
            if row["status"] == "wrong":
                row["sql"] = result.sql
            rows.append(row)
    statuses = Counter(r["status"] for r in rows)
    changed = [r for r in rows if r.get("changed") == "tree"]
    return {
        "cases": len(rows),
        "models": len(names),
        "transformations": transformations(),
        "kept": statuses["kept"],
        "declined": statuses["declined"],
        "wrong": [r["id"] for r in rows if r["status"] == "wrong"],
        "errors": {r["id"]: r["detail"] for r in rows if r["status"] == "error"},
        "changed_tree": len(changed),
        "changed_layout": sum(1 for r in rows if r.get("changed") == "layout"),
        "changed_proven": sum(1 for r in changed if r.get("verification") == "proven"),
        "verification": dict(Counter(r.get("verification", "none") for r in changed)),
        "databases": len(dbs),
        "held_out": [sum(1 for r in rows if r["held_out"] and r["status"] in ("kept", "declined")),
                     sum(r["held_out"] for r in rows)],
        "rows": rows,
    }


def _to_duckdb(sql: str) -> str:
    duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    return duck.replace("FARM_FINGERPRINT(", "farm_fp(").replace("AS BIGDECIMAL)", "AS DECIMAL(38, 5))")


def _rows(con, sql: str) -> list[dict]:
    cursor = con.execute(_to_duckdb(sql))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def float_normalize(con, schema: str, names) -> dict[str, str]:
    floats = {}
    for name in names:
        for column, kind, *_ in con.execute(f"DESCRIBE {schema}.{name}").fetchall():
            if kind in ("DOUBLE", "FLOAT"):
                floats[column] = f"ROUND({{col}}, {FLOAT_DIGITS})"
    return floats


KEYS = {"stg_customers": ["customer_id"], "stg_orders": ["order_id"], "stg_payments": ["payment_id"],
        "customers": ["customer_id"], "orders": ["order_id"]}


def compare_with_api(con, before, after, before_schema: str, after_schema: str, names) -> dict[str, dict]:
    """``plan_output_comparison`` run on DuckDB: per model, the joined verdict, the snapshot verdict, drill-down rows."""

    from kumosql import Location, compare_snapshots, plan_output_comparison, summarize_comparison

    plan = plan_output_comparison(
        before, after, before_location=Location(dataset=before_schema), after_location=Location(dataset=after_schema),
        models=[key(n) for n in names], normalize=float_normalize(con, before_schema, names))
    joined = {d.model: d for d in summarize_comparison(_rows(con, plan.compare_sql()))}
    snapshots = {d.model: d for d in compare_snapshots(_rows(con, plan.fingerprint_sql("before")),
                                                       _rows(con, plan.fingerprint_sql("after")))}
    out = {}
    for name in names:
        model_key = key(name)
        drill = _rows(con, plan.drilldown_sql(model_key, keys=KEYS.get(name, [])))
        out[name] = {
            "joined": bool(joined[model_key].matches) if model_key in joined else None,
            "snapshot": bool(snapshots[model_key].matches) if model_key in snapshots else None,
            "drilldown_rows": len(drill),
        }
    return out


def track_comparison(models: dict[str, DbtModel], pipeline) -> dict:
    """The output-comparison API on the unchanged pipeline: built twice, and dbt's build against KumoSQL's."""

    names = list(models)
    con = connect(seed_rows())
    build_pipeline(con, pipeline, "b")
    build_pipeline(con, pipeline, "a")
    build_dbt(con, models, "d")
    pairs = {"kumosql_twice": compare_with_api(con, pipeline, pipeline, "b", "a", names),
             "dbt_vs_kumosql": compare_with_api(con, pipeline, pipeline, "d", "a", names)}
    checks = agreed = 0
    disagreements = []
    for pair, verdicts in pairs.items():
        for name, verdict in verdicts.items():
            for method, ok in (("joined", verdict["joined"] is True), ("snapshot", verdict["snapshot"] is True),
                               ("drilldown", verdict["drilldown_rows"] == 0)):
                checks += 1
                agreed += ok
                if not ok:
                    disagreements.append(f"{pair}/{name}/{method}")
    return {"checks": checks, "agreed": agreed, "disagreements": disagreements, "verdicts": pairs}


# ------------------------------------------------------------------ authored refactors


def _replace(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"expected one occurrence of {old!r}")
    return text.replace(old, new)


def _replace_between(text: str, start: str, end: str, new: str) -> str:
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    return head + start + new + end + tail


@dataclass
class Refactor:
    id: str
    label: str  # "equivalent" or "different"
    note: str
    edits: dict[str, str]  # model -> full new Jinja text (a new name adds a model)

    @property
    def held_out(self) -> bool:
        return held_out(f"refactor/{self.id}")


def refactors(models: dict[str, DbtModel] | None = None) -> list[Refactor]:
    """Refactors of the project written for this eval (not upstream); every label is checked by execution."""

    models = models or dbt_models()
    customers, orders, stg_orders = models["customers"].text, models["orders"].text, models["stg_orders"].text
    pivot = "sum(case when payment_method = '{{ payment_method }}' then amount else 0 end)"
    order_payments_body = (
        "{% set payment_methods = ['credit_card', 'coupon', 'bank_transfer', 'gift_card'] %}\n\n"
        "select\n    order_id,\n\n    {% for payment_method in payment_methods -%}\n"
        "    " + pivot + " as {{ payment_method }}_amount,\n    {% endfor -%}\n\n"
        "    sum(amount) as total_amount\n\nfrom {{ ref('stg_payments') }}\n\ngroup by order_id\n")
    inlined = customers
    for old, new in (
        ("select * from {{ ref('stg_customers') }}", "select id as customer_id, first_name, last_name from {{ ref('raw_customers') }}"),
        ("select * from {{ ref('stg_orders') }}",
         "select id as order_id, user_id as customer_id, order_date, status from {{ ref('raw_orders') }}"),
        ("select * from {{ ref('stg_payments') }}",
         "select id as payment_id, order_id, payment_method, amount / 100 as amount from {{ ref('raw_payments') }}"),
    ):
        inlined = _replace(inlined, old, new)
    return [
        Refactor("inline_staging_into_customers", "equivalent",
                 "customers reads the seeds directly, with the staging logic inlined", {"customers": inlined}),
        Refactor("extract_order_payments", "equivalent",
                 "the payment pivot moves into its own model, which orders reads",
                 {"order_payments": order_payments_body,
                  "orders": _replace_between(orders, "order_payments as (\n", "\n),\n\nfinal as (",
                                             "\n    select * from {{ ref('order_payments') }}\n")}),
        Refactor("lifetime_value_from_orders_mart", "equivalent",
                 "customer lifetime value summed from the orders mart instead of joining payments to orders again",
                 {"customers": _replace_between(customers, "customer_payments as (\n", "\n),\n\nfinal as (",
                                                "\n    select\n        customer_id,\n        sum(amount) as total_amount\n\n"
                                                "    from {{ ref('orders') }}\n\n    group by customer_id\n")}),
        Refactor("simple_case_pivot", "equivalent", "the pivot written with a simple CASE",
                 {"orders": _replace(orders, "case when payment_method = '{{ payment_method }}' then",
                                     "case payment_method when '{{ payment_method }}' then")}),
        Refactor("customer_payments_inner_join", "equivalent",
                 "payments joined to orders with an inner join: the unmatched payments only formed a NULL customer group, "
                 "which the final join never matches",
                 {"customers": _replace(customers, "left join orders on", "join orders on")}),
        Refactor("right_join_order_payments", "equivalent", "orders' final LEFT JOIN written as a RIGHT JOIN with the sides swapped",
                 {"orders": _replace(orders, "    from orders\n\n\n    left join order_payments\n",
                                     "    from order_payments\n\n\n    right join orders\n")}),
        Refactor("customers_join_order_swapped", "equivalent", "the two LEFT JOINs of customers' final step in the other order",
                 {"customers": _replace(
                     customers,
                     "    left join customer_orders\n        on customers.customer_id = customer_orders.customer_id\n\n"
                     "    left join customer_payments\n        on  customers.customer_id = customer_payments.customer_id\n",
                     "    left join customer_payments\n        on  customers.customer_id = customer_payments.customer_id\n\n"
                     "    left join customer_orders\n        on customers.customer_id = customer_orders.customer_id\n")}),
        Refactor("customer_orders_from_orders_mart", "equivalent",
                 "first, last and number of orders counted from the orders mart, which has one row per staged order",
                 {"customers": _replace(customers, "    from orders\n\n    group by customer_id",
                                        "    from {{ ref('orders') }}\n\n    group by customer_id")}),
        Refactor("inner_join_order_payments", "different",
                 "orders without payments disappear (the seeds have none, so only other data shows it)",
                 {"orders": _replace(orders, "left join order_payments", "join order_payments")}),
        Refactor("pivot_coalesce", "different",
                 "COALESCE(SUM(CASE ... THEN amount END), 0): an order whose payments of a method all have NULL amounts "
                 "was NULL and becomes 0",
                 {"orders": _replace(orders, pivot,
                                     "coalesce(sum(case when payment_method = '{{ payment_method }}' then amount end), 0)")}),
        Refactor("count_star_orders", "different", "COUNT(*) counts orders whose id is NULL",
                 {"customers": _replace(customers, "count(order_id) as number_of_orders", "count(*) as number_of_orders")}),
        Refactor("drop_returned_orders", "different",
                 "returned orders filtered out of the exposed staging model, and so out of both marts",
                 {"stg_orders": _replace(stg_orders, "        status\n\n    from source\n",
                                         "        status\n\n    from source\n\n    where status <> 'returned'\n")}),
        Refactor("customers_inner_join_orders", "different", "customers with no orders disappear from customers",
                 {"customers": _replace(customers, "left join customer_orders", "join customer_orders")}),
        Refactor("count_distinct_orders", "different", "COUNT(DISTINCT order_id) counts a repeated order id once",
                 {"customers": _replace(customers, "count(order_id) as number_of_orders",
                                        "count(distinct order_id) as number_of_orders")}),
        Refactor("lifetime_value_coalesce", "different", "customers with no payments get 0 instead of NULL",
                 {"customers": _replace(customers, "customer_payments.total_amount as customer_lifetime_value",
                                        "coalesce(customer_payments.total_amount, 0) as customer_lifetime_value")}),
    ]


def world(models: dict[str, DbtModel], case: Refactor) -> tuple[dict[str, str], dict[str, str]]:
    """``(SQLX files, rename)``: the original project plus the changed models and everything downstream as ``<name>__after``."""

    texts = {name: model.text for name, model in models.items()}
    after_texts = {**texts, **case.edits}
    affected = set(case.edits)
    grew = True
    while grew:
        grew = False
        for name, text in after_texts.items():
            if name not in affected and refs(text) & affected:
                affected.add(name)
                grew = True
    rename = {name: f"{name}__after" for name in affected}
    files = sqlx_models(models)
    after_models = {**models, **{n: DbtModel(n, "", t, models[n].materialized if n in models else "table")
                                 for n, t in case.edits.items()}}
    changed = sqlx_models(after_models, {n: after_texts[n] for n in affected}, rename)
    files.update({rename[n]: changed[rename[n]] for n in affected})
    return files, rename


def prover_schema(pipeline):
    from kumosql.prover_schema import from_pipeline

    schema = from_pipeline(pipeline)
    for table_key in list(schema.columns):
        name = table_key.replace("`", "").split(".")[-1].lower()
        if name in SEEDS:
            schema.types[table_key] = dict(SEEDS[name])
    return schema


def _counterexample_rows(example) -> dict[str, list[tuple]] | None:
    data = {table: [] for table in SEEDS}
    for name, rows in example.tables.items():
        table = name.replace("`", "").split(".")[-1].lower()
        if table not in SEEDS:
            return None
        convert = {"INT64": lambda v: None if v is None else int(v), "STRING": lambda v: None if v is None else str(v),
                   "DATE": lambda v: v if v is None or isinstance(v, datetime.date) else datetime.date.fromisoformat(str(v))}
        data[table] = [tuple(convert[t](row.get(c)) for c, t in SEEDS[table].items()) for row in rows]
    return data


def _outputs_differ(combined, data, outputs, rename) -> list[str]:
    con = connect(data)
    build_pipeline(con, combined, "w")
    names = [*outputs, *(rename[o] for o in outputs if o in rename)]
    bags = read_bags(con, "w", names)
    differ = [o for o in outputs if o in rename and bags[o] != bags[rename[o]]]
    if not differ:
        return []

    from kumosql.duckdb_load import run_unoptimized

    con = connect(data)
    con.execute("PRAGMA disable_optimizer")
    try:
        build_pipeline(con, combined, "w")
    finally:
        con.execute("PRAGMA enable_optimizer")
    confirmed = []
    for out in differ:
        left, right = run_unoptimized(con, f"SELECT * FROM w.{out}", f"SELECT * FROM w.{rename[out]}")
        if bag(left) != bag(right):
            confirmed.append(out)
    return confirmed


def run_refactor(models: dict[str, DbtModel], base, case: Refactor, dbs, timeout_ms: int = 5000) -> dict:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.pipeline_equivalence import _inlined, prove_models
    from kumosql.smt_equivalence import SmtStatus

    started = time.perf_counter()
    row = {"id": case.id, "label": case.label, "note": case.note, "held_out": case.held_out, "verdict": "error",
           "refuted_by": "", "detail": ""}
    try:
        files, rename = world(models, case)
        combined = load(files)
        schema = prover_schema(combined)
        outputs = list(models)
        # ground truth by execution (seeds first)
        first_difference = None
        seed_differs = None
        for label, data in dbs:
            differ = _outputs_differ(combined, data, outputs, rename)
            if label == "seeds":
                seed_differs = bool(differ)
            if differ and first_difference is None:
                first_difference = (label, differ)
        row["executed_agree"] = first_difference is None
        row["seed_data_shows_difference"] = seed_differs
        # proof
        statuses = {}
        for out in outputs:
            if out not in rename:
                statuses[out] = "same"
                continue
            result = prove_models(combined, key(out), key(rename[out]), declared=[], schema=schema, timeout_ms=timeout_ms)
            statuses[out] = "proof" if result.proven else ("timeout" if "time" in result.reason.lower() else "unknown")
            if not result.proven:
                row.setdefault("reasons", {})[out] = result.reason[:200]
        row["outputs"] = statuses
        if all(s in ("same", "proof") for s in statuses.values()):
            row["verdict"] = "equivalent"
        else:
            for out, status in statuses.items():
                if status in ("same", "proof"):
                    continue
                flat = _inlined(combined, key(out), [], None), _inlined(combined, key(rename[out]), [], None)
                if not (flat[0] and flat[1]):
                    continue
                result = prove_equivalent_algebraic(flat[0][0], flat[1][0], schema=schema.columns or None,
                                                    types=schema.types or None, timeout_ms=timeout_ms,
                                                    search_counterexample=True)
                if result.status is SmtStatus.NOT_EQUIVALENT and result.counterexample is not None:
                    data = _counterexample_rows(result.counterexample)
                    if data is not None and _outputs_differ(combined, data, outputs, rename):
                        row["refuted_by"] = "prover"
                        row["counterexample"] = {t: [list(map(str, r)) for r in rows] for t, rows in data.items()}
                        break
            if row["refuted_by"]:
                row["verdict"] = "different"
            elif first_difference is not None:
                row["verdict"] = "different"
                row["refuted_by"] = "executed"
                row["detail"] = f"{first_difference[0]}: {', '.join(first_difference[1])} differ"
            else:
                row["verdict"] = "timeout" if "timeout" in statuses.values() else "unknown"
        # The label is checked by execution: a difference on a database tried, or the replayed counterexample.
        shown_different = first_difference is not None or row["refuted_by"] == "prover"
        row["ground_truth_ok"] = shown_different == (case.label == "different")
        # the output-comparison API on the seeds, against the direct comparison
        after = load(sqlx_models({**models, **{n: DbtModel(n, "", t, models[n].materialized if n in models else "table")
                                              for n, t in case.edits.items()}}))
        con = connect(seed_rows())
        build_pipeline(con, base, "b")
        build_pipeline(con, after, "a")
        direct = {n: read_bags(con, "b", [n])[n] == read_bags(con, "a", [n])[n] for n in outputs}
        api = compare_with_api(con, base, after, "b", "a", outputs)
        row["comparison_agrees"] = all(api[n]["joined"] == direct[n] and api[n]["snapshot"] == direct[n]
                                       and (api[n]["drilldown_rows"] == 0) == direct[n] for n in outputs)
        row["comparison"] = {n: {**api[n], "direct": direct[n]} for n in outputs}
    except Exception as error:  # noqa: BLE001 - an error is its own outcome, never a pass
        row["verdict"] = "error"
        row["detail"] = f"{type(error).__name__}: {str(error)[:200]}"
    row["wrong"] = (case.label == "equivalent" and row["verdict"] == "different") or (
        case.label == "different" and row["verdict"] == "equivalent")
    row["seconds"] = round(time.perf_counter() - started, 2)
    return row


def track_refactors(models: dict[str, DbtModel], base, dbs, only: str | None = None) -> dict:
    cases = [c for c in refactors(models) if not only or only in c.id]
    rows = [run_refactor(models, base, case, dbs) for case in cases]
    equivalent = [r for r in rows if r["label"] == "equivalent"]
    different = [r for r in rows if r["label"] == "different"]
    return {
        "cases": len(rows),
        "equivalent_cases": len(equivalent),
        "different_cases": len(different),
        "proved": sum(r["verdict"] == "equivalent" for r in equivalent),
        "refuted": sum(r["verdict"] == "different" for r in different),
        "refuted_by_prover": sum(r["verdict"] == "different" and r["refuted_by"] == "prover" for r in different),
        "refuted_by_execution": sum(r["verdict"] == "different" and r["refuted_by"] == "executed" for r in different),
        "unknown": sum(r["verdict"] in ("unknown", "timeout") for r in rows),
        "error": sum(r["verdict"] == "error" for r in rows),
        "wrong": [r["id"] for r in rows if r["wrong"]],
        "bad_ground_truth": [r["id"] for r in rows if not r.get("ground_truth_ok", False)],
        "comparison_disagrees": [r["id"] for r in rows if not r.get("comparison_agrees", False)],
        "seed_data_misses": [r["id"] for r in different if r.get("seed_data_shows_difference") is False],
        "held_out": {
            "proved": [sum(r["verdict"] == "equivalent" for r in equivalent if r["held_out"]), sum(r["held_out"] for r in equivalent)],
            "refuted": [sum(r["verdict"] == "different" for r in different if r["held_out"]), sum(r["held_out"] for r in different)],
        },
        "databases": len(dbs),
        "rows": rows,
    }


# ------------------------------------------------------------------ running


def run(trials: int = 10, refactor_trials: int = 30, tracks: tuple[str, ...] = ("all",), only: str | None = None) -> dict:
    os.environ.setdefault("KUMOSQL_SCHEMA_FETCH", "0")
    started = time.perf_counter()
    models = dbt_models()
    pipeline = load(sqlx_models(models))
    want = lambda name: "all" in tracks or name in tracks  # noqa: E731
    out: dict = {"source": {k: v for k, v in source().items() if k != "files"}, "pin_mismatches": verify_pin()}
    if want("build"):
        out["build"] = track_build(models)
    if want("graph"):
        out["graph"] = track_graph(models, pipeline)
    if want("lineage"):
        con = connect(seed_rows())
        build_dbt(con, models)
        out["lineage"] = track_lineage(pipeline, {name: columns_of(con, DATASET, name) for name in models})
    dbs = databases(trials)
    if want("round_trip"):
        out["round_trip"] = track_round_trip(models, pipeline, dbs)
    if want("rewrites"):
        out["rewrites"] = track_rewrites(pipeline, dbs)
    if want("comparison"):
        out["comparison"] = track_comparison(models, pipeline)
    if want("refactors"):
        out["refactors"] = track_refactors(models, pipeline, databases(refactor_trials, seed=23, edge=True), only)
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


def wrong_count(out: dict) -> int:
    return (len(out.get("rewrites", {}).get("wrong", [])) + len(out.get("refactors", {}).get("wrong", []))
            + len(out.get("round_trip", {}).get("differing", [])))


def report(out: dict) -> None:
    print(f"source {out['source']['repo']} @ {out['source']['commit'][:12]}; pin mismatches: {out['pin_mismatches'] or 'none'}")
    if "build" in out:
        b = out["build"]
        print(f"dbt build: {b['passed']}/{b['total']} upstream tests pass; renderer same as jinja2: {b['rendered_same_as_jinja2']}; rows {b['rows']}")
    if "graph" in out:
        g = out["graph"]
        print(f"graph: {g['found']}/{g['expected']} ref edges, extra {g['extra'] or 'none'}, missing {g['missing'] or 'none'}")
    if "lineage" in out:
        lin = out["lineage"]
        print(f"lineage: {lin['correct']}/{lin['columns']} columns (held out {lin['held_out'][0]}/{lin['held_out'][1]}), "
              f"wrong {lin['wrong'] or 'none'}")
    if "round_trip" in out:
        r = out["round_trip"]
        print(f"loader round trip: {r['models']} models on {r['databases']} databases, differing {r['differing'] or 'none'}")
    if "rewrites" in out:
        r = out["rewrites"]
        print(f"rewrites: {r['cases']} cases ({r['models']} models x {len(r['transformations'])} transformations): "
              f"kept {r['kept']} (tree changed {r['changed_tree']}, {r['changed_proven']} proven; layout only {r['changed_layout']}), "
              f"declined {r['declined']}, errors {len(r['errors'])}, wrong {len(r['wrong'])}; held out {r['held_out'][0]}/{r['held_out'][1]}")
        for case in r["wrong"]:
            print(f"  WRONG {case}")
    if "comparison" in out:
        c = out["comparison"]
        print(f"output comparison: {c['agreed']}/{c['checks']} checks agree, disagreements {c['disagreements'] or 'none'}")
    if "refactors" in out:
        r = out["refactors"]
        print(f"authored refactors: proved {r['proved']}/{r['equivalent_cases']}, refuted {r['refuted']}/{r['different_cases']} "
              f"(prover counterexample {r['refuted_by_prover']}, execution {r['refuted_by_execution']}), unknown {r['unknown']}, "
              f"error {r['error']}, wrong {len(r['wrong'])}; bad labels {r['bad_ground_truth'] or 'none'}; "
              f"comparison API disagrees {r['comparison_disagrees'] or 'none'}; seed data misses {r['seed_data_misses']}")
        for row in r["rows"]:
            print(f"  {row['id']}: {row['label']} -> {row['verdict']} {row['refuted_by']} {row.get('reasons', '') or ''} {row['detail']}")
    print(f"{out['seconds']} s")


def write_results(out: dict, command: str) -> None:
    from bench_common import today, write_results as write

    b, g, lin, rt, rw, cmp_, rf = (out[k] for k in ("build", "graph", "lineage", "round_trip", "rewrites", "comparison", "refactors"))
    pin = f"dbt-labs/jaffle_shop_duckdb@{out['source']['commit'][:12]} (duckdb branch, Apache-2.0)"
    write("jaffle-shop", {
        "suite": "Jaffle Shop dbt project (rewrites, lineage, graph)",
        "order": 115,
        "size": rw["cases"],
        "score": (f"{rw['kept'] + rw['declined']}/{rw['cases']} model rewrites keep every output, {len(rw['wrong'])} wrong; "
                  f"graph {g['found']}/{g['expected']} edges; lineage {lin['correct']}/{lin['columns']} columns; "
                  f"{b['passed']}/{b['total']} dbt tests"),
        "metric": ("A real dbt project (3 seeds, 5 models in Jinja, 20 YAML tests) rendered, built in DuckDB and loaded into "
                   "KumoSQL as a Dataform project. Every rewrite rule and the whole cleanup are applied to each model and the "
                   "pipeline rebuilt; a rewrite counts as kept when every model's output is unchanged on the seeds and on "
                   f"{rw['databases'] - 1} random databases."),
        "evidence": "executed",
        "correctness": (f"{len(rw['wrong'])} behaviour-changing rewrites ({rw['changed_tree']} rewrites changed the SQL tree, "
                        f"{rw['changed_proven']} of them proven by the rule's own check); the loader's SQL rebuilt in DuckDB "
                        f"returns the dbt build's rows for all {rt['models']} models on {rt['databases']} databases "
                        f"({len(rt['differing'])} differences); the output-comparison API agrees on {cmp_['agreed']}/{cmp_['checks']} "
                        "checks of the unchanged pipeline."),
        "coverage": {"proven": rw["kept"] + rw["declined"], "error": len(rw["errors"])},
        "analysis": (f"Graph: {g['found']}/{g['expected']} dbt ref edges, {len(g['extra'])} extra. Lineage: {lin['correct']}/"
                     f"{lin['columns']} output columns match the hand-written sources, transform and seed sources. Rewrites: "
                     f"{rw['changed_tree']} changed the tree, {rw['changed_layout']} layout only, {rw['declined']} declined."),
        "held_out": (f"rewrites {rw['held_out'][0]}/{rw['held_out'][1]}, lineage {lin['held_out'][0]}/{lin['held_out'][1]} "
                     "(a fifth by SHA-1 of the case id; nothing was tuned on this project)"),
        "docs": "docs/evals/pipeline-equivalence.md#jaffle-shop-a-real-dbt-project",
        "command": command,
        "date": today(),
        "caveats": (f"Source {pin}. Adapted: the Jinja is rendered by a small renderer checked against jinja2, and the models "
                    "are written as SQLX with ${ref()} for the loader; dbt itself is not run. The lineage expectation is "
                    "written for this eval. One small project: 5 models, 27 output columns. Floats compared to 6 decimals."),
    }, scoreboard=False)
    held = rf["held_out"]
    write("jaffle-shop-refactors", {
        "suite": "Jaffle Shop authored refactors (proof)",
        "order": 116,
        "size": rf["equivalent_cases"],
        "score": f"{rf['proved']}/{rf['equivalent_cases']} proved, {len([w for w in rf['wrong'] if w])} wrong",
        "metric": ("Equivalent refactors of the Jaffle Shop project written for this eval (staging inlined, a model extracted, "
                   "a measure taken from another mart, CASE forms, an outer join that changes nothing) whose every model "
                   "output is proved equal by prove_models."),
        "evidence": "proof",
        "correctness": (f"0 false proofs: none of the {rf['different_cases']} breaking refactors is proved. Every label is "
                        f"checked by running both pipelines on the seeds, {rf['databases'] - 2} random databases and a hand-written "
                        "edge database."),
        "coverage": {"proven": rf["proved"], "unknown": rf["equivalent_cases"] - rf["proved"]},
        "held_out": (f"{held['proved'][0]}/{held['proved'][1]} proved (a fifth by SHA-1 of the case id)" if held["proved"][1]
                     else "none: no equivalent refactor falls in the held-out fifth (by SHA-1 of the case id)"),
        "docs": "docs/evals/pipeline-equivalence.md#jaffle-shop-a-real-dbt-project",
        "command": command,
        "date": today(),
        "caveats": f"Authored cases on an upstream project ({pin}), not upstream cases. Only {rf['equivalent_cases']} cases.",
    }, scoreboard=False)
    write("jaffle-shop-refutation", {
        "suite": "Jaffle Shop authored refactors (refutation)",
        "order": 117,
        "size": rf["different_cases"],
        "score": f"{rf['refuted']}/{rf['different_cases']} refuted, 0 wrong" if not rf["wrong"] else
                 f"{rf['refuted']}/{rf['different_cases']} refuted, {len(rf['wrong'])} wrong",
        "metric": ("Breaking refactors of the Jaffle Shop project written for this eval (a join type, a COALESCE around a "
                   "pivot, COUNT(*), a filter in an exposed staging model) shown different by a database on which an "
                   "output differs."),
        "evidence": "executed",
        "correctness": (f"0 wrong: no equivalent refactor is called different. {rf['refuted_by_prover']} refutations come from "
                        f"the prover's counterexample and {rf['refuted_by_execution']} from random databases, each replayed "
                        f"through both pipelines in DuckDB. {len(rf['seed_data_misses'])} of the breaks do not show on the "
                        "upstream seed data."),
        "coverage": {"refuted": rf["refuted"], "unknown": rf["different_cases"] - rf["refuted"]},
        "held_out": f"{held['refuted'][0]}/{held['refuted'][1]} refuted (a fifth by SHA-1 of the case id)",
        "docs": "docs/evals/pipeline-equivalence.md#jaffle-shop-a-real-dbt-project",
        "command": command,
        "date": today(),
        "caveats": f"Authored cases on an upstream project ({pin}). Bounded by the databases tried.",
    }, scoreboard=True)


def main(argv: list[str] | None = None) -> int:
    from bench_common import quiet

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=10, help="random databases for the round trip and the rewrites")
    parser.add_argument("--refactor-trials", type=int, default=30, help="random databases for the authored refactors")
    parser.add_argument("--track", action="append", help="build, graph, lineage, round_trip, rewrites, comparison, refactors")
    parser.add_argument("--only", help="substring of a refactor id")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args(argv)
    quiet()
    out = run(args.trials, args.refactor_trials, tuple(args.track or ["all"]), args.only)
    report(out)
    if args.json:
        args.json.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    if args.write_results:
        write_results(out, "python tools/jaffle_shop_bench.py --write-results")
    return 1 if wrong_count(out) or out["pin_mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
