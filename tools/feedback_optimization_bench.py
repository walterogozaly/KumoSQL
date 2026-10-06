"""Score the checked rewrites in the feedback-driven SQL optimization run bundle.

The GPL-3.0 run bundle is downloaded from a pinned commit and checked by SHA-256; its SQL is
never stored in this repository. The source's full-result checks are empirical claims, not
proofs. This eval asks KumoSQL to prove each passing pair and searches for replayed
counterexamples on databases satisfying the published TPC-H schema.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import querybooster_bench as qb

COMMIT = "2d6de251f3befec6ef473b77196510243f59f2d5"
ARCHIVE = "real-world-sf1-mixed-duckdb-20260630-160514-raw-run-data.zip"
ARCHIVE_SHA256 = "5df49e439afa6299d02488ebf40f73cf480a53089f93cb552b84aa4cf43d261f"
URL = f"https://raw.githubusercontent.com/KostovMartin/mk-feedback-driven-sql-optimization/{COMMIT}/experiment-artifacts/{ARCHIVE}"
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "feedback-optimization" / COMMIT[:12]
RUN_ID = "real-world-sf1-mixed-duckdb-20260630-160514"
PARAMETER = re.compile(r"\$(\d+)\s*::\s*(date|numeric|text|integer)\b", re.IGNORECASE)
CAST_PARAMETER = re.compile(r"\bCAST\s*\(\s*\$(\d+)\s+AS\s+(date|numeric|text|integer)\s*\)", re.IGNORECASE)
PARAMETER_ANY = re.compile(r"\$\d+")
PARAMETER_TYPES = {
    "date": ("DATE", "DATE"),
    "numeric": ("NUMERIC", "NUMERIC"),
    "text": ("TEXT", "STRING"),
    "integer": ("INTEGER", "INT64"),
}
PROVER_TIMEOUT_MS = qb.PROVER_TIMEOUT_MS
PROVER_WALL_S = qb.PROVER_WALL_S
SEARCH_BUDGET_S = qb.SEARCH_BUDGET_S


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    source: str
    left: str
    right: str
    claim: str
    schema_name: str = "tpch"
    rows: tuple[str, ...] = ()

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"feedback-e15\n{self.id}".encode()).hexdigest(), 16) % 5 == 0


def _get(url: str, path: Path, digest: str) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read()
                break
            except OSError as error:
                last = error
        else:
            raise OSError(f"could not download {url}: {last}")
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        temporary.write_bytes(data)
        temporary.replace(path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise OSError(f"{path} has SHA-256 {actual}, expected {digest}; delete it to download again")
    return path


def fetch() -> Path:
    """Fetch the pinned GPL source bundle into the user cache and verify its digest."""

    return _get(URL, CACHE / ARCHIVE, ARCHIVE_SHA256)


def _csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    try:
        raw = archive.read(name)
    except KeyError as error:
        raise ValueError(f"pinned bundle is missing {name}") from error
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def _checked_parameters(row: dict[str, str]) -> None:
    """Refuse an artifact whose source check did not compare concrete, matching result bags."""

    try:
        checks = json.loads(row["checks"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"equivalence check {row.get('id')} has malformed per-parameter evidence") from error
    if not checks:
        raise ValueError(f"equivalence check {row.get('id')} has no parameter-set evidence")
    if int(row.get("rows_compared", "-1")) != sum(int(check["rows_compared"]) for check in checks):
        raise ValueError(f"equivalence check {row.get('id')} has inconsistent compared-row totals")
    for check in checks:
        if int(check["original_row_count"]) != int(check["candidate_row_count"]):
            raise ValueError(f"equivalence check {row.get('id')} has a row-count mismatch")
        if int(check["rows_compared"]) != int(check["original_row_count"]):
            raise ValueError(f"equivalence check {row.get('id')} did not compare every row")


def load_cases(path: Path | None = None) -> list[Case]:
    """Load passing full-comparison pairs from the pinned run; omit unchecked/failed candidates."""

    path = path or fetch()
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("run_id") != RUN_ID:
            raise ValueError(f"bundle run id is {manifest.get('run_id')!r}, expected {RUN_ID!r}")
        templates = {row["template_fingerprint"]: row for row in _csv(archive, "query_templates.csv")}
        candidates = {row["id"]: row for row in _csv(archive, "candidates.csv")}
        checks = _csv(archive, "equivalence_checks.csv")

    if len(candidates) != 11 or len(checks) != 11:
        raise ValueError(f"pinned run changed shape: expected 11 candidates/checks, got {len(candidates)}/{len(checks)}")
    cases: list[Case] = []
    seen: set[str] = set()
    for check in checks:
        candidate_id = check["candidate_id"]
        if candidate_id in seen:
            raise ValueError(f"duplicate equivalence check for candidate {candidate_id}")
        seen.add(candidate_id)
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(f"equivalence check refers to missing candidate {candidate_id}")
        if check["method"] != "full_comparison":
            raise ValueError(f"candidate {candidate_id} was checked by {check['method']!r}, not full_comparison")
        if check["check_type"] != "initial":
            raise ValueError(f"candidate {candidate_id} has unexpected check type {check['check_type']!r}")
        if check["passed"] != "t":
            continue  # the single failed check is an upstream DuckDB execution error, not a negative label
        _checked_parameters(check)
        template = templates.get(candidate["template_fingerprint"])
        if template is None or not template["normalized_sql"].strip() or not candidate["sql_text"].strip():
            raise ValueError(f"candidate {candidate_id} lacks source SQL")
        mapping = json.loads(candidate["parameter_mapping"] or "{}")
        if any(source != target for source, target in mapping.get("mapping", {}).items()):
            raise ValueError(f"candidate {candidate_id} changed parameter positions")
        cases.append(Case(
            id=candidate_id,
            family="feedback-optimization",
            source=f"{RUN_ID}: candidate {candidate_id} ({candidate['source_type']})",
            left=template["normalized_sql"],
            right=candidate["sql_text"],
            claim="passed full_comparison on the source's generated TPC-H SF1 instance",
        ))
    if len(cases) != 10:
        raise ValueError(f"pinned run has {len(cases)} usable passing checks; expected 10")
    return cases


def _parameter_specs(*queries: str) -> dict[int, str]:
    """Return each bind's SQL type and reject untyped or inconsistently typed source parameters."""

    specs: dict[int, str] = {}
    recognized: set[int] = set()
    for sql in queries:
        for pattern in (CAST_PARAMETER, PARAMETER):
            for match in pattern.finditer(sql):
                index, kind = int(match.group(1)), match.group(2).lower()
                if index in specs and specs[index] != kind:
                    raise ValueError(f"bind ${index} has conflicting source types {specs[index]} and {kind}")
                specs[index] = kind
                recognized.add(index)
    found = {int(index[1:]) for index in PARAMETER_ANY.findall("\n".join(queries))}
    if found != recognized:
        raise ValueError(f"untyped source bind parameters: {sorted(found - recognized)}")
    return specs


def _symbolize(sql: str) -> str:
    """Represent each bind as a scalar value from a synthetic relation for proof and replay.

    ``MAX`` always returns one scalar. Every possible bind tuple is represented by a one-row
    parameter relation; arbitrary relations can only produce another tuple from those domains.
    """

    def replacement(match: re.Match[str]) -> str:
        index, kind = match.group(1), match.group(2).lower()
        sql_type, _ = PARAMETER_TYPES[kind]
        value = f"(SELECT MAX(p{index}) FROM __kumo_param_source)"
        return f"CAST({value} AS {sql_type})"

    symbolized = CAST_PARAMETER.sub(replacement, sql)
    symbolized = PARAMETER.sub(replacement, symbolized)
    if PARAMETER_ANY.search(symbolized):
        raise ValueError(f"untyped bind parameter remains in SQL: {symbolized}")
    return symbolized


def _case_schema(base: qb.Schema, case: Case) -> qb.Schema:
    """Extend TPC-H with a synthetic typed relation that supplies this pair's scalar binds."""

    specs = _parameter_specs(case.left, case.right)
    if not specs:
        return base
    types = dict(base.types)
    kinds = dict(base.kinds)
    types["__kumo_param_source"] = {f"p{index}": PARAMETER_TYPES[kind][0] for index, kind in specs.items()}
    kinds["__kumo_param_source"] = {f"p{index}": PARAMETER_TYPES[kind][1] for index, kind in specs.items()}
    not_null = dict(base.not_null)
    not_null["__kumo_param_source"] = set()
    keys = dict(base.keys)
    keys["__kumo_param_source"] = []
    unique = dict(base.unique)
    unique["__kumo_param_source"] = []
    return qb.Schema(base.dialect, types, kinds, not_null, keys, unique, base.foreign,
                     base.case_insensitive, base.origin + " + symbolic bind parameters")


def decide(case: Case, schema: qb.Schema) -> dict:
    started = time.time()
    left, right = _symbolize(case.left), _symbolize(case.right)
    # The source's bind values were not in the archive. Scalar MAX expressions preserve their
    # symbolic values for the prover and make them available to the schema-valid replay search.
    adapted = Case(case.id, case.family, case.source, left, right, case.claim, case.schema_name, case.rows)
    proven, reason, proposed = qb.prove(left, right, schema)
    counterexample = qb.refute(adapted, schema, left, right, proposed)
    return {
        "id": case.id,
        "family": case.family,
        "claim": case.claim,
        "schema": schema.origin,
        "held_out": case.held_out,
        "outcome": "refuted" if counterexample is not None else "proven" if proven else "unknown",
        "proven": proven,
        "wrong": proven and counterexample is not None,
        "reason": "" if proven or counterexample is not None else reason,
        "counterexample": counterexample,
        "seconds": round(time.time() - started, 1),
    }


def run(cases: list[Case]) -> list[dict]:
    base = qb.load_tpch_schema()
    return [decide(case, _case_schema(base, case)) for case in cases]


def summarize(results: list[dict]) -> dict[str, int]:
    counts = Counter(row["outcome"] for row in results)
    return {name: counts.get(name, 0) for name in ("proven", "refuted", "unknown")}


def results_row(results: list[dict]) -> dict:
    from bench_common import today

    counts = summarize(results)
    held = [row for row in results if row["held_out"]]
    held_counts = summarize(held)
    decided = counts["proven"] + counts["refuted"]
    held_decided = held_counts["proven"] + held_counts["refuted"]
    return {
        "suite": "Feedback-driven SQL optimization rewrites",
        "order": 62,
        "size": len(results),
        "score": f"{decided}/{len(results)} decided ({counts['proven']} proven, {counts['refuted']} label failures refuted), {sum(bool(r['wrong']) for r in results)} wrong",
        "metric": "Rewrites that passed the source run's full-result check, proven by KumoSQL or refuted by a replayed counterexample on schema-valid synthetic TPC-H data.",
        "evidence": "proof",
        "correctness": "Every pair is checked by the algebraic prover and by a DuckDB counterexample search confirmed with the optimizer off. Any overlap between a proof and a replayed counterexample counts as wrong.",
        "coverage": {key: value for key, value in counts.items() if value},
        "held_out": f"{held_decided}/{len(held)} decided ({held_counts['proven']} proven, {held_counts['refuted']} refuted)",
        "docs": "docs/evals/feedback-optimization.md",
        "command": "python tools/feedback_optimization_bench.py --write-results",
        "date": today(),
        "caveats": (
            f"GPL-3.0 archive downloaded at run time and SHA-256 checked (commit {COMMIT[:7]}), never committed. "
            "The archive contains ten passing full-result checks over generated TPC-H SF1 data, but not the database, "
            "workload manifest or parameter files; source bind values are unavailable. Binds are modeled symbolically as "
            "scalar MAX values over a synthetic relation, with declared TPC-H and parameter types. "
            "The eleventh candidate is excluded because DuckDB could not execute its original query. Tuned on test: "
            "all source SQL was inspected during the source-gate investigation; no prover code changed."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--only", action="append", help="only these candidate IDs (repeatable)")
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--write-results", action="store_true", help="update the scoreboard result file")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    cases = load_cases()
    if args.only:
        cases = [case for case in cases if case.id in args.only]
    if args.split != "all":
        cases = [case for case in cases if case.held_out == (args.split == "held-out")]
    if args.write_results and (args.only or args.split != "all"):
        parser.error("--write-results needs every case")
    results = run(cases)
    for row in results:
        print(f"{row['id']} {'held-out' if row['held_out'] else 'dev':8} {row['outcome']:8} {'WRONG' if row['wrong'] else ''} {row['seconds']:.1f}s {row['reason'][:90]}")
    counts = summarize(results)
    print(", ".join(f"{key} {value}" for key, value in counts.items()))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.write_results:
        write_results("feedback_optimization", results_row(results))
    return 1 if any(row["wrong"] for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
