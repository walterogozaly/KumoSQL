"""Score KumoSQL on DB-GPT's 36 query-rewrite examples, with labels checked on DuckDB.

DB-GPT (https://github.com/TsinghuaDatabaseGroup/DB-GPT, Apache-2.0) ships 36 before/after
PostgreSQL rewrites as demonstrations for an LLM rewriter
(``multiagents/prompt_template_scripts/query_rewrite/data/raw/train/rules.json``). They come
without labels or schemas, so each case in ``tests/fixtures/dbgpt_rules/cases.jsonl`` carries a
schema and a hand-assigned label:

* ``equivalent`` (30): checked here on random DuckDB databases (no difference allowed);
* ``not_equivalent`` (3): each has a counterexample database, checked here;
* ``invalid`` (3): a query is not valid SQL (both PostgreSQL and DuckDB reject it); never scored.

KumoSQL then decides each valid case without reading the label:

* **proven**: ``prove_equivalent_algebraic`` (PostgreSQL dialect, the case's keys and NOT NULL
  columns, output names ignored, exact arithmetic since every number column is an INTEGER);
* **refuted**: random DuckDB databases (respecting the same constraints) on which the two
  queries return different bags, confirmed with DuckDB's optimizer off
  (``kumosql.duckdb_load.run_unoptimized``);
* **unknown**: neither.

``wrong`` is a proof of a ``not_equivalent`` case or a refutation of an ``equivalent`` one.
Four cases are adapted (the ``adapted`` field says how); the original text is in ``rules.json``.
The set is too small to hold out a split: every case was seen while labelling.

    python tools/dbgpt_rules_bench.py              # score and check every label
    python tools/dbgpt_rules_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import random
import re
import sys

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "dbgpt_rules"
TRIALS = 400
PROVER_TIMEOUT_MS = 5000


@dataclass
class Case:
    id: str
    left: str
    right: str
    label: str
    note: str
    adapted: str | None
    schema: dict[str, dict[str, str]]  # table -> column -> INTEGER, VARCHAR, BOOLEAN or TIMESTAMP
    constraints: dict = field(default_factory=dict)  # table -> {"keys": [[...]], "not_null": [...]}
    counterexample: dict | None = None  # table -> rows
    dialect: str = "postgres"  # sqlglot dialect the queries are written in
    source: str = ""  # where the rewrite is documented (cases written for KumoSQL from a documented claim)
    claim: str = ""  # what the documentation says about the rewrite


def load_cases(fixtures: Path = FIXTURES) -> list[Case]:
    return [Case(**json.loads(line)) for line in (fixtures / "cases.jsonl").read_text(encoding="utf-8").splitlines()]


def to_duckdb(sql: str, dialect: str = "postgres") -> str:
    return sqlglot.transpile(sql.strip().rstrip(";"), read=dialect, write="duckdb")[0]


# -- databases ------------------------------------------------------------------------


def _values(case: Case) -> tuple[list[int], list[str]]:
    """Small integers and the literals the queries mention, with neighbours, for filling tables."""

    numbers = {-1, 0, 1, 2, 3}
    texts = {"a", "f", "x"}
    for sql in (case.left, case.right):
        for number in re.findall(r"(?<![\w.])\d+(?![\w.])", sql):
            numbers.update({int(number) - 1, int(number), int(number) + 1})
        for text in re.findall(r"'([^']*)'", sql):
            stripped = text.replace("%", "").replace("_", "")
            texts.update({text, stripped, stripped + "x", stripped[:-1] + chr(ord(stripped[-1]) + 1) if stripped else "b"})
    return sorted(numbers), sorted(texts)


DOMAINS = {
    "BOOLEAN": [True, False],
    "TIMESTAMP": ["2020-01-01 00:00:00", "2020-01-01 12:30:00", "2020-01-02 00:00:00", "2020-06-30 08:00:00", "2019-12-31 23:59:59"],
}


def random_database(case: Case, rng: random.Random) -> dict[str, list[tuple]]:
    numbers, texts = _values(case)
    database = {}
    for table, columns in case.schema.items():
        rules = case.constraints.get(table, {})
        not_null = set(rules.get("not_null", ())) | {c for key in rules.get("keys", ()) for c in key}
        rows, seen = [], set()
        for _ in range(rng.choice([0, 1, 2, 3, 4, 5])):
            row = []
            for column, declared in columns.items():
                if column not in not_null and rng.random() < 0.15:
                    row.append(None)
                else:
                    row.append(rng.choice(DOMAINS[declared]) if declared in DOMAINS else rng.choice(numbers) if declared == "INTEGER" else rng.choice(texts))
            marker = tuple(tuple(row[list(columns).index(c)] for c in key) for key in rules.get("keys", ()))
            if marker and marker in seen:
                continue
            seen.add(marker)
            rows.append(tuple(row))
        database[table] = rows
    return database


def _load(db, case: Case, database: dict[str, list]) -> None:
    from kumosql.duckdb_load import insert_rows

    for table, columns in case.schema.items():
        db.execute(f'DROP TABLE IF EXISTS "{table}"')
        db.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)} {k}" for c, k in columns.items())})')
        rows = database.get(table) or []
        if rows:
            insert_rows(db, f'"{table}"', [tuple(r) for r in rows])


def _bag(rows) -> Counter:
    return Counter(tuple(round(v, 9) if isinstance(v, float) else v for v in row) for row in rows)


def differs_on(case: Case, database: dict[str, list]) -> bool | None:
    """True when the two queries return different bags with DuckDB's optimizer on and off, None if either errors."""

    import duckdb

    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    try:
        _load(db, case, database)
        left, right = to_duckdb(case.left, case.dialect), to_duckdb(case.right, case.dialect)
        try:
            if _bag(db.execute(left).fetchall()) == _bag(db.execute(right).fetchall()):
                return False
            plain_left, plain_right = run_unoptimized(db, left, right)
        except duckdb.Error:
            return None
        return _bag(plain_left) != _bag(plain_right)
    finally:
        db.close()


def search_difference(case: Case, trials: int = TRIALS, seed: int = 11) -> dict[str, list] | None:
    rng = random.Random(seed)
    for _ in range(trials):
        database = random_database(case, rng)
        if differs_on(case, database):
            return database
    return None


def runs(case: Case) -> bool:
    """Both queries run in DuckDB on empty tables."""

    import duckdb

    db = duckdb.connect()
    try:
        _load(db, case, {})
        db.execute(to_duckdb(case.left, case.dialect)).fetchall()
        db.execute(to_duckdb(case.right, case.dialect)).fetchall()
        return True
    except (duckdb.Error, sqlglot.errors.SqlglotError):
        return False
    finally:
        db.close()


# -- labels and verdicts --------------------------------------------------------------


def label_problems(case: Case) -> list[str]:
    """Why the case's label does not hold on DuckDB (empty when it does)."""

    if case.label == "invalid":
        return [] if not runs(case) else ["both queries run, so the case is not invalid"]
    if not runs(case):
        return ["a query does not run in DuckDB"]
    if case.label == "not_equivalent":
        return [] if case.counterexample and differs_on(case, case.counterexample) else ["the counterexample shows no difference"]
    found = search_difference(case)
    return [f"the queries differ on {found}"] if found is not None else []


def prove(case: Case) -> str:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus, TableConstraints

    constraints = {
        table: TableConstraints(
            not_null=frozenset(rules.get("not_null", ())) | frozenset(c for key in rules.get("keys", ()) for c in key),
            keys=tuple(tuple(k) for k in rules.get("keys", ())),
        )
        for table, rules in case.constraints.items()
    }
    try:
        result = prove_equivalent_algebraic(
            case.left.strip().rstrip(";"), case.right.strip().rstrip(";"),
            schema={t: list(c) for t, c in case.schema.items()}, constraints=constraints or None,
            compare_names=False, dialect=case.dialect, timeout_ms=PROVER_TIMEOUT_MS,
            # + - * are exact on integers: every number column here is INTEGER
            exact_arithmetic=all(k in ("INTEGER", "VARCHAR", "BOOLEAN", "TIMESTAMP") for cols in case.schema.values() for k in cols.values()),
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return "unknown"
    return "proven" if result.status is SmtStatus.PROVEN_EQUIVALENT else "unknown"


def decide(case: Case) -> dict:
    if case.label == "invalid":
        outcome = "proven" if prove(case) == "proven" else "unsupported"
    elif prove(case) == "proven":
        outcome = "proven"
    elif search_difference(case) is not None:
        outcome = "refuted"
    else:
        outcome = "unknown"
    wrong = (outcome == "proven" and case.label != "equivalent") or (outcome == "refuted" and case.label == "equivalent")
    return {"id": case.id, "label": case.label, "outcome": outcome, "wrong": wrong, "adapted": bool(case.adapted)}


def results_row(results: list[dict]) -> dict:
    from bench_common import today

    valid = [r for r in results if r["label"] != "invalid"]
    equivalent = [r for r in valid if r["label"] == "equivalent"]
    different = [r for r in valid if r["label"] == "not_equivalent"]
    proven = sum(r["outcome"] == "proven" for r in equivalent)
    refuted = sum(r["outcome"] == "refuted" for r in different)
    counts = Counter(r["outcome"] for r in valid)
    return {
        "suite": "DB-GPT rewrite examples",
        "order": 36,
        "size": len(valid),
        "score": f"{proven}/{len(equivalent)} proved, {refuted}/{len(different)} refuted, {sum(r['wrong'] for r in results)} wrong",
        "metric": "DB-GPT's PostgreSQL rewrite demonstrations, labelled by hand and checked on DuckDB: equivalent rewrites proved, broken ones refuted by a database.",
        "evidence": "proof",
        "correctness": "Labels checked on DuckDB by the test suite (random databases for equivalent cases, a stored counterexample for the others, confirmed with the optimizer off); wrong is a proof against a not-equivalent label or a refutation against an equivalent one.",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown") if counts[k]},
        "held_out": "none",
        "docs": "docs/evals/dbgpt-rules.md",
        "command": "python tools/dbgpt_rules_bench.py --write-results",
        "date": today(),
        "caveats": f"The source has no labels or schemas: both were written for this eval (tuned on test; 36 cases, no held-out split). {sum(r['label'] == 'invalid' for r in results)} cases are not valid SQL and are left out; 4 are adapted (a PostgreSQL internal function, two DDL fragments, a table-name typo).",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/dbgpt-rules.json")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    cases = load_cases()
    bad = {c.id: p for c in cases if (p := label_problems(c))}
    for case_id, problems in bad.items():
        print(f"label of case {case_id} does not hold: {'; '.join(problems)}")
    results = [decide(c) for c in cases]
    for result in results:
        print(f"{result['id']:>3} {result['label']:15} {result['outcome']:12}{' WRONG' if result['wrong'] else ''}")
    row = results_row(results)
    print(row["score"])
    if args.write_results:
        write_results("dbgpt-rules", row)
    return 1 if bad or any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
