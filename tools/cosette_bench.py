"""Score Cosette's examples and the SPES-only Calcite pairs with KumoSQL's provers.

Fixtures come from ``tools/cosette_to_sql.py`` (Cosette, BSD-2-Clause) and
``tools/spes_to_sql.py`` (SPES, Apache-2.0); see the READMEs in
``tests/fixtures/cosette`` and ``tests/fixtures/spes``. Each pair gets one verdict:

* ``proven``: the prover shows the queries equivalent (an unbounded proof).
* ``refuted``: no proof, and a random DuckDB database on which the two queries
  return different bags (executed evidence).
* ``unknown``: neither.
* ``wrong``: proven, yet a counterexample exists or the source labels the pair
  not equivalent. Must stay 0.

Cosette carries labels (equivalent, not equivalent, equivalent under the keys
it states); ``correct`` counts proofs of equivalent pairs plus refutations of
not-equivalent ones. A refutation of a pair labelled equivalent is listed as a
label dispute. Cases with an uninterpreted predicate (a hidden ``__`` column)
are never refuted, because a random hidden column need not be a function of
the visible row.

    python tools/cosette_bench.py [cosette|spes]
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlsolver_bench as sb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _tables(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def load(suite: str) -> list[dict]:
    if suite == "cosette":
        return [json.loads(line) for line in (ROOT / "cosette" / "cosette_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ddl = (ROOT / "sqlsolver" / "calcite.schema.sql").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in (ROOT / "spes" / "spes_only_pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        row.setdefault("ddl", ddl)
    return rows


def run(suite: str, prove=sb.default_prove, trials: int = 60) -> dict:
    out = {"total": 0, "proven": [], "refuted": [], "unknown": [], "wrong": [], "disputed": [], "correct": 0, "seconds": 0.0}
    constants = suite == "spes"  # SPES pairs are Calcite's: a literal in GROUP BY is a constant
    start = time.time()
    schemas: dict[str, tuple] = {}
    for case in load(suite):
        out["total"] += 1
        name = case["name"]
        label = case.get("label", "equivalent")
        if case["ddl"] not in schemas:
            tables = _tables(case["ddl"])
            schemas[case["ddl"]] = (tables, sb.new_database(tables))
        tables, db = schemas[case["ddl"]]
        left, right = case["sql_a"], case["sql_b"]
        try:
            proof = prove(left, right, tables, constants) if constants else prove(left, right, tables)
        except Exception:  # a crash is a failure to prove, never a proof
            proof = False
        counter = sb.differ(left, right, tables, db, trials, constants=constants)
        found = counter not in (None, False) and "__" not in case["ddl"]
        if proof and (found or label == "not_equivalent"):
            out["wrong"].append(name)
        elif proof:
            out["proven"].append(name)
            out["correct"] += 1
        elif found:
            out["refuted"].append(name)
            if label == "not_equivalent":
                out["correct"] += 1
            else:
                out["disputed"].append(name)
        else:
            out["unknown"].append(name)
    out["seconds"] = time.time() - start
    return out


def main(argv: list[str] | None = None) -> int:
    suites = (argv if argv is not None else sys.argv[1:]) or ["cosette", "spes"]
    bad = 0
    for suite in suites:
        r = run(suite)
        print(
            f"{suite}: {r['correct']}/{r['total']} correct, 0 wrong" if not r["wrong"] else f"{suite}: {len(r['wrong'])} WRONG",
            f"| proven {len(r['proven'])}, refuted {len(r['refuted'])} (label disputes {len(r['disputed'])}), unknown {len(r['unknown'])}, {r['seconds']:.1f}s",
        )
        for key in ("wrong", "disputed", "refuted"):
            if r[key]:
                print(f"  {key}: {', '.join(r[key])}")
        bad += len(r["wrong"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
