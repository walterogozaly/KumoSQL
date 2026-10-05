"""Run the test suite in parallel with pytest-xdist: ``python tools/run_tests.py [--evals | --no-evals] [pytest args]``.

* no flag: the whole fast suite (``-m "not slow"``);
* ``--evals``: only the benchmark floors (tests marked ``eval``);
* ``--no-evals``: everything except the floors;
* ``--routine``: quick non-eval regression suite; run affected evals separately;
* ``--quick``: skip the slow tier (a few minutes instead of the whole run; ``tests/order.json`` lists the slow tests);
* ``--label TEXT`` and ``--target PATH`` (repeatable): what this run is for, written to the shared test history
  (``tools/test_history.py``) so a later report can tell a failure inside your targets from one outside them. Without
  ``--target`` the test files your branch changed are the targets.

sqlglot runs compiled when ``sqlglotc`` (mypyc wheels, same version as ``sqlglot``) is installed, which is
about 10-15% faster on the prover tests; ``--install-compiled`` installs it. ``--pure`` runs the same suite
against a pure-Python copy for focused diagnostics. Routine CI and Dell full-suite runs use compiled
SQLGlot 30.21.0 once per revision; older versions are no longer supported.
KumoSQL itself is always pure Python.

Workers default to the number of CPUs (``-j N`` to change it, ``-j 1`` for a plain serial run). Any other
argument goes to pytest, e.g. ``python tools/run_tests.py tests/test_tags.py -k background``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _sqlglot_dir() -> Path:
    spec = importlib.util.find_spec("sqlglot")
    assert spec and spec.submodule_search_locations
    return Path(next(iter(spec.submodule_search_locations)))


def _compiled() -> bool:
    return any(_sqlglot_dir().rglob("*.so")) or any(_sqlglot_dir().rglob("*.pyd"))


def _pure_copy() -> Path:
    """A directory holding sqlglot's .py files only (no compiled extensions), for PYTHONPATH."""

    from importlib.metadata import version

    target = Path(tempfile.gettempdir()) / f"kumosql-pure-sqlglot-{version('sqlglot')}"
    if not (target / "sqlglot").is_dir():
        staging = target.with_name(target.name + f".{os.getpid()}")
        shutil.copytree(_sqlglot_dir(), staging / "sqlglot", ignore=shutil.ignore_patterns("*.so", "*.pyd", "__pycache__"))
        try:
            staging.rename(target)
        except OSError:  # another run made it first
            shutil.rmtree(staging, ignore_errors=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--routine", action="store_true", help="quick non-eval regression suite; affected evals remain required")
    group.add_argument("--evals", action="store_true", help="only the benchmark floors")
    group.add_argument("--no-evals", action="store_true", help="everything except the benchmark floors")
    parser.add_argument("--quick", action="store_true", help="skip the slow tier of tests (see tests/order.json)")
    parser.add_argument("--label", help="what this run is for (default: the branch name); recorded in the test history")
    parser.add_argument("--target", action="append", default=[], help="a test file or test id this run is aiming at (repeatable); recorded in the test history")
    parser.add_argument("--pure", action="store_true", help="test against pure-Python sqlglot even if sqlglotc is installed")
    parser.add_argument("--install-compiled", action="store_true", help="pip install the sqlglotc that matches the installed sqlglot first")
    parser.add_argument("--eval-cache", metavar="PATH", help="local SQLSolver sample-execution cache directory; 'off' forces fresh checks")
    parser.add_argument("--eval-jobs", type=int, help="processes for the large MV workload eval (default: spare CPUs, capped at 3; 1 forces serial)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="worker processes (default: all CPUs)")
    args, rest = parser.parse_known_args(argv)

    if args.install_compiled and not _compiled():
        from importlib.metadata import version

        subprocess.call([sys.executable, "-m", "pip", "install", f"sqlglotc=={version('sqlglot')}"])
    env = dict(os.environ)
    if args.eval_jobs is not None and args.eval_jobs < 1:
        parser.error("--eval-jobs must be positive")
    spare = max(1, (os.cpu_count() or 1) - max(1, args.jobs) + 1)
    env["KUMOSQL_EVAL_JOBS"] = str(args.eval_jobs or min(3, spare))
    if args.eval_cache is not None:
        env["KUMOSQL_EVAL_CACHE"] = args.eval_cache
    if args.label:
        env["KUMOSQL_TASK"] = args.label
    if args.target:
        env["KUMOSQL_TEST_TARGETS"] = ",".join(args.target)
    if args.pure:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_pure_copy()), env.get("PYTHONPATH")]))
        print("sqlglot: pure Python", file=sys.stderr)
    elif _compiled():
        print("sqlglot: compiled (sqlglotc)", file=sys.stderr)
    else:
        print("sqlglot: pure Python (python tools/run_tests.py --install-compiled makes the suite ~10-15% faster)", file=sys.stderr)

    marker = "not slow"
    if args.evals:
        marker = "eval and not slow"
    elif args.no_evals or args.routine:
        marker = "not eval and not slow"
    command = [sys.executable, "-m", "pytest", "-m", marker, "-q", *(["--quick"] if args.quick or args.routine else []), *rest]
    if args.jobs > 1:
        if importlib.util.find_spec("xdist") is None:
            print("pytest-xdist is not installed (pip install -e '.[dev]'); running serially", file=sys.stderr)
        else:
            command += ["-n", str(args.jobs), "--dist", "loadgroup"]
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
