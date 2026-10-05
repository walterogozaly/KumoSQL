#!/usr/bin/env python3
"""Read the Dell runner's results from the results branch (no credentials needed; the repo is public).

    python read_results.py                 # the rolling table
    python read_results.py --failed        # the newest result files with failures, with test ids
    python read_results.py --sha 04af1a9   # results for one commit
    python read_results.py --job candidate-7

Fetches into a throwaway clone next to this file; safe to run from a cloud thread.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

URL = "https://github.com/walterogozaly/KumoSQL.git"
BRANCH = "dell-runner/results"
CLONE = Path(__file__).resolve().parent / ".results-clone"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--sha")
    parser.add_argument("--job")
    args = parser.parse_args()
    if not (CLONE / ".git").exists():
        run("clone", "-q", "--depth", "50", "--branch", BRANCH, "--single-branch", URL, str(CLONE))
    else:
        run("fetch", "-q", "--depth", "50", "origin", BRANCH, cwd=CLONE)
        run("reset", "-q", "--hard", "FETCH_HEAD", cwd=CLONE)
    if not (args.failed or args.sha or args.job):
        print((CLONE / "latest.md").read_text(encoding="utf-8"))
        return 0
    for path in sorted((CLONE / "results").glob("*.json"), reverse=True):
        record = json.loads(path.read_text(encoding="utf-8"))
        if args.sha and not record["sha"].startswith(args.sha):
            continue
        if args.job and record["job"] != args.job:
            continue
        bad = record["status"] != "finished" or record["failed"] or record["errors"]
        if args.failed and not bad:
            continue
        print(f"{path.name}: {record['status']} exit {record['exit']} passed {record['passed']}/{record['tests']} failed {len(record['failed'])} errors {len(record['errors'])}")
        for name in record["failed"] + record["errors"]:
            print("   ", name)
        if bad and record.get("tail"):
            print("   --- tail ---\n" + record["tail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
