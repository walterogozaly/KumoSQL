"""Table-minimization eval: keep the protected tables identical, make the pipeline as simple as possible.

Each case (``benchmarks/table_minimization/*.jsonl``, format in ``docs/evals/table-minimization.md``)
is a pipeline of tables, a list of protected tables, a verified reference minimization and traps
(tempting simplifications that change a protected output). A *minimizer* gets the pipeline and the
protected names and returns a new pipeline. The harness then checks it:

* every protected table must exist under its name and give the same column names and the same bag
  of rows as the original on every check database: the targeted ones, ``--databases`` random ones,
  the case's ``data`` rows and every trap witness, on DuckDB with the optimizer off. A difference, a
  missing protected table or an output that does not run is **wrong**;
* each protected table that changed is then given to KumoSQL's ``prove_models``.

Scores, kept apart: correctness (``proved`` / ``agreed`` / ``same`` / ``wrong``, wrong must be 0),
coverage (share of cases whose output scores lower than the original and is not wrong), quality
(per case ``(original - output) / (original - reference)``, wrong counted as 0, averaged over the
cases where the reference is lower) and runtime. Complexity is
``kumosql.formatting.pipeline_complexity``. No LLM runs at evaluation time.

    python tools/minimization_bench.py                          # dev split, the Refactor search
    python tools/minimization_bench.py --minimizer kumosql.table_minimizer:minimize_case
    python tools/minimization_bench.py --minimizer reference     # sanity: the stored references
    python tools/minimization_bench.py --split held_out          # once, at the end
    python tools/minimization_bench.py --cases sourced           # cases adapted from public projects
    python tools/minimization_bench.py --verify benchmarks/table_minimization/generated.jsonl
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
from dataclasses import asdict, dataclass, field
import importlib
import io
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minimization_cases as mc  # noqa: E402

DATABASES = 100

# ------------------------------------------------------------------ minimizers


def identity(case_input: Mapping) -> dict[str, str]:
    """Returns the pipeline unchanged: never wrong, never improved."""

    return dict(case_input["tables"])


def refactor_search(case_input: Mapping, max_seconds: float = 60.0, max_states: int = 40) -> dict[str, str]:
    """KumoSQL's Refactor search (``kumosql.refactor.search``): protected tables protected, the rest editable.

    Returns the lowest-complexity pipeline on its Pareto front.
    """

    import sqlglot
    from sqlglot import exp

    from kumosql import refactor
    from kumosql.formatting import pipeline_complexity

    sources, tables = case_input["sources"], case_input["tables"]
    pipeline, schema = mc.load_project(sources, {"": tables}, case_input.get("dialect", "bigquery"))
    prefix = f"{mc.PROJECT}.{mc.DATASET}."
    roles = {prefix + t: ("protected" if t in case_input["protected"] else "editable") for t in tables}
    with contextlib.redirect_stderr(io.StringIO()):
        result = refactor.search(pipeline, roles=roles, schema=schema, max_seconds=max_seconds, max_states=max_states)

    def bare(sql: str) -> str:
        tree = sqlglot.parse_one(sql, read="bigquery")
        for table in tree.find_all(exp.Table):
            if table.catalog == mc.PROJECT and table.db in (mc.DATASET, mc.RAW):
                table.set("catalog", None)
                table.set("db", None)
        return tree.sql(dialect="bigquery")

    # A table the search left as it was keeps the case's own SQL: re-rendering it through sqlglot would
    # change its score (a comma join comes back as CROSS JOIN, which sqlfluff counts as a join).
    unchanged = result.baseline.sql_map
    best, best_key = dict(tables), None
    for entry in result.front:
        candidate = {key[len(prefix):]: tables[key[len(prefix):]] if sql == unchanged.get(key) else bare(sql)
                     for key, sql in entry.sql_map.items() if key.startswith(prefix)}
        try:
            key = (pipeline_complexity(candidate)["score"], sum(len(s) for s in candidate.values()))
        except ValueError:
            continue
        if best_key is None or key < best_key:
            best, best_key = candidate, key
    return best


BUILT_IN = {"identity": identity, "refactor": refactor_search}


def load_minimizer(spec: str) -> tuple[str, Callable | None]:
    """``(label, callable)``; ``reference`` and ``trap`` are answered from the case itself (``None``)."""

    if spec in ("reference", "trap"):
        return spec, None
    if spec in BUILT_IN:
        return spec, BUILT_IN[spec]
    module, _, name = spec.partition(":")
    if not name:
        raise SystemExit(f"--minimizer wants module:function, identity, refactor, reference or trap, not {spec!r}")
    return spec, getattr(importlib.import_module(module), name)


# ------------------------------------------------------------------ one case


@dataclass
class CaseResult:
    id: str
    split: str
    families: list[str]
    reference_kind: str
    status: str = "error"  # proved | agreed | same | wrong | error
    reason: str = ""
    original: float = 0.0
    reference: float = 0.0
    output: float | None = None
    improved: bool = False
    quality: float | None = None  # None when the reference is no simpler than the original
    seconds: float = 0.0
    check_seconds: float = 0.0
    tables: int = 0
    proofs: dict = field(default_factory=dict)


def run_case(case: Mapping, minimize: Callable | str | None, databases: int = DATABASES, prove: bool = True,
             timeout_ms: int = 5000) -> CaseResult:
    from kumosql.formatting import pipeline_complexity

    original, protected = case["tables"], case["protected"]
    result = CaseResult(case["id"], case["split"], list(case.get("families", [])), case.get("reference_kind", "generated"),
                        original=case["original"]["complexity"]["score"],
                        reference=case["reference"]["complexity"]["score"], tables=len(original))
    started = time.time()
    try:
        if minimize == "reference":
            out = dict(case["reference"]["tables"])
        elif minimize == "trap":
            out = dict(case["traps"][0]["tables"]) if case.get("traps") else dict(original)
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                out = minimize(mc.case_input(case))
    except Exception as error:  # a minimizer that fails returns nothing: an error, never a pass
        result.seconds = time.time() - started
        result.reason = f"minimizer failed: {type(error).__name__}: {error}"[:300]
        return result
    result.seconds = time.time() - started
    checked = time.time()
    try:
        status, reason, proofs = check_output(case, out, databases, prove, timeout_ms)
    finally:
        result.check_seconds = time.time() - checked
    result.status, result.reason, result.proofs = status, reason, proofs
    if status != "wrong":
        try:
            result.output = pipeline_complexity(out)["score"]
        except ValueError as error:
            result.output, result.reason = result.original, f"complexity not available: {error}"[:300]
        result.improved = result.output < result.original
    gain = result.original - result.reference
    if gain > 0:
        result.quality = 0.0 if status == "wrong" else (result.original - result.output) / gain
    return result


def check_output(case: Mapping, out: object, databases: int = DATABASES, prove: bool = True,
                 timeout_ms: int = 5000) -> tuple[str, str, dict]:
    """``(status, reason, proofs)`` for a minimizer's output pipeline."""

    original, protected, sources = case["tables"], case["protected"], case["sources"]
    dialect = case.get("dialect", "bigquery")
    if not isinstance(out, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in out.items()):
        return "wrong", "the output is not a mapping of table names to SQL", {}
    out = {k.lower(): v for k, v in out.items()}
    missing = [p for p in protected if p not in out]
    if missing:
        return "wrong", f"protected tables missing: {missing}", {}
    clash = sorted(set(out) & set(sources))
    if clash:
        return "wrong", f"output tables named like sources: {clash}", {}
    engine = mc.Engine(sources, {"o": original, "n": out}, dialect)
    try:
        if "o" in engine.errors:
            raise RuntimeError(f"{case['id']}: the original does not run: {engine.errors['o']}")
        if "n" in engine.errors:
            return "wrong", f"the output does not run: {engine.errors['n']}", {}
        dbs = mc.check_databases(case, [original, out], databases)
        found = mc.compare(engine, dbs, "o", ["n"], protected, stable_only=mc.order_sensitive(original),
                           skip_errors=mc.real_data(case))["n"]
    finally:
        engine.close()
    if found:
        return "wrong", f"{found['table']}: {found['reason']} on {mc.witness_json(found['database'])}"[:500], {}
    if all(mc.unchanged(original, out, p, dialect) for p in protected):
        return "same", "", {}
    if not prove:
        return "agreed", "not proved (proofs off)", {}
    proofs = mc.prove(sources, original, out, protected, dialect, timeout_ms)
    if all(p["status"] in ("same", "proved") for p in proofs.values()):
        return "proved", "", proofs
    unknown = {k: v["reason"] for k, v in proofs.items() if v["status"] == "unknown"}
    return "agreed", f"not proved: {unknown}"[:300], proofs


# ------------------------------------------------------------------ summary


def _mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 3) if values else None


def summarise(results: list[CaseResult], label: str) -> dict:
    counts = Counter(r.status for r in results)
    improvable = [r for r in results if r.quality is not None]
    seconds = [r.seconds for r in results]
    by_family = defaultdict(list)
    for r in results:
        for family in r.families:
            by_family[family].append(r)
    by_kind = defaultdict(list)
    for r in improvable:
        by_kind[r.reference_kind].append(r)
    return {
        "minimizer": label,
        "cases": len(results),
        "correctness": {s: counts.get(s, 0) for s in ("proved", "agreed", "same", "wrong", "error")},
        "wrong": [{"id": r.id, "reason": r.reason} for r in results if r.status == "wrong"],
        "errors": [{"id": r.id, "reason": r.reason} for r in results if r.status == "error"],
        "coverage": {
            "improved": sum(r.improved for r in results),
            "improvable": len(improvable),
            "share": round(sum(r.improved for r in results) / len(results), 3) if results else None,
        },
        "quality": {
            "mean": _mean(r.quality for r in improvable),
            "beats_reference": sum(r.output is not None and r.output < r.reference for r in results if r.status != "wrong"),
            "reaches_reference": sum(r.output is not None and r.output <= r.reference for r in improvable if r.status != "wrong"),
            "by_reference_kind": {k: _mean(r.quality for r in v) for k, v in sorted(by_kind.items())},
            "complexity": {
                "original": round(sum(r.original for r in results), 1),
                "reference": round(sum(r.reference for r in results), 1),
                "output": round(sum(r.output if r.output is not None else r.original for r in results), 1),
            },
        },
        "runtime": {
            "total_seconds": round(sum(seconds), 1),
            "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
            "max_seconds": round(max(seconds), 2) if seconds else None,
            "check_seconds": round(sum(r.check_seconds for r in results), 1),
        },
        "by_family": {
            family: {"cases": len(rs), "improved": sum(r.improved for r in rs), "wrong": sum(r.status == "wrong" for r in rs),
                     "quality": _mean(r.quality for r in rs if r.quality is not None)}
            for family, rs in sorted(by_family.items())
        },
    }


def _run_one(args) -> CaseResult:
    case, spec, databases, prove, timeout_ms = args
    label, minimize = load_minimizer(spec)
    return run_case(case, minimize if minimize is not None else label, databases, prove, timeout_ms)


def run(cases: list[dict], spec: str = "refactor", databases: int = DATABASES, prove: bool = True,
        timeout_ms: int = 5000, jobs: int = 1) -> tuple[dict, list[CaseResult]]:
    work = [(case, spec, databases, prove, timeout_ms) for case in cases]
    if jobs > 1:
        from multiprocessing import Pool

        with Pool(jobs) as pool:
            results = pool.map(_run_one, work, chunksize=1)
    else:
        results = [_run_one(w) for w in work]
    return summarise(results, spec), results


# ------------------------------------------------------------------ case-file checks


def verify_cases(cases: list[dict], databases: int = 200) -> list[str]:
    """Problems with stored cases: fields, splits, complexities, reference agreement, trap witnesses."""

    from kumosql.formatting import pipeline_complexity

    problems = []
    seen = set()
    required = ("id", "source", "split", "dialect", "sources", "tables", "protected", "original", "reference", "traps")
    for case in cases:
        cid = case.get("id", "?")
        lacking = [f for f in required if f not in case]
        if lacking:
            problems.append(f"{cid}: missing fields {lacking}")
            continue
        if cid in seen:
            problems.append(f"{cid}: duplicate id")
        seen.add(cid)
        if case["split"] not in ("dev", "held_out"):
            problems.append(f"{cid}: split must be dev or held_out")
        original, reference = case["tables"], case["reference"]["tables"]
        for label, tables, stored in (("original", original, case["original"]["complexity"]),
                                      ("reference", reference, case["reference"]["complexity"])):
            try:
                if pipeline_complexity(tables) != stored:
                    problems.append(f"{cid}: stored {label} complexity {stored} is not {pipeline_complexity(tables)}")
            except ValueError as error:
                problems.append(f"{cid}: {label} cannot be scored: {error}")
        missing = [p for p in case["protected"] if p not in original or p not in reference]
        if missing:
            problems.append(f"{cid}: protected tables missing: {missing}")
            continue
        worlds = {"o": original, "r": reference, **{f"t{i}": t["tables"] for i, t in enumerate(case["traps"])}}
        engine = mc.Engine(case["sources"], worlds, case["dialect"])
        try:
            if engine.errors:
                problems.append(f"{cid}: does not run: {engine.errors}")
                continue
            checked = Counter()
            found = mc.compare(engine, mc.check_databases(case, worlds.values(), databases), "o", ["r"], case["protected"],
                               stable_only=mc.order_sensitive(original), skip_errors=mc.real_data(case), checked=checked)
            if found["r"]:
                problems.append(f"{cid}: reference differs on {found['r']['table']} ({found['r']['reason']})")
            unchecked = [p for p in case["protected"] if not checked[p]]
            if unchecked:
                problems.append(f"{cid}: no database checks {unchecked} (the original fails or depends on row order)")
            for i, trap in enumerate(case["traps"]):
                witness = mc.witness_rows(case["sources"], trap["witness"])
                if not mc.differs_on(engine, witness, "o", f"t{i}", case["protected"]):
                    problems.append(f"{cid}: trap {i} does not differ on its witness")
        finally:
            engine.close()
    return problems


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minimizer", default="refactor",
                        help="identity, refactor, reference, trap, or module:function (default: refactor)")
    parser.add_argument("--split", default="dev", choices=["dev", "held_out", "all"])
    parser.add_argument("--file", type=Path, action="append", help="case files (default: every *.jsonl)")
    parser.add_argument("--cases", choices=["own", "sourced", "all"],
                        help="own: generated and hand-written (the default without --file), sourced: adapted from "
                             "public projects, all (the default with --file)")
    parser.add_argument("--only", help="run case ids containing this text")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--databases", type=int, default=DATABASES)
    parser.add_argument("--no-prove", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--json", type=Path, help="write the summary and per-case results here")
    parser.add_argument("--verify", type=Path, action="append", help="check a case file and exit")
    args = parser.parse_args(argv)
    if args.verify:
        problems = verify_cases(mc.load_cases(args.verify))
        print("\n".join(problems) or "every case checks out")
        return 1 if problems else 0
    cases = mc.load_cases(args.file, None if args.split == "all" else args.split)
    kind = args.cases or ("all" if args.file else "own")
    if kind != "all":
        cases = [c for c in cases if mc.sourced(c) == (kind == "sourced")]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    cases = cases[: args.limit] if args.limit else cases
    summary, results = run(cases, args.minimizer, args.databases, not args.no_prove, args.timeout_ms, args.jobs)
    print(json.dumps({k: v for k, v in summary.items() if k != "by_family"}, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=1), encoding="utf-8")
    return 1 if summary["correctness"]["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
