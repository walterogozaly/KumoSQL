"""Run the test suite in parallel with pytest-xdist: ``python tools/run_tests.py [--evals | --no-evals] [pytest args]``.

* no flag: the whole fast suite (``-m "not slow"``);
* ``--evals``: only the benchmark floors (tests marked ``eval``);
* ``--no-evals``: everything except the floors.

Workers default to the number of CPUs (``-j N`` to change it, ``-j 1`` for a plain serial run). Any other
argument goes to pytest, e.g. ``python tools/run_tests.py tests/test_tags.py -k background``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--evals", action="store_true", help="only the benchmark floors")
    group.add_argument("--no-evals", action="store_true", help="everything except the benchmark floors")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="worker processes (default: all CPUs)")
    args, rest = parser.parse_known_args(argv)

    marker = "not slow"
    if args.evals:
        marker = "eval and not slow"
    elif args.no_evals:
        marker = "not eval and not slow"
    command = [sys.executable, "-m", "pytest", "-m", marker, "-q", *rest]
    if args.jobs > 1:
        if importlib.util.find_spec("xdist") is None:
            print("pytest-xdist is not installed (pip install -e '.[dev]'); running serially", file=sys.stderr)
        else:
            command += ["-n", str(args.jobs), "--dist", "loadgroup"]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
