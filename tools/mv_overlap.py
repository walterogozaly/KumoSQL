"""Inventory of overlap between the Calcite materialized-view cases and the repository's other eval corpora.

    python tools/mv_overlap.py            # print the table
    python tools/mv_overlap.py --json out.json

A case overlaps a corpus when the case's query, or its materialization, appears in that corpus after
the SQL is lower-cased and stripped of quotes, schema prefixes and whitespace. The count is a lower
bound (the same query written differently is not found).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def norm(sql: str) -> str:
    sql = sql.lower().replace('"', "").replace("`", "")
    sql = re.sub(r"\b(foodmart|scott|hr|tpch|tpcds)\.", "", sql)
    return re.sub(r"[\s;()]+", "", sql)


def corpus_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return norm(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json")
    args = parser.parse_args()
    cases = json.loads((FIXTURES / "mv_reuse" / "calcite_mv_cases.json").read_text(encoding="utf-8"))["cases"]
    corpora = {
        "sqlsolver-calcite": FIXTURES / "sqlsolver" / "calcite_pairs.txt",
        "sqlsolver-spark": FIXTURES / "sqlsolver" / "spark_pairs.txt",
        "sqlsolver-tpch": FIXTURES / "sqlsolver" / "tpch_pairs.txt",
        "cosette": FIXTURES / "cosette" / "cosette_cases.jsonl",
    }
    for folder in ("qed", "rbot", "spes"):
        for path in sorted((FIXTURES / folder).rglob("*")):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".txt", ".sql"):
                corpora.setdefault(f"{folder}", path)
    texts = {name: corpus_text(path) for name, path in corpora.items()}
    for folder in ("qed", "rbot", "spes"):
        texts[folder] = "".join(corpus_text(p) for p in sorted((FIXTURES / folder).rglob("*")) if p.is_file() and p.suffix in (".json", ".jsonl", ".txt", ".sql"))
    report = {}
    for name, text in texts.items():
        hits = [c["id"] for c in cases if len(norm(c["query"])) > 20 and (norm(c["query"]) in text or norm(c["materialization"]) in text)]
        report[name] = {"overlapping_cases": len(hits), "of": len(cases), "ids": hits}
        print(f"{name:20s} {len(hits):4d} of {len(cases)}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
