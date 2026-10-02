"""Run the QED prover's Calcite test cases through KumoSQL's provers.

QED (https://github.com/qed-solver/prover, MIT, VLDB 2024) ships about 440
query pairs from Apache Calcite's optimizer tests as relational-algebra JSON.
``tools/qed_to_sql.py`` turns 375 of them into SQL (``tests/fixtures/qed``;
the rest are skipped with a reason, never guessed). For every pair this
reports ``proved``, ``different`` (not proved, and a random DuckDB database
shows the two queries disagree), ``unknown`` and ``wrong`` (proved, yet a
counterexample exists; must stay 0).

    python tools/qed_bench.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlsolver_bench as sb  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qed"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "qed_calcite_pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def _tables(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def run(prove=sb.default_prove, trials: int = 30, limit: int | None = None) -> dict:
    out = {"total": 0, "proved": 0, "different": [], "unknown": [], "wrong": [], "seconds": 0.0}
    start = time.time()
    schemas: dict[str, tuple] = {}
    for case in load_cases()[:limit]:
        out["total"] += 1
        key = case["schema_id"]
        if key not in schemas:
            tables = _tables(case["ddl"])
            schemas[key] = (tables, sb.new_database(tables))
        tables, db = schemas[key]
        left, right = case["sql_a"], case["sql_b"]
        try:
            proof = prove(left, right, tables, True)  # Calcite reads a literal GROUP BY key as a constant
        except Exception:  # a crash is a failure to prove, never a proof
            proof = False
        counter = sb.differ(left, right, tables, db, trials, constants=True)
        found = counter not in (None, False)
        if proof and found:
            out["wrong"].append(case["name"])
        elif proof:
            out["proved"] += 1
        elif found:
            out["different"].append(case["name"])
        else:
            out["unknown"].append(case["name"])
    out["seconds"] = time.time() - start
    return out


def main() -> int:
    r = run()
    print(f"qed-calcite {r['proved']}/{r['total']} proved, {len(r['different'])} different (counterexample), {len(r['unknown'])} unknown, {len(r['wrong'])} wrong, {r['seconds']:.1f}s")
    for key in ("different", "wrong"):
        if r[key]:
            print(f"  {key}: {', '.join(r[key])}")
    return 1 if r["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
