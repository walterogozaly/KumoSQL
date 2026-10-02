"""Incremental-versus-full-refresh correctness eval (Dataform incremental tables).

Each case in ``tests/fixtures/incremental/*.json`` is a SQLX incremental model,
its source tables, a *contract* (the kinds of source change allowed), the
author's label for that contract (``safe`` or ``diverges``) and a scripted
change sequence that exercises it. Two things are scored, separately:

* **Harness fidelity.** The script is replayed in the DuckDB simulator of
  Dataform's run cycle; the outcome must match the label (a ``diverges`` case
  must diverge at the stated batch, a ``safe`` case must agree throughout).
* **Detection.** ``check_incremental`` gets only the model and the contract,
  never the script. ``proven`` (a proof rule) on a ``diverges`` case is a false
  proof; ``refuted`` (a minimised counterexample that replays) on a ``safe``
  case is a false alarm. Both count as wrong and must stay 0. ``unknown`` is a
  miss, not an error.

    python tools/incremental_bench.py            # all splits
    python tools/incremental_bench.py --split dev
    python tools/incremental_bench.py --json
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kumosql.incremental import (  # noqa: E402
    IncrementalError,
    IncrementalModel,
    SourceTable,
    check_incremental,
    first_divergence,
    parse_incremental_sqlx,
    replay,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "incremental"
SOURCE_FILES = ("boundary_cases.json",)


def load_cases(split: str = "all") -> list[dict]:
    cases: list[dict] = []
    for name in SOURCE_FILES:
        path = FIXTURES / name
        if path.exists():
            cases += json.loads(path.read_text(encoding="utf-8"))["cases"]
    return [c for c in cases if split == "all" or c["split"] == split]


def build(case: dict):
    model = parse_incremental_sqlx(case["sqlx"], case["target"])
    if case.get("ignore_columns"):
        model = replace(model, ignore_columns=tuple(case["ignore_columns"]))
    sources = {
        name: SourceTable(spec["columns"], tuple(spec.get("key", ())), spec.get("time_column"))
        for name, spec in case["sources"].items()
    }
    return model, sources


def fidelity(case: dict) -> tuple[bool, str]:
    """Does the scripted workload behave the way the label says?"""

    model, sources = build(case)
    script = case["script"]
    try:
        divergence = first_divergence(replay(model, sources, script["initial"], script["batches"]))
    except IncrementalError as exc:
        return False, f"script failed: {exc}"
    if case["label"] == "safe":
        return divergence is None, "agrees" if divergence is None else f"diverged at batch {divergence.index}"
    if divergence is None:
        return False, "never diverged"
    expected = script.get("diverges_at")
    return expected in (None, divergence.index), f"diverged at batch {divergence.index}"


def detect(case: dict, seeds: int) -> dict:
    model, sources = build(case)
    contract = case["contract"]
    start = time.monotonic()
    verdict = check_incremental(
        model, sources, contract["kinds"], seeds=seeds, tables=tuple(contract["tables"]) if contract.get("tables") else None
    )
    elapsed = time.monotonic() - start
    outcome = {"safe": "proven", "diverges": "refuted"}.get(verdict.outcome, verdict.outcome)
    label = case["label"]
    wrong = (verdict.outcome == "safe" and label == "diverges") or (verdict.outcome == "diverges" and label == "safe")
    if verdict.counterexample is not None and not wrong:
        again = first_divergence(replay(model, sources, verdict.counterexample.initial, verdict.counterexample.batches))
        wrong = again is None  # a counterexample that does not replay is a wrong answer
    return {
        "id": case["id"],
        "family": case["family"],
        "split": case["split"],
        "label": label,
        "outcome": outcome,
        "rule": verdict.rule,
        "wrong": wrong,
        "seconds": round(elapsed, 2),
        "cex_size": verdict.counterexample.size if verdict.counterexample else None,
    }


def run(split: str = "all", seeds: int = 60) -> dict:
    rows, bad_fidelity = [], []
    for case in load_cases(split):
        ok, why = fidelity(case)
        if not ok:
            bad_fidelity.append((case["id"], why))
        try:
            rows.append(detect(case, seeds))
        except Exception as exc:  # an error is its own outcome, never a pass
            rows.append(
                {"id": case["id"], "family": case["family"], "split": case["split"], "label": case["label"], "outcome": "error", "rule": str(exc)[:80], "wrong": False, "seconds": 0, "cex_size": None}
            )
    coverage = {k: sum(r["outcome"] == k for r in rows) for k in ("proven", "refuted", "unknown", "unsupported", "timeout", "error")}
    sizes = [r["cex_size"] for r in rows if r["cex_size"]]
    return {
        "total": len(rows),
        "coverage": coverage,
        "decided": coverage["proven"] + coverage["refuted"],
        "wrong": [r["id"] for r in rows if r["wrong"]],
        "fidelity_failures": bad_fidelity,
        "mean_counterexample_statements": round(sum(sizes) / len(sizes), 1) if sizes else None,
        "seconds": round(sum(r["seconds"] for r in rows), 1),
        "rows": rows,
    }


def pgivm_build(case: dict):
    """Model, sources and script of an adapted pg_ivm workload (see tools/incremental_pgivm.py)."""

    adapted = case["adapted"]
    load = adapted["load_column"]
    sources = {
        name: SourceTable({**spec["columns"], load: "TIMESTAMP"}, (), load, {load: adapted["load_default"]})
        for name, spec in adapted["tables"].items()
    }
    target = "view_inc"
    model = IncrementalModel(
        target,
        adapted["full_sql"],
        adapted["incremental_sql"].replace("__SELF__", target),
        dialect=adapted["dialect"],
    )
    return model, sources, adapted["initial"], adapted["batches"]


def pgivm_kinds(batches: list[list[str]]) -> list[str]:
    """Change kinds present in a workload: what a contract for it has to allow."""

    kinds = {"empty"}
    for batch in batches:
        for statement in batch:
            word = statement.split(None, 1)[0].upper()
            kinds.add({"INSERT": "insert_new", "UPDATE": "update", "DELETE": "delete"}[word])
    return sorted(kinds)


def run_pgivm(seeds: int = 30, limit: int | None = None) -> dict:
    """Adapted pg_ivm workloads: replay the script, then ask the checker without it."""

    path = FIXTURES / "pgivm_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"] if path.exists() else []
    out = {"total": len(cases), "unsupported_extraction": 0, "script_error": 0, "script_agrees": 0, "script_diverges": 0,
           "coverage": {k: 0 for k in ("proven", "refuted", "unknown", "unsupported", "timeout", "error")}, "wrong": [], "rows": []}
    for case in cases:
        if "adapted" not in case:
            out["unsupported_extraction"] += 1
            continue
        if limit is not None and out["script_diverges"] >= limit:
            break
        model, sources, initial, batches = pgivm_build(case)
        try:
            divergence = first_divergence(replay(model, sources, initial, batches))
        except IncrementalError:
            out["script_error"] += 1
            continue
        if divergence is None:
            out["script_agrees"] += 1
            continue
        out["script_diverges"] += 1
        verdict = check_incremental(model, sources, pgivm_kinds(batches), seeds=seeds, batches=4, time_limit=20)
        outcome = {"safe": "proven", "diverges": "refuted"}.get(verdict.outcome, verdict.outcome)
        wrong = verdict.outcome == "safe"
        if verdict.counterexample is not None:
            wrong = wrong or first_divergence(replay(model, sources, verdict.counterexample.initial, verdict.counterexample.batches)) is None
        out["coverage"][outcome] = out["coverage"].get(outcome, 0) + 1
        if wrong:
            out["wrong"].append(case["id"])
        out["rows"].append({"id": case["id"], "outcome": outcome, "rule": verdict.rule})
    return out


def run_pgivm_heldout(seeds: int = 30, workload_seed: int = 9001) -> dict:
    """Held-out pg_ivm track: fresh random workloads on the adapted views, never seen while tuning.

    Each distinct adapted view gets one new random insert/update/delete script (generator
    seed ``workload_seed``, unrelated to the checker's own seeds). Scripts that drift are
    scored once at the frozen ``seeds``.
    """

    import random

    from kumosql.incremental import _Generator, _insert

    path = FIXTURES / "pgivm_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    seen, out = set(), {"views": 0, "script_agrees": 0, "script_diverges": 0, "script_error": 0,
                        "coverage": {k: 0 for k in ("proven", "refuted", "unknown", "unsupported", "timeout", "error")}, "wrong": []}
    for case in cases:
        if "adapted" not in case or case["adapted"]["full_sql"] in seen:
            continue
        seen.add(case["adapted"]["full_sql"])
        out["views"] += 1
        model, sources, _, _ = pgivm_build(case)
        rng = random.Random(f"{workload_seed}:{case['adapted']['full_sql']}")
        gen = _Generator(sources, frozenset({"insert_new", "update", "delete", "empty"}), rng)
        initial = []
        for name in sorted(sources):
            for _ in range(rng.randint(1, 2)):
                gen.next_hour = rng.randint(0, 2)
                row = gen._fresh(name, None)
                initial.append(_insert(name, sources[name], row))
                gen.rows[name].append(row)
        plan = [gen.batch() for _ in range(4)]
        try:
            divergence = first_divergence(replay(model, sources, initial, plan))
        except IncrementalError:
            out["script_error"] += 1
            continue
        if divergence is None:
            out["script_agrees"] += 1
            continue
        out["script_diverges"] += 1
        verdict = check_incremental(model, sources, pgivm_kinds(plan), seeds=seeds, batches=4, time_limit=20)
        outcome = {"safe": "proven", "diverges": "refuted"}.get(verdict.outcome, verdict.outcome)
        out["coverage"][outcome] += 1
        if verdict.outcome == "safe" or (verdict.counterexample is not None and first_divergence(
                replay(model, sources, verdict.counterexample.initial, verdict.counterexample.batches)) is None):
            out["wrong"].append(case["id"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("all", "dev", "held_out"), default="all")
    parser.add_argument("--seeds", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--track", choices=("boundary", "pgivm", "pgivm-heldout"), default="boundary")
    args = parser.parse_args()
    if args.track == "pgivm-heldout":
        print(run_pgivm_heldout(args.seeds))
        return
    if args.track == "pgivm":
        result = run_pgivm(args.seeds)
        print(json.dumps(result, indent=1) if args.json else {k: v for k, v in result.items() if k != "rows"})
        return
    result = run(args.split, args.seeds)
    if args.json:
        print(json.dumps(result, indent=1, default=str))
        return
    for r in result["rows"]:
        flag = "WRONG" if r["wrong"] else ""
        print(f"{r['outcome']:10} label={r['label']:9} {r['id']:44} {r['rule'][:34]:34} {flag}")
    c = result["coverage"]
    print(
        f"\n{result['decided']}/{result['total']} decided ({c['proven']} proven safe, {c['refuted']} refuted with a replayable counterexample), "
        f"{c['unknown']} unknown, {c['unsupported']} unsupported, {c['timeout']} timeout, {c['error']} error; "
        f"{len(result['wrong'])} wrong; fidelity failures {len(result['fidelity_failures'])}; {result['seconds']} s"
    )
    for item in result["fidelity_failures"]:
        print("fidelity:", *item)
    for item in result["wrong"]:
        print("WRONG:", item)


if __name__ == "__main__":
    main()
