"""Query-containment eval: is every result of q1 also a result of q2?

    python tools/containment_bench.py                # development families
    python tools/containment_bench.py --baseline     # the baseline: equality by the existing prover, plus random search
    python tools/containment_bench.py --held-out     # the reserved families (final evaluation only)
    python tools/containment_bench.py --all --json out.json

Cases are in tests/fixtures/containment/cases.json (written by tools/make_containment_cases.py). Each case is scored
twice, once for set containment and once for bag containment. A ``contained`` answer needs a proof; a
``not_contained`` answer needs a database that is replayed on its own and separates the queries. Anything else is
``unknown``, ``unsupported``, ``timeout`` or ``error``. A proof for a case labelled not contained, or a
counterexample for one labelled contained, is **wrong**.
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

from kumosql.containment import Containment, check_containment  # noqa: E402
from kumosql.random_check import CheckError, find_difference, prover_constraints, replay  # noqa: E402
from make_containment_cases import SHOP  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "containment" / "cases.json"


def load() -> tuple[list[dict], set[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["cases"], set(data["held_out_families"])


def baseline(case: dict, semantics: str, timeout_ms: int) -> Containment:
    """What the repository could do before: equality by the existing prover, and a random-database search."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    try:
        result = prove_equivalent_algebraic(case["q1"], case["q2"], schema=SHOP.columns, constraints=prover_constraints(SHOP), dialect="postgres", compare_names=False, timeout_ms=timeout_ms)
    except Exception as error:  # noqa: BLE001
        return Containment("error", semantics, f"{type(error).__name__}: {error}")
    if result.proven:
        return Containment("contained", semantics, "equal", "equal")
    mode = "subset" if semantics == "set" else "subbag"
    try:
        witness = find_difference(SHOP, case["q1"], case["q2"], mode=mode, trials=200)
    except CheckError as error:
        return Containment("unsupported", semantics, str(error))
    if witness is not None:
        return Containment("not_contained", semantics, "random search", "random-search", witness)
    return Containment("unknown", semantics, "no proof")


def run(case: dict, semantics: str, use_baseline: bool, timeout_ms: int) -> dict:
    start = time.time()
    try:
        if use_baseline:
            answer = baseline(case, semantics, timeout_ms)
        else:
            answer = check_containment(
                case["q1"], case["q2"], schema=SHOP.columns, constraints=prover_constraints(SHOP), semantics=semantics, database=SHOP, timeout_ms=timeout_ms
            )
    except Exception as error:  # noqa: BLE001 - reported, never scored as an answer
        return {"id": case["id"], "semantics": semantics, "status": "error", "reason": f"{type(error).__name__}: {error}", "seconds": time.time() - start}
    label = case[semantics]
    status = answer.status
    outcome = status
    if status == "not_contained":
        mode = "subset" if semantics == "set" else "subbag"
        confirmed = answer.witness is not None and replay(SHOP, answer.witness, case["q1"], case["q2"], mode=mode)
        if not confirmed:
            outcome = "error"
    wrong = (label == "not_contained" and outcome == "contained") or (label == "contained" and outcome == "not_contained")
    return {
        "id": case["id"],
        "family": case["family"],
        "semantics": semantics,
        "label": label,
        "status": outcome,
        "method": answer.method,
        "wrong": wrong,
        "reason": answer.reason,
        "seconds": round(time.time() - start, 3),
    }


def summarize(records: list[dict]) -> dict:
    out: dict = {}
    for key in ["all", "set", "bag", *sorted({r["family"] for r in records if "family" in r})]:
        part = [r for r in records if key in ("all", r.get("semantics"), r.get("family"))]
        statuses = Counter(r["status"] for r in part)
        decided = sum(1 for r in part if not r["wrong"] and ((r["label"] == "contained" and r["status"] == "contained") or (r["label"] == "not_contained" and r["status"] == "not_contained")))
        out[key] = {
            "items": len(part),
            "decided_correctly": decided,
            "proven": sum(1 for r in part if r["status"] == "contained" and not r["wrong"]),
            "refuted": sum(1 for r in part if r["status"] == "not_contained" and not r["wrong"]),
            "unknown": statuses.get("unknown", 0),
            "unsupported": statuses.get("unsupported", 0),
            "timeout": statuses.get("timeout", 0),
            "error": statuses.get("error", 0),
            "wrong": sum(1 for r in part if r["wrong"]),
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
    parser.add_argument("--id", action="append")
    args = parser.parse_args(argv)
    cases, held = load()
    if not args.all:
        cases = [c for c in cases if (c["family"] in held) == args.held_out]
    if args.id:
        cases = [c for c in cases if c["id"] in args.id]
    records = [run(c, s, args.baseline, args.timeout_ms) for c in cases for s in ("set", "bag")]
    summary = summarize(records)
    split = "all" if args.all else ("held-out" if args.held_out else "development")
    print(f"{'baseline' if args.baseline else 'containment'} on the {split} split: {len(cases)} cases, {len(records)} items")
    for key, value in summary.items():
        print(f"  {key}: {json.dumps(value)}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "records": records}, indent=1), encoding="utf-8")
    return 1 if summary["all"]["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
