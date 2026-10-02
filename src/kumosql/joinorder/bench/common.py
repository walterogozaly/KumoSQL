"""Shared helpers for the join-order and cardinality benchmarks (dev tooling)."""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from typing import Any, Callable, Iterable


def data_dir() -> str:
    """Where benchmark data and caches live (outside git)."""
    path = os.environ.get("KUMOSQL_BENCH_DATA", os.path.join(os.path.expanduser("~"), ".kumosql-bench"))
    os.makedirs(path, exist_ok=True)
    return path


def q_error(est: float, true: float) -> float:
    est, true = max(est, 1.0), max(true, 1.0)
    return max(est / true, true / est)


def percentiles(values: Iterable[float], ps: tuple[int, ...] = (50, 90, 95, 99)) -> dict[str, float]:
    vals = sorted(values)
    if not vals:
        return {}
    out = {}
    for p in ps:
        idx = min(len(vals) - 1, max(0, math.ceil(p / 100 * len(vals)) - 1))
        out[f"p{p}"] = vals[idx]
    out["max"] = vals[-1]
    out["mean"] = statistics.fmean(vals)
    return out


def cached_json(path: str, build: Callable[[], Any]) -> Any:
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    value = build()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(value, fh)
    os.replace(tmp, path)
    return value


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def psql_explain_rows(psql: list[str], sqls: list[str]) -> list[float]:
    """Postgres' own row estimate for each query's input to its top aggregate.

    ``psql`` is the command prefix, e.g. ``["psql", "-h", "/tmp", "-p", "5433", "-d", "stats"]``.
    """
    import subprocess

    script = "\n".join("EXPLAIN (FORMAT JSON) " + s.strip().rstrip(";") + ";" for s in sqls)
    out = subprocess.run(psql + ["-At", "-v", "ON_ERROR_STOP=1"], input=script,
                         capture_output=True, text=True, check=True).stdout
    rows, buf, depth = [], [], 0
    for line in out.splitlines():
        buf.append(line)
        depth += line.count("[") - line.count("]")
        if depth == 0 and buf:
            text = "\n".join(buf).strip()
            buf = []
            if not text:
                continue
            plan = json.loads(text)[0]["Plan"]
            while plan.get("Node Type") in ("Aggregate", "Gather", "Gather Merge", "Sort", "Limit") and plan.get("Plans"):
                plan = plan["Plans"][0]
            rows.append(float(plan["Plan Rows"]))
    if len(rows) != len(sqls):
        raise RuntimeError(f"expected {len(sqls)} plans, parsed {len(rows)}")
    return rows
