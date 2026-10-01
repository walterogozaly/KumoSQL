"""Score KumoSQL on SQL-IQ's SQL Equivalence Judge task, with no language model.

SQL-IQ (https://github.com/SQL-IQ/SQL-IQ, MIT) asks a judge whether two SQLite
queries return identical results on the database described by a schema. Its
models answer "yes" or "no"; here KumoSQL answers instead, in three steps:

1. **prove**: the algebraic prover (``prove_equivalent_algebraic``) with the
   schema's keys. Proved means "yes".
2. **test**: otherwise both queries run on random SQLite databases that respect
   the schema's primary and foreign keys and are filled from the values the
   queries mention. A difference means "no" (a counterexample on a valid database).
3. **default**: queries that agree on every database but were not proved are
   answered "yes" (``tested``), the only guess made.

Nothing here reads the benchmark's labels except to score the answers; no
answer is looked up and no pair is treated specially. Run it from a checkout
of SQL-IQ:

    git clone --depth 1 https://github.com/SQL-IQ/SQL-IQ
    python tools/sqliq_bench.py --data SQL-IQ            # or set SQLIQ_DIR
    python tools/sqliq_bench.py --data SQL-IQ --limit 100 --jobs 4

Two more tasks are answered by hand-written rules (``sqliq_judge.py``,
``sqliq_errors.py``); ``--tasks`` picks which to score. Text-to-SQL and
Conversational SQL must write SQL from English, SQL Debugging runs in BIRD-CRITIC's
environment and SQL Translation executes on five database servers and the BIRD
databases, so they are left out.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

DATA_FILE = Path("data") / "sql_equ_judge" / "sql_equ_judge.jsonl"
TRIALS = 1000
NULL_RATE = 0.1
PROVER_TIMEOUT_MS = 3000


@dataclass
class Pair:
    id: int
    sql1: str
    sql2: str
    tables: dict[str, dict[str, str]]  # lower-case table -> column -> declared type
    keys: dict[str, tuple[str, ...]]  # lower-case table -> primary key columns
    label: str  # "yes" or "no"
    foreign: tuple = ()  # ((child table, child column, parent table, parent column), ...)


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
            foreign = []
            for constraint in schema.get("constraint", []):
                refs = constraint.get("foreign")
                if refs and len(refs) == 2:
                    (ct, _, cc), (pt, _, pc) = (r["value"].lower().partition("__") for r in refs)
                    if cc in tables.get(ct, {}) and pc in tables.get(pt, {}):
                        foreign.append((ct, cc, pt, pc))
            pairs.append(Pair(row["id"], row["sql1"], row["sql2"], tables, keys, row["semantic equivalence"].lower(), tuple(foreign)))
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
    numeric = [0, 1, 2, 3, 4, 5, 6, 7]
    for n in numbers:
        for candidate in (n, n - 1, n + 1):
            numeric.append(_numeric(candidate))
    numeric = list(dict.fromkeys(numeric))[:24]
    text = list(dict.fromkeys(["a", "b", "c"] + texts))[:24]
    return {"num": numeric, "text": text}


def _kind(declared: str) -> str:
    return "text" if any(word in declared for word in ("TEXT", "CHAR", "CLOB", "DATE", "TIME")) else "num"


def table_order(pair: Pair) -> list[str]:
    """Tables with the ones other tables point to first (cycles broken arbitrarily)."""

    parents = {t: {p for c, _, p, _ in pair.foreign if c == t and p != t} for t in pair.tables}
    order: list[str] = []
    while len(order) < len(parents):
        ready = [t for t in parents if t not in order and parents[t] <= set(order)]
        order.append((ready or [t for t in parents if t not in order])[0])
    return order


def random_rows(pair: Pair, table: str, domains: dict[str, list], rng: random.Random, made: dict[str, list[list]]) -> list[list]:
    """Rows shaped like a real database: single-column keys count up, foreign keys point at existing rows."""

    columns = list(pair.tables[table])
    key = pair.keys.get(table, ())
    references = {c: (p, pc) for ct, c, p, pc in pair.foreign if ct == table and p in made}
    rows, seen = [], set()
    for number in range(rng.choice([0, 1, 2, 3, 4, 5, 6, 8, 10])):
        row = []
        for name in columns:
            declared = pair.tables[table][name]
            if name in references:
                parent, parent_column = references[name]
                options = [r[list(pair.tables[parent]).index(parent_column)] for r in made[parent]]
                value = rng.choice(options) if options else None
            elif key == (name,) and _kind(declared) == "num":
                value = number + 1
            else:
                value = rng.choice(domains[_kind(declared)])
            if name not in key and rng.random() < NULL_RATE:
                value = None
            row.append(value)
        if key:
            marker = tuple(row[columns.index(k)] for k in key)
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
            made: dict[str, list[list]] = {}
            for table in table_order(pair):
                columns = pair.tables[table]
                connection.execute(f'DELETE FROM "{table}"')
                rows = made[table] = random_rows(pair, table, domains, rng, made)
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
    """(answer, how): answer is "yes" or "no"; how is proved, differs, tested or error."""

    outcome = prove(pair)
    if outcome == "proved":
        return "yes", "proved"
    # A prover counterexample is only a hint: it may ignore foreign keys, so the
    # random databases (which respect them) decide.
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


def score_judge(data_dir: Path) -> dict:
    import sqliq_judge

    rows = [json.loads(line) for line in open(data_dir / "data" / "sql_judge" / "sql_judge_dev.jsonl", encoding="utf-8")]
    right = sum(
        sqliq_judge.judge(r["question"], r["evidence"], r["schema_str"], r["candidate_a"], r["candidate_b"]) == r["correct_answer"]
        for r in rows
    )
    return {"correct": right, "total": len(rows), "accuracy": right / len(rows)}


def score_errors(data_dir: Path) -> dict:
    """SQL-IQ's own metrics for SQL Error Classification (same definitions as its ``evaluate``)."""

    import sqliq_errors

    rows = json.load(open(data_dir / "data" / "sql_err_class" / "err_class.json", encoding="utf-8"))
    schemas = {}
    for line in open(data_dir / "data" / "schemas" / "bird_dev.jsonl", encoding="utf-8"):
        item = json.loads(line)
        schemas[item["db_name"]] = item["schema"]
    det = [0, 0, 0]
    uni = [0, 0, 0]
    typ = [0, 0, 0]
    exact = 0
    for row in rows:
        predicted = sqliq_errors.classify(row["question"], row["evidence"], schemas[row["db_id"]], row["sql"])
        truth = set() if row["output_label"] is True else {
            e["error_type"] if isinstance(e, dict) else e for e in row["error_types"]
        }
        predicted_set = set(predicted)
        p_unified = predicted_set or {"No error"}
        g_unified = truth or {"No error"}
        uni[0] += len(p_unified & g_unified)
        uni[1] += len(p_unified - g_unified)
        uni[2] += len(g_unified - p_unified)
        if predicted_set and truth:
            det[0] += 1
        elif predicted_set:
            det[1] += 1
        elif truth:
            det[2] += 1
        typ[0] += len(predicted_set & truth)
        typ[1] += len(predicted_set - truth)
        typ[2] += len(truth - predicted_set)
        exact += predicted_set == truth

    def f1(tp, fp, fn):
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return {
        "correct": exact,
        "total": len(rows),
        "accuracy": exact / len(rows),
        "detection_f1": f1(*det),
        "classification_f1": f1(*typ),
        "unified_f1": f1(*uni),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", default=os.environ.get("SQLIQ_DIR"), help="a checkout of SQL-IQ (or set SQLIQ_DIR)")
    parser.add_argument("--tasks", default="sql_equ_judge,sql_judge,sql_err_class", help="comma-separated task names")
    parser.add_argument("--limit", type=int, help="score only the first N equivalence pairs")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--json", help="write the equivalence metrics and every answer to this file")
    args = parser.parse_args(argv)
    if not args.data:
        parser.error("pass --data <SQL-IQ checkout> or set SQLIQ_DIR")
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = set(tasks) - {"sql_equ_judge", "sql_judge", "sql_err_class"}
    if unknown:
        parser.error(f"unknown tasks {sorted(unknown)}; text2sql, conversational_sql, sql_debugging and sql_trans cannot run without a model or databases")
    data_dir = Path(args.data)
    results = {}
    if "sql_equ_judge" in tasks:
        pairs = load_pairs(data_dir)[: args.limit]
        answers, seconds = run(pairs, args.jobs)
        metrics = score(pairs, answers)
        results["sql_equ_judge"] = metrics
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
    if "sql_judge" in tasks:
        metrics = score_judge(data_dir)
        results["sql_judge"] = metrics
        print(f"sql_judge {metrics['correct']}/{metrics['total']} ({metrics['accuracy']:.2%})")
    if "sql_err_class" in tasks:
        metrics = score_errors(data_dir)
        results["sql_err_class"] = metrics
        print(
            f"sql_err_class exact {metrics['correct']}/{metrics['total']} ({metrics['accuracy']:.2%}), "
            f"detection F1 {metrics['detection_f1']:.2%}, classification F1 {metrics['classification_f1']:.2%}, "
            f"unified F1 {metrics['unified_f1']:.2%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
