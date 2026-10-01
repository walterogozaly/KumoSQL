"""Generate a large synthetic Dataform project for exercising the query graph.

    python tools/make_large_project.py OUT_DIR [--nodes 2500] [--seed 7]
    kumosql-ui --project OUT_DIR

Models are spread over a dozen datasets and ten layers, each reading one to three models from
earlier layers. Names are generic; nothing here resembles a real project.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

DATASETS = ["raw", "staging", "core", "marts", "finance", "growth", "ops", "ml", "reporting", "audit", "tmp", "archive"]


def _deep_cte() -> str:
    parts = []
    for i in range(60):
        source = '${ref("m0_9")}' if i == 0 else f"c{i - 1}"
        parts.append(f"c{i} as (select id from {source})")
    return 'config { type: "table" }\nwith ' + ", ".join(parts) + "\nselect * from c59"


ODD = [
    # (file name, contents): the awkward things real Dataform repositories contain.
    ("odd/wide.sqlx", 'config { type: "table" }\nselect ' + ", ".join(f"c{i} as col_{i}" for i in range(180)) + ' from ${ref("m0_0")}'),
    ("odd/star_chain.sqlx", 'config { type: "view" }\nselect * from ${ref("odd_wide")}'),
    ("odd/union.sqlx", 'config { type: "table" }\nselect id from ${ref("m0_1")} union all select id from ${ref("m0_2")} union distinct select id from ${ref("m0_3")}'),
    ("odd/unnest.sqlx", 'config { type: "table" }\nselect x, y from ${ref("m0_4")}, unnest(split(label, ",")) as x left join unnest([1,2,3]) as y on true'),
    ("odd/js_block.sqlx", 'config { type: "table" }\njs { const cols = ["a","b"]; }\nselect ${cols.map(c => c).join(", ")} from ${ref("m0_5")}'),
    ("odd/operation.sqlx", 'config { type: "operations", hasOutput: true }\ncreate or replace table x.y as select 1 as a;\nselect 2 as b'),
    ("odd/assertion.sqlx", 'config { type: "assertion" }\nselect * from ${ref("m0_6")} where id is null'),
    ("odd/incremental.sqlx", 'config { type: "incremental", uniqueKey: ["id"] }\nselect id from ${ref("m0_7")}\n${when(incremental(), `where id > (select max(id) from ${self()})`)}'),
    ("odd/pre_post.sqlx", 'config { type: "table" }\npre_operations { delete from ${self()} where true }\nselect 1 as id\npost_operations { grant select on ${self()} to "x" }'),
    ("odd/missing_ref.sqlx", 'config { type: "table" }\nselect * from ${ref("does_not_exist")} join ${ref("other_schema", "also_missing")} using (id)'),
    ("odd/syntax_error.sqlx", 'config { type: "table" }\nselect from from where ((('),
    ("odd/cycle_a.sqlx", 'config { type: "table" }\nselect * from ${ref("odd_cycle_b")}'),
    ("odd/cycle_b.sqlx", 'config { type: "table" }\nselect * from ${ref("odd_cycle_a")}'),
    ("odd/unicode_\u00e9.sqlx", 'config { type: "table", name: "caf\u00e9" }\nselect "\u2603" as s'),
    ("odd/empty.sqlx", 'config { type: "table" }\n'),
    ("odd/declaration.sqlx", 'config { type: "declaration", schema: "ext", name: "src_tbl" }'),
    ("odd/inlist.sqlx", 'config { type: "table" }\nselect id from ${ref("m0_8")} where id in (' + ",".join(str(i) for i in range(5000)) + ")"),
    ("odd/deep_cte.sqlx", _deep_cte()),
]


def generate(out: Path, nodes: int, seed: int, messy: bool = False) -> int:
    rng = random.Random(seed)
    layers = 10
    per_layer = max(1, nodes // layers)
    previous: list[str] = []
    count = 0
    for layer in range(layers):
        current: list[str] = []
        for i in range(per_layer):
            name = f"m{layer}_{i}"
            dataset = DATASETS[(layer * 3 + i) % len(DATASETS)]
            folder = out / "definitions" / dataset
            folder.mkdir(parents=True, exist_ok=True)
            if layer == 0 or not previous:
                body = f"select {i} as id, 'x' as label"
            else:
                parents = rng.sample(previous, min(len(previous), rng.randint(1, 3)))
                body = "select a.id, a.label from " + " join ".join(
                    [f'${{ref("{parents[0]}")}} as a'] + [f'${{ref("{p}")}} as b{k} on a.id = b{k}.id' for k, p in enumerate(parents[1:])])
            (folder / f"{name}.sqlx").write_text(f'config {{ type: "table", name: "{name}", schema: "{dataset}" }}\n{body}\n')
            current.append(name)
            count += 1
        previous = current
    if messy:
        folder = out / "definitions"
        for name, text in ODD:
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--nodes", type=int, default=2500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--messy", action="store_true", help="also write awkward files: wide tables, unions, JS blocks, operations, missing refs, cycles, syntax errors")
    args = parser.parse_args()
    print(f"Wrote {generate(args.out, args.nodes, args.seed, args.messy)} models to {args.out}")
