"""Measure verified, short difference predicates on refuted SPJ pairs (issue #512)."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TOOLS))

from bench_common import quiet, today, write_results  # noqa: E402

MAX_ATOMS = 3
SINGH_SAMPLE = 50
OTHER_SAMPLE = 50


@dataclass
class QueryPair:
    source: str
    case_id: str
    left: str
    right: str
    dialect: str
    options: dict[str, Any] = field(default_factory=dict)
    native: Any = field(default=None, repr=False, compare=False)
    source_replay: bool = False
    source_detail: str = ""


def is_spj(sql: str, dialect: str) -> bool:
    """Whether a query is a simple SELECT-project-join block the row-local checker targets."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, RecursionError):
        return False
    if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1:
        return False
    if tree.find(exp.Subquery, exp.SetOperation, exp.Window, exp.AggFunc):
        return False
    if any(tree.args.get(key) for key in ("with_", "group", "having", "distinct", "order", "limit", "offset", "qualify")):
        return False
    tables = list(tree.find_all(exp.Table))
    names = [".".join(part.name.lower() for part in table.parts) for table in tables]
    return bool(names) and len(names) == len(set(names))


def is_spj_pair(left: str, right: str, dialect: str) -> bool:
    """Require one occurrence of the same input tables on each side."""

    if not is_spj(left, dialect) or not is_spj(right, dialect):
        return False
    try:
        names = lambda sql: {".".join(part.name.lower() for part in table.parts)
                             for table in sqlglot.parse_one(sql, read=dialect).find_all(exp.Table)}
        return names(left) == names(right)
    except (sqlglot.errors.SqlglotError, RecursionError):
        return False


def _stable_key(pair: QueryPair) -> str:
    value = f"{pair.source}\n{pair.left}\n{pair.right}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def stable_sample(pairs: list[QueryPair], limit: int) -> list[QueryPair]:
    """A fixed sample selected by a hash of source and pair text, never by verdict."""

    return sorted(pairs, key=lambda pair: (_stable_key(pair), pair.case_id))[: max(0, limit)]


def _singh_candidates(split: str, sample_size: int) -> list[QueryPair]:
    import singh_bedathur_bench as singh

    pairs = singh.split(singh.load_pairs(), split)
    candidates = [
        QueryPair(
            "singh-bedathur",
            pair.key,
            pair.left,
            pair.right,
            "mysql",
            {"schema": pair.tables, "dialect": "mysql", "compare_names": False, "exact_arithmetic": True},
            pair,
        )
        for pair in pairs
        if pair.gold == "different" and is_spj_pair(pair.left, pair.right, "mysql")
    ]
    return stable_sample(candidates, min(sample_size, SINGH_SAMPLE))


def _verieql_options(case: dict, verieql) -> dict[str, Any]:
    from kumosql.smt_equivalence import TableConstraints

    spec = verieql.build_spec(case)
    tables = {table.name.lower(): table for table in spec.tables.values()}
    schema = {name: [column.name.lower() for column in table.columns] for name, table in tables.items()}
    types = {name: {column.name.lower(): column.type for column in table.columns} for name, table in tables.items()}
    constraints = {
        name: TableConstraints(
            not_null=frozenset(column.name.lower() for column in table.columns if column.not_null),
            keys=tuple(
                tuple(column.lower() for column in key)
                for key in ([table.primary_key] if table.primary_key else []) + list(table.unique)
            ),
            foreign_keys=tuple(
                ((column.lower(),), parent.lower(), (parent_column.lower(),))
                for child, column, parent, parent_column in spec.foreign_keys
                if child.lower() == name
            ),
        )
        for name, table in tables.items()
    }
    return {
        "schema": schema,
        "types": types,
        "constraints": constraints,
        "dialect": "mysql",
        "compare_names": False,
        "exact_arithmetic": True,
    }


def _verieql_candidates(suite: str, sample_size: int, split: str) -> list[QueryPair]:
    # VeriEQL publishes no held-out partition. The fixed LeetCode/Literature samples are
    # development-only and are never described as held out.
    if split == "held-out":
        return []
    import verieql_bench as verieql

    cases = verieql.load_cases(suite)
    eligible = [
        case for case in cases
        if not case.get("adapted") and is_spj_pair(case["pair"][0], case["pair"][1], "mysql")
    ]
    records = verieql.load_veri_states(suite)
    out = []
    for case in eligible:
        record = records.get(tuple(case["pair"]))
        if not record or "NEQ" not in (record.get("states") or ()):
            continue
        if verieql.replay(case, record) is not True:
            continue
        out.append(
            QueryPair(
                f"verieql-{suite}",
                f"{suite}:{case['index']}",
                case["pair"][0],
                case["pair"][1],
                "mysql",
                _verieql_options(case, verieql),
                case,
                True,
                "VeriEQL NEQ counterexample replayed on DuckDB",
            )
        )
    return stable_sample(out, sample_size)


def _unsafe_candidates(split: str, sample_size: int) -> list[QueryPair]:
    import unsafe_fuzz

    cases = unsafe_fuzz.build_cases("unsafe", 20, 1)
    candidates = []
    for case in cases:
        if split == "dev" and case.heldout:
            continue
        if split == "held-out" and not case.heldout:
            continue
        if case.expect == "equivalent" or not is_spj_pair(case.left, case.right, "bigquery"):
            continue
        candidates.append(
            QueryPair(
                "unsafe-rewrite-detection",
                case.id,
                case.left,
                case.right,
                "bigquery",
                {
                    "schema": {table: list(unsafe_fuzz.COLUMNS) for table in unsafe_fuzz.TABLES},
                    "types": {table: {column: "INT64" for column in unsafe_fuzz.COLUMNS}
                              for table in unsafe_fuzz.TABLES},
                    "dialect": "bigquery",
                    "compare_names": False,
                    "exact_arithmetic": True,
                },
                case,
            )
        )
    return stable_sample(candidates, sample_size)


def _pipeline_query_pair(case, pipeline_bench) -> tuple[str, str] | None:
    from kumosql import load_sqlx_project
    from kumosql.pipeline_equivalence import _inlined

    if case.label != "different" or len(case.outputs) != 1:
        return None
    with tempfile.TemporaryDirectory(prefix="kumosql-difference-pipeline-") as tmp:
        root = Path(tmp)
        rename = pipeline_bench.write_project(case, root)
        pipeline = load_sqlx_project(root)
        output = case.outputs[0]
        left = _inlined(pipeline, f"{pipeline_bench.PROJECT}.{pipeline_bench.DATASET}.{output}", [], None)
        right_name = rename.get(output, output)
        right = _inlined(pipeline, f"{pipeline_bench.PROJECT}.{pipeline_bench.DATASET}.{right_name}", [], None)
        if not left or not right:
            return None
        return left[0], right[0]


def _pipeline_candidates(split: str, sample_size: int) -> list[QueryPair]:
    import pipeline_bench

    cases = pipeline_bench.all_cases(held_out=split == "held-out")
    candidates = []
    for case in cases:
        pair = _pipeline_query_pair(case, pipeline_bench)
        if pair is None or not is_spj_pair(pair[0], pair[1], "bigquery"):
            continue
        schema = {
            table: list(columns)
            for table, columns in pipeline_bench.source_schema().items()
        }
        candidates.append(
            QueryPair(
                "pipeline-refutation",
                case.id,
                pair[0],
                pair[1],
                "bigquery",
                {"schema": schema, "dialect": "bigquery", "compare_names": False},
                case,
            )
        )
    return stable_sample(candidates, sample_size)


def source_candidates(split: str = "dev", sample_size: int = OTHER_SAMPLE, only: str | None = None) -> dict[str, list[QueryPair]]:
    """Build a fixed, row-local sample; held-out sources are omitted from dev runs."""

    if split not in ("dev", "held-out"):
        raise ValueError("split must be dev or held-out")
    result: dict[str, list[QueryPair]] = {}
    builders = {
        "singh-bedathur": lambda: _singh_candidates(split, sample_size),
        "verieql-leetcode": lambda: _verieql_candidates("leetcode", sample_size, split),
        "verieql-literature": lambda: _verieql_candidates("literature", sample_size, split),
        "unsafe-rewrite-detection": lambda: _unsafe_candidates(split, sample_size),
        "pipeline-refutation": lambda: _pipeline_candidates(split, sample_size),
    }
    for name, build in builders.items():
        if only and only != name:
            continue
        result[name] = build()
    return result


def _confirm(pair: QueryPair, unsafe_oracle=None) -> tuple[bool, str]:
    """Require the source eval's refutation evidence before counting an explanation attempt."""

    if pair.source == "singh-bedathur":
        import singh_bedathur_bench as singh

        verdict = singh.decide(pair.native)
        return verdict.kind == "different", verdict.kind
    if pair.source.startswith("verieql-"):
        return pair.source_replay, pair.source_detail
    if pair.source == "unsafe-rewrite-detection":
        import unsafe_fuzz

        outcome = unsafe_fuzz.evaluate(pair.native, unsafe_oracle)
        valid = outcome.prover == "refuted" and outcome.replayed is True and outcome.truth == "different"
        return valid, f"{outcome.prover}/{outcome.truth}/replayed={outcome.replayed}"
    if pair.source == "pipeline-refutation":
        import pipeline_bench

        result = pipeline_bench.run_case(pair.native, trials=25, timeout_ms=5000, declared=True)
        valid = result.verdict == "different" and result.ground_truth_ok and result.executed_agree is False
        return valid, f"{result.verdict}/ground_truth={result.ground_truth_ok}"
    return False, "no source refuter"


def _explain(pair: QueryPair, explain_seconds: float) -> dict:
    from kumosql.difference_explanation import explain_or_why

    options = dict(pair.options)
    options["explain_seconds"] = explain_seconds
    started = time.monotonic()
    explanation, why = explain_or_why(pair.left, pair.right, **options)
    seconds = time.monotonic() - started
    if explanation is None:
        return {"source": pair.source, "case_id": pair.case_id, "explained": False, "reason": why, "seconds": seconds}
    atom_count = len(explanation.atoms)
    return {
        "source": pair.source,
        "case_id": pair.case_id,
        "explained": atom_count <= MAX_ATOMS,
        "atoms": atom_count,
        "exact": explanation.exact,
        "reason": "" if atom_count <= MAX_ATOMS else "predicate exceeds the three-atom cap",
        "seconds": seconds,
    }


def run_split(split: str, sample_size: int = OTHER_SAMPLE, explain_seconds: float = 5.0, only: str | None = None) -> dict:
    pairs_by_source = source_candidates(split, sample_size, only)
    selected = [pair for pairs in pairs_by_source.values() for pair in pairs]
    unsafe_oracle = None
    if any(pair.source == "unsafe-rewrite-detection" for pair in selected):
        import unsafe_fuzz

        unsafe_oracle = unsafe_fuzz.Oracle(trials=40)
    raw = []
    considered = Counter()
    refuted = Counter()
    source_details: dict[str, Counter] = defaultdict(Counter)
    source_seconds = 0.0
    for pair in selected:
        considered[pair.source] += 1
        started = time.monotonic()
        valid, detail = _confirm(pair, unsafe_oracle)
        source_seconds += time.monotonic() - started
        source_details[pair.source]["source_refutations" if valid else "not_refuted"] += 1
        if not valid:
            continue
        refuted[pair.source] += 1
        row = _explain(pair, explain_seconds)
        row["source_evidence"] = detail
        raw.append(row)

    rows_by_source: dict[str, dict] = {}
    all_times = []
    reasons = Counter()
    for source in pairs_by_source:
        rows = [row for row in raw if row["source"] == source]
        times = [row["seconds"] for row in rows]
        all_times.extend(times)
        reasons.update(row["reason"] for row in rows if not row["explained"])
        rows_by_source[source] = {
            "selected_spj": considered[source],
            "refuted_spj": refuted[source],
            "explained": sum(row["explained"] for row in rows),
            "no_predicate": sum(not row["explained"] for row in rows),
            "multi_branch": sum(row["reason"] == "a set operation" for row in rows),
            "unsupported_shape": sum(row["reason"].startswith("unsupported:") for row in rows),
            "single_block_refuted": sum(
                row["reason"] != "a set operation" and not row["reason"].startswith("unsupported:") for row in rows
            ),
            "single_block_explained": sum(
                row["explained"]
                and row["reason"] != "a set operation"
                and not row["reason"].startswith("unsupported:")
                for row in rows
            ),
            "median_explain_seconds": round(statistics.median(times), 4) if times else None,
            "source_checks": dict(source_details[source]),
        }
    total_refuted = len(raw)
    total_explained = sum(row["explained"] for row in raw)
    expanded = [row for row in raw if row["reason"] == "a set operation"]
    unsupported = [row for row in raw if row["reason"].startswith("unsupported:")]
    one_block = [row for row in raw if row not in expanded and row not in unsupported]
    one_block_explained = sum(row["explained"] for row in one_block)
    return {
        "split": split,
        "sample_size_per_source": sample_size,
        "selected_spj": sum(considered.values()),
        "source_refuted_spj": total_refuted,
        "explained": total_explained,
        "coverage": round(total_explained / total_refuted, 4) if total_refuted else None,
        "multi_branch": len(expanded),
        "unsupported_shape": len(unsupported),
        "single_block_refuted_spj": len(one_block),
        "single_block_explained": one_block_explained,
        "single_block_coverage": round(one_block_explained / len(one_block), 4) if one_block else None,
        "no_predicate": total_refuted - total_explained,
        "median_explain_seconds": round(statistics.median(all_times), 4) if all_times else None,
        "source_validation_seconds": round(source_seconds, 2),
        "by_source": rows_by_source,
        "miss_reasons": dict(reasons.most_common()),
        "cases": raw,
    }


def result_row(dev: dict, held_out: dict) -> dict:
    size = dev["single_block_refuted_spj"]
    explained = dev["single_block_explained"]
    rate = f"{100 * explained / size:.1f}%" if size else "n/a"
    held_size = held_out["single_block_refuted_spj"]
    held_rate = f"{held_out['single_block_explained']}/{held_size}" if held_size else "no eligible held-out single-block SPJ pairs"
    return {
        "suite": "Verified difference explanations",
        "order": 49,
        "size": size,
        "score": f"{explained}/{size} ({rate}) with a verified predicate of at most three atoms; 0 unverified",
        "metric": "Share of independently refuted single-block SPJ pairs in a fixed development sample with a verified difference predicate.",
        "evidence": "proof",
        "correctness": "Every reported predicate has a replayable witness and is proved by both the SMT and algebraic provers after filtering the difference rows.",
        "coverage": {"proven": explained, "unknown": size - explained},
        "held_out": f"{held_rate}; only source-provided held-out partitions were scored",
        "coverage_of": f"Explanation outcomes over {size} independently refuted pairs that compile to one SPJ block in the fixed development sample.",
        "docs": "docs/evals/difference-explanations.md",
        "command": "python tools/difference_bench.py --split all --write-results",
        "date": today(),
        "caveats": f"Fixed samples, not full source-corpus scores. The broader syntactic SPJ sample was {dev['explained']}/{dev['source_refuted_spj']} ({100 * dev['coverage']:.1f}%); {dev['multi_branch']} cases compiled into multiple branches and are excluded from this one-block metric. VeriEQL has no held-out partition and is development-only.",
    }


def _line(result: dict) -> str:
    rate = "n/a" if result["single_block_coverage"] is None else f"{100 * result['single_block_coverage']:.1f}%"
    broad_rate = "n/a" if result["coverage"] is None else f"{100 * result['coverage']:.1f}%"
    return (
        f"{result['split']}: {result['single_block_explained']}/{result['single_block_refuted_spj']} one-block SPJ pairs "
        f"({rate}); {result['explained']}/{result['source_refuted_spj']} of all syntactic SPJ pairs ({broad_rate}); "
        f"{result['multi_branch']} multi-branch; {result['selected_spj']} selected; median explanation "
        f"{result['median_explain_seconds']}s; source checks {result['source_validation_seconds']}s"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", choices=("dev", "held-out", "all"), default="dev")
    parser.add_argument("--sample-size", type=int, default=OTHER_SAMPLE, help="fixed candidate cap per source")
    parser.add_argument("--explain-seconds", type=float, default=5.0)
    parser.add_argument("--source", choices=("singh-bedathur", "verieql-leetcode", "verieql-literature", "unsafe-rewrite-detection", "pipeline-refutation"))
    parser.add_argument("--json", type=Path, help="write the detailed report here")
    parser.add_argument("--write-results", action="store_true", help="write the scoreboard row (runs dev and held-out once)")
    args = parser.parse_args(argv)
    quiet()
    if args.split == "all":
        dev = run_split("dev", args.sample_size, args.explain_seconds, args.source)
        held_out = run_split("held-out", args.sample_size, args.explain_seconds, args.source)
        report = {"development": dev, "held_out": held_out}
        print(_line(dev))
        print(_line(held_out))
        if args.write_results:
            result_path = write_results("difference-explanations", result_row(dev, held_out))
            print(f"wrote {result_path.relative_to(ROOT)}")
    else:
        report = run_split(args.split, args.sample_size, args.explain_seconds, args.source)
        print(_line(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
