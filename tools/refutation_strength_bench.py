"""Score how often KumoSQL refutes query pairs that are known to differ (the refutation-strength eval).

Every pair here returns different rows on some database, and the case says which: its witness
database is replayed on DuckDB (optimizer off, as ``kumosql.duckdb_load.run_unoptimized`` does)
before anything is scored. A pair is

1. **refuted** when ``prove_equivalent_algebraic(..., search_counterexample=True)`` returns
   ``not_equivalent`` with a counterexample database, and that database, replayed independently
   by this harness, separates the two queries;
2. **proven**: the prover proved the pair. This is **wrong** (every pair differs);
3. **unknown**: anything else.

A refutation whose database the harness cannot replay to a difference also counts as wrong.

Sources (see ``tests/fixtures/refutation_strength/README.md``):

* ``r024``: an outside research assistant's sweep (R024) of confirmed-different pairs, minus the two parts kept elsewhere
  (``tests/fixtures/refutation_strength/r024.jsonl``): correlated domain joins (R006), aggregation
  pushdown (R009), windows (R011), documented rewrites (R012b) and decorrelation (R002).
* ``optimizer-bugs``: the 24 pairs of ``tests/fixtures/optimizer_bugs`` (R019 in the sweep), with
  that eval's held-out split.
* ``verieql``: R013's four VeriEQL pairs (Literature 46 and 47, which need more than 1,000 rows,
  and Calcite 12 and 231), read from the VeriEQL download (CC BY-NC-SA 4.0, never stored here).

    python tools/refutation_strength_bench.py
    python tools/refutation_strength_bench.py --show unknown
    python tools/refutation_strength_bench.py --baseline      # master's search, for comparison
    python tools/refutation_strength_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import functools
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
FIXTURES = ROOT / "tests" / "fixtures" / "refutation_strength"
PROVER_TIMEOUT_MS = 10000

VERIEQL_CASES = (("literature", 46), ("literature", 47), ("calcite", 12), ("calcite", 231))


@dataclass
class Case:
    id: str
    source: str
    dialect: str
    schema: dict  # table -> {column: type}
    left: str
    right: str
    witness: dict | None  # table -> rows (positional, in schema column order)
    keys: dict = field(default_factory=dict)  # table -> [[columns]]
    not_null: dict = field(default_factory=dict)  # table -> [columns]
    foreign_keys: list = field(default_factory=list)  # [(table, [columns], parent, [parent columns])]
    setup: str = ""  # DuckDB DDL to replay the witness with, when the case comes with its own
    engine: str = "duckdb"  # where the witness is replayed ("sqlite" for a pair DuckDB cannot run)
    held_out: bool = False
    refutable: bool = True  # False: no database can show the difference soundly (stated in ``note``)
    note: str = ""
    options: dict = field(default_factory=dict)  # extra prover arguments the source's own eval uses
    suite: str = ""  # the targeted-data suite an escape comes from (its label is checked on that suite)


def r024_cases() -> list[Case]:
    cases = []
    for line in (FIXTURES / "r024.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(Case(
            id=row["id"], source=f"r024/{row['source']}", dialect=row["dialect"], schema=row["schema"],
            left=row["left"], right=row["right"], witness=row["witness"], keys=row.get("keys") or {},
            not_null=row.get("not_null") or {}, refutable=not row.get("unrefutable"), note=row.get("note", ""),
        ))
    return cases


# Pairs that differ only through an engine's free choice: no database shows the difference soundly.
UNREFUTABLE = {
    "bug-025": "the right query differs only when DISTINCT ON picks a row of a group without the matching s, "
    "and picking the matching row gives the same result: the difference rests on an arbitrary pick",
}


def optimizer_bug_cases() -> list[Case]:
    import optimizer_bugs_bench as ob

    cases = []
    for bug in ob.load_cases():
        schema, constraints = ob.declared(bug)
        types = ob.declared_types(bug)
        cases.append(Case(
            id=bug.id, source="optimizer-bugs", dialect="duckdb",
            schema={t: {c: types[t][c] for c in cols} for t, cols in schema.items()},
            left=bug.left, right=bug.right, witness=bug.data, setup=bug.setup, engine=bug.engine,
            keys={t: [list(k) for k in c.keys] for t, c in constraints.items() if c.keys},
            not_null={t: sorted(c.not_null) for t, c in constraints.items() if c.not_null},
            held_out=bug.held_out, refutable=bug.id not in UNREFUTABLE, note=UNREFUTABLE.get(bug.id, bug.note),
        ))
    return cases


def verieql_cases() -> list[Case]:
    import verieql_bench as vb

    cases = []
    suites: dict[str, list] = {}
    for suite, index in VERIEQL_CASES:
        try:
            suites.setdefault(suite, vb.load_cases(suite))
        except vb.DataUnavailable:
            return []
        case = suites[suite][index]
        spec = vb.build_spec(case)
        left, right, _ = vb.repaired_pair(case, spec)
        keys = {t.name: [list(t.primary_key)] + [list(u) for u in t.unique] if t.primary_key else [list(u) for u in t.unique] for t in spec.tables.values()}
        cases.append(Case(
            id=f"verieql-{suite}-{index}", source="verieql", dialect="mysql",
            schema={t.name: {c.name: c.type for c in t.columns} for t in spec.tables.values()},
            left=left, right=right, witness=None,
            keys={t: k for t, k in keys.items() if k},
            not_null={t.name: [c.name for c in t.columns if c.not_null] for t in spec.tables.values() if any(c.not_null for c in t.columns)},
            foreign_keys=[(ct, [cc], pt, [pc]) for ct, cc, pt, pc in spec.foreign_keys],
            options={"exact_arithmetic": True},
        ))
    return cases


# Told apart only on databases with a zero divisor, where BigQuery raises an error (as in
# tests/test_targeted_data_bench.py): no BigQuery witness exists.
_ZERO_DIVISOR_ONLY = {("calcite", 204, " / EMP.COMM"), ("calcite", 205, " / t1.COMM")}


@functools.lru_cache(maxsize=1)
def _targeted_suites():
    import targeted_data_bench as tb

    return tb.build_corpus("dev")[1]


def targeted_escape_cases() -> list[Case]:
    """The mutants that slip past the default eight random databases but not the targeted suite
    (``tests/fixtures/targeted_data/cases.json``, from the multi-database-semantic eval)."""

    suites = _targeted_suites()
    cases = []
    rows = json.loads((ROOT / "tests" / "fixtures" / "targeted_data" / "cases.json").read_text(encoding="utf-8"))
    for number, row in enumerate(rows):
        suite = suites[row["suite"]]
        zero = any((row["suite"], row["index"]) == key[:2] and key[2] in row["mutant"] for key in _ZERO_DIVISOR_ONLY)
        cases.append(Case(
            id=f"escape-{number:03d}", source="targeted-escapes", dialect="bigquery", schema=suite["schema"], suite=row["suite"],
            left=row["original"], right=row["mutant"], witness=None,
            keys={t: [list(k) for k in c.keys] for t, c in suite["constraints"].items() if c.keys},
            not_null={t: sorted(c.not_null) for t, c in suite["constraints"].items() if c.not_null},
            refutable=not zero,
            note="differs only where a divisor is zero, and BigQuery raises an error there" if zero else
            f"{row['suite']} query {row['index']}, {row['operator']} (caught by {row['killed_by']})",
        ))
    return cases


def unsafe_control_cases() -> list[Case]:
    """Controls: the unsafe rewrites of ``tests/fixtures/unsafe_rewrite_cases.jsonl`` (the unsafe-rewrite
    eval refutes all of them already), with that file's held-out split."""

    import unsafe_fuzz

    cases = []
    for line in (ROOT / "tests" / "fixtures" / "unsafe_rewrite_cases.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["expect"] == "equivalent":
            continue
        cases.append(Case(
            id=f"unsafe-{row['id']}", source="unsafe-controls", dialect="bigquery", schema=unsafe_fuzz.SCHEMA,
            left=row["left"], right=row["right"], witness=None, held_out=bool(row["heldout"]), note=row["family"],
        ))
    return cases


MAIN = ("r024", "optimizer-bugs", "verieql")  # the scored pairs; the escapes and controls are reported beside them


def load_cases(controls: bool = True) -> list[Case]:
    cases = r024_cases() + optimizer_bug_cases() + verieql_cases()
    if controls:
        cases += targeted_escape_cases() + unsafe_control_cases()
    return cases


# --- deciding a case ---------------------------------------------------------------------------------


def constraints_of(case: Case):
    from kumosql.smt_equivalence import TableConstraints

    out = {}
    for table in case.schema:
        keys = tuple(tuple(c.lower() for c in k) for k in case.keys.get(table, ()))
        not_null = frozenset(c.lower() for c in case.not_null.get(table, ())) | {c for k in keys for c in k}
        fks = tuple(
            (tuple(c.lower() for c in cols), parent.lower(), tuple(c.lower() for c in pcols))
            for child, cols, parent, pcols in case.foreign_keys
            if child == table
        )
        if keys or not_null or fks:
            out[table.lower()] = TableConstraints(not_null=frozenset(not_null), keys=keys, foreign_keys=fks)
    return out


def prove(case: Case, *, search: bool = True, timeout_ms: int = PROVER_TIMEOUT_MS):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    schema = {t.lower(): [c.lower() for c in cols] for t, cols in case.schema.items()}
    types = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in case.schema.items()}
    return prove_equivalent_algebraic(
        case.left, case.right, schema=schema, types=types, constraints=constraints_of(case) or None,
        compare_names=False, dialect=case.dialect, timeout_ms=timeout_ms, search_counterexample=search, **case.options,
    )


def decide(case: Case, *, search: bool = True) -> dict:
    from kumosql.smt_equivalence import SmtStatus

    started = time.monotonic()
    try:
        result = prove(case, search=search)
        status, reason = result.status, result.reason
    except Exception as error:  # a crash is a failure to refute, never a refutation
        result, status, reason = None, None, f"{type(error).__name__}: {error}"
    seconds = time.monotonic() - started
    outcome = {SmtStatus.PROVEN_EQUIVALENT: "proven", SmtStatus.NOT_EQUIVALENT: "refuted"}.get(status, "unknown")
    replayed = None
    if outcome == "refuted":
        from kumosql.refutation_replay import replay_counterexample

        replayed = replay_counterexample(
            case.left, case.right, result.counterexample, schema=case.schema, dialect=case.dialect,
            keys=case.keys, not_null=case.not_null, foreign_keys=case.foreign_keys,
        )
    rows = None
    if outcome == "refuted" and result.counterexample is not None:
        rows = sum(len(v) for v in result.counterexample.tables.values())
    wrong = outcome == "proven" or (outcome == "refuted" and replayed is False)
    return {
        "id": case.id, "source": case.source, "outcome": outcome, "reason": reason[:160], "seconds": round(seconds, 2),
        "rows": rows, "replayed": replayed, "wrong": wrong, "held_out": case.held_out, "refutable": case.refutable,
    }


def confirm(case: Case) -> bool | None:
    """Whether the case's own witness separates the pair (``None``: no witness to replay). An escape
    has no stored witness: its label is checked on the targeted suite that caught it."""

    from kumosql.refutation_replay import witness_differs

    if case.witness is not None:
        return witness_differs(case.left, case.right, case.witness, schema=case.schema, dialect=case.dialect, setup=case.setup, engine=case.engine)
    if case.suite:
        return _suite_differs(case)
    return None


def _suite_differs(case: Case) -> bool:
    from kumosql.refutation_replay import Judge, Verdict
    from kumosql.targeted_data import database_suite

    suite = _targeted_suites()[case.suite]
    with Judge(case.left, case.right, case.schema, dialect=case.dialect, keys=case.keys, not_null=case.not_null) as judge:
        for labeled in database_suite(case.left, suite["schema"], suite["rules"]):
            data = {name: [tuple(row) for row in table.rows] for name, table in labeled.dataset.tables.items()}
            if judge.verdict(data) is Verdict.DIFFERS:
                return True
    return False


def _work(args):
    case, search = args
    row = decide(case, search=search)
    row["confirmed"] = confirm(case)
    return row


def run(cases: list[Case], *, search: bool = True, jobs: int = 1) -> list[dict]:
    work = [(case, search) for case in cases]
    if jobs <= 1:
        return [_work(w) for w in work]
    import multiprocessing

    with multiprocessing.get_context("fork").Pool(jobs, maxtasksperchild=4) as pool:
        return pool.map(_work, work, chunksize=1)


def _group(source: str) -> str:
    return source.split("/")[0]


def summary(rows: list[dict]) -> dict:
    counts = Counter(r["outcome"] for r in rows)
    by_source = {}
    for source in sorted({_group(r["source"]) for r in rows}):
        part = [r for r in rows if _group(r["source"]) == source]
        refutable = [r for r in part if r["refutable"]]
        by_source[source] = f"{sum(r['outcome'] == 'refuted' for r in refutable)}/{len(refutable)}"
    main = [r for r in rows if _group(r["source"]) in MAIN]
    times = [r["seconds"] for r in main]
    return {
        "refuted": counts["refuted"], "proven": counts["proven"], "unknown": counts["unknown"],
        "wrong": sum(r["wrong"] for r in rows), "size": len(rows), "by_source": by_source,
        "median_seconds": round(statistics.median(times), 2) if times else 0.0,
    }


def results_row(rows: list[dict]) -> dict:
    from bench_common import today

    main = [r for r in rows if _group(r["source"]) in MAIN]
    refutable = [r for r in main if r["refutable"]]
    refuted = sum(r["outcome"] == "refuted" for r in refutable)
    held = [r for r in main if r["held_out"]]
    escapes = [r for r in rows if _group(r["source"]) == "targeted-escapes" and r["refutable"]]
    controls = [r for r in rows if _group(r["source"]) == "unsafe-controls"]
    wrong = sum(r["wrong"] for r in rows)
    part = lambda name: next((v for k, v in summary(main)["by_source"].items() if k == name), "0/0")  # noqa: E731
    counts = Counter(r["outcome"] for r in main)
    return {
        "suite": "Refutation strength",
        "order": 39,
        "size": len(main),
        "score": f"{refuted}/{len(refutable)} refuted, {counts['proven']} proved, {wrong} wrong",
        "metric": (
            "Query pairs known to return different rows (the R024 sweep of an outside research assistant, the optimizer wrong-result bugs and VeriEQL's "
            "large-cardinality pairs): refuted means prove_equivalent_algebraic(..., search_counterexample=True) returned a "
            "database that, replayed on DuckDB with the optimizer off, separates the pair. "
            f"R024 {part('r024')}, optimizer bugs {part('optimizer-bugs')}, VeriEQL {part('verieql')}; "
            f"median {summary(main)['median_seconds']} s per pair."
        ),
        "evidence": "executed",
        "correctness": (
            "A proof or a refutation whose database does not replay to a difference counts as wrong. "
            f"Beside the score: {sum(r['outcome'] == 'refuted' for r in escapes)}/{len(escapes)} targeted-data escapes and "
            f"{sum(r['outcome'] == 'refuted' for r in controls)}/{len(controls)} unsafe-rewrite controls refuted."
        ),
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown") if counts[k]},
        "held_out": f"{sum(r['outcome'] == 'refuted' for r in held)}/{len(held)} refuted (optimizer-bugs' held-out pairs)",
        "docs": "docs/evals/refutation-strength.md",
        "command": "python tools/refutation_strength_bench.py --jobs 4 --write-results",
        "date": today(),
        "caveats": (
            f"{len(main) - len(refutable)} pairs are not counted as refutable: their difference rests on an arbitrary DISTINCT ON "
            "pick (bug-025) or on APPROX_COUNT_DISTINCT's approximation (R012b-18). bug-005 runs only on SQLite. The R024 pairs "
            "came from an outside research assistant and were re-checked on DuckDB here; they and the VeriEQL pairs were all seen "
            "while building the refuter, so only optimizer-bugs' held-out pairs are held out, and bug-001 among them prompted "
            "the replay of solver counterexamples (tuned on test)."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    from bench_common import quiet

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", default="", help="comma-separated outcomes to print, e.g. unknown,refuted")
    parser.add_argument("--only", default="", help="comma-separated case ids or sources")
    parser.add_argument("--baseline", action="store_true", help="the prover without the synthesized search")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args(argv)
    quiet()

    cases = load_cases()
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted or c.source in wanted or c.source.split("/")[0] in wanted]
    if args.baseline:
        import os

        os.environ["KUMOSQL_SYNTHESIS"] = "0"
    rows = run(cases, jobs=args.jobs)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    shown = set(filter(None, args.show.split(",")))
    for row in rows:
        if row["outcome"] in shown or row["wrong"] or (row["confirmed"] is False and row["refutable"]):
            print(f"{row['id']:22} {row['outcome']:8} {row['seconds']:6.2f}s confirmed={row['confirmed']} {row['reason']}")
    print(json.dumps(summary(rows)))
    if args.write_results:
        if args.only or args.baseline:
            raise SystemExit("--write-results needs every case and the synthesized search")
        if not any(r["source"] == "verieql" for r in rows):
            raise SystemExit("--write-results needs the VeriEQL download (the verieql pairs were skipped)")
        path = ROOT / "benchmarks" / "results" / "refutation-strength.json"
        path.write_text(json.dumps(results_row(rows), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
