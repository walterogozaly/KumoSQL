"""Rerun every eval on a base commit and on this checkout, and show where their output differs.

    python tools/eval_diff.py                        # base origin/master, every eval
    python tools/eval_diff.py --only sqlsolver qed   # evals whose command contains a word
    python tools/eval_diff.py --skip targeted_data   # leave slow ones out
    python tools/eval_diff.py --list                 # print the commands and stop

Use it before merging a change that should not move any score (a refactor, a shared helper) or to see
exactly which evals a change moves. The commands are the ``command`` keys of benchmarks/results/*.json
(``--write-results`` is dropped, so no results file is rewritten; commands that need a local checkout,
such as ``<SQL-IQ checkout>`` or ``PATH``, are skipped). The base runs in a temporary git worktree; this
checkout runs as it is, uncommitted changes included. Timings are masked before comparing, so only
answers count. Exit status 1 when any eval differs or fails on one side only.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"<[^>]+>|\bPATH\b")
_TIMING = [
    (re.compile(r'"(seconds|elapsed|time|wall|runtime|peak_mb)[a-z_]*": ?[0-9.]+'), r'"\1": T'),
    (re.compile(r"\b[0-9]+(\.[0-9]+)? ?(s|ms|seconds|MB)\b"), "T"),
    (re.compile(r"\b[0-9]{2}:[0-9]{2}:[0-9]{2}\b"), "HH:MM:SS"),
    # A summary row that ends in a bare elapsed-seconds column (``calcite 232 232 224 8 0 7 1245.0``): a name,
    # integer counts, then a decimal. Only that last column is masked; the counts stay in the comparison.
    (re.compile(r"(?m)^([A-Za-z][\w-]*(?: +[0-9]+){3,}) +[0-9]+\.[0-9]+[ \t]*$"), r"\1 T"),
]


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


def run(tree: Path, command: str, timeout: int) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(tree / "src"), KUMOSQL_TIMING="0", PYTHONHASHSEED="0")
    try:
        done = subprocess.run(command, shell=True, cwd=tree, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout} s"
    return done.returncode, done.stdout if done.returncode == 0 else done.stdout + done.stderr[-2000:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/master", help="commit to compare against (default origin/master)")
    parser.add_argument("--only", nargs="*", default=[], help="run only commands containing one of these words")
    parser.add_argument("--skip", nargs="*", default=[], help="leave out commands containing one of these words")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per command (default 3600)")
    parser.add_argument("--serial", action="store_true", help="run base and checkout one after the other, not side by side")
    parser.add_argument("--list", action="store_true", help="print the commands and stop")
    args = parser.parse_args(argv)

    todo = {
        c: names for c, names in commands(ROOT).items()
        if (not args.only or any(w in c for w in args.only)) and not any(w in c for w in args.skip)
    }
    if args.list:
        for command, names in todo.items():
            print(f"{', '.join(names)}: {command}")
        return 0
    differ = 0
    with tempfile.TemporaryDirectory(prefix="kumosql-eval-base-") as folder:
        base = Path(folder) / "base"
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(base), args.base], cwd=ROOT, check=True)
        try:
            with ThreadPoolExecutor(max_workers=1 if args.serial else 2) as pool:
                for command, names in todo.items():
                    old, new = pool.submit(run, base, command, args.timeout), pool.submit(run, ROOT, command, args.timeout)
                    (old_code, old_out), (new_code, new_out) = old.result(), new.result()
                    label = ", ".join(names)
                    if old_code != 0 and new_code != 0:
                        print(f"FAILS ON BOTH  {label}: {command}\n    {old_out.strip().splitlines()[-1:]}", flush=True)
                        continue
                    a, b = normalize(old_out), normalize(new_out)
                    if old_code == new_code and a == b:
                        print(f"same           {label}", flush=True)
                        continue
                    differ += 1
                    print(f"DIFFERS        {label}: {command} (exit {old_code} -> {new_code})", flush=True)
                    for line in list(difflib.unified_diff(a, b, "base", "checkout", n=0, lineterm=""))[:40]:
                        print(f"    {line}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base)], cwd=ROOT, check=False)
    print(f"{len(todo) - differ} of {len(todo)} commands agree with {args.base}")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
