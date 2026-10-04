"""Run table-minimization cases one per process, each with a memory cap and a wall-clock limit.

The real-pipeline cases (Fivetran packages of 18 to 49 tables) can use gigabytes in the search, so each runs
in its own process. A case that exceeds the cap or the limit counts as an error (not improved, never wrong), with
the reason recorded. The summary is the harness's own (``minimization_bench.summarise``).

    python tools/minimization_isolated.py --cases sourced --split dev --jobs 2 --json out.json
    python tools/minimization_isolated.py --only fivetran-klaviyo --memory-gb 5 --timeout 300
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minimization_bench as mb  # noqa: E402
import minimization_cases as mc  # noqa: E402


def _child(case_id: str, spec: str, out: Path, databases: int, prove: bool, timeout_ms: int, files) -> None:
    case = next(c for c in mc.load_cases(files, None) if c["id"] == case_id)
    result = mb._run_one((case, spec, databases, prove, timeout_ms))
    out.write_text(json.dumps(asdict(result)), encoding="utf-8")


def run_isolated(case: dict, spec: str, memory_gb: float, timeout: float, databases: int, prove: bool,
                 timeout_ms: int, files) -> mb.CaseResult:
    failed = mb.CaseResult(case["id"], case["split"], list(case.get("families", [])), case.get("reference_kind", "generated"),
                           original=case["original"]["complexity"]["score"],
                           reference=case["reference"]["complexity"]["score"], tables=len(case["tables"]))
    cap = int(memory_gb * 2**30)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.json"
        command = [sys.executable, __file__, "--child", case["id"], "--minimizer", spec, "--json", str(out),
                   "--databases", str(databases), "--timeout-ms", str(timeout_ms)]
        command += [] if prove else ["--no-prove"]
        for f in files or []:
            command += ["--file", str(f)]
        started = time.time()
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                  preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_AS, (cap, cap)))
        except subprocess.TimeoutExpired:
            failed.seconds = time.time() - started
            failed.reason = f"time: over {timeout:.0f} s"
            return failed
        failed.seconds = time.time() - started
        if proc.returncode == 0 and out.exists():
            return mb.CaseResult(**json.loads(out.read_text()))
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        memory = "MemoryError" in (proc.stderr or "") or proc.returncode in (-9, -11)
        failed.reason = ("memory: " if memory else "crashed: ") + tail[0][:200]
        return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minimizer", default="minimizer")
    parser.add_argument("--split", default="dev", choices=["dev", "held_out", "all"])
    parser.add_argument("--file", type=Path, action="append")
    parser.add_argument("--cases", choices=["own", "sourced", "all"])
    parser.add_argument("--only", help="run case ids containing this text")
    parser.add_argument("--databases", type=int, default=mb.DATABASES)
    parser.add_argument("--no-prove", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--memory-gb", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per case")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--child", help="internal: run one case by exact id and write its result to --json")
    args = parser.parse_args(argv)
    if args.child:
        _child(args.child, args.minimizer, args.json, args.databases, not args.no_prove, args.timeout_ms, args.file)
        return 0
    cases = mc.load_cases(args.file, None if args.split == "all" else args.split)
    kind = args.cases or ("all" if args.file else "own")
    if kind != "all":
        cases = [c for c in cases if mc.sourced(c) == (kind == "sourced")]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]

    def one(case):
        result = run_isolated(case, args.minimizer, args.memory_gb, args.timeout, args.databases, not args.no_prove,
                              args.timeout_ms, args.file)
        print(f"{result.id} {result.status} {result.original}->{result.output} {result.seconds:.0f}s {result.reason[:80]}",
              flush=True)
        return result

    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(one, cases))
    summary = mb.summarise(results, args.minimizer)
    print(json.dumps({k: v for k, v in summary.items() if k != "by_family"}, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=1),
                             encoding="utf-8")
    return 1 if summary["correctness"]["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
