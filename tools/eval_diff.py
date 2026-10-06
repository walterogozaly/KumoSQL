"""Rerun every eval on a base commit and on this checkout, and show where their output differs.

    python tools/eval_diff.py                        # base origin/master, every eval
    python tools/eval_diff.py --only sqlsolver qed   # eval rows or commands containing a word
    python tools/eval_diff.py --skip targeted_data   # leave slow ones out
    python tools/eval_diff.py --only targeted-test-data --sample 25 --serial --timings timings.json
    python tools/eval_diff.py --list                 # print the commands and stop

Use it before merging a change that should not move any score (a refactor, a shared helper) or to see
exactly which evals a change moves. The commands are the ``command`` keys of benchmarks/results/*.json
(``--write-results`` is dropped, so no results file is rewritten; commands that need a local checkout,
such as ``<SQL-IQ checkout>`` or ``PATH``, are skipped). The base runs in a temporary git worktree; this
checkout runs as it is, uncommitted changes included. Timings are masked before comparing, so only
answers count. A row is comparable only when both commands exit successfully; failures and timeouts
are reported as uncomparable. Exit status is 1 for a difference or any uncomparable row.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"<[^>]+>|\bPATH\b")
_TIMING = [
    (re.compile(r"""(["'])([\w-]*(?:seconds|elapsed|time|wall|runtime|peak_mb|growth_mb|median_ms|p95_ms))\1: ?[0-9.]+"""), r"\1\2\1: T"),
    (re.compile(r"\b[0-9]+(\.[0-9]+)? ?(s|ms|seconds|MB)\b"), "T"),
    (re.compile(r"\b[0-9]{2}:[0-9]{2}:[0-9]{2}\b"), "HH:MM:SS"),
    # A summary row that ends in a bare elapsed-seconds column (``calcite 232 232 224 8 0 7 1245.0``): a name,
    # integer counts, then a decimal. Only that last column is masked; the counts stay in the comparison.
    (re.compile(r"(?m)^([A-Za-z][\w-]*(?: +[0-9]+){3,}) +[0-9]+\.[0-9]+[ \t]*$"), r"\1 T"),
]
_SAMPLE_TOOLS = {
    "bounded_bench.py": "--limit",
    "engine_suites.py": "--limit",
    "singh_bedathur_bench.py": "--sample",
    "targeted_data_bench.py": "--limit",
}
_SLOW_NAMES = re.compile(r"targeted|singh|duckdb-slt|sqlite-slt|bounded|conditional", re.I)


class RunResult(NamedTuple):
    returncode: int
    output: str
    seconds: float
    state: str = "finished"


def sampled_command(command: str, sample: int) -> tuple[str | None, str | None]:
    """Add a deterministic harness sample, or explain why the command cannot be sampled."""

    match = re.match(r"^python tools/([\w-]+\.py)(?:\s|$)", command)
    if not match or "&&" in command:
        return None, "no deterministic sample mode for this command"
    tool = match.group(1)
    if tool == "conditional_bench.py":
        option = "--limit" if re.search(r"\bverieql\b", command) else "--sample"
    else:
        option = _SAMPLE_TOOLS.get(tool)
    if option is None:
        return None, "no deterministic sample mode for this command"
    return f"{command} {option} {sample}", None


def commands(root: Path) -> dict[str, list[str]]:
    """Rerun command -> the results files that name it."""

    found: dict[str, list[str]] = {}
    for path in sorted((root / "benchmarks" / "results").glob("*.json")):
        command = json.loads(path.read_text(encoding="utf-8")).get("command", "")
        if not command or _PLACEHOLDER.search(command):
            continue
        command = re.sub(r"\s*--write-results\b", "", command)
        found.setdefault(command, []).append(path.stem)
    return found


def normalize(text: str) -> list[str]:
    for pattern, replacement in _TIMING:
        text = pattern.sub(replacement, text)
    return text.splitlines()


def _terminate_tree(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill a timed-out eval and every subprocess it started, then drain its pipes."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return proc.communicate()


def _command_with_current_python(command: str) -> str:
    """Use the interpreter running eval_diff so its paired runs share the same environment."""

    executable = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
    return re.sub(r"(?<![\w./-])python(?=\s+tools/)", lambda _: executable, command)


def run(tree: Path, command: str, timeout: int) -> RunResult:
    env = dict(os.environ, PYTHONPATH=str(tree / "src"), KUMOSQL_TIMING="0", PYTHONHASHSEED="0")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    proc = subprocess.Popen(
        _command_with_current_python(command),
        shell=True,
        cwd=tree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_tree(proc)
        elapsed = time.monotonic() - started
        tail = (stdout + stderr)[-2000:]
        detail = f"timed out after {timeout} s; process tree terminated"
        return RunResult(-1, detail + (f"\n{tail}" if tail.strip() else ""), elapsed, "timed out")
    elapsed = time.monotonic() - started
    output = stdout if proc.returncode == 0 else stdout + stderr[-2000:]
    return RunResult(proc.returncode, output, elapsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/master", help="commit to compare against (default origin/master)")
    parser.add_argument("--only", nargs="*", default=[], help="run only commands containing one of these words")
    parser.add_argument("--skip", nargs="*", default=[], help="leave out commands containing one of these words")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per ordinary command (default 3600)")
    parser.add_argument("--slow-timeout", type=int, default=7200, help="per-command cap for known slow evals in full mode (default 7200)")
    parser.add_argument("--sample", type=int, help="run a deterministic sample of supported slow eval harnesses")
    parser.add_argument("--sample-timeout", type=int, default=1800, help="same per-side cap in sample mode (default 1800)")
    parser.add_argument("--timings", type=Path, help="write per-command base/checkout durations and outcomes as JSON")
    parser.add_argument("--serial", action="store_true", help="run base and checkout one after the other, not side by side")
    parser.add_argument("--list", action="store_true", help="print the commands and stop")
    args = parser.parse_args(argv)

    if args.timeout <= 0 or args.slow_timeout <= 0 or args.sample_timeout <= 0:
        parser.error("timeouts must be positive")
    if args.sample is not None and args.sample <= 0:
        parser.error("--sample must be a positive case count")

    todo = {
        c: names for c, names in commands(ROOT).items()
        if (not args.only or any(w in c or any(w in n for n in names) for w in args.only))
        and not any(w in c or any(w in n for n in names) for w in args.skip)
    }
    if args.list:
        for command, names in todo.items():
            slow = bool(_SLOW_NAMES.search(", ".join(names)))
            sample_status = "sample supported" if sampled_command(command, args.sample or 1)[0] else "full run only"
            cap = args.sample_timeout if args.sample else args.slow_timeout if slow else args.timeout
            print(f"{', '.join(names)} [{sample_status}; cap {cap}s]: {command}")
        return 0
    differ = 0
    uncomparable = 0
    comparisons = 0
    timing_rows = []
    with tempfile.TemporaryDirectory(prefix="kumosql-eval-base-") as folder:
        base = Path(folder) / "base"
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(base), args.base], cwd=ROOT, check=True)
        try:
            with ThreadPoolExecutor(max_workers=1 if args.serial else 2) as pool:
                for command, names in todo.items():
                    label = ", ".join(names)
                    run_command, sample_error = (command, None)
                    if args.sample:
                        run_command, sample_error = sampled_command(command, args.sample)
                    slow = bool(_SLOW_NAMES.search(label))
                    timeout = args.sample_timeout if args.sample else args.slow_timeout if slow else args.timeout
                    if sample_error:
                        uncomparable += 1
                        print(f"UNCOMPARED     {label}: {sample_error}", flush=True)
                        timing_rows.append({"evals": names, "command": command, "state": "sample unsupported"})
                        continue
                    old = pool.submit(run, base, run_command, timeout)
                    new = pool.submit(run, ROOT, run_command, timeout)
                    old_result, new_result = old.result(), new.result()
                    elapsed = f"base {old_result.seconds:.1f}s; checkout {new_result.seconds:.1f}s"
                    timing_rows.append({
                        "evals": names,
                        "command": run_command,
                        "base_seconds": round(old_result.seconds, 3),
                        "checkout_seconds": round(new_result.seconds, 3),
                        "base_state": old_result.state,
                        "checkout_state": new_result.state,
                        "base_exit": old_result.returncode,
                        "checkout_exit": new_result.returncode,
                    })
                    if old_result.returncode != 0 or new_result.returncode != 0:
                        uncomparable += 1
                        print(
                            f"UNCOMPARED     {label} ({elapsed}; base {old_result.state}/exit {old_result.returncode}, "
                            f"checkout {new_result.state}/exit {new_result.returncode})",
                            flush=True,
                        )
                        continue
                    comparisons += 1
                    a, b = normalize(old_result.output), normalize(new_result.output)
                    if a == b:
                        print(f"same           {label} ({elapsed})", flush=True)
                        continue
                    differ += 1
                    print(f"DIFFERS        {label} ({elapsed}): {run_command}", flush=True)
                    for line in list(difflib.unified_diff(a, b, "base", "checkout", n=0, lineterm=""))[:40]:
                        print(f"    {line}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base)], cwd=ROOT, check=False)
    print(f"{comparisons} comparisons completed; {differ} differ; {uncomparable} could not be compared")
    if args.timings:
        args.timings.parent.mkdir(parents=True, exist_ok=True)
        args.timings.write_text(json.dumps({"base": args.base, "sample": args.sample, "runs": timing_rows}, indent=2) + "\n", encoding="utf-8")
    return 1 if differ or uncomparable else 0


if __name__ == "__main__":
    sys.exit(main())
