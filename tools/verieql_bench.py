"""Score KumoSQL on the VeriEQL benchmark suites (LeetCode, Literature, Calcite-397) with no LLM.

Each case is a pair of queries plus a schema and integrity constraints. The
benchmark files carry no labels, so every verdict has to stand on evidence:

* ``different`` - a database satisfying the constraints was found and *both queries were
  run on it* (DuckDB) with different result bags.
* ``equivalent`` - the SMT prover (``kumosql.prover``'s algebraic + z3 pipeline) proved the
  bags equal under the schema's keys and NOT NULL columns.
* ``unknown`` - neither. An unknown is always better than a wrong verdict.
* ``wrong`` - an ``equivalent`` verdict that a second, independent random search (other seed,
  more databases) refutes, or that VeriEQL's published counterexample (replayed on DuckDB)
  refutes. This must stay 0.

    python tools/verieql_bench.py literature
    python tools/verieql_bench.py calcite --limit 100
    python tools/verieql_bench.py leetcode --jobs 4 --audit

The suites are downloaded once into ``~/.cache/kumosql/verieql`` (they are CC BY-NC-SA 4.0
and are not copied into this repository). They come from https://github.com/VeriEQL/VeriEQL
(Pan Yi's group, see the README for the paper and licence); nothing from that code is
vendored here, only its benchmark files are read.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import signal
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql import counterexample as cx  # noqa: E402

COMMIT = "493cbb81000205e33b0623cfd1c39106fa035fae"
BASE = f"https://raw.githubusercontent.com/VeriEQL/VeriEQL/{COMMIT}"
SUITES = {
    "literature": ("benchmarks/literature/literature.jsonlines", "experiments/2025_10_31/literature.out"),
    "calcite": ("benchmarks/calcite/calcite2.jsonlines", "experiments/2025_10_31/calcite.out"),
    "leetcode": ("benchmarks/leetcode/leetcode.jsonlines", "experiments/2025_10_31/leetcode.out"),
}
CACHE = Path(os.environ.get("KUMOSQL_VERIEQL_CACHE", Path.home() / ".cache" / "kumosql" / "verieql"))


def fetch(path: str) -> Path:
    target = CACHE / path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{BASE}/{path}", timeout=120) as response:
            data = response.read()
        target.write_bytes(data)
    return target


def load_cases(suite: str) -> list[dict]:
    path = fetch(SUITES[suite][0])
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for position, case in enumerate(cases):
        case["index"] = position  # the files restart their own index per problem, so number the cases here
    return cases


def load_veri_states(suite: str) -> dict[tuple, dict]:
    """VeriEQL's own published outcome per query pair (used only to audit, never to decide)."""

    path = fetch(SUITES[suite][1])
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            out[tuple(record["pair"])] = record
    return out


# --- schema and constraints -------------------------------------------------------------


def build_spec(case: dict) -> cx.Spec:
    tables = {}
    for name, columns in case["schema"].items():
        cols = []
        for column, kind in columns.items():
            if kind.startswith("ENUM"):
                cols.append(cx.Column(column, "ENUM", values=tuple(kind.split(",")[1:])))
            else:
                cols.append(cx.Column(column, kind))
        tables[name] = cx.Table(name, cols)
    spec = cx.Spec(tables)

    def split(ref: str) -> tuple[str, str]:
        for table in tables:
            if ref.startswith(table + "__") and ref[len(table) + 2 :] in {c.name for c in tables[table].columns}:
                return table, ref[len(table) + 2 :]
        raise KeyError(ref)

    for constraint in case.get("constraint") or []:
        (kind, body), = constraint.items()
        if kind == "primary":
            refs = [split(v["value"]) for v in body]
            table = refs[0][0]
            tables[table].primary_key = tuple(c for _, c in refs)
            for _, c in refs:
                tables[table].column(c).not_null = True
        elif kind == "not_null":
            t, c = split(body["value"])
            tables[t].column(c).not_null = True
        elif kind == "foreign":
            (ct, cc), (pt, pc) = split(body[0]["value"]), split(body[1]["value"])
            spec.foreign_keys.append((ct, cc, pt, pc))
        elif kind == "unique":
            refs = [split(v["value"]) for v in body]
            tables[refs[0][0]].unique.append(tuple(c for _, c in refs))
        elif kind in ("inc", "consec"):
            t, c = split(body["value"])
            tables[t].sequential = tables[t].sequential + (c,)
            tables[t].column(c).not_null = True
        else:
            spec.checks.append(_check(constraint, split))
    return spec


def _check(constraint: dict, split) -> cx.Check:
    used: list[str] = []

    def compile_expr(node):
        if isinstance(node, dict):
            if "value" in node:
                t, c = split(node["value"])
                if t not in used:
                    used.append(t)
                return lambda rows, t=t, c=c: rows[t][c]
            if "literal" in node:
                return lambda rows, v=node["literal"]: v
            if "date" in node:
                return lambda rows, v=node["date"]: v
            (op, args), = node.items()
            if op in ("and", "or"):
                parts = [compile_expr(a) for a in args]
                if op == "and":
                    return lambda rows: _and([p(rows) for p in parts])
                return lambda rows: _or([p(rows) for p in parts])
            if op == "not":
                inner = compile_expr(args)
                return lambda rows: None if inner(rows) is None else not inner(rows)
            if op == "imply":
                a, b = compile_expr(args[0]), compile_expr(args[1])
                return lambda rows: _or([None if a(rows) is None else not a(rows), b(rows)])
            if op == "in":
                value, options = compile_expr(args[0]), [compile_expr(a) for a in args[1]]
                return lambda rows: None if value(rows) is None else value(rows) in [o(rows) for o in options]
            if op == "between":
                v, lo, hi = (compile_expr(a) for a in args)
                return lambda rows: _and([_cmp("gte", v(rows), lo(rows)), _cmp("lte", v(rows), hi(rows))])
            if op in ("gt", "gte", "lt", "lte", "eq", "neq"):
                a, b = compile_expr(args[0]), compile_expr(args[1])
                return lambda rows: _cmp(op, a(rows), b(rows))
            raise KeyError(op)
        return lambda rows, v=node: v

    test = compile_expr(constraint)
    tables = tuple(used)
    return cx.Check(tables, lambda *picked, test=test, tables=tables: test(dict(zip(tables, picked))))


def _cmp(op, a, b):
    if a is None or b is None:
        return None
    try:
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b, "eq": a == b, "neq": a != b}[op]
    except TypeError:
        return None


def _and(values):
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def _or(values):
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


# --- verdicts ---------------------------------------------------------------------------

EQUIVALENT, DIFFERENT, UNKNOWN, WRONG = "equivalent", "different", "unknown", "wrong"


def prove(case: dict, spec: cx.Spec, timeout_ms: int):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import TableConstraints

    lower = {t.name.lower(): t for t in spec.tables.values()}
    schema = {n: [c.name.lower() for c in t.columns] for n, t in lower.items()}
    constraints = {
        n: TableConstraints(
            not_null=frozenset(c.name.lower() for c in t.columns if c.not_null),
            keys=tuple(tuple(k.lower() for k in key) for key in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for n, t in lower.items()
    }
    types = {n: {c.name.lower(): c.type for c in t.columns} for n, t in lower.items()}
    left, right = case["pair"]
    return prove_equivalent_algebraic(
        left, right, schema=schema, constraints=constraints, types=types, compare_names=False,
        dialect="mysql", exact_arithmetic=True, timeout_ms=timeout_ms,
    )


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout


@dataclass
class Verdict:
    index: int
    status: str
    detail: str = ""
    seconds: float = 0.0


def decide(case: dict, *, trials: int = 150, recheck_trials: int = 600, timeout_ms: int = 3000, budget: int = 30) -> Verdict:
    """The program's verdict for one case: search for a counterexample, else try to prove, else unknown."""

    start = time.time()
    index = case["index"]
    try:
        spec = build_spec(case)
    except Exception as error:  # unreadable constraints: no verdict
        return Verdict(index, UNKNOWN, f"constraints: {type(error).__name__}", time.time() - start)
    left, right = case["pair"]
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(budget)
    try:
        try:
            searcher = cx.Searcher(spec, left, right)
        except Exception as error:
            return Verdict(index, UNKNOWN, f"parse: {type(error).__name__}", time.time() - start)
        if not searcher.runs():
            return Verdict(index, UNKNOWN, "a query DuckDB rejects", time.time() - start)
        found = searcher.search(trials, seed=index)
        if found is not None:
            return Verdict(index, DIFFERENT, "counterexample", time.time() - start)
        try:
            result = prove(case, spec, timeout_ms)
        except _Timeout:
            raise
        except Exception as error:
            return Verdict(index, UNKNOWN, f"prover crash: {type(error).__name__}", time.time() - start)
        if result.proven:
            again = searcher.search(recheck_trials, seed=index + 1_000_003)
            if again is not None:
                return Verdict(index, WRONG, "proved, then refuted by a counterexample", time.time() - start)
            return Verdict(index, EQUIVALENT, "proof", time.time() - start)
        return Verdict(index, UNKNOWN, result.reason[:80], time.time() - start)
    except _Timeout:
        return Verdict(index, UNKNOWN, "time budget", time.time() - start)
    finally:
        signal.alarm(0)


def _work(args):
    case, options = args
    return decide(case, **options)


# --- auditing against VeriEQL's published outcome ---------------------------------------


def replay(case: dict, record: dict) -> bool | None:
    """Does VeriEQL's counterexample for this case make the two queries differ on DuckDB?"""

    import duckdb
    import sqlglot

    script = record.get("counterexample")
    if not script:
        return None
    db = duckdb.connect(":memory:")
    statements = [s for s in sqlglot.parse(script, read="mysql") if s is not None]
    try:
        results = []
        for statement in statements:
            sql = statement.sql(dialect="duckdb")
            if statement.key == "select":
                results.append(cx._bag(db.execute(sql).fetchall()))
            else:
                db.execute(sql)
    except Exception:
        return None
    return results[-2] != results[-1] if len(results) >= 2 else None


@dataclass
class SuiteResult:
    suite: str
    total: int = 0
    counts: Counter = field(default_factory=Counter)
    verdicts: list = field(default_factory=list)
    seconds: float = 0.0
    audit: Counter = field(default_factory=Counter)
    audit_cases: dict = field(default_factory=dict)


def run_suite(suite: str, *, limit: int | None = None, offset: int = 0, every: int = 1, jobs: int = 1, audit: bool = False, **options) -> SuiteResult:
    cases = load_cases(suite)[offset : None if limit is None else offset + limit : every]
    result = SuiteResult(suite, total=len(cases))
    start = time.time()
    work = [(case, options) for case in cases]
    if jobs > 1:
        with multiprocessing.Pool(jobs) as pool:
            verdicts = list(pool.imap(_work, work, chunksize=8))
    else:
        verdicts = [_work(item) for item in work]
    result.verdicts = verdicts
    result.counts = Counter(v.status for v in verdicts)
    if audit:
        states = load_veri_states(suite)
        by_index = {c["index"]: c for c in cases}
        for verdict in verdicts:
            record = states.get(tuple(by_index[verdict.index]["pair"]))
            if not record:
                continue
            veri = (record.get("states") or ["?"])[-1]
            result.audit[(verdict.status, veri)] += 1
            if verdict.status == EQUIVALENT and veri == "NEQ":
                if replay(by_index[verdict.index], record):
                    result.audit[("WRONG-vs-VeriEQL-counterexample", "")] += 1
                    result.audit_cases.setdefault("equivalent_but_refuted", []).append(verdict.index)
                else:
                    result.audit_cases.setdefault("equivalent_vs_NEQ_not_replayable", []).append(verdict.index)
            if verdict.status == DIFFERENT and veri in ("EQU", "SYN"):
                result.audit_cases.setdefault("different_vs_EQU", []).append(verdict.index)
    result.seconds = time.time() - start
    return result


def wrong_count(result: SuiteResult) -> int:
    return result.counts[WRONG] + len(result.audit_cases.get("equivalent_but_refuted", []))


def format_result(result: SuiteResult) -> str:
    c = result.counts
    line = (
        f"{result.suite:10} cases {result.total:6}  equivalent {c[EQUIVALENT]:6}  different {c[DIFFERENT]:6}  "
        f"unknown {c[UNKNOWN]:6}  wrong {wrong_count(result):3}  {result.seconds:7.1f}s"
    )
    return line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("suites", nargs="*", default=list(SUITES))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--every", type=int, default=1, help="take every Nth case")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--audit", action="store_true", help="compare with VeriEQL's published outcomes")
    parser.add_argument("--trials", type=int, default=150)
    parser.add_argument("--timeout-ms", type=int, default=3000)
    parser.add_argument("--dump", help="write one JSON line per case to this file")
    args = parser.parse_args(argv)
    bad = 0
    for suite in args.suites:
        result = run_suite(
            suite, limit=args.limit, offset=args.offset, every=args.every, jobs=args.jobs, audit=args.audit,
            trials=args.trials, timeout_ms=args.timeout_ms,
        )
        print(format_result(result))
        if args.audit:
            for key, value in sorted(result.audit.items()):
                print(f"   ours={key[0]:10} VeriEQL={key[1]:4} {value}")
            for key, indexes in result.audit_cases.items():
                print(f"   {key}: {indexes[:30]}")
        if args.dump:
            with open(args.dump, "a", encoding="utf-8") as out:
                for v in result.verdicts:
                    out.write(json.dumps({"suite": suite, "index": v.index, "status": v.status, "detail": v.detail}) + "\n")
        bad += wrong_count(result)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
