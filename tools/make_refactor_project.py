"""Generate a Dataform project with redundancy for the Refactor search (see docs/refactor.md).

    python tools/make_refactor_project.py OUT_DIR [--reports 40] [--seed 3]

Each source table has a staging view, several copies of a filtered view over it (what teams
duplicate), a few unread leftovers, and reports that read the copies. Reports are the tables to
protect; every other model is editable. Names are generic.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

SOURCES = {"orders": "id, customer_id, amount, status", "events": "id, kind, amount, user_id", "items": "order_id, sku, qty, price"}
FILTERS = {"orders": "amount > 0", "events": "kind = 'buy'", "items": "qty > 0"}
GROUPS = {"orders": "status", "events": "kind", "items": "sku"}
MEASURES = {"orders": "SUM(amount)", "events": "SUM(amount)", "items": "SUM(qty * price)"}


def generate(out: Path, reports: int = 40, seed: int = 3) -> dict[str, list[str]]:
    rng = random.Random(seed)
    out = Path(out)
    (out / "definitions").mkdir(parents=True, exist_ok=True)
    (out / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: an\n", encoding="utf-8")
    roles = {"protected": [], "editable": []}

    def write(name: str, kind: str, body: str) -> None:
        (out / "definitions" / f"{name}.sqlx").write_text(f'config {{ type: "{kind}" }}\n{body}\n', encoding="utf-8")

    for source, columns in SOURCES.items():
        (out / "definitions" / f"src_{source}.sqlx").write_text(
            f'config {{ type: "declaration", schema: "raw", name: "{source}" }}\n', encoding="utf-8")
        write(f"stg_{source}", "view", f'SELECT {columns} FROM ${{ref("raw", "{source}")}}')
        roles["editable"].append(f"stg_{source}")
        copies = max(2, reports // 10)
        for copy in range(copies):
            write(f"{source}_clean_{copy}", "view", f'SELECT {columns} FROM ${{ref("stg_{source}")}} WHERE {FILTERS[source]}')
            roles["editable"].append(f"{source}_clean_{copy}")
        write(f"{source}_leftover", "view", f'SELECT {columns.split(",")[0]} FROM ${{ref("stg_{source}")}}')
        roles["editable"].append(f"{source}_leftover")
    names = list(SOURCES)
    for index in range(reports):
        source = rng.choice(names)
        copy = rng.randrange(max(2, reports // 10))
        group, measure = GROUPS[source], MEASURES[source]
        write(f"report_{index}", "table",
              f'SELECT {group}, {measure} AS total FROM ${{ref("{source}_clean_{copy}")}} GROUP BY {group}')
        roles["protected"].append(f"report_{index}")
    return roles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--reports", type=int, default=40)
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()
    roles = generate(args.out, args.reports, args.seed)
    print(f"{len(roles['protected'])} protected, {len(roles['editable'])} editable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
