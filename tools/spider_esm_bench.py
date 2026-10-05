"""Score KumoSQL on TestSuiteEval's hand-labelled ESM false negatives, with no language model.

TestSuiteEval (https://github.com/ruiqi-zhong/TestSuiteEval, no licence; Zhong, Yu and Klein, "Semantic
Evaluation for Text-to-SQL with Distilled Test Suites", EMNLP 2020) ships ``ESMFalseNegatives.tsv``: rows
(database, gold query, predicted query, reason) that Spider's exact-set-match metric marked wrong and the
authors judged **equivalent**. The reasons are the authors' words ("redundant join with a parent and primary
key", "different joining order and aliases", "max is a descending sort with limit 1", ...). The rows repeat
pairs (several models wrote the same prediction), so each distinct pair is decided once.

Every pair is decided as ``tools/llm_sql_solver_bench.py`` decides a Spider pair, with Spider's schemas
(``tables.json``) in place of its databases, which are not downloadable here:

1. **proven**: the algebraic prover (SQLite dialect, **no keys**, output names ignored) proves the pair, and
   neither query compares a text column with a number. Results are compared as lists when the gold query ends
   in ``ORDER BY`` and as bags otherwise. Every proof is re-run on 1,000 random databases that respect the
   listed keys and foreign keys; a difference there makes the proof **wrong**.
2. **refuted**: SQLite returns different results on such a database (random, targeted, then the z3 bounded
   check, each replayed in SQLite; ties under a ``LIMIT`` never count). For a pair labelled equivalent this is a
   **label dispute**, never a KumoSQL answer: the equivalence needs something the schema does not declare (a
   column that is never NULL, a foreign key that always matches) or the authors' metric, which removes every
   ``DISTINCT`` before comparing. The database and a cause come with it.
3. **unknown**: anything else, and a pair that runs past ``PAIR_TIMEOUT`` or crashes z3 (each pair runs in its
   own process). **unsupported**: SQLite rejects a query on the schema.

Many predictions hold value placeholders (``'terminal'``, ``"value"``, ``1``): the model did not predict
values, and TestSuiteEval plugs the gold query's values into the prediction's value slots, accepting a
prediction that matches for any plug-in. Those pairs are **plugged** and scored apart from the pairs taken as
published: a plugged pair is proven when one plug-in is proven and refuted when every plug-in is refuted
(``LIMIT`` and ``OFFSET`` counts are not slots). The prediction as published is always tried first.

Keys. ``tables.json`` lists only the first column of a composite primary key, so a listed key is used to build
databases (it is unique in every Spider database) and never given to the prover.

The data is downloaded at run time from pinned commits and never committed (see ``tools/spider_data.py``).
One distinct pair in five, by the SHA-1 of its text, is held out; a run reads only the dev pairs unless
``--split all`` or ``--split held-out`` is given (``--write-results`` scores every pair).

    python tools/spider_esm_bench.py                         # the dev pairs
    python tools/spider_esm_bench.py --show proven,refuted
    python tools/spider_esm_bench.py --write-results
    python tools/spider_esm_bench.py --check-sources         # the pinned files match their digests
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import logging
import multiprocessing
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

import llm_sql_solver_bench as solver
import spider_check as check
import spider_data

PAIR_TIMEOUT = 300  # seconds; the bounded check can run past its own limit, and explaining a dispute searches more
PLUG_LIMIT = 64  # most plug-ins tried for one prediction
HEADER = ["database name", "gold", "pred", "equivalent reason", "note"]

# Proved pairs a replayed counterexample shows are different (counted as wrong, kept as regressions).
FALSE_PROOFS: dict[str, str] = {}


def normalize(sql: str) -> str:
    """One space between tokens and no final semicolon."""

    return " ".join(sql.strip().rstrip(";").split())


@dataclass
class Row:
    index: int  # data row of the file, from 0
    database: str
    gold: str
    pred: str
    reason: str
    note: str


@dataclass
class Pair:
    database: str
    gold: str
    pred: str
    rows: tuple[int, ...]  # data rows with this pair
    reasons: tuple[str, ...]
    schema: spider_data.Schema

    @property
    def id(self) -> str:
        return f"esm-{self.rows[0]:03d}"

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"spider-esm\n{self.database}\n{self.gold}\n{self.pred}".encode()).hexdigest(), 16) % 5 == 0


def load_rows(path: Path | None = None) -> list[Row]:
    text = (path or spider_data.fetch("ESMFalseNegatives.tsv")).read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if rows[0] != HEADER:
        raise ValueError(f"unexpected header {rows[0]}")
    return [Row(i, r[0], r[1], r[2], r[3].strip(), r[4].strip()) for i, r in enumerate(rows[1:])]


def load_pairs(rows: list[Row] | None = None, schemas: dict | None = None) -> list[Pair]:
    rows = rows if rows is not None else load_rows()
    schemas = schemas or spider_data.load_schemas()
    grouped: dict[tuple[str, str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault((row.database, normalize(row.gold), normalize(row.pred)), []).append(row)
    return [
        Pair(database, gold, pred, tuple(r.index for r in group), tuple(dict.fromkeys(r.reason for r in group if r.reason)), schemas[database])
        for (database, gold, pred), group in grouped.items()
    ]


# -- placeholders: TestSuiteEval's plug-in of the gold's values ------------------------


def respace(sql: str) -> str:
    """TestSuiteEval's own clean-up before it runs a prediction: ``> =`` is read as ``>=`` (and ``< =``, ``! =``)."""

    return sql.replace("> =", ">=").replace("< =", "<=").replace("! =", "!=")


def _slots(tree) -> list:
    """The value slots: every literal except a LIMIT or OFFSET count."""

    return [node for node in tree.find_all(exp.Literal) if not node.find_ancestor(exp.Limit, exp.Offset)]


def _value(node) -> tuple[str, str]:
    if node.is_string:
        return ("text", node.this)
    try:
        return ("number", repr(float(node.this)))
    except ValueError:
        return ("text", node.this)


def needs_values(gold: str, pred: str) -> bool:
    """The prediction holds a value the gold query does not: a placeholder the model wrote instead of a value."""

    g, p = solver._tree(gold), solver._tree(pred)
    if g is None or p is None:
        return False
    known = {_value(v) for v in _slots(g)}
    return any(_value(v) not in known for v in _slots(p))


def plug_values(gold: str, pred: str, limit: int = PLUG_LIMIT) -> list[str] | None:
    """Every way to fill the prediction's value slots with the gold's values, or None past ``limit``."""

    g, p = solver._tree(gold), solver._tree(pred)
    if g is None or p is None:
        return []
    choices = list({_value(v): v for v in _slots(g)}.values())
    slots = len(_slots(p))
    if not choices or not slots:
        return []
    if len(choices) ** slots > limit:
        return None
    out: list[str] = []
    for combination in itertools.product(choices, repeat=slots):
        tree = p.copy()
        for slot, value in zip(_slots(tree), combination):
            slot.replace(value.copy())
        sql = tree.sql(dialect="sqlite")
        if sql not in out:
            out.append(sql)
    return out


def strip_distinct(sql: str) -> str | None:
    """The query with every DISTINCT removed (TestSuiteEval's default), or None when it has none."""

    tree = solver._tree(sql)
    if tree is None:
        return None
    changed = False
    for select in list(tree.find_all(exp.Select)):
        if select.args.get("distinct") is not None:
            select.set("distinct", None)
            changed = True
    for node in list(tree.find_all(exp.Distinct)):
        if len(node.expressions) == 1:
            node.replace(node.expressions[0].copy())
            changed = True
    return tree.sql(dialect="sqlite") if changed else None


# -- deciding a pair ------------------------------------------------------------------


def prove(pair: Pair, sql1: str, sql2: str) -> bool:
    """LLM-SQL-Solver's proof of a Spider pair: no keys, lists under ORDER BY, no text-number comparisons."""

    return check.prove(pair.schema, sql1, sql2)


def decide_pair(pair: Pair) -> dict:
    started = time.time()
    schema = pair.schema
    sql1, sql2 = solver.adapt(respace(pair.gold), schema.tables), solver.adapt(respace(pair.pred), schema.tables)
    plugged = needs_values(sql1, sql2)
    out = {
        "id": pair.id, "database": pair.database, "rows": len(pair.rows), "held_out": pair.held_out, "plugged": plugged,
        "outcome": "unknown", "how": "", "cause": "", "wrong": False, "detail": "",
    }
    candidates: list[str] | None = [sql2]
    if plugged:
        candidates = plug_values(sql1, sql2)
        if candidates is not None:
            candidates = [sql2, *[c for c in candidates if c != sql2]]
    if candidates is None:
        out["detail"] = f"more than {PLUG_LIMIT} ways to plug the gold's values in"
    elif check.differs(schema, sql1, sql1, trials=1) == "error":
        out.update(outcome="unsupported", detail="SQLite rejects the gold query on the schema")
    else:
        candidates = [c for c in candidates if check.differs(schema, sql1, c, trials=1) != "error"]
        if not candidates:
            out.update(outcome="unsupported", detail="SQLite rejects the prediction on the schema")
        else:
            out.update(_decide(pair, sql1, candidates))
    out["seconds"] = round(time.time() - started, 2)
    return out


def _decide(pair: Pair, gold: str, candidates: list[str]) -> dict:
    schema = pair.schema
    for candidate in candidates:
        if prove(pair, gold, candidate):
            # a proof is re-checked on random databases: a difference there would be a false proof
            return {"outcome": "proven", "how": "prover", "wrong": check.differs(schema, gold, candidate) == "differs", "detail": candidate}
    refutations = []
    for candidate in candidates:
        witness: dict = {}
        how = check.refute(schema, gold, candidate, witness)
        if how not in ("differs", "targeted", "bounded"):
            return {"outcome": "unknown"}
        refutations.append((candidate, how, witness))
    candidate, how, witness = refutations[0]
    out = {"outcome": "refuted", "how": how, "detail": candidate}
    if witness:
        out["witness"] = check.shrink(schema, gold, candidate, witness)
    out["cause"] = cause(pair, gold, refutations)
    return out


CONVENTIONS = ("columns", "values", "distinct", "null", "nonempty")
CAUSE_VARIANTS = 24


def cause(pair: Pair, gold: str, refutations: list[tuple[str, str, dict]]) -> str:
    """Why a pair labelled equivalent is refuted: the fewest of TestSuiteEval's conventions under which the two agree.

    ``columns``: results compared up to the order of columns (its comparison permutes them); ``values``: the gold's
    values plugged into the prediction's value slots; ``distinct``: DISTINCT removed from both queries (its
    default); ``null``: only databases without NULL (random ones, plus each counterexample with its NULLs replaced
    by fresh values), although the schema allows NULL; ``nonempty``: only databases where every table has a row and
    the gold query returns one (a join without a condition, or ``max`` over no rows, differs on an empty table). ``other`` when none of these makes the two agree on the
    databases tried.
    """

    schema = pair.schema
    candidates = [c for c, _, _ in refutations]
    published = solver.adapt(respace(pair.pred), schema.tables)
    tree = solver._tree(published)
    usable = [
        c for c in CONVENTIONS
        if not (c == "values" and (len(candidates) > 1 or tree is None or not _slots(tree)))
        and not (c == "distinct" and not (strip_distinct(gold) or any(strip_distinct(x) for x in candidates)))
        and not (c == "columns" and len(check.column_orders(candidates[0])) == 1)
    ]
    for size in range(1, min(len(usable), 3) + 1):
        for chosen in itertools.combinations(usable, size):
            left, rights = gold, list(candidates)
            if "values" in chosen:
                rights += plug_values(gold, published, limit=CAUSE_VARIANTS) or []
            if "columns" in chosen:
                rights = [p for r in rights for p in check.column_orders(r)]
            if "distinct" in chosen:
                left = strip_distinct(left) or left
                rights = [strip_distinct(r) or r for r in rights]
            databases = None
            if "null" in chosen or "nonempty" in chosen:
                databases = check.pool(schema, gold, candidates[0], nulls="null" not in chosen, nonempty="nonempty" in chosen)
                converted = [check.without_nulls(schema, w) if "null" in chosen else w for _, _, w in refutations if w]
                databases += [d for d in converted if d is not None and (all(d.get(t) for t in schema.tables) or "nonempty" not in chosen)]
            if _agrees_on_any(schema, left, list(dict.fromkeys(rights))[:CAUSE_VARIANTS], databases):
                return "+".join(chosen)
    return "other"


def _agrees_on_any(schema, left: str, rights: list[str], databases) -> bool:
    """The two queries agree on every database tried for at least one of ``rights`` (the metric
    accepts a prediction that matches under any of them)."""

    return any(check.differs(schema, left, right, trials=check.CAUSE_TRIALS, databases=databases) == "agree" for right in rights)


def _worker(pair: Pair, connection) -> None:
    try:
        connection.send(decide_pair(pair))
    finally:
        connection.close()


def decide_guarded(pair: Pair, timeout: float = PAIR_TIMEOUT) -> dict:
    """``decide_pair`` in its own process: a pair that runs past ``timeout`` seconds (one z3 call can ignore its own
    time limit) is killed and counted as unknown, never as a proof or a refutation."""

    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(pair, sender), daemon=True)
    started = time.time()
    process.start()
    sender.close()
    result, how = None, "timeout"
    if receiver.poll(timeout):
        try:
            result = receiver.recv()
        except EOFError:  # the worker died before answering (z3 can crash while building a model)
            how = "crash"
    process.kill()
    process.join()
    if result is not None:
        return result
    return {
        "id": pair.id, "database": pair.database, "rows": len(pair.rows), "held_out": pair.held_out, "plugged": needs_values(*[
            solver.adapt(respace(s), pair.schema.tables) for s in (pair.gold, pair.pred)]),
        "outcome": "unknown", "how": how, "cause": "", "wrong": False, "detail": "", "seconds": round(time.time() - started, 2),
    }


def run(pairs: list[Pair], jobs: int = 1) -> list[dict]:
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        return list(pool.map(decide_guarded, pairs))


# -- reporting ------------------------------------------------------------------------


def summarize(results: list[dict]) -> dict[str, dict]:
    out = {}
    for name, rows in (("published", [r for r in results if not r["plugged"]]), ("plugged", [r for r in results if r["plugged"]]), ("all", results)):
        counts = Counter(r["outcome"] for r in rows)
        out[name] = {
            "pairs": len(rows), "rows": sum(r["rows"] for r in rows),
            **{k: counts.get(k, 0) for k in ("proven", "refuted", "unknown", "unsupported")},
            "wrong": sum(r["wrong"] for r in rows),
        }
    return out


def results_row(results: list[dict]) -> dict:
    from bench_common import today

    counts = Counter(r["outcome"] for r in results)
    held = [r for r in results if r["held_out"]]
    summary, held_summary = summarize(results), summarize(held)
    causes = Counter(r["cause"] for r in results if r["outcome"] == "refuted")
    refuted = counts.get("refuted", 0)
    return {
        "suite": "Spider ESM false negatives (TestSuiteEval)",
        "order": 39,
        "size": len(results),
        "score": (
            f"{counts.get('proven', 0)}/{len(results)} proved, {refuted} refuted (label disputes), {summary['all']['wrong']} wrong"
        ),
        "metric": "Distinct (gold, prediction) pairs the TestSuiteEval authors hand-labelled equivalent: proved by the algebraic prover with no keys; refuted means SQLite returns different results on a database KumoSQL builds from Spider's declared schema, so the label needs a constraint the schema does not declare or the metric's DISTINCT removal.",
        "evidence": "proof",
        "correctness": "Every proof is re-run on 1,000 random databases that respect the listed keys and foreign keys (a difference counts as wrong). A refutation of a pair the authors call equivalent is a replayed SQLite run on a schema-valid database: a label dispute, not a KumoSQL answer.",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown", "unsupported") if counts[k]},
        "held_out": f"{held_summary['all']['proven']}/{len(held)} proved, {held_summary['all']['refuted']} refuted, {held_summary['all']['wrong']} wrong",
        "docs": "docs/evals/spider-esm.md",
        "command": "python tools/spider_esm_bench.py --write-results",
        "date": today(),
        "caveats": (
            f"Downloaded at run time from pinned commits (TestSuiteEval has no licence) and never committed. The {sum(r['rows'] for r in results)} rows hold {len(results)} distinct pairs, each decided once. "
            f"Spider's databases are blocked here, so refutations use databases KumoSQL builds from tables.json's types, keys and foreign keys; the prover gets no key (tables.json lists only the first column of a composite key). "
            f"Taken as published: {summary['published']['proven']}/{summary['published']['pairs']} proved, {summary['published']['refuted']} refuted. "
            f"{summary['plugged']['pairs']} pairs hold value placeholders and are decided with TestSuiteEval's plug-in of the gold's values (scored apart): {summary['plugged']['proven']} proved, {summary['plugged']['refuted']} refuted. "
            f"Refuted causes: {', '.join(f'{k or chr(63)} {v}' for k, v in sorted(causes.items()))}. A pair that runs past {PAIR_TIMEOUT} s or crashes z3 is unknown ({sum(r['how'] in ('timeout', 'crash') for r in results)} here). "
            "Tuned on test: no knowledge of it; the harness was written by an earlier, interrupted session whose log is not available, and this run is the first full scoring of the held-out pairs (the development pairs alone scored 26/293 proved earlier the same day)."
        ),
    }


def check_sources() -> list[str]:
    problems = spider_data.check_sources(("tables.json", "ESMFalseNegatives.tsv"))
    if not problems:
        pairs = load_pairs()
        if len(pairs) != 359 or sum(len(p.rows) for p in pairs) != 558:
            problems.append(f"expected 558 rows in 359 distinct pairs, found {sum(len(p.rows) for p in pairs)} in {len(pairs)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--split", choices=solver.SPLITS, default=None,
        help="dev (default) for development runs; held-out is for final scoring only; all reports both apart (default with --write-results)",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--show", default="", help="comma-separated outcomes to list (proven, refuted, unknown, unsupported)")
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--check-sources", action="store_true", help="download the pinned files and check their digests and counts")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/spider-esm.json (scores every pair: --split all)")
    args = parser.parse_args(argv)
    if args.check_sources:
        problems = check_sources()
        print("\n".join(problems) or "the pinned files match")
        return 1 if problems else 0
    try:
        split = solver.choose_split(args.split, args.write_results)
    except ValueError as error:
        parser.error(str(error))
    from bench_common import quiet, write_results

    quiet()
    pairs = load_pairs()
    selected = solver.split_cases(pairs, split)
    started = time.time()
    results = run(selected, args.jobs)
    for part in ("dev", "held-out") + (("all",) if split == "all" else ()):
        rows = results if part == "all" else [r for r in results if r["held_out"] == (part == "held-out")]
        for group, counts in summarize(rows).items():
            print(f"{part:9} {group:10} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    causes = Counter(r["cause"] for r in results if r["outcome"] == "refuted" and (split != "held-out" or True))
    print("refuted causes: " + ", ".join(f"{k} {v}" for k, v in sorted(causes.items())))
    print(f"{len(results)} pairs ({split}) in {time.time() - started:.0f}s")
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    for pair, result in zip(selected, results):
        if result["outcome"] in show or result["wrong"]:
            print(f"\n{result['id']} [{pair.database}] {result['outcome']} {result['how']} {result['cause']}{' WRONG' if result['wrong'] else ''}")
            if pair.held_out and split != "held-out":
                print("  held out: rerun with --split held-out to see the SQL")
            else:
                print(f"  gold: {pair.gold}\n  pred: {pair.pred}\n  reason: {'; '.join(pair.reasons)[:200]}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if args.write_results:
        write_results("spider-esm", results_row(results))
    return 1 if any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
