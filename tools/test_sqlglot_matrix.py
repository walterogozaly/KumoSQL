"""Run parser and rewrite regressions against the supported compiled SQLGlot release.

Each version gets a temporary virtual environment, so matrix runs cannot alter
the caller's installed packages or the repository checkout.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_SQLGLOT_VERSIONS = ("30.21.0",)
REGRESSION_TESTS = (
    "tests/test_match_recognize.py",
    "tests/test_lift_subqueries.py",
    "tests/test_rule_registry.py",
    "tests/test_sqlx.py",
)


def _run(command: list[str], *, version: str) -> bool:
    print(f"[sqlglot {version}] + {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode == 0


def _python_in(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions",
        nargs="+",
        default=SUPPORTED_SQLGLOT_VERSIONS,
        choices=SUPPORTED_SQLGLOT_VERSIONS,
        help="exact sqlglot releases to test (default: %(default)s)",
    )
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        parser.error("the compatibility matrix requires Python 3.11 or newer")
    invalid = [
        version
        for version in args.versions
        if not re.fullmatch(r"\d+\.\d+\.\d+", version)
    ]
    if invalid:
        parser.error(f"versions must be exact x.y.z releases: {', '.join(invalid)}")
    if len(set(args.versions)) != len(args.versions):
        parser.error("version list contains duplicates")

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kumosql-sqlglot-matrix-") as temporary:
        temporary_root = Path(temporary)
        for version in args.versions:
            environment = temporary_root / f"sqlglot-{version}"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = str(_python_in(environment))
            install_ok = _run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    ".[dev]",
                    f"sqlglot=={version}",
                    f"sqlglotc=={version}",
                    "--only-binary",
                    "sqlglotc",
                ],
                version=version,
            )
            if not install_ok:
                failures.append(f"sqlglot {version}: environment setup failed")
                continue

            tests_ok = _run(
                [python, "-m", "pytest", "-q", *REGRESSION_TESTS],
                version=version,
            )
            if not tests_ok:
                failures.append(f"sqlglot {version}: regression tests failed")

    if failures:
        print("\nCompatibility matrix failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("\nCompatibility matrix passed for: " + ", ".join(args.versions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
