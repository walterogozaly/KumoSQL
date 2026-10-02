"""Helpers shared by the eval scripts in tools/.

Each eval owns ``benchmarks/results/<eval>.json`` (format in benchmarks/README.md), and the README
scoreboard is generated from those files by ``tools/scoreboard.py``. Import from a script with::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bench_common import quiet, today, write_results
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "benchmarks" / "results"


def quiet() -> None:
    """Command-line runs print results, not per-stage timings or sqlglot warnings.

    Call it from ``main`` rather than at import, so tests that import a bench are unaffected.
    """

    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


def today() -> str:
    """The ``date`` a results file records: the day the numbers were measured."""

    return datetime.date.today().isoformat()


def write_results(name: str, row: dict, *, scoreboard: bool = True) -> Path:
    """Write ``benchmarks/results/<name>.json`` and regenerate the README scoreboard from it."""

    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    if scoreboard:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import scoreboard as board

        board.main([])
    return path
