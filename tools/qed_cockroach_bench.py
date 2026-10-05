"""Run the QED prover's CockroachDB test cases through KumoSQL's provers.

QED (https://github.com/qed-solver/prover, MIT, VLDB 2024) ships 1,287 query pairs from
CockroachDB's optimizer tests (memo, norm and xform) as relational-algebra JSON.
``tools/qed_cockroach_to_sql.py`` turns the pairs it can model exactly into SQL
(``tests/fixtures/qed_cockroach``; the rest are skipped with a reason, never guessed). For every
converted pair this reports ``proved``, ``different`` (not proved, and a random DuckDB database
shows the two queries disagree; these leave the score's denominator, since they are not
equivalent here), ``unknown`` and ``wrong`` (proved, yet a counterexample exists; must stay 0).
A proved pair whose SQL DuckDB cannot run (``unchecked``) is still counted as proved but listed,
because no random database could test it.

    python tools/qed_cockroach_bench.py [--workers N] [--verdicts out.json]
"""

from __future__ import annotations

import argparse
import contextlib
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlsolver_bench as sb  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qed_cockroach"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "qed_cockroach_pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def load_skipped() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "qed_cockroach_skipped.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


@contextlib.contextmanager
def boolean_columns():
    """Run the shared harness with real BOOLEAN columns while the block lasts.

    ``sqlsolver_bench`` stores BOOLEAN columns as BIGINT filled with 0..3, so ``b = TRUE`` and ``b``
    would differ on a row holding 3, which no BOOLEAN column can. Here the DuckDB tables declare BOOLEAN
    and the random rows hold only True, False or NULL there. The targeted search treats booleans as
    integers, so it is left out for tables with a BOOLEAN column. The patch is undone on exit; processes
    never share it.
    """

    real_type, real_rows, real_targeted = sb._duck_type, sb.random_rows, sb.targeted_differ

    def duck_type(column):
        return "BOOLEAN" if column.type.split("(")[0] in ("BOOLEAN", "BOOL") else real_type(column)

    def random_rows(table, rng, numbers=None):
        rows = real_rows(table, rng, numbers)
        flags = [i for i, c in enumerate(table.columns) if duck_type(c) == "BOOLEAN"]
        return [[v if v is None or i not in flags else bool(v) for i, v in enumerate(row)] for row in rows] if flags else rows

    def targeted(left_sql, right_sql, used):
        if any(duck_type(c) == "BOOLEAN" for t in used for c in t.columns):
            return None
        return real_targeted(left_sql, right_sql, used)

    sb._duck_type, sb.random_rows, sb.targeted_differ = duck_type, random_rows, targeted
    try:
        yield
    finally:
        sb._duck_type, sb.random_rows, sb.targeted_differ = real_type, real_rows, real_targeted


def _tables(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def judge(case: dict, prove=sb.default_prove, trials: int = 30, schemas: dict | None = None) -> tuple[str, bool]:
    """(verdict, unchecked) for one case: verdict is proved / different / unknown / wrong."""

    with boolean_columns():
        return _judge(case, prove, trials, {} if schemas is None else schemas)


def _judge(case: dict, prove, trials: int, schemas: dict) -> tuple[str, bool]:
    key = case["schema_id"]
    if key not in schemas:
        tables = _tables(case["ddl"])
        schemas[key] = (tables, sb.new_database(tables))
    tables, db = schemas[key]
    left, right = case["sql_a"], case["sql_b"]
    try:
        proof = prove(left, right, tables)
    except Exception:  # a crash is a failure to prove, never a proof
        proof = False
    counter = sb.differ(left, right, tables, db, trials)
    found = counter not in (None, False)
    if proof and found:
        return "wrong", False
    if proof:
        return "proved", counter is False
    return ("different" if found else "unknown"), False


def _chunk(args):
    cases, trials = args
    schemas: dict = {}
    return [(c["name"],) + judge(c, trials=trials, schemas=schemas) for c in cases]


def run(prove=sb.default_prove, trials: int = 30, limit: int | None = None, workers: int = 1) -> dict:
    cases = load_cases()[:limit]
    start = time.time()
    if workers > 1 and prove is sb.default_prove:
        chunks = [(cases[i::workers * 4], trials) for i in range(workers * 4)]
        with ProcessPoolExecutor(workers) as pool:
            rows = [r for part in pool.map(_chunk, chunks) for r in part]
        order = {c["name"]: i for i, c in enumerate(cases)}
        rows.sort(key=lambda r: order[r[0]])
    else:
        schemas: dict = {}
        rows = [(c["name"],) + judge(c, prove, trials, schemas) for c in cases]
    out = {"total": len(rows), "proved": 0, "different": [], "unknown": [], "wrong": [], "unchecked": [], "verdicts": {}}
    for name, verdict, unchecked in rows:
        out["verdicts"][name] = verdict
        if verdict == "proved":
            out["proved"] += 1
            if unchecked:
                out["unchecked"].append(name)
        else:
            out[verdict].append(name)
    out["scored"] = out["total"] - len(out["different"])  # a pair with a replayed counterexample is not one to prove
    out["seconds"] = time.time() - start
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=1, help="processes to spread the pairs over")
    ap.add_argument("--verdicts", type=Path, help="write per-case verdicts as JSON")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    r = run(workers=args.workers, limit=args.limit)
    print(f"qed-cockroach {r['proved']}/{r['scored']} proved ({r['total']} pairs), {len(r['different'])} different (counterexample), {len(r['unknown'])} unknown, {len(r['wrong'])} wrong, {len(r['unchecked'])} proved but not runnable in DuckDB, {r['seconds']:.1f}s")
    for key in ("different", "wrong"):
        if r[key]:
            print(f"  {key}: {', '.join(r[key])}")
    if args.verdicts:
        args.verdicts.write_text(json.dumps(r["verdicts"], indent=1) + "\n")
    return 1 if r["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
