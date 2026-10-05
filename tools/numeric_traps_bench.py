"""Score KumoSQL's SMT prover on numeric traps: BigQuery number and error semantics a rational model gets wrong.

Each case in ``tests/fixtures/numeric_traps/cases.jsonl`` is a query pair written for KumoSQL (BigQuery
dialect, one fixed schema with declared INT64, FLOAT64, NUMERIC, STRING and BOOL columns) around a trap:
integers past 2**53, INT64 overflow in a pushed-down expression, NaN grouping and ordering, ``-0.0``,
NUMERIC rounding, INT64 to FLOAT64 conversion in CASE and UNION, ``DIV`` against ``/``, a division moved
ahead of a filter, a ``SUM`` over a group that ``HAVING`` or ``WHERE`` keeps or drops, ``SAFE_CAST`` and ``SAFE_DIVIDE``, and the literals of the October 2 audit.

Labels follow BigQuery's documented rules (``source`` names the GoogleSQL page; ``unverified`` says what
the author could not confirm), not DuckDB's. A case that DuckDB can run faithfully
(:mod:`kumosql.bigquery_on_duckdb`) also carries a ``witness`` database with the rows each side returns, and
the test suite replays it. The label says what holds on every database:

* ``equivalent``: the same rows, and neither query can raise an error the other cannot;
* ``not_equivalent``: some database where both queries succeed with different rows;
* ``refines``: the same rows wherever the original succeeds, the rewrite never raises an error the original
  cannot, and the original errors somewhere the rewrite returns rows;
* ``introduces_error``: the same rows wherever both succeed, but the rewrite raises an error on a database where
  the original returns rows.

BigQuery promises no evaluation order between ``WHERE``, ``ON`` and the select list, so an operation that can
fail is exposed to every row of its ``FROM``; only ``CASE``, ``IF``, ``COALESCE`` and ``NULLIF`` branches (and
the ``SAFE_`` functions) guard one.

The prover decides each pair without the label:

* **proven**: ``prove_equivalent_smt`` proves the pair (with the error verdict it reports, if any); a rewrite that
  can fail where the original succeeds is not proven: the verdict ``introduces`` withholds the proof;
* **refuted**: it finds a database on which the rows differ;
* **assumed**: proven, but only under an assumption the case violates (or says does not apply, field ``discharged``) and the result still lists;
* **unknown**: neither.

``wrong`` is a proof or refutation that contradicts the label (an ``equivalent`` case refuted, a
``not_equivalent`` one proven with no disclosure of the violated assumption, an error case whose verdict
claims the opposite). A quarter of the cases is held out: develop on ``dev``. Held out is, by default, every fourth case from the fourth (by position in the file);
a case that carries a boolean ``held_out`` field is held out or not as the field says, whatever its position (the cases added after the
position rule was in use set it, so that appending cases never moves an earlier case between the splits).

    python tools/numeric_traps_bench.py                 # every case
    python tools/numeric_traps_bench.py --split dev
    python tools/numeric_traps_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "numeric_traps"
TIMEOUT_MS = 5000

SCHEMA = {"t": ["x", "y", "f", "g", "n", "m", "s", "c"]}
TYPES = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "g": "FLOAT64", "n": "NUMERIC", "m": "NUMERIC", "s": "STRING", "c": "BOOL"}}
DUCKDB_TYPES = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "NUMERIC": "DECIMAL(38,9)", "STRING": "VARCHAR", "BOOL": "BOOLEAN"}

LABELS = ("equivalent", "not_equivalent", "refines", "introduces_error")
# The verdict an error-labelled case must get, and the verdicts that claim the opposite.
EXPECTED_VERDICT = {"refines": "refines", "introduces_error": "introduces"}
SAFE_VERDICTS = {"none", "same"}


@dataclass
class Case:
    id: str
    left: str
    right: str
    label: str
    why: str
    source: str
    executable: bool = False
    witness: dict | None = None
    violates: list = field(default_factory=list)  # assumption labels the case breaks: a proof that lists one is disclosed
    unverified: str | None = None  # what the label rests on that the author could not confirm in the documentation
    discharged: list = field(default_factory=list)  # assumption labels that do not apply here: a proof that still lists one is not clean
    held_out: bool | None = None  # explicit split designation; None means the position rule (every fourth case from the fourth)


def load_cases(split: str = "all") -> list[Case]:
    lines = (FIXTURES / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [Case(**json.loads(line)) for line in lines]
    if split == "all":
        return cases
    held = [c.held_out if c.held_out is not None else i % 4 == 3 for i, c in enumerate(cases)]
    return [c for c, h in zip(cases, held) if h == (split == "held-out")]


# -- the witness databases, replayed on DuckDB with BigQuery's guards ---------------------------


def run_witness(case: Case, sql: str):
    """The rows ``sql`` returns on the case's witness database, or ``"error"`` where BigQuery would fail."""

    import duckdb
    import sqlglot

    from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful, is_bigquery_failure

    witness = case.witness
    db = duckdb.connect()
    try:
        configure(db)
        columns = witness["columns"]
        declared = ", ".join(f"{c} {DUCKDB_TYPES[TYPES[witness['table']][c]]}" for c in columns)
        db.execute(f"CREATE TABLE {witness['table']} ({declared})")
        for row in witness["rows"]:
            db.execute(f"INSERT INTO {witness['table']} VALUES ({', '.join('?' for _ in columns)})", row)
        tree = faithful(sqlglot.parse_one(sql, read="bigquery"))
        try:
            return sorted(bigquery_rows(db.execute(tree.sql(dialect="duckdb")).fetchall()), key=repr)
        except Exception as error:  # noqa: BLE001 - DuckDB raises several error types
            if is_bigquery_failure(error):
                return "error"
            raise
    finally:
        db.close()


def witness_problems(case: Case) -> list[str]:
    """Why the case's witness does not hold on DuckDB (empty when it does, or when the case has none)."""

    if not case.executable:
        return []
    problems = []
    for side, sql in (("left", case.left), ("right", case.right)):
        expected = case.witness[side]
        expected = expected if expected == "error" else sorted((tuple(r) for r in expected), key=repr)
        found = run_witness(case, sql)
        if found != expected:
            problems.append(f"{side} returns {found!r}, the case says {expected!r}")
    return problems


# -- the prover's verdict -------------------------------------------------------------------


def decide(case: Case) -> dict:
    from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

    result = prove_equivalent_smt(case.left, case.right, schema=SCHEMA, types=TYPES, timeout_ms=TIMEOUT_MS)
    report = getattr(result, "errors", None)
    verdict = report.verdict if report is not None else None
    listed = [a for a in result.assumptions if any(a.startswith(v) for v in case.violates + case.discharged)]
    if result.status is SmtStatus.NOT_EQUIVALENT:
        outcome = "refuted"
    elif result.status in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        outcome = "assumed" if listed else "proven"
    else:
        outcome = "unknown"
    wrong = False
    if case.label == "equivalent":
        wrong = outcome == "refuted" or (outcome in ("proven", "assumed") and verdict in ("introduces", "refines"))
    elif case.label == "not_equivalent":
        wrong = outcome == "proven"
    elif outcome == "refuted":
        wrong = True
    elif verdict is not None and (outcome in ("proven", "assumed") or case.label == "introduces_error"):
        wrong = verdict in SAFE_VERDICTS or verdict != EXPECTED_VERDICT[case.label] and verdict != "unknown"
    # an error that the rewrite introduces is reported with the proof withheld (outcome unknown); a refinement is a proof
    allowed = ("unknown",) if case.label == "introduces_error" else ("proven",)
    classified = case.label in EXPECTED_VERDICT and outcome in allowed and verdict == EXPECTED_VERDICT[case.label]
    return {
        "id": case.id,
        "label": case.label,
        "outcome": outcome,
        "errors": verdict,
        "classified": classified,
        "wrong": wrong,
        "reason": result.reason,
        "assumptions": list(result.assumptions),
    }


def score(results: list[dict]) -> str:
    equivalent = [r for r in results if r["label"] == "equivalent"]
    different = [r for r in results if r["label"] == "not_equivalent"]
    errors = [r for r in results if r["label"] in EXPECTED_VERDICT]
    return (
        f"{sum(r['outcome'] == 'proven' for r in equivalent)}/{len(equivalent)} equivalent proved, "
        f"{sum(r['outcome'] == 'refuted' for r in different)}/{len(different)} differing refuted "
        f"({sum(r['outcome'] == 'proven' for r in different)} proved, {sum(r['outcome'] == 'assumed' for r in different)} assumed), "
        f"{sum(r['classified'] for r in errors)}/{len(errors)} error cases classified, {sum(r['wrong'] for r in results)} wrong"
    )


def results_row(results: list[dict], held_out: list[dict]) -> dict:
    from bench_common import today

    # a proof that lists an assumption the case violates is not counted as a proof
    counts = Counter("unknown" if r["outcome"] == "assumed" else r["outcome"] for r in results)
    return {
        "suite": "Numeric traps (SMT prover)",
        "order": 39,
        "size": len(results),
        "score": score(results),
        "metric": "Hand-written pairs around BigQuery's number and error rules (2**53, INT64 overflow, NaN, -0.0, NUMERIC, INT64 to FLOAT64, DIV against /, a division ahead of a filter, SAFE_ functions, the order a FLOAT64 SUM adds in, NUMERIC scale and rounding, a SUM over a group that a HAVING or a WHERE keeps or drops): sound ones proved, trap pairs never proved, and whether a rewrite can raise an error the original cannot.",
        "evidence": "proof",
        "correctness": "Labels follow the GoogleSQL documentation (the case file names the page; the ones it could not confirm say so); the cases DuckDB can run faithfully are replayed on a witness database by the test suite. Wrong is a proof or refutation that contradicts the label, or an error verdict that claims safety where the label says the rewrite can fail.",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown") if counts[k]},
        "held_out": score(held_out) + f" ({len(held_out)} cases)",
        "docs": "docs/evals/numeric-traps.md",
        "command": "python tools/numeric_traps_bench.py --write-results",
        "date": today(),
        "caveats": "Written by the author of the numeric value layer from the issue's trap list; the cases were fixed before the prover changed and a quarter held out, but only the cases were, not the prover's rules, so this is a regression and honesty check, not an independent benchmark. Labels the author could not confirm in the documentation are marked in the case file. NaN is modelled for declared FLOAT64 columns: the three original NaN pairs are now refuted by a database holding a NaN, and twelve more NaN cases were added in a later pass (three held out by an explicit held_out field, labelled before the prover answered). Tuned on test: the two original held-out NaN cases (nan-distinct-group, nan-not-equal-split) were the stated target of the NaN work and were run during it, so the held-out score is not independent for them. Rules of BigQuery the author could not confirm (NaN <> NaN is TRUE, IS_NAN(NULL), how MIN, MAX and set operations treat a NaN) are marked unverified in the case file and the prover. Twenty float-sum-order cases were added in a later pass (15 development, 5 held out, fixed before the rule was run); their labels rest on the repo's own note that a FLOAT64 sum has no fixed order, which is unverified against the GoogleSQL aggregate page, and the INT64 ones on the unverified rule that a partial INT64 sum cannot overflow when the total fits. A proof that still lists the row-order assumption on a case that says it does not apply, or on one that violates it, counts as unknown, not as a proof. The 16 numeric-* cases (NUMERIC scale and rounding) came with the rounding model: four are held out, but their answers were seen during development (tuned on test), and several rest on the unverified rule that NUMERIC * and / round to nine decimal digits, half away from zero. A refutation that needs a nonlinear NUMERIC product can hit the solver's time limit on a loaded machine, so the refuted count can move by one or two between runs; it never produces a wrong answer. The 17 group-SUM cases (12 development, 5 held out) were written, with their labels, before the prover compared sums group by group; the held-out five were run only after it was written and nothing was changed in response. Two of them are window pairs the prover does not prove equal, so they stay unknown. Their labels rest on BigQuery adding up a group that HAVING then drops (unverified: an optimizer may push a key filter below the aggregation) and on SUM(DISTINCT) and window sums failing on INT64 overflow as SUM does (unverified); a regrouped or pre-aggregated sum has no case because BigQuery's rule for partial sums that overflow while the total does not is unverified.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/numeric-traps.json (all cases)")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    cases = load_cases(args.split if not args.write_results else "all")
    bad = {c.id: p for c in cases if (p := witness_problems(c))}
    for case_id, problems in bad.items():
        print(f"witness of case {case_id} does not hold: {'; '.join(problems)}")
    results = [decide(c) for c in cases]
    for result in results:
        print(f"{result['id']:34} {result['label']:17} {result['outcome']:8} {str(result['errors']):10}{' WRONG' if result['wrong'] else ''}")
    print(score(results))
    if args.write_results:
        everything = load_cases("all")
        held_ids = {c.id for c in load_cases("held-out")}
        write_results("numeric-traps", results_row(results, [r for r in results if r["id"] in held_ids]))
        del everything
    return 1 if bad or any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
