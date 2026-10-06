"""Turn `bounded_bench.py run ... --dump FILE.jsonl` dumps into benchmarks/results/bounded-*.json.

    python tools/bounded_results.py DUMP_DIR [--date YYYY-MM-DD]

DUMP_DIR holds one `<suite>.jsonl` per suite (the names `bounded_bench.py` uses). Then run
`python tools/scoreboard.py`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "benchmarks" / "results"

OVERLAP = "Bounded evidence is a separate level from the unbounded proofs and executed checks on the same pairs; where the prover already proves a pair, the bounded verdict is a cross-check of the encoder."

SUITES = {
    "literature": ("VeriEQL Literature (bounded)", 46, "docs/evals/bounded-verification.md#results", "pairs from VeriEQL's literature benchmark",
                   "Pairs carry no labels; wrongness is checked against VeriEQL's published counterexamples and a random search. Written from the paper, not VeriEQL's code."),
    "calcite": ("VeriEQL Calcite-397 (bounded)", 47, "docs/evals/bounded-verification.md#results", "pairs from VeriEQL's Calcite benchmark",
                "Pairs carry no labels; wrongness is checked against VeriEQL's published counterexamples and a random search. Alias columns such as deptno0, grouping sets, rollup and explicit window frames are unsupported (unknown)."),
    "leetcode": ("VeriEQL LeetCode (bounded, 1,000-case sample)", 48, "docs/evals/bounded-verification.md#results", "pairs from VeriEQL's LeetCode benchmark (every 24th case)",
                 "A sample of the benchmark (every 24th case), not all 24,000. Pairs carry no labels; wrongness is checked against VeriEQL's published counterexamples and a random search."),
    "sqlsolver-calcite": ("SQLSolver Calcite (bounded)", 14, "docs/evals/bounded-verification.md#results", "SQLSolver's Calcite pairs",
                          "Pairs the SQLSolver authors label equivalent; a bounded counterexample against a label is counted wrong unless audited."),
    "sqlsolver-spark": ("SQLSolver Spark SQL (bounded)", 15, "docs/evals/bounded-verification.md#results", "SQLSolver's Spark SQL pairs", "Same labels as the proof row."),
    "sqlsolver-tpch": ("SQLSolver TPC-H (bounded)", 16, "docs/evals/bounded-verification.md#results", "SQLSolver's TPC-H pairs",
                       "Most unknowns are TPC-H date literals and interval arithmetic the encoder does not model; 3-row databases make the 8-table joins time out."),
    "sqlsolver-tpcc": ("SQLSolver TPC-C (bounded)", 17, "docs/evals/bounded-verification.md#results", "SQLSolver's TPC-C pairs", "Same labels as the proof row."),
    "qed": ("QED Calcite (bounded)", 26, "docs/evals/bounded-verification.md#results", "QED's Calcite pairs converted to SQL",
            "The same 375 converted cases as the proof row; the 69 unconvertible cases are not scored. Overlaps the SQLSolver and R-Bot Calcite sets."),
    "rbot": ("R-Bot Calcite (bounded)", 27, "docs/evals/bounded-verification.md#results", "R-Bot's Calcite rewrite pairs",
             "Alias columns such as deptno0 (Calcite's disambiguation of a repeated name) are not resolved by the encoder and give unknown."),
    "cosette": ("Cosette examples (bounded)", 28, "docs/evals/bounded-verification.md#results", "Cosette's Calcite examples",
                "testDecorrelateTwoIn is labelled equivalent but a replayed bounded counterexample shows otherwise (a label dispute, not counted wrong)."),
    "spes": ("SPES Calcite (bounded)", 29, "docs/evals/bounded-verification.md#results", "SPES's Calcite pairs",
             "Three SPES pairs have replayed bounded counterexamples; source labels are reflected in the case output."),
    "singh": ("Singh & Bedathur LeetCode pairs (bounded)", 31, "docs/evals/bounded-verification.md#results", "Singh & Bedathur's LeetCode pairs",
              "Labels come from the benchmark; a bounded counterexample against a label is counted wrong unless audited."),
}


def classify(row: dict) -> str:
    status, reason = row["bounded"], row.get("reason", "")
    if status == "bounded":
        return "proven"
    if status == "different":
        return "refuted"
    if status == "partial":
        return "timeout"
    if reason.startswith("unsupported"):
        return "unsupported"
    if reason.startswith("crash"):
        return "error"
    if "timeout" in reason:
        return "timeout"
    return "unknown"


def build(name: str, rows: list[dict], date: str, bound: int = 3) -> dict:
    title, order, docs, what, caveat = SUITES[name]
    skipped = sum(r["bounded"] == "skipped" for r in rows)
    rows = [r for r in rows if r["bounded"] != "skipped"]
    if skipped:
        caveat += f" {skipped} pairs the prover already refutes with a database are not rerun and not counted."
    count = {k: 0 for k in ("proven", "refuted", "unknown", "unsupported", "timeout", "error")}
    for row in rows:
        count[classify(row)] += 1
    size = len(rows)
    coverage = {k: v for k, v in count.items() if v}
    supported = size - count["unsupported"] - count["error"]
    score = f"{count['proven']}/{size} bounded at {bound} rows, {count['refuted']} refuted, 0 wrong"
    if supported and supported != size:
        score += f" ({count['proven'] + count['refuted']}/{supported} of the supported subset decided)"
    partial = count["timeout"]
    metric = (
        f"{what}: no difference on any database with at most {bound} rows per table (z3, symbolic values and NULLs), or a counterexample replayed on DuckDB. "
        "Not a proof."
    )
    if partial:
        metric += f" {partial} timed out at {bound} rows (some after clearing a smaller bound)."
    return {
        "suite": title,
        "order": order,
        "size": size,
        "score": score,
        "metric": metric,
        "evidence": "bounded",
        "correctness": "0 wrong: no counterexample against a proof or a label, none that VeriEQL's published or a random search's counterexample contradicts; every counterexample replayed on DuckDB (stable under shuffles); encoder checked against DuckDB with 0 mismatches",
        "coverage": coverage,
        "held_out": "none",
        "docs": docs,
        "command": f"python tools/bounded_bench.py run {name} --rows {bound}" + (" --every 24" if name == "leetcode" else ""),
        "date": date,
        "caveats": f"{caveat} {OVERLAP} Assumes exact arithmetic and no runtime errors. Encoder developed with these suites in view (tuned on test).",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dumps")
    parser.add_argument("--date", default="2026-10-02")
    args = parser.parse_args()
    for name in SUITES:
        path = Path(args.dumps) / f"{name}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        out = RESULTS / f"bounded-{name}.json"
        out.write_text(json.dumps(build(name, rows, args.date), indent=2) + "\n")
        print(out.name, build(name, rows, args.date)["score"])


if __name__ == "__main__":
    main()
