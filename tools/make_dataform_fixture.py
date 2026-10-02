"""Generate a realistic synthetic Dataform repository for testing KumoSQL as a real project would be.

    python tools/make_dataform_fixture.py OUT_DIR [--models 3000] [--seed 11] [--config yaml|json|both]

It imitates what large enterprise Dataform repositories look like, including the awkward parts that
have broken loading on locked-down machines: thousands of ``.sqlx`` files, paths over 260
characters, ``workflow_settings.yaml`` or ``dataform.json`` (or both), ``includes/`` JavaScript,
declarations, assertions, operations, incremental tables, ``tags: []`` and missing config fields,
``js`` blocks, pre and post operations, copy-pasted near-duplicates, broken refs, and files with a
byte-order mark, CRLF or tab indentation, a non-UTF-8 encoding, or a config and no query.
Names are generic; nothing here resembles a real project. The output is deterministic per seed.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

DOMAINS = ["sales", "finance", "growth", "ops", "risk", "supply", "people", "product", "support", "marketing", "ml", "audit"]
LAYERS = ["staging", "intermediate", "core", "marts", "reporting"]
SOURCE_DATASETS = ["raw_app", "raw_billing", "raw_crm", "raw_events", "raw_ledger", "raw_inventory"]
PROJECT = "kumosql"
PREFIX = "fx_"  # every dataset this project would create is prefixed, so it never collides with real ones
MESSY_TABLES = ["raw_orders", "raw_order_items", "raw_products", "raw_users", "stg_orders", "stg_order_items", "stg_products", "stg_users", "dim_customer", "fct_orders"]

WORKFLOW_SETTINGS = """\
# Dataform workflow settings (fixture)
defaultProject: kumosql
defaultDataset: fx_scratch
defaultLocation: EU
defaultAssertionDataset: fx_assertions
dataformCoreVersion: 3.0.0
vars:
  lookback_days: "30"
  env: prod
  start_date: "2020-01-01"
"""

DATAFORM_JSON = """\
{
  "warehouse": "bigquery",
  "defaultSchema": "fx_scratch",
  "assertionSchema": "fx_assertions",
  "defaultDatabase": "kumosql",
  "defaultLocation": "EU",
  "vars": { "lookback_days": "30", "env": "prod" }
}
"""

INCLUDES = {
    "constants.js": """\
const START_DATE = "2020-01-01";
const LOOKBACK_DAYS = dataform.projectConfig.vars.lookback_days || 30;
const PROJECT = dataform.projectConfig.defaultDatabase;
module.exports = { START_DATE, LOOKBACK_DAYS, PROJECT };
""",
    "helpers.js": """\
function dateFilter(column, days) {
  return `${column} >= DATE_SUB(CURRENT_DATE(), INTERVAL ${days} DAY)`;
}
function safeRatio(numerator, denominator) {
  return `SAFE_DIVIDE(${numerator}, NULLIF(${denominator}, 0))`;
}
function columnList(columns) {
  return columns.map((c) => `\\`${c}\\``).join(", ");
}
module.exports = { dateFilter, safeRatio, columnList };
""",
    "docs.js": """\
const columns = {
  id: "Surrogate key",
  created_at: "When the row was created (UTC)",
  amount: "Amount in minor units, } or { are fine in docs",
};
module.exports = { columns };
""",
}


def sql_body(rng: random.Random, parents: list[str], n: int, flavor: int) -> str:
    """A BigQuery query reading ``parents`` (already rendered as table expressions).

    Every model outputs ``id, created_at, amount, items`` so any model can read any other.
    """

    first = parents[0]
    extra = ""
    for k, p in enumerate(parents[1:], start=1):
        extra += f"\nleft join {p} as b{k}\n  on a.id = b{k}.id"
    if flavor == 0:
        return (f"select\n  a.id,\n  a.created_at,\n  coalesce(a.amount, 0) as amount,\n  a.items\nfrom {first} as a{extra}\n"
                "where a.created_at >= '2020-01-01'\n")
    if flavor == 1:
        return (f"with base as (\n  select * from {first}\n),\nagg as (\n  select id, max(created_at) as created_at, sum(amount) as amount,\n"
                f"    any_value(items) as items, count(*) as n\n  from base\n  group by id\n)\n"
                f"select a.id, a.created_at, a.amount / nullif(a.n, 0) as amount, a.items\nfrom agg as a{extra}\n")
    if flavor == 2:
        return (f"select\n  a.id,\n  a.created_at,\n  a.amount,\n  a.items,\n"
                f"  row_number() over (partition by a.id order by a.created_at desc) as rn\nfrom {first} as a{extra}\nqualify rn = 1\n")
    if flavor == 3:
        return (f"select\n  a.id,\n  a.created_at,\n  item.qty * item.price as amount,\n  a.items\nfrom {first} as a{extra},\n"
                f"  unnest(a.items) as item\nwhere item.qty > 0\n")
    if flavor == 4:
        return (f"select\n  a.id,\n  max(a.created_at) as created_at,\n  safe_cast(sum(a.amount) as numeric) as amount,\n"
                f"  any_value(a.items) as items\nfrom {first} as a{extra}\ngroup by 1\n")
    return (f"select\n  a.*,\n  struct(a.id as id, a.created_at as at) as key,\n"
            f"  format_date('%Y-%m', a.created_at) as period\nfrom {first} as a{extra}\n")


def wide_body(first: str, n: int) -> str:
    """A wide model with a long chain of CTEs, the shape that makes column tracing slow."""

    base = ", ".join(f"a.amount + {i} as c{i}" for i in range(60))
    ctes = [f"s0 as (\n  select a.id, a.created_at, a.items, {base} from {first} as a\n)"]
    for i in range(1, 16):
        extra = ", ".join(f"c{j} + {i} as c{j}" for j in range(0, 60, 3))
        keep = ", ".join(f"c{j}" for j in range(60) if j % 3)
        ctes.append(f"s{i} as (\n  select id, created_at, items, {extra}, {keep} from s{i - 1}\n  where c{i} is not null\n)")
    return "with " + ",\n".join(ctes) + "\nselect id, created_at, c0 as amount, items from s15\n"


def long_dir(rng: random.Random, depth: int) -> str:
    parts = []
    for _ in range(depth):
        parts.append(rng.choice(["quarterly_business_review", "finance_reconciliation_pipeline", "regional_marketing_attribution",
                                 "customer_lifetime_value_models", "supply_chain_forecasting_inputs", "legacy_migration_staging_area"]))
    return "/".join(parts)


def generate(out: Path, models: int, seed: int, config: str = "yaml") -> dict:
    rng = random.Random(seed)
    if sys.platform == "win32":
        out = Path("\\\\?\\" + str(out.resolve()))  # extended-length path: lets paths pass 260 characters without admin rights
    if out.exists():
        shutil.rmtree(out)
    (out / "includes").mkdir(parents=True)
    defs = out / "definitions"
    counts: dict[str, int] = {}

    def write(path: Path, text: str | bytes, kind: str, newline: str = "\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, str):
            data = text.replace("\n", newline).encode("utf-8")
        else:
            data = text
        path.write_bytes(data)
        counts[kind] = counts.get(kind, 0) + 1

    if config in ("yaml", "both"):
        write(out / "workflow_settings.yaml", WORKFLOW_SETTINGS, "config")
    if config in ("json", "both"):
        write(out / "dataform.json", DATAFORM_JSON, "config")
    write(out / "package.json", '{\n  "name": "fixture-dataform",\n  "dependencies": { "@dataform/core": "3.0.0" }\n}\n', "other")
    write(out / ".gitignore", "node_modules/\n", "other")
    write(out / "README.md", "Synthetic Dataform project for testing KumoSQL. Not a real project.\n", "other")
    for name, text in INCLUDES.items():
        write(out / "includes" / name, text, "include")

    # Declarations: raw tables the project reads but does not build.
    declared: list[tuple[str, str]] = []
    for ds in SOURCE_DATASETS:
        for i in range(8):
            name = f"{ds.removeprefix('raw_')}_{i}"
            declared.append((ds, name))
            write(defs / "sources" / ds / f"{name}.sqlx",
                  f'config {{\n  type: "declaration",\n  schema: "{ds}",\n  name: "{name}",\n  columns: {{ id: "key", created_at: "ts" }}\n}}\n',
                  "declaration")
    for name in MESSY_TABLES:  # real tables in the KumoSQL BigQuery test bed
        declared.append(("kumosql_messy", name))
        write(defs / "sources" / "kumosql_messy" / f"{name}.sqlx",
              f'config {{\n  type: "declaration",\n  database: "{PROJECT}",\n  schema: "kumosql_messy",\n  name: "{name}"\n}}\n', "declaration")
    write(defs / "sources" / "declarations.js", 'declare({ schema: "raw_misc", name: "javascript_declared" });\n', "js")
    # Declarations that need evaluating: a literal loop (read from the files) and a computed list (needs the Dataform API).
    write(defs / "sources" / "declared_in_loop.js",
          'const schema = "raw_loop";\n["loop_declared_a", "loop_declared_b"].forEach((t) => declare({ schema, name: t }));\n', "js")
    write(defs / "sources" / "declared_dynamic.js",
          'const names = ["computed_declared_a", "computed_declared_b"].map((n) => n);\nnames.forEach((t) => declare({ schema: "raw_computed", name: t }));\n', "js")

    declared += [("raw_loop", "loop_declared_a"), ("raw_loop", "loop_declared_b"),
                 ("raw_computed", "computed_declared_a"), ("raw_computed", "computed_declared_b")]

    # Layered models. Each layer reads earlier layers, so the graph is deep and wide.
    per_layer = max(1, models // len(LAYERS))
    built: list[tuple[str, str]] = []  # (schema, name) available for ref
    previous: list[tuple[str, str]] = list(declared)
    index = 0
    deep_every = max(1, models // 40)
    for layer_number, layer in enumerate(LAYERS):
        current: list[tuple[str, str]] = []
        for i in range(per_layer):
            index += 1
            domain = DOMAINS[(index * 7 + layer_number) % len(DOMAINS)]
            schema = f"{PREFIX}{layer}_{domain}"
            name = f"{domain}_{layer}_{i}"
            n_parents = 1 if layer_number == 0 else rng.randint(1, 3)
            pool = previous if rng.random() < 0.85 or not built else built
            parents = rng.sample(pool, min(len(pool), n_parents))
            rendered = []
            for ps, pn in parents:
                style = rng.random()
                if (ps, pn) in declared and style < 0.1:
                    rendered.append(f"`{PROJECT}.{ps}.{pn}`")  # a hard-coded table reference
                elif style < 0.5:
                    rendered.append(f'${{ref("{pn}")}}')
                elif style < 0.9:
                    rendered.append(f'${{ref("{ps}", "{pn}")}}')
                else:
                    rendered.append(f'${{ref({{ schema: "{ps}", name: "{pn}" }})}}')
            if rng.random() < 0.01:
                rendered.append('${ref("fx_deleted", "model_that_was_deleted")}')  # drift: a broken ref
            kind = rng.random()
            flavor = rng.randrange(6)
            body = sql_body(rng, rendered, index, flavor)
            if index % 97 == 0:
                body = wide_body(rendered[0], index)
            header: str
            pre = post = js = ""
            if kind < 0.62:
                tags = rng.choice(['tags: ["daily"],', 'tags: [],', 'tags: ["daily", "' + domain + '"],', "", 'tags: ["weekly"],'])
                header = f'config {{\n  type: "table",\n  schema: "{schema}",\n  {tags}\n  bigquery: {{ partitionBy: "DATE(created_at)", clusterBy: ["id"] }}\n}}'
                if rng.random() < 0.5:
                    header = header.replace(f'  schema: "{schema}",\n', f'  schema: "{schema}",\n  name: "{name}",\n')
            elif kind < 0.77:
                header = f'config {{\n  type: "view",\n  schema: "{schema}",\n  description: "View of {domain} {layer} data",\n  columns: require("includes/docs").columns\n}}'
            elif kind < 0.87:
                header = (f'config {{\n  type: "incremental",\n  schema: "{schema}",\n  uniqueKey: ["id"],\n'
                          f'  bigquery: {{ partitionBy: "DATE(created_at)", updatePartitionFilter: "created_at >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)" }}\n}}')
                body = "select * from (\n" + body.rstrip("\n") + "\n)\n${when(incremental(), `where 1 = 1`)}\n"
                post = 'post_operations {\n  grant `roles/bigquery.dataViewer` on table ${self()} to "group:analysts@example.com"\n}\n'
            elif kind < 0.92:
                header = f'config {{\n  schema: "{schema}"\n}}'  # no type, which Dataform treats as a table
            elif kind < 0.96:
                header = f'config {{\n  type: "table",\n  name: "{name}",\n  schema: "{schema}",\n  disabled: true\n}}'
            else:
                header = f'config {{ type: "table", schema: "{schema}", name: "{name}", tags: [] }}'
            if rng.random() < 0.12:
                js = ('js {\n  const { dateFilter, safeRatio } = require("includes/helpers");\n'
                      '  const cols = ["id", "created_at"];  // } a brace in a comment\n  const label = "}{";\n}\n')
                body = body.replace("a.created_at >= '2020-01-01'", "${dateFilter('a.created_at', constants.LOOKBACK_DAYS)}")
            if rng.random() < 0.08:
                pre = 'pre_operations {\n  declare cutoff default (select max(created_at) from ${self()});\n  ---\n  set cutoff = current_timestamp()\n}\n'
            text = header + "\n\n" + js + pre + post + body if rng.random() < 0.5 else header + "\n\n" + pre + js + body + post
            if rng.random() < 0.04:
                text = "-- Owner: " + domain + " team (ticket DAT-" + str(1000 + index) + ")\n/* legacy: do not edit } { */\n" + text

            # Where the file goes: mostly a tidy folder, sometimes very deep, sometimes with odd characters.
            folder = defs / layer / domain
            roll = rng.random()
            if index % deep_every == 0:
                folder = defs / long_dir(rng, rng.randint(4, 7)) / layer / domain  # past 260 characters on Windows
            elif roll < 0.03:
                folder = defs / layer / f"{domain} (archive)" / "v2.0"
            elif roll < 0.05:
                folder = defs / layer / f"{domain}_café"
            filename = f"{name}.sqlx"
            if index % deep_every == 0:
                filename = f"{name}_{'x' * 90}.sqlx"  # a long file name on top of the deep folder
                if 'name: "' not in text.split("}")[0]:
                    text = text.replace("config {\n", f'config {{\n  name: "{name}",\n', 1)  # an action is named by its file unless the config says otherwise
            newline = "\r\n" if rng.random() < 0.08 else "\n"
            if rng.random() < 0.02:
                text = text.replace("\n  ", "\n\t")
            path = folder / filename
            flavor_roll = rng.random()
            if flavor_roll < 0.015:
                write(path, b"\xef\xbb\xbf" + text.replace("\n", newline).encode("utf-8"), "sqlx-bom")
            elif flavor_roll < 0.02:
                latin = text.replace("a.created_at", "a.created_at /* déjà vu */").replace("\n", newline).encode("latin-1")
                write(path, latin, "sqlx-latin1")
            else:
                if flavor_roll < 0.022:  # a stub with a config and no query, as left behind by an unfinished change
                    write(defs / "placeholders" / f"stub_{index}.sqlx", f'config {{ type: "table", schema: "{PREFIX}placeholders" }}\n', "sqlx-config-only")
                write(path, text.rstrip("\n") if rng.random() < 0.05 else text, "sqlx", newline)
            if not 0.92 <= kind < 0.96:  # a disabled action is never read by others
                current.append((schema, name))
        built.extend(current)
        previous = current or previous

    # Assertions, operations, plain .sql files and a few copy-pasted near-duplicates.
    sample = rng.sample(built, min(len(built), max(10, models // 15)))
    for i, (schema, name) in enumerate(sample):
        write(defs / "assertions" / f"assert_{name}_unique.sqlx",
              f'config {{\n  type: "assertion",\n  tags: ["quality"]\n}}\n\nselect id, count(*) as n from ${{ref("{schema}", "{name}")}} group by id having n > 1\n', "assertion")
        if i % 5 == 0:
            write(defs / "operations" / f"refresh_{name}.sqlx",
                  f'config {{\n  type: "operations",\n  hasOutput: true,\n  schema: "{schema}",\n  name: "{name}_snapshot"\n}}\n\n'
                  f'create or replace table ${{self()}} as\nselect * from ${{ref("{schema}", "{name}")}}\n', "operation")
        if i % 7 == 0:
            write(defs / "adhoc" / f"{name}_report.sql", f"select id, created_at from `{PROJECT}.{schema}.{name}` where created_at > '2021-01-01'\n", "sql")
        if i % 9 == 0:
            body = (defs / "assertions" / f"assert_{name}_unique.sqlx").read_text()
            write(defs / "assertions" / f"assert_{name}_unique_copy.sqlx", body.replace("count(*)", "count(1)"), "assertion")
    for i, (schema, name) in enumerate(rng.sample(built, min(len(built), max(5, models // 40)))):
        original = next(defs.rglob(f"{name}.sqlx"), None)
        if original is None or not original.is_file():
            continue
        text = original.read_bytes().decode("utf-8", "replace")
        copy = text.replace("coalesce(a.amount, 0)", "ifnull(a.amount, 0)").replace(f'name: "{name}"', f'name: "{name}_v2_final_copy"')
        write(original.with_name(f"{name}_v2_final_copy.sqlx"), copy, "near-duplicate")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--models", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--config", choices=["yaml", "json", "both"], default="yaml")
    args = parser.parse_args(argv)
    counts = generate(args.out, args.models, args.seed, args.config)
    root = Path("\\\\?\\" + str(args.out.resolve())) if sys.platform == "win32" else args.out
    longest = max((len(str(p.relative_to(root))) for p in root.rglob("*") if p.is_file()), default=0)
    print(f"Wrote {sum(counts.values())} files to {args.out} (longest path {longest} characters): {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
