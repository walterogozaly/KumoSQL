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


def generate(out: Path, nodes: int, seed: int) -> int:
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
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--nodes", type=int, default=2500)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    print(f"Wrote {generate(args.out, args.nodes, args.seed)} models to {args.out}")
