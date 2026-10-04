"""Run the conditional verdict over constraint-touched benchmark pairs, with the schema's constraints taken away.

A candidate file lists benchmark pairs whose queries touch a declared constraint in a WHERE, join, GROUP BY, DISTINCT, IN
or COUNT (the mining rule is in ``docs/evals/conditional-equivalence.md``). For each pair this keeps the schema's tables,
columns and types but none of its keys, NOT NULL columns or foreign keys, runs ``prove_equivalent_algebraic(...,
conditional=True)`` and records which conditions the proof needs. It then compares them with what the schema declared and
re-checks every conditional proof on random databases that meet only the returned conditions.

Corpora read here: VeriEQL Calcite and Literature (pinned download, ``tools/verieql_bench.py``) and SQLSolver Calcite
(``tests/fixtures/sqlsolver``). WeTune Calcite and the Cosette examples are not run. None of the three defines a held-out split of its own
(mined Calcite pairs and the adapted Cosette pairs, which do, are not in this list).

    python tools/conditional_candidates.py candidates.json --json out.json
    python tools/conditional_candidates.py candidates.json --corpus sqlsolver-calcite --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import conditional_bench as bench  # noqa: E402
from conditional_bench import Outcome  # noqa: E402

CORPORA = ("verieql-calcite", "verieql-literature", "sqlsolver-calcite")
SKIPPED = {"wetune-calcite": "WeTune's catalog format has no loader here", "cosette-examples": "four pairs, split by the Cosette eval's own hash"}


# --- SQLSolver Calcite --------------------------------------------------------------------------------------


def _bare_tables(tables):
    import sqlsolver_bench as sol

    return {
        n: sol.Table(t.name, [sol.Column(c.name, c.type) for c in t.columns]) for n, t in tables.items()
    }


def _with_conditions(bare, conditions):
    import sqlsolver_bench as sol

    out = {n: sol.Table(t.name, [sol.Column(c.name, c.type) for c in t.columns]) for n, t in bare.items()}
    for c in conditions:
        table = out[c.table]
        if c.kind == "not_null":
            next(col for col in table.columns if col.name == c.columns[0]).not_null = True
        elif c.kind == "unique":
            table.unique.append(tuple(c.columns))  # a unique key allows NULLs: it adds no NOT NULL fact
    return out


def decide_sqlsolver(job: tuple[int, str, str]) -> Outcome:
    import sqlsolver_bench as sol
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.conditional_equivalence import with_conditions
    from kumosql.smt_equivalence import SmtStatus

    index, left, right = job
    start = time.time()
    base = dict(key=str(index))
    declared = sol.load_schema(sol.FIXTURES / "calcite.schema.sql")
    bare = _bare_tables(declared)
    schema = {t.name: [c.name for c in t.columns] for t in bare.values()}
    types = {t.name: {c.name: c.type for c in t.columns} for t in bare.values()}
    options = dict(schema=schema, types=types, compare_names=False, dialect="mysql", exact_arithmetic=True)
    try:
        result = prove_equivalent_algebraic(sol.spark_days(left), sol.spark_days(right), conditional=True, **options)
    except Exception as error:
        return Outcome("unknown", f"{type(error).__name__}: {error}"[:200], seconds=time.time() - start, **base)
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return Outcome("equivalent", "proved outright, no condition", seconds=time.time() - start, **base)
    if result.status is not SmtStatus.PROVEN_CONDITIONALLY:
        return Outcome("unknown", result.reason[:200], seconds=time.time() - start, **base)
    conditions = list(result.conditions)
    detail = [c.to_json() for c in conditions]
    if any(c.kind == "foreign_key" for c in conditions):
        # this harness's random databases do not repair foreign keys, so the proof cannot be re-run on a database that meets them
        return Outcome("unchecked", result.reason, detail, 0, seconds=time.time() - start, **base)
    import duckdb  # noqa: F401  (the harness needs it; fail loudly when it is missing)

    db = sol.new_database(bare)
    stricter = _with_conditions(bare, conditions)
    found = sol.differ(left, right, stricter, db, trials=40, seed=index + 17)
    if found not in (None, False):
        return Outcome("wrong", "proved under conditions but a database that meets them separates the queries", detail, seconds=time.time() - start, **base)
    if found is False:
        return Outcome("unchecked", result.reason, detail, 0, seconds=time.time() - start, **base)
    without = sol.differ(left, right, bare, sol.new_database(bare), trials=30, seed=index + 29)
    minimal = True
    for dropped in conditions:
        rest = [c for c in conditions if c is not dropped]
        again = prove_equivalent_algebraic(sol.spark_days(left), sol.spark_days(right), constraints=with_conditions(None, rest) or None, **options)
        if again.status is SmtStatus.PROVEN_EQUIVALENT:
            minimal = False
    return Outcome(
        "conditional", result.reason, detail, 80, needed=without not in (None, False), minimal=minimal, seconds=time.time() - start, **base
    )


# --- driver -------------------------------------------------------------------------------------------------


def _with_schema(corpus: str, payload) -> bool:
    """Whether the pair is proved outright when the schema's own constraints are given to the prover."""

    if corpus == "sqlsolver-calcite":
        import sqlsolver_bench as sol

        _, left, right = payload
        return sol.default_prove(left, right, sol.load_schema(sol.FIXTURES / "calcite.schema.sql"))
    return bench.decide_verieql(payload, declared=True).kind == "equivalent"


def _job(args):
    corpus, payload = args
    try:
        outcome = decide_sqlsolver(payload) if corpus == "sqlsolver-calcite" else bench.decide_verieql(payload, declared=False)
        return outcome, (_with_schema(corpus, payload) if outcome.kind == "unknown" else None)
    except Exception as error:
        return Outcome("crash", f"{type(error).__name__}: {error}"[:200], key=str(payload[0] if isinstance(payload, tuple) else payload.get("index"))), None


def _describe(condition: dict) -> str:
    columns = ",".join(condition["columns"])
    if condition["kind"] == "foreign_key":
        return f"FOREIGN KEY {condition['table']}({columns})->{condition['parent']}({','.join(condition['parent_columns'])})"
    return f"{'NOT NULL' if condition['kind'] == 'not_null' else 'UNIQUE'} {condition['table']}({columns})"


def _declared_facts(texts: list[str]) -> tuple[set[tuple[str, tuple[str, ...]]], set[tuple[str, tuple[str, ...]]], set[str]]:
    """``(NOT NULL (table, column) facts, key (table, columns) facts, foreign key spellings)`` the schema declares for a pair
    (a primary key is a key and gives NOT NULL)."""

    not_null, keys, foreign = set(), set(), set()
    for text in texts:
        match = re.match(r"(NOT NULL|PRIMARY KEY|UNIQUE|FOREIGN KEY)\s+(\w+)\((.*?)\)(?:\s+REFERENCES\s+(\w+)\((.*?)\))?", text)
        if not match:
            continue
        kind, table, columns, parent, parent_columns = match.groups()
        table, columns = table.lower(), tuple(c.strip().lower() for c in columns.split(","))
        if kind == "FOREIGN KEY" and parent:
            foreign.add(f"{table}({','.join(columns)})->{parent.lower()}({','.join(c.strip().lower() for c in parent_columns.split(','))})")
        elif kind == "NOT NULL":
            not_null.add((table, columns[0]))
        else:
            keys.add((table, columns))
            if kind == "PRIMARY KEY":
                not_null.update((table, c) for c in columns)
    return not_null, keys, foreign


def _beyond_schema(conditions: list[dict], declared: list[str]) -> list[str]:
    """The conditions a proof used that the schema does not declare (a key licenses every superset of its columns)."""

    not_null, keys, foreign = _declared_facts(declared)
    out = []
    for c in conditions:
        columns = tuple(c["columns"])
        if c["kind"] == "not_null":
            licensed = (c["table"], columns[0]) in not_null
        elif c["kind"] == "unique":
            licensed = any(table == c["table"] and set(key) <= set(columns) for table, key in keys)
        else:
            licensed = f"{c['table']}({','.join(columns)})->{c['parent']}({','.join(c['parent_columns'])})" in foreign
        if not licensed:
            out.append(_describe(c))
    return sorted(out)


def load_candidates(path: Path, corpora, limit: int | None):
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["pairs"]  # none of the three corpora read here defines a held-out split
    jobs, declared = [], {}
    import sqlsolver_bench as sol
    import verieql_bench as veri

    for corpus in corpora:
        mine = [r for r in records if r["benchmark"] == corpus]
        if limit:
            mine = mine[:limit]
        if corpus == "sqlsolver-calcite":
            pairs = sol.load_pairs(sol.FIXTURES / "calcite_pairs.txt")
            where = {p: i for i, p in enumerate(pairs)}
            for r in mine:
                i = where.get((r["left"], r["right"]))
                if i is not None:
                    jobs.append((corpus, (i, r["left"], r["right"])))
                    declared[(corpus, str(i))] = r
        else:
            cases = veri.load_cases(corpus.split("-", 1)[1])
            where = {tuple(c["pair"]): c for c in cases}
            for r in mine:
                case = where.get((r["left"], r["right"]))
                if case is not None:
                    jobs.append((corpus, case))
                    declared[(corpus, str(case["index"]))] = r
    return jobs, declared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--corpus", action="append", choices=CORPORA)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    corpora = args.corpus or list(CORPORA)
    jobs, declared = load_candidates(args.candidates, corpora, args.limit)
    start = time.time()
    with ProcessPoolExecutor(max_workers=args.workers or os.cpu_count()) as pool:
        outcomes = list(pool.map(_job, jobs, chunksize=2))
    records = []
    for (corpus, _), (outcome, with_schema) in zip(jobs, outcomes):
        record = declared.get((corpus, outcome.key), {})
        records.append(
            {
                "corpus": corpus,
                "key": outcome.key,
                "pair_id": record.get("pair_id"),
                "kind": outcome.kind,
                "detail": outcome.detail,
                "conditions": sorted(_describe(c) for c in outcome.conditions),
                "beyond_schema": _beyond_schema(outcome.conditions, record.get("constraints_declared", [])),
                "proved_with_schema": with_schema,
                "validated": outcome.validated,
                "needed": outcome.needed,
                "minimal": outcome.minimal,
            }
        )
    print(f"{len(records)} pairs in {time.time() - start:.0f}s")
    for corpus in corpora:
        mine = [r for r in records if r["corpus"] == corpus]
        counts = Counter(r["kind"] for r in mine)
        conditional = [r for r in mine if r["kind"] == "conditional"]
        kinds = Counter(c.split(" ", 1)[0] if not c.startswith("NOT NULL") else "NOT NULL" for r in conditional for c in r["conditions"])
        print(f"{corpus}: {len(mine)} pairs: {dict(counts)}")
        print(f"  conditional proofs {len(conditional)}: conditions by kind {dict(kinds)}; "
              f"{sum(1 for r in conditional if r['beyond_schema'])} use a condition the schema does not declare; "
              f"{sum(1 for r in conditional if r['needed'])} separated without conditions; {sum(1 for r in conditional if r['minimal'])} minimal")
        unknown = [r for r in mine if r["kind"] == "unknown"]
        print(f"  unknown {len(unknown)}: {sum(1 for r in unknown if r['proved_with_schema'])} are proved outright when the schema's own constraints are given")
    if args.json:
        args.json.write_text(json.dumps(records, indent=1))
    return 1 if any(r["kind"] in ("wrong", "crash") for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
