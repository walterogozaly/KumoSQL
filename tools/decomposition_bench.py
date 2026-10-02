"""Aggregate-decomposition eval: rebuild a coarser aggregate from a finer summary, or say it cannot be done.

    python tools/decomposition_bench.py                # development families
    python tools/decomposition_bench.py --baseline     # the baseline: the existing prover, whole-summary matches only
    python tools/decomposition_bench.py --held-out     # the reserved families (final evaluation only)
    python tools/decomposition_bench.py --all --json out.json

Cases are in tests/fixtures/decomposition/cases.json (written by tools/make_decomposition_cases.py). Each case is up to
three kinds of item:

* ``synth``: build a replacement for the target query that reads only the summary. Correct when the case expects a
  rewrite and a proven replacement comes back (it is then re-run on random databases), or when the case expects none
  and no replacement is returned. A replacement for an ``expect: none`` case is wrong: two stored databases have
  the same summary but different targets, so no function of the summary can be right.
* ``trap``: a tempting wrong replacement (average of averages, SUM of distinct counts, ...). Correct when it is
  refuted by a database; wrong if it is proven.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kumosql.model_reuse import check_replacement, rewrite_over_model  # noqa: E402
from kumosql.random_check import CheckError, Witness, find_difference, prover_constraints, replay  # noqa: E402
from make_decomposition_cases import SALES, _connect, _duck, _load, _signature  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "decomposition" / "cases.json"


def load() -> tuple[list[dict], set[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["cases"], set(data["held_out_families"])


def _tables(packed: dict) -> dict:
    return {name: [tuple(row) for row in rows] for name, rows in packed.items()}


def witness_pair_holds(case: dict) -> bool:
    """The stored pair has equal summaries and different targets."""

    db = _connect(SALES)
    s_sql, q_sql = _duck(case["summary"], "postgres"), _duck(case["query"], "postgres")
    sigs = []
    for key in ("a", "b"):
        _load(db, SALES, _tables(case["witness"][key]))
        sigs.append((_signature(db, s_sql), _signature(db, q_sql)))
    return sigs[0][0] == sigs[1][0] and sigs[0][1] != sigs[1][1]


def trap_witness_holds(case: dict, trap: dict) -> bool:
    witness = Witness(0, _tables(trap["witness"]), (), ())
    inlined = _inline_trap(case, trap["sql"])
    return inlined is not None and replay(SALES, witness, case["query"], inlined, mode="bag")


def _inline_trap(case: dict, sql: str) -> str | None:
    from kumosql import model_reuse as mr
    import sqlglot

    try:
        tree = mr._prepare(case["summary"], SALES.columns, "postgres")
        block = mr._block(tree)
        names = mr._output_names(block)
        plain = mr._plain(case["summary"], SALES.columns, "postgres")
        named = mr._named_model_sql(block, names, tree, plain)
        return mr._inline(sqlglot.parse_one(sql, read="postgres"), "mv0", named)
    except Exception:  # noqa: BLE001
        return None


def synth(case: dict, use_baseline: bool, timeout_ms: int, trials: int) -> dict:
    start = time.time()
    if use_baseline:
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic

        try:
            proven = prove_equivalent_algebraic(case["query"], case["summary"], schema=SALES.columns, constraints=prover_constraints(SALES), dialect="postgres", compare_names=False, timeout_ms=timeout_ms).proven
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "reason": str(error), "seconds": time.time() - start}
        status, reason, sql, inlined = ("rewritten", "equal to the summary", "SELECT * FROM mv0", case["summary"]) if proven else ("no_rewrite", "not the same query", None, None)
    else:
        try:
            reuse = rewrite_over_model(case["query"], case["summary"], schema=SALES.columns, constraints=prover_constraints(SALES), timeout_ms=timeout_ms)
        except Exception as error:  # noqa: BLE001
            return {"status": "error", "reason": f"{type(error).__name__}: {error}", "seconds": time.time() - start}
        status, reason, sql, inlined = reuse.status, reuse.reason, reuse.sql, reuse.inlined_sql
    record = {"status": status, "reason": reason, "sql": sql, "seconds": round(time.time() - start, 3)}
    if status == "rewritten":
        try:
            witness = find_difference(SALES, case["query"], inlined, mode="bag", trials=trials)
        except CheckError as error:
            record["check"] = f"unchecked: {error}"
        else:
            record["check"] = "verified" if witness is None else "WRONG"
    return record


def trap(case: dict, item: dict, timeout_ms: int, trials: int) -> dict:
    start = time.time()
    try:
        check = check_replacement(case["query"], case["summary"], item["sql"], schema=SALES.columns, constraints=prover_constraints(SALES), database=SALES, timeout_ms=timeout_ms, trials=trials)
    except Exception as error:  # noqa: BLE001
        return {"status": "error", "reason": str(error), "seconds": time.time() - start}
    status = check.status
    if status == "refuted":
        confirmed = check.witness is not None and replay(SALES, check.witness, case["query"], _inline_trap(case, item["sql"]) or "", mode="bag")
        if not confirmed:
            status = "error"
    return {"status": status, "reason": check.reason, "seconds": round(time.time() - start, 3)}


def run_case(case: dict, args) -> list[dict]:
    out = []
    record = synth(case, args.baseline, args.timeout_ms, args.trials)
    record.update(id=case["id"], family=case["family"], kind="synth", expect=case["expect"])
    if case["expect"] == "none":
        record["label_checked"] = witness_pair_holds(case)
        record["outcome"] = "wrong" if record["status"] == "rewritten" else ("correct" if record["status"] == "no_rewrite" else record["status"])
    else:
        record["outcome"] = "wrong" if record.get("check") == "WRONG" else ("correct" if record["status"] == "rewritten" and record.get("check") == "verified" else ("miss" if record["status"] == "no_rewrite" else record["status"]))
    out.append(record)
    for index, item in enumerate(case["traps"]):
        result = trap(case, item, args.timeout_ms, args.trials)
        result.update(id=f"{case['id']}#trap{index}", family=case["family"], kind="trap", expect="refuted", sql=item["sql"])
        result["label_checked"] = trap_witness_holds(case, item)
        result["outcome"] = "correct" if result["status"] == "refuted" else ("wrong" if result["status"] == "proven" else "miss" if result["status"] == "unknown" else result["status"])
        out.append(result)
    return out


def summarize(records: list[dict]) -> dict:
    out = {}
    for key in ["all", "synth", "trap", *sorted({r["family"] for r in records})]:
        part = [r for r in records if key in ("all", r["kind"], r["family"])]
        outcomes = Counter(r["outcome"] for r in part)
        out[key] = {
            "items": len(part),
            "correct": outcomes.get("correct", 0),
            "rewrites_found": sum(1 for r in part if r["kind"] == "synth" and r["expect"] == "rewrite" and r["outcome"] == "correct"),
            "rewrites_expected": sum(1 for r in part if r["kind"] == "synth" and r["expect"] == "rewrite"),
            "impossible_declined": sum(1 for r in part if r["kind"] == "synth" and r["expect"] == "none" and r["outcome"] == "correct"),
            "impossible_cases": sum(1 for r in part if r["kind"] == "synth" and r["expect"] == "none"),
            "traps_refuted": sum(1 for r in part if r["kind"] == "trap" and r["outcome"] == "correct"),
            "traps": sum(1 for r in part if r["kind"] == "trap"),
            "miss": outcomes.get("miss", 0),
            "unsupported": outcomes.get("unsupported", 0),
            "timeout": outcomes.get("timeout", 0),
            "error": outcomes.get("error", 0),
            "wrong": outcomes.get("wrong", 0),
            "labels_replayed_ok": sum(1 for r in part if r.get("label_checked") is True),
            "seconds": round(sum(r["seconds"] for r in part), 1),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--held-out", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--trials", type=int, default=150)
    parser.add_argument("--id", action="append")
    args = parser.parse_args(argv)
    cases, held = load()
    if not args.all:
        cases = [c for c in cases if (c["family"] in held) == args.held_out]
    if args.id:
        cases = [c for c in cases if c["id"] in args.id]
    records = [r for c in cases for r in run_case(c, args)]
    summary = summarize(records)
    split = "all" if args.all else ("held-out" if args.held_out else "development")
    print(f"{'baseline' if args.baseline else 'decomposition'} on the {split} split: {len(cases)} cases, {len(records)} items")
    for key, value in summary.items():
        print(f"  {key}: {json.dumps(value)}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "records": records}, indent=1), encoding="utf-8")
    return 1 if summary["all"]["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
