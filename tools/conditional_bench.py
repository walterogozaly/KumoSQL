"""Score the conditional-equivalence verdict: pairs the prover settles only under stated conditions.

``prove_equivalent_algebraic(..., conditional=True)`` retries a pair it cannot prove under facts
taken from the queries (NOT NULL columns, unique keys, foreign keys) and reports the minimal set
that proves it (``kumosql.conditional_equivalence``). This harness counts those verdicts on two
corpora whose "equivalent" labels assume keys and constraints their files drop or only partly keep,
and checks every one of them for soundness.

* ``singh``: the 2,800 LeetCode pairs of Singh & Bedathur (``tools/singh_bedathur_bench.py``): table and
  column names only, no keys, no NOT NULL facts (MySQL dialect). Pairs labelled equivalent get a valid
  counterexample without constraints in that eval, so this is where conditions have the most to give.
* ``verieql-leetcode``: the same problems as VeriEQL ships them (``tools/verieql_bench.py``), with each
  problem's types, primary keys, NOT NULL columns and foreign keys declared. Conditions here are facts
  *beyond* the declared ones.

A pair is ``equivalent`` when the prover proves it outright (not counted again here), ``conditional``
when the proof needs conditions, ``unknown`` otherwise. Nothing reads the published labels while
deciding; they are only compared afterwards.

Soundness checks (a failure makes the pair ``wrong``, which must stay 0):

* every conditional proof is re-run on random DuckDB databases *repaired to satisfy the conditions*
  (NULLs filled in, duplicate keys dropped, orphaned foreign keys pointed at a parent or dropped); a database
  on which the two queries return different bags would make the pair wrong;
* the conditions of a proof are the minimal ones: the prover itself fails when any one is dropped (checked
  again here, by running it without each condition in turn);
* the conditions are needed: the share of conditional pairs for which a database *without* them
  separates the queries is reported (a pair no database can separate would not need them).

    python tools/conditional_bench.py singh --split dev --sample 200
    python tools/conditional_bench.py singh                        # all 2,800 pairs
    python tools/conditional_bench.py verieql --every 8 --jobs 4
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402

import singh_bedathur_bench as singh  # noqa: E402
from kumosql.conditional_equivalence import Condition, broken_by, with_conditions  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

VALIDATION_DATABASES = 900  # random databases per conditional proof (the Singh eval re-checks its proofs on 900)
WITHOUT_DATABASES = 300  # random databases without the conditions, to show they are needed


@dataclass
class Outcome:
    kind: str  # equivalent | conditional | refuted | unknown | wrong | unchecked (a conditional proof the executed check cannot re-run) | crash (a harness bug: must stay 0)
    detail: str = ""
    conditions: list[dict] = field(default_factory=list)
    validated: int = 0  # databases that met the conditions and ran on both queries
    needed: bool | None = None  # a database without the conditions separates the queries
    minimal: bool | None = None
    label: str = ""
    key: str = ""
    held_out: bool = False
    seconds: float = 0.0


# --- repairing a random database so that it meets the conditions ------------------------------------------


def _pools(pair: singh.Pair, kinds, domains) -> dict[tuple[str, str], list]:
    numbers, strings, dates = domains
    whole = [n for n in numbers if isinstance(n, int)] or [0, 1, 2]
    fractions = sorted(set(numbers) | {0.5, 1.25, 2.75})
    pools = {}
    for table, columns in pair.tables.items():
        for column in columns:
            kind = kinds.get((table, column), "BIGINT")
            pools[(table, column)] = {"VARCHAR": list(strings), "DATE": list(dates)}.get(kind, fractions if kind.startswith("DECIMAL") else whole)
    return pools


def legalize(data: dict[str, list[list]], conditions: list[Condition], pair: singh.Pair, pools, rng: random.Random) -> dict[str, list[list]] | None:
    """``data`` repaired so every condition holds, or ``None`` when that did not work out."""

    columns = pair.tables
    data = {t: [list(r) for r in rows] for t, rows in data.items()}

    def index(table: str, column: str) -> int | None:
        return columns[table].index(column) if table in columns and column in columns[table] else None

    for _ in range(5):
        for c in conditions:
            if c.kind == "not_null" and c.table in data:
                at = index(c.table, c.columns[0])
                for row in data[c.table]:
                    if at is not None and row[at] is None:
                        row[at] = rng.choice(pools[(c.table, c.columns[0])])
        for c in conditions:
            if c.kind == "unique" and c.table in data:
                at = [index(c.table, name) for name in c.columns]
                seen, kept = set(), []
                for row in data[c.table]:
                    key = tuple(row[i] for i in at)
                    if None not in key:
                        if key in seen:
                            continue
                        seen.add(key)
                    kept.append(row)
                data[c.table] = kept
        for c in conditions:
            if c.kind == "foreign_key" and c.table in data and c.parent in data:
                child_at = [index(c.table, name) for name in c.columns]
                parent_at = [index(c.parent, name) for name in c.parent_columns]
                parents = [tuple(r[i] for i in parent_at) for r in data[c.parent]]
                kept = []
                for row in data[c.table]:
                    key = tuple(row[i] for i in child_at)
                    if None in key or key in parents:
                        kept.append(row)
                    elif parents:
                        for i, value in zip(child_at, rng.choice(parents)):
                            row[i] = value
                        kept.append(row)
                data[c.table] = kept
        as_dicts = {t: [dict(zip(columns[t], r)) for r in rows] for t, rows in data.items()}
        if not any(broken_by(c, as_dicts) for c in conditions):
            return data
    return None


# --- Singh & Bedathur ---------------------------------------------------------------------------------------


def _prove_options(pair: singh.Pair) -> dict:
    return dict(schema=pair.tables, compare_names=False, dialect="mysql", timeout_ms=4000)


def _singh_prove(pair: singh.Pair):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.canonical_rules import canonicalize

    options = _prove_options(pair)
    first = prove_equivalent_algebraic(pair.left, pair.right, conditional=True, **options)
    if first.status in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        return first, (pair.left, pair.right)
    try:
        left, right = canonicalize(pair.left, "mysql", pair.tables), canonicalize(pair.right, "mysql", pair.tables)
    except sqlglot.errors.SqlglotError:
        return first, (pair.left, pair.right)
    if (left, right) == (pair.left, pair.right):
        return first, (pair.left, pair.right)
    second = prove_equivalent_algebraic(left, right, conditional=True, **options)
    return (second, (left, right)) if second.status in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY) else (first, (pair.left, pair.right))


def _singh_search(pair: singh.Pair, trees, conditions: list[Condition], trials: int, seed: int):
    """``(separating database or None, databases run)`` on random databases repaired to meet ``conditions``."""

    import duckdb

    from kumosql.duckdb_load import insert_rows, run_unoptimized

    kinds = singh.column_kinds(trees, pair.tables)
    domains = singh.literal_domains(trees)
    sizes = singh._row_counts(trees)
    try:
        left_sql, right_sql = singh.translate(pair.left), singh.translate(pair.right)
    except sqlglot.errors.SqlglotError:
        return None, 0
    pools = _pools(pair, kinds, domains)
    db = singh.new_database(pair.tables, kinds)
    rng = random.Random(seed)
    used = [t for t in pair.tables if re.search(rf"\b{re.escape(t)}\b", pair.left + " " + pair.right, re.I)]
    ran = 0
    for attempt in range(trials):
        data = singh._random_tables(pair, used, kinds, domains, rng, sizes, skewed=attempt % 2 == 1)
        if conditions:
            data = legalize(data, conditions, pair, pools, rng)
            if data is None:
                continue
        try:
            for table in used:
                db.execute(f'DELETE FROM "{table}"')
                insert_rows(db, f'"{table}"', data.get(table, []))
            a = singh.normalise(db.execute(left_sql).fetchall())
            b = singh.normalise(db.execute(right_sql).fetchall())
            if a != b and [singh.normalise(rows) for rows in run_unoptimized(db, left_sql, right_sql)] != [a, b]:
                continue
        except duckdb.Error:
            continue
        ran += 1
        if a != b:
            return {"tables": {t: [dict(zip(pair.tables[t], r)) for r in data.get(t, [])] for t in used}}, ran
    return None, ran


def _conditions_of(result) -> list[Condition]:
    return list(result.conditions)


def _each_needed(pair: singh.Pair, conditions: list[Condition], options: dict) -> bool:
    """Dropping any one condition makes the prover fail (the set is minimal)."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    for dropped in conditions:
        rest = [c for c in conditions if c is not dropped]
        result = prove_equivalent_algebraic(pair.left, pair.right, constraints=with_conditions(None, rest) or None, **options)
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            return False
    return True


def _refuted_or_unknown(pair: singh.Pair, trees, texts, result, start: float, base: dict) -> Outcome:
    """Not proved, even under conditions: ``refuted`` when a database that meets *every* candidate condition separates
    the queries (no set of conditions can help), otherwise ``unknown``."""

    from kumosql.conditional_equivalence import candidate_conditions

    checked = singh.Pair(pair.index, texts[0], texts[1], pair.tables, pair.gold)
    if singh.unsafe_to_refute(*trees):
        return Outcome("unknown", "no counterexample allowed (LIMIT with ties, LOWER, or non-determinism)", seconds=time.time() - start, **base)
    candidates = candidate_conditions(checked.left, checked.right, schema=pair.tables, dialect="mysql")
    witness, _ = _singh_search(checked, trees, candidates, VALIDATION_DATABASES // 3, seed=303)
    if witness:
        return Outcome("refuted", f"a database meeting all {len(candidates)} candidate conditions separates the queries", [c.to_json() for c in candidates], seconds=time.time() - start, **base)
    return Outcome("unknown", result.reason[:200], seconds=time.time() - start, **base)


def decide_singh(pair: singh.Pair) -> Outcome:
    start = time.time()
    base = dict(label=pair.gold, key=pair.key, held_out=pair.held_out)
    try:
        trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
        result, texts = _singh_prove(pair)
    except Exception as error:  # a crash is a failure to prove, never a proof
        return Outcome("unknown", f"{type(error).__name__}: {error}"[:200], seconds=time.time() - start, **base)
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return Outcome("equivalent", "proved outright", seconds=time.time() - start, **base)
    if result.status is not SmtStatus.PROVEN_CONDITIONALLY:
        return _refuted_or_unknown(pair, trees, texts, result, start, base)
    conditions = _conditions_of(result)
    checked = singh.Pair(pair.index, texts[0], texts[1], pair.tables, pair.gold)
    witness, ran = _singh_search(checked, trees, conditions, VALIDATION_DATABASES, seed=202)
    if witness:
        return Outcome("wrong", "proved under conditions but DuckDB separates the queries on a database that meets them", [c.to_json() for c in conditions], ran, **base)
    without, _ = _singh_search(checked, trees, [], WITHOUT_DATABASES, seed=7)
    minimal = _each_needed(checked, conditions, _prove_options(pair))
    return Outcome(
        "conditional", result.reason, [c.to_json() for c in conditions], ran, needed=without is not None, minimal=minimal,
        seconds=time.time() - start, **base,
    )


def _decide_singh_job(pair: singh.Pair) -> Outcome:
    return decide_singh(pair)


# --- VeriEQL LeetCode ---------------------------------------------------------------------------------------


def decide_verieql(case: dict) -> Outcome:
    import verieql_bench as veri
    from kumosql import counterexample as cx
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import TableConstraints

    start = time.time()
    index = case["index"]
    base = dict(key=str(index))
    try:
        spec = veri.build_spec(case)
        left, right, predicates = veri.repaired_pair(case, spec)
    except Exception as error:
        return Outcome("unknown", f"constraints: {type(error).__name__}", seconds=time.time() - start, **base)
    if predicates:
        return Outcome("unknown", "uninterpreted predicates", seconds=time.time() - start, **base)
    lower = {t.name.lower(): t for t in spec.tables.values()}
    schema = {n: [c.name.lower() for c in t.columns] for n, t in lower.items()}
    types = {n: {c.name.lower(): c.type for c in t.columns} for n, t in lower.items()}
    constraints = {
        n: TableConstraints(
            not_null=frozenset(c.name.lower() for c in t.columns if c.not_null),
            keys=tuple(tuple(k.lower() for k in key) for key in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
            foreign_keys=tuple(
                ((column.lower(),), parent.lower(), (parent_column.lower(),))
                for child, column, parent, parent_column in spec.foreign_keys
                if child.lower() == n
            ),
        )
        for n, t in lower.items()
    }
    options = dict(schema=schema, types=types, compare_names=False, dialect="mysql", exact_arithmetic=True, timeout_ms=3000)
    try:
        result = prove_equivalent_algebraic(left, right, constraints=constraints, conditional=True, **options)
    except Exception as error:
        return Outcome("unknown", f"{type(error).__name__}: {error}"[:200], seconds=time.time() - start, **base)
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return Outcome("equivalent", "proved outright", seconds=time.time() - start, **base)
    if result.status is not SmtStatus.PROVEN_CONDITIONALLY:
        return Outcome("unknown", result.reason[:200], seconds=time.time() - start, **base)
    conditions = _conditions_of(result)
    if any(c.kind == "foreign_key" and len(c.columns) != 1 for c in conditions):
        # the executed check has no composite foreign key to impose, so the proof cannot be re-checked: not scored as conditional
        return Outcome("unchecked", result.reason, [c.to_json() for c in conditions], 0, seconds=time.time() - start, **base)
    stricter = veri.build_spec(case)  # the spec again, with the conditions as extra constraints
    names = {t.name.lower(): t for t in stricter.tables.values()}
    def actual(table, name):  # the spec keeps each column's own case; the prover works in lower case
        return next(col.name for col in table.columns if col.name.lower() == name.lower())

    for c in conditions:
        table = names[c.table]
        if c.kind == "not_null":
            table.column(actual(table, c.columns[0])).not_null = True
        elif c.kind == "unique":
            table.unique.append(tuple(actual(table, n) for n in c.columns))
        else:
            parent = names[c.parent]
            stricter.foreign_keys.append((table.name, actual(table, c.columns[0]), parent.name, actual(parent, c.parent_columns[0])))
    searcher = cx.Searcher(stricter, left, right)
    if not searcher.runs():
        return Outcome("unchecked", result.reason, [c.to_json() for c in conditions], 0, seconds=time.time() - start, **base)
    found = searcher.search(VALIDATION_DATABASES // 3, seed=index + 5_000_011) or searcher.search(VALIDATION_DATABASES // 3, seed=index + 6_000_011, wide=True)
    if found is not None:
        return Outcome("wrong", "proved under conditions but a database that meets them separates the queries", [c.to_json() for c in conditions], seconds=time.time() - start, **base)
    plain = cx.Searcher(spec, left, right)
    without = plain.search(WITHOUT_DATABASES // 2, seed=index + 7_000_011) if plain.runs() else None
    minimal = True
    for dropped in conditions:
        rest = [c for c in conditions if c is not dropped]
        again = prove_equivalent_algebraic(left, right, constraints=with_conditions(constraints, rest), **options)
        if again.status is SmtStatus.PROVEN_EQUIVALENT:
            minimal = False
    return Outcome(
        "conditional", result.reason, [c.to_json() for c in conditions], VALIDATION_DATABASES // 3, needed=without is not None, minimal=minimal,
        seconds=time.time() - start, **base,
    )


def _decide_verieql_job(case: dict) -> Outcome:
    try:
        return decide_verieql(case)
    except Exception as error:
        return Outcome("crash", f"{type(error).__name__}: {error}"[:200])


# --- running ------------------------------------------------------------------------------------------------


@dataclass
class Report:
    suite: str
    outcomes: list[Outcome]
    seconds: float

    @property
    def counts(self) -> Counter:
        return Counter(o.kind for o in self.outcomes)

    def line(self) -> str:
        c = self.counts
        conditional = [o for o in self.outcomes if o.kind == "conditional"]
        needed = sum(1 for o in conditional if o.needed)
        minimal = sum(1 for o in conditional if o.minimal)
        return (
            f"{self.suite}: {len(self.outcomes)} pairs: {c['equivalent']} proved outright, {c['conditional']} equivalent under conditions, "
            f"{c['refuted']} refuted by a database meeting every candidate condition, {c['unknown']} unknown, {c['wrong']} wrong, {c['unchecked']} not re-checkable, {c['crash']} crashed; "
            f"of the conditional ones {needed} separated without the conditions and {minimal} have a minimal set"
        )


def run_singh(pairs: list[singh.Pair], workers: int | None = None) -> Report:
    start = time.time()
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        outcomes = list(pool.map(_decide_singh_job, pairs, chunksize=4))
    return Report("singh", outcomes, time.time() - start)


def run_verieql(cases: list[dict], workers: int | None = None) -> Report:
    start = time.time()
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        outcomes = list(pool.map(_decide_verieql_job, cases, chunksize=2))
    return Report("verieql-leetcode", outcomes, time.time() - start)


def summary(report: Report, gold: list[str] | None = None) -> str:
    lines = [report.line()]
    conditional = [o for o in report.outcomes if o.kind == "conditional"]
    kinds = Counter(c["kind"] for o in conditional for c in o.conditions)
    sizes = Counter(len(o.conditions) for o in conditional)
    lines.append(f"  conditions by kind: {dict(kinds)}; conditions per proof: {dict(sorted(sizes.items()))}")
    validated = sum(1 for o in conditional if o.validated)
    lines.append(f"  re-checked on databases that meet the conditions: {validated}/{len(conditional)}")
    labels = Counter((o.kind, o.label) for o in report.outcomes if o.label)
    if labels:
        lines.append(f"  against the published labels: {dict(sorted(labels.items()))}")
    held = [o for o in report.outcomes if o.held_out]
    if held:
        c = Counter(o.kind for o in held)
        lines.append(f"  held-out fifth: {len(held)} pairs, {c['conditional']} conditional, {c['wrong']} wrong")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("suite", choices=("singh", "verieql"))
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all", help="Singh only; held-out pairs are for final scoring")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--every", type=int, default=1, help="VeriEQL only: take every Nth case")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--show", action="store_true", help="print each conditional proof and any wrong one")
    args = parser.parse_args(argv)
    if args.suite == "singh":
        pairs = singh.split(singh.load_pairs(), args.split)
        if args.sample:
            pairs = random.Random(2024).sample(pairs, min(args.sample, len(pairs)))
        pairs = pairs[: args.limit]
        report = run_singh(pairs, args.workers)
        texts = {p.key: p for p in pairs}
    else:
        import verieql_bench as veri

        cases = veri.load_cases("leetcode")[:: args.every][: args.limit]
        report = run_verieql(cases, args.workers)
        texts = {}
    print(summary(report), f"\n  in {report.seconds:.0f}s")
    if args.json:
        args.json.write_text(json.dumps([o.__dict__ for o in report.outcomes], indent=1, default=str))
    if args.show:
        for o in report.outcomes:
            if o.kind in ("conditional", "wrong"):
                pair = texts.get(o.key)
                print(f"\n#{o.key} {o.kind} (label {o.label or '-'}): {o.detail}")
                if pair:
                    print(f"  {pair.left}\n  {pair.right}")
    return 1 if report.counts["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
