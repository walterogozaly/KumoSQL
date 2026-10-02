"""Run the test suite in parallel with pytest-xdist: ``python tools/run_tests.py [--evals | --no-evals] [pytest args]``.

* no flag: the whole fast suite (``-m "not slow"``);
* ``--evals``: only the benchmark floors (tests marked ``eval``);
* ``--no-evals``: everything except the floors.

sqlglot runs compiled when ``sqlglotc`` (mypyc wheels, same version as ``sqlglot``) is installed, which is
about 10-15% faster on the prover tests; ``--install-compiled`` installs it. ``--pure`` runs the same suite
against a pure-Python copy of sqlglot even when ``sqlglotc`` is installed, so both builds can be checked.
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
    group.add_argument("--evals", action="store_true", help="only the benchmark floors")
    group.add_argument("--no-evals", action="store_true", help="everything except the benchmark floors")
    parser.add_argument("--pure", action="store_true", help="test against pure-Python sqlglot even if sqlglotc is installed")
    parser.add_argument("--install-compiled", action="store_true", help="pip install the sqlglotc that matches the installed sqlglot first")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="worker processes (default: all CPUs)")
    args, rest = parser.parse_known_args(argv)

    if args.install_compiled and not _compiled():
        from importlib.metadata import version

        subprocess.call([sys.executable, "-m", "pip", "install", f"sqlglotc=={version('sqlglot')}"])
    env = dict(os.environ)
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
    elif args.no_evals:
        marker = "not eval and not slow"
    command = [sys.executable, "-m", "pytest", "-m", marker, "-q", *rest]
    if args.jobs > 1:
        if importlib.util.find_spec("xdist") is None:
            print("pytest-xdist is not installed (pip install -e '.[dev]'); running serially", file=sys.stderr)
        else:
            command += ["-n", str(args.jobs), "--dist", "loadgroup"]
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
