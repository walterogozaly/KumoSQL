"""Run open-source BigQuery projects (real code, not fixtures written for KumoSQL) through every KumoSQL stage.

The projects are copied, at pinned commits and with their licences, into ``tests/fixtures/bq_corpora/``
(``python tools/fetch_bq_corpora.py``; the list is ``sources.json`` there). Each project is loaded whole, as a user
would load it, and scored on:

* correctness: no crash in loading, cleanup or formatting; no cleanup or format change accepted without a proof; no
  backticked name changed; every literal ``${ref("x")}`` and config dependency is an edge in the graph;
* coverage: statements read, columns traced, files the cleanup rules and formatter could act on, and the gaps the
  loader reports, by kind.

A fifth of the files (by SHA-1 of ``project/path``) is also scored apart as a held-out split.

    python tools/bq_corpus_bench.py [--json out.json] [--write-results]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from bq_syntax_coverage import FAIL, PASS, expected_dependencies, stage_cleanup, stage_format  # noqa: E402

from kumosql.pipeline import load_sqlx_project  # noqa: E402

CORPORA = ROOT / "tests" / "fixtures" / "bq_corpora"
RESULTS = ROOT / "benchmarks" / "results" / "bq-real-corpora.json"


def held_out(project: str, relative: str) -> bool:
    """A fifth of the files, by SHA-1 of ``project/path``, scored apart: fixes are not tuned on them."""

    return int(hashlib.sha1(f"{project}/{relative}".encode()).hexdigest(), 16) % 5 == 0


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}"


def run_project(root: Path, *, stages: bool = True) -> dict:
    """The scores of one project folder. ``stages=False`` loads and traces it but runs no cleanup or format."""

    files = sorted(p for p in root.rglob("*") if p.suffix in (".sql", ".sqlx"))
    out: dict = {"files": len(files), "failures": [], "gaps": {}, "stages": {}}
    started = time.perf_counter()
    try:
        pipeline = load_sqlx_project(root)
    except Exception as exc:  # noqa: BLE001
        out["failures"].append(f"load: {_error(exc)}")
        return out
    out["load_seconds"] = round(time.perf_counter() - started, 2)
    coverage = pipeline.coverage()
    for key in ("assets_total", "assets_analyzed", "statements_total", "statements_matched", "columns_total", "columns_traced"):
        out[key] = coverage.get(key)
    out["gaps"] = dict(sorted(Counter(gap["code"] for gap in pipeline.completeness().get("gaps", [])).items()))

    by_path = {(model.path or "").replace("\\", "/"): model for model in pipeline.models.values()}
    gapped = {gap["asset"] for gap in pipeline.completeness().get("gaps", []) if gap.get("blocking")}
    deps = {"expected": 0, "found": 0}
    stages: dict[str, Counter] = {"cleanup": Counter(), "format": Counter()}
    handled: Counter = Counter()  # True: read with no gap, cleaned up and formatted with no gap either
    held = Counter()
    for path in files:
        relative = path.relative_to(root).as_posix()
        failures_before = len(out["failures"])
        text = path.read_text(encoding="utf-8", errors="replace")
        model = by_path.get(relative)
        if model is not None and path.suffix == ".sqlx":
            expected, _ = expected_dependencies(text)
            upstream = {pipeline.models[k].target.name if k in pipeline.models else k.split(".")[-1]
                        for k in pipeline.upstream.get(model.key, set())}
            deps["expected"] += len(expected)
            deps["found"] += len(expected & upstream)
            out["failures"] += [f"{relative}: ref({name!r}) is not an edge" for name in sorted(expected - upstream)]
        clean = model is None or model.key not in gapped
        for stage, run in (("cleanup", stage_cleanup), ("format", stage_format)) if stages else ():
            status, detail = run(text, PASS)
            stages[stage][status] += 1
            clean = clean and status == PASS
            if status == FAIL:
                out["failures"].append(f"{relative}: {stage}: {detail}")
        handled[clean] += 1
        if held_out(root.name, relative):
            held["files"] += 1
            held["handled"] += clean
            held["failures"] += len(out["failures"]) > failures_before
    out["held_out"] = {key: held[key] for key in ("files", "handled", "failures")}
    out["dependencies"] = deps
    out["stages"] = {stage: dict(counts) for stage, counts in stages.items()}
    out["handled"] = handled[True]
    return out


def run_all(skip: frozenset[str] | set[str] = frozenset()) -> dict[str, dict]:
    sources = json.loads((CORPORA / "sources.json").read_text())
    return {project["name"]: run_project(CORPORA / project["name"]) for project in sources["projects"] if project["name"] not in skip}


def totals(results: dict[str, dict]) -> dict:
    keys = ("files", "statements_total", "statements_matched", "columns_total", "columns_traced")
    out = {key: sum(r.get(key) or 0 for r in results.values()) for key in keys}
    out["failures"] = sum(len(r["failures"]) for r in results.values())
    out["handled"] = sum(r.get("handled", 0) for r in results.values())
    out["held_out"] = {key: sum(r.get("held_out", {}).get(key, 0) for r in results.values()) for key in ("files", "handled", "failures")}
    out["dependencies_expected"] = sum(r.get("dependencies", {}).get("expected", 0) for r in results.values())
    out["dependencies_found"] = sum(r.get("dependencies", {}).get("found", 0) for r in results.values())
    for stage in ("cleanup", "format"):
        out[stage] = dict(sum((Counter(r["stages"].get(stage, {})) for r in results.values()), Counter()))
    out["gaps"] = dict(sum((Counter(r["gaps"]) for r in results.values()), Counter()).most_common())
    return out


def _counts(counts: dict) -> str:
    return ", ".join(f"{value} {key}" for key, value in counts.items()) or "none"


def write_results(results: dict[str, dict], seconds: float) -> None:
    t = totals(results)
    record = json.loads(RESULTS.read_text()) if RESULTS.is_file() else {}
    record.update({
        "suite": "BigQuery real-code corpora",
        "order": record.get("order", 225),
        "size": t["files"],
        "score": (f"{t['files']} files in {len(results)} projects: {t['failures']} failures; statements read "
                  f"{t['statements_matched']}/{t['statements_total']}; columns traced {t['columns_traced']}/{t['columns_total']}"),
        "metric": ("Open-source Dataform projects and BigQuery SQL, copied at pinned commits. Each project is loaded whole; "
                   "every file goes through the cleanup rules and the formatter. A failure is a crash, a change accepted "
                   "without a proof, a changed backticked name, or a literal ref() that is not a graph edge."),
        "evidence": "executed",
        "correctness": (f"{t['failures']} failures; dependencies {t['dependencies_found']}/{t['dependencies_expected']} "
                        "literal ref() and config dependencies found"),
        "coverage": {"proven": t["handled"], "unsupported": t["files"] - t["handled"]},
        "analysis": (f"Files read with no blocking gap, cleaned up and formatted: {t['handled']}/{t['files']}. "
                     f"Cleanup: {_counts(t['cleanup'])}; format: {_counts(t['format'])}. Gaps the loader reports: {_counts(t['gaps'])}."),
        "docs": "docs/evals/bq-real-corpora.md",
        "command": "python tools/bq_corpus_bench.py --write-results",
        "date": date.today().isoformat(),
        "performance": f"{seconds:.1f} s for every project and stage",
        "held_out": (f"A fifth of the files, chosen by SHA-1 of project/path, scored apart: {t['held_out']['handled']}/"
                     f"{t['held_out']['files']} handled, {t['held_out']['failures']} with a failure. Tuned on test: bugs "
                     "were fixed with every file's failures in view, held-out files included."),
    })
    record.setdefault("caveats", "")
    RESULTS.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="write per-project results here")
    parser.add_argument("--write-results", action="store_true", help=f"update {RESULTS.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    results = run_all()
    seconds = time.perf_counter() - started
    for name, result in results.items():
        print(f"{name}: files {result['files']}, statements {result.get('statements_matched')}/{result.get('statements_total')}, "
              f"columns {result.get('columns_traced')}/{result.get('columns_total')}, gaps {result['gaps']}, "
              f"cleanup {result['stages'].get('cleanup')}, format {result['stages'].get('format')}")
        for failure in result["failures"]:
            print(f"  FAIL {failure}")
    print(json.dumps(totals(results), indent=1))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    if args.write_results:
        write_results(results, seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
