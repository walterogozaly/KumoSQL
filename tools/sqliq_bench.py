"""Score KumoSQL on SQL-IQ's SQL Equivalence Judge task, with no language model.

SQL-IQ (https://github.com/SQL-IQ/SQL-IQ, MIT) asks a judge whether two SQLite
queries return identical results on the database described by a schema. Its
models answer "yes" or "no"; here KumoSQL answers instead, in three steps:

1. **prove**: the algebraic prover (``prove_equivalent_algebraic``) with the
   schema's keys. Proved means "yes"; a counterexample means "no".
2. **test**: otherwise both queries run on random SQLite databases that respect
   the schema's keys and are filled from the values the queries mention. A
   difference means "no" (a real counterexample).
3. **default**: queries that agree on every database but were not proved are
   answered "yes" (``tested``), the only guess made.

Nothing here reads the benchmark's labels except to score the answers; no
answer is looked up and no pair is treated specially. Run it from a checkout
of SQL-IQ:

    git clone --depth 1 https://github.com/SQL-IQ/SQL-IQ
    python tools/sqliq_bench.py --data SQL-IQ            # or set SQLIQ_DIR
    python tools/sqliq_bench.py --data SQL-IQ --limit 100 --jobs 4

The other six SQL-IQ tasks need a language model (a question in English, an
error taxonomy) or running database servers (SQL Translation), so they are not
part of this harness.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

DATA_FILE = Path("data") / "sql_equ_judge" / "sql_equ_judge.jsonl"
TRIALS = 60
PROVER_TIMEOUT_MS = 3000


@dataclass
class Pair:
    id: int
    sql1: str
    sql2: str
    tables: dict[str, dict[str, str]]  # lower-case table -> column -> declared type
    keys: dict[str, tuple[str, ...]]  # lower-case table -> primary key columns
    label: str  # "yes" or "no"


def load_pairs(data_dir: Path) -> list[Pair]:
    pairs = []
    with open(data_dir / DATA_FILE, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            schema = row["schema"]
            tables = {t.lower(): {c.lower(): str(k).upper() for c, k in cols.items()} for t, cols in schema["tables"].items()}
            primary: dict[str, list[str]] = {}
            for constraint in schema.get("constraint", []):
                for item in constraint.get("primary", []):
                    table, _, column = item["value"].partition("__")
                    if table.lower() in tables and column.lower() in tables[table.lower()]:
                        primary.setdefault(table.lower(), []).append(column.lower())
            keys = {t: tuple(dict.fromkeys(c)) for t, c in primary.items()}
            pairs.append(Pair(row["id"], row["sql1"], row["sql2"], tables, keys, row["semantic equivalence"].lower()))
    return pairs


# -- step 1: prove -----------------------------------------------------------------


def prove(pair: Pair) -> str:
    """"proved", "refuted" or "unknown"."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus, TableConstraints

    schema = {t: list(cols) for t, cols in pair.tables.items()}
    constraints = {t: TableConstraints(not_null=frozenset(k), keys=(k,)) for t, k in pair.keys.items()}
    try:
        result = prove_equivalent_algebraic(
            pair.sql1,
            pair.sql2,
            schema=schema,
            constraints=constraints,
            compare_names=False,
            dialect="sqlite",
            timeout_ms=PROVER_TIMEOUT_MS,
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return "unknown"
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return "proved"
    if result.status is SmtStatus.NOT_EQUIVALENT:
        return "refuted"
    return "unknown"


# -- step 2: test on random databases ------------------------------------------------


def mentioned_values(*queries: str) -> tuple[list[str], list[float]]:
    """Text and numeric literals the queries compare against, for filling tables."""

    texts: list[str] = []
    numbers: list[float] = []
    for sql in queries:
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
        except sqlglot.errors.SqlglotError:
            continue
        for literal in tree.find_all(exp.Literal):
            if literal.is_string:
                value = literal.name
                texts.append(value)
                stripped = value.strip("%_")
                if stripped and stripped != value:
                    texts.append(stripped)
                if re.fullmatch(r"-?\d+(\.\d+)?", value):
                    numbers.append(float(value))
            else:
                try:
                    numbers.append(float(literal.name))
                except ValueError:
                    pass
    return list(dict.fromkeys(texts)), list(dict.fromkeys(numbers))


def has_limit(sql: str) -> bool:
    try:
        return sqlglot.parse_one(sql, read="sqlite").args.get("limit") is not None
    except sqlglot.errors.SqlglotError:
        return False


def _numeric(value: float):
    return int(value) if float(value).is_integer() else value


def make_domains(pair: Pair, texts: list[str], numbers: list[float]) -> dict[str, list]:
    numeric = [0, 1, 2, 3]
    for n in numbers:
        for candidate in (n, n - 1, n + 1):
            numeric.append(_numeric(candidate))
    numeric = list(dict.fromkeys(numeric))[:24]
    text = list(dict.fromkeys(["a", "b", "c"] + texts))[:24]
    return {"num": numeric, "text": text}


def _kind(declared: str) -> str:
    return "text" if any(word in declared for word in ("TEXT", "CHAR", "CLOB", "DATE", "TIME")) else "num"


def random_rows(pair: Pair, table: str, domains: dict[str, list], rng: random.Random) -> list[list]:
    columns = list(pair.tables[table].items())
    key = pair.keys.get(table, ())
    rows, seen = [], set()
    for _ in range(rng.choice([0, 1, 2, 3, 4, 5])):
        row = []
        for name, declared in columns:
            value = rng.choice(domains[_kind(declared)])
            if name not in key and rng.random() < 0.15:
                value = None
            row.append(value)
        if key:
            marker = tuple(row[[n for n, _ in columns].index(k)] for k in key)
            if marker in seen:
                continue
            seen.add(marker)
        rows.append(row)
    return rows


def _normalize(value):
    if isinstance(value, float):
        return 0.0 if value == 0 else float(f"{value:.9g}")
    return value


def _sort_key(row):
    return tuple((v is None, type(v).__name__, repr(v)) for v in row)


def run_query(connection: sqlite3.Connection, sql: str):
    cursor = connection.execute(sql.strip().rstrip(";"))
    return [tuple(_normalize(v) for v in row) for row in cursor.fetchall()]


def test(pair: Pair, trials: int = TRIALS, seed: int = 7) -> str:
    """"differs" on a database where the queries disagree, "agree" if none found, "error" if SQLite rejects them."""

    rng = random.Random(seed)
    texts, numbers = mentioned_values(pair.sql1, pair.sql2)
    domains = make_domains(pair, texts, numbers)
    ordered = has_limit(pair.sql1) or has_limit(pair.sql2)
    connection = sqlite3.connect(":memory:")
    try:
        for table, columns in pair.tables.items():
            connection.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)} {k}" for c, k in columns.items())})')
        for _ in range(trials):
            for table, columns in pair.tables.items():
                connection.execute(f'DELETE FROM "{table}"')
                rows = random_rows(pair, table, domains, rng)
                if rows:
                    marks = ", ".join("?" * len(columns))
                    connection.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
            try:
                left, right = run_query(connection, pair.sql1), run_query(connection, pair.sql2)
            except sqlite3.Error:
                return "error"
            if ordered:
                same = left == right
            else:
                same = sorted(left, key=_sort_key) == sorted(right, key=_sort_key)
            if not same:
                return "differs"
    finally:
        connection.close()
    return "agree"


# -- the judge -----------------------------------------------------------------------


def judge(pair: Pair) -> tuple[str, str]:
    """(answer, how): answer is "yes" or "no"; how is proved, refuted, differs, tested or error."""

    outcome = prove(pair)
    if outcome == "proved":
        return "yes", "proved"
    if outcome == "refuted":
        return "no", "refuted"
    result = test(pair)
    if result == "differs":
        return "no", "differs"
    if result == "agree":
        return "yes", "tested"
    return "no", "error"


def _judge_task(pair: Pair):
    return judge(pair)


def score(pairs: list[Pair], answers: list[tuple[str, str]]) -> dict:
    equ_total = sum(p.label == "yes" for p in pairs)
    neq_total = sum(p.label == "no" for p in pairs)
    equ_ok = sum(p.label == "yes" and a == "yes" for p, (a, _) in zip(pairs, answers))
    neq_ok = sum(p.label == "no" and a == "no" for p, (a, _) in zip(pairs, answers))
    equ_acc = equ_ok / equ_total if equ_total else 0.0
    neq_acc = neq_ok / neq_total if neq_total else 0.0
    by_how: dict[str, Counter] = {}
    for p, (a, how) in zip(pairs, answers):
        by_how.setdefault(how, Counter())["right" if a == p.label else "wrong"] += 1
    return {
        "correct": equ_ok + neq_ok,
        "total": len(pairs),
        "accuracy": (equ_ok + neq_ok) / len(pairs) if pairs else 0.0,
        "equivalent_accuracy": equ_acc,
        "non_equivalent_accuracy": neq_acc,
        "geometric_mean": math.sqrt(equ_acc * neq_acc),
        "by_method": {how: dict(c) for how, c in sorted(by_how.items())},
    }


def run(pairs: list[Pair], jobs: int = 1) -> tuple[list[tuple[str, str]], float]:
    start = time.time()
    if jobs > 1:
        with multiprocessing.Pool(jobs) as pool:
            answers = pool.map(_judge_task, pairs, chunksize=4)
    else:
        answers = [judge(p) for p in pairs]
    return answers, time.time() - start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", default=os.environ.get("SQLIQ_DIR"), help="a checkout of SQL-IQ (or set SQLIQ_DIR)")
    parser.add_argument("--limit", type=int, help="score only the first N pairs")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--json", help="write the metrics and every answer to this file")
    args = parser.parse_args(argv)
    if not args.data:
        parser.error("pass --data <SQL-IQ checkout> or set SQLIQ_DIR")
    pairs = load_pairs(Path(args.data))[: args.limit]
    answers, seconds = run(pairs, args.jobs)
    metrics = score(pairs, answers)
    wrong_proofs = sum(1 for p, (a, how) in zip(pairs, answers) if how == "proved" and p.label == "no")
    print(
        f"sql_equ_judge {metrics['correct']}/{metrics['total']} ({metrics['accuracy']:.2%}), "
        f"equivalent {metrics['equivalent_accuracy']:.2%}, non-equivalent {metrics['non_equivalent_accuracy']:.2%}, "
        f"geometric mean {metrics['geometric_mean']:.2%}, {seconds:.0f}s"
    )
    for how, counts in metrics["by_method"].items():
        print(f"  {how:8} right {counts.get('right', 0):4}  wrong {counts.get('wrong', 0):4}")
    print(f"  proofs the benchmark labels non-equivalent: {wrong_proofs}")
    if args.json:
        record = {
            "metrics": metrics,
            "answers": [{"id": p.id, "label": p.label, "answer": a, "how": how} for p, (a, how) in zip(pairs, answers)],
        }
        Path(args.json).write_text(json.dumps(record, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
