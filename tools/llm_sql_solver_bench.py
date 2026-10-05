"""Score KumoSQL on LLM-SQL-Solver's Spider query pairs, with no language model.

LLM-SQL-Solver (https://github.com/ZhaoFuheng/LLM-SQL-Solver, MIT; Zhao et al.,
"LLM-SQL-Solver: Can LLMs Determine SQL Equivalence?", arXiv 2312.10321) pairs
Spider's gold queries with DAIL-SQL's generated queries. Two of its files are used:

* ``semantic_inequivalent.jsonl``: 180 pairs whose results differ on the Spider
  databases. They are a **must-not-prove** set: any proof is a soundness bug.
* ``relaxed.jsonl``: 70 pairs labelled by a majority vote of experts (52
  "equivalent", 18 "inequivalent"); scored on their own.

Its third file, ``semantic_equivalent.jsonl``, is SQLSolver's 232 Calcite pairs
(``tests/fixtures/sqlsolver``) and is not copied. The data is pinned in
``tests/fixtures/llm_sql_solver`` (see the README there for the source commit,
overlap and the one adaptation made).

Each pair gets one outcome:

1. **proven**: the algebraic prover (SQLite dialect) proved it. No keys are given to
   the prover: Spider lists only the first column of a composite primary key, so a
   listed key may not be unique.
2. **refuted**: two SQLite runs return different results on a database that respects
   the listed keys and foreign keys (random databases, the targeted suite, then the
   z3 bounded check, as in ``sqliq_bench``). A listed key is a subset of the true key,
   so every such database is a valid Spider database.
3. **unsupported**: SQLite rejects a query. **unknown**: anything else.

``wrong`` is a proof of a pair labelled inequivalent. A refutation of a pair the
experts call equivalent is reported as a label dispute: the database is shown.

One pair in five (by a hash of its two queries) is held out. A development run reads the
dev pairs only (``--split dev``, the default); ``--split held-out`` is for final scoring
and ``--split all`` runs both, reporting dev and held-out pairs separately.
``--write-results`` scores every pair (the published headline is over all pairs, with
the held-out part in its own ``held_out`` field) and needs ``--split all`` or no split.
``--show`` never prints a held-out pair's SQL unless ``--split held-out`` was asked for.

    python tools/llm_sql_solver_bench.py                      # dev pairs of both files
    python tools/llm_sql_solver_bench.py --show proven,refuted
    python tools/llm_sql_solver_bench.py --split all          # dev and held-out, reported apart
    python tools/llm_sql_solver_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "llm_sql_solver"
FILES = {"negatives": "semantic_inequivalent.jsonl", "relaxed": "relaxed.jsonl"}
PROVER_TIMEOUT_MS = 3000
TRIALS = 1000
# Spider's column types; "number" columns are filled with integers, which every number column accepts
DECLARED = {"number": "INTEGER", "text": "TEXT", "time": "TEXT", "boolean": "INTEGER", "others": "TEXT"}


@dataclass
class Case:
    suite: str  # "negatives" or "relaxed"
    index: int  # line number in the source file, from 0
    database: str
    sql1: str  # as published
    sql2: str
    label: str  # "inequivalent" or "equivalent"
    tables: dict[str, dict[str, str]]  # lower-case table -> lower-case column -> declared SQLite type
    keys: dict[str, tuple[str, ...]]  # the listed primary key columns (used for refuting only)
    foreign: tuple = ()  # ((child table, child column, parent table, parent column), ...)

    @property
    def id(self) -> str:
        return f"{self.suite}-{self.index:03d}"

    @property
    def held_out(self) -> bool:
        digest = hashlib.sha1(f"llm-sql-solver\n{self.sql1}\n{self.sql2}".encode()).hexdigest()
        return int(digest, 16) % 5 == 0


def _schema(raw: str, database: str, types: dict) -> tuple[dict, dict, tuple]:
    described = json.loads(raw)[database]
    spider = {t.lower(): {c.lower(): k for c, k in cols.items()} for t, cols in types[database].items()}
    tables: dict[str, dict[str, str]] = {}
    for table, info in described["tables"].items():
        if table.lower().startswith("sqlite_"):
            continue  # SQLite's own bookkeeping table, never queried
        columns = [c.strip() for c in info["columns"].split(",") if c.strip() and c.strip() != "*"]
        known = spider.get(table.lower(), {})
        tables[table.lower()] = {c.lower(): DECLARED.get(known.get(c.lower(), "text"), "TEXT") for c in columns}
    keys: dict[str, tuple[str, ...]] = {}
    for item in filter(None, (s.strip() for s in described.get("primary_keys", "").split(","))):
        table, _, column = item.lower().partition(".")
        if column in tables.get(table, {}):
            keys[table] = keys.get(table, ()) + (column,)
    foreign = []
    for item in filter(None, (s.strip() for s in described.get("foreign_keys", "").split(","))):
        left, _, right = item.partition("=")
        (child, _, child_column), (parent, _, parent_column) = (side.strip().lower().partition(".") for side in (left, right))
        if child_column in tables.get(child, {}) and parent_column in tables.get(parent, {}):
            foreign.append((child, child_column, parent, parent_column))
    return tables, keys, tuple(foreign)


def load_cases(fixtures: Path = FIXTURES) -> list[Case]:
    types = json.loads((fixtures / "spider_column_types.json").read_text(encoding="utf-8"))
    cases = []
    for suite, name in FILES.items():
        for index, line in enumerate((fixtures / name).read_text(encoding="utf-8").splitlines()):
            row = json.loads(line)
            if suite == "negatives":
                sql1, sql2, label = row["sql1"], row["sql2"], "inequivalent"
                database = next(iter(json.loads(row["schema"])))
            else:
                sql1, sql2, label, database = row["gold_sql"], row["pred_sql"], row["human_preference"], row["database"]
            tables, keys, foreign = _schema(row["schema"], database, types)
            cases.append(Case(suite, index, database, sql1, sql2, label, tables, keys, foreign))
    return cases


# -- the one adaptation ---------------------------------------------------------------


def adapt(sql: str, tables: dict[str, dict[str, str]]) -> str:
    """Spider writes strings in double quotes; SQLite reads ``"x"`` as a string when no column is named x.

    sqlglot always reads it as a column, so such names are turned into string literals. Everything
    else is left as published.
    """

    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return sql
    columns = {c for cols in tables.values() for c in cols}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
    changed = False
    for column in list(tree.find_all(exp.Column)):
        identifier = column.this
        if (
            isinstance(identifier, exp.Identifier)
            and identifier.quoted
            and not column.table
            and identifier.name.lower() not in columns
            and identifier.name.lower() not in aliases
        ):
            column.replace(exp.Literal.string(identifier.name))
            changed = True
    return tree.sql(dialect="sqlite") if changed else sql


# -- deciding a pair ------------------------------------------------------------------

UNBOUNDED = 1_000_000_000  # an ORDER BY without LIMIT is read as ORDER BY .. LIMIT this, so the prover compares the order


def _tree(sql: str):
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None


def _top(tree):
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    return tree


def ordered(sql: str) -> bool:
    """Spider compares results as lists when the gold query ends in ORDER BY, as bags otherwise."""

    tree = _top(_tree(sql))
    return tree is not None and tree.args.get("order") is not None


def has_any_limit(*queries: str) -> bool:
    return any((tree := _tree(sql)) is not None and tree.find(exp.Limit) is not None for sql in queries)


def for_prover(sql: str) -> str:
    """The prover compares bags and ignores a final ORDER BY without LIMIT; with a LIMIT it compares the order too."""

    tree = _tree(sql)
    top = _top(tree)
    if top is None or top.args.get("order") is None or top.args.get("limit") is not None or top.args.get("offset") is not None:
        return sql
    top.set("limit", exp.Limit(expression=exp.Literal.number(UNBOUNDED)))
    return tree.sql(dialect="sqlite")


def _family(node, tables: dict[str, dict[str, str]], aliases: dict[str, str]) -> str | None:
    if isinstance(node, exp.Literal):
        return "text" if node.is_string else "number"
    if not isinstance(node, exp.Column):
        return None
    name = node.name.lower()
    if node.table:
        declared = tables.get(aliases.get(node.table.lower(), node.table.lower()), {}).get(name)
        found = {declared} if declared else set()
    else:
        found = {tables[t][name] for t in set(aliases.values()) if name in tables.get(t, {})}
    families = {"text" if d == "TEXT" else "number" for d in found}
    return families.pop() if len(families) == 1 else None


def mixed_type_comparison(sql: str, tables: dict[str, dict[str, str]]) -> bool:
    """A comparison of a text column with a number (or a number column with text).

    SQLite compares them after converting one side, so ``t.id = u.ref`` holds while the two columns
    still return different values (3 and '3'). The prover reasons about typed values (BigQuery rejects
    such comparisons), so its proofs do not cover these queries.
    """

    tree = _tree(sql)
    if tree is None:
        return True
    aliases = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = _family(node.this, tables, aliases), _family(node.expression, tables, aliases)
        if left and right and left != right:
            return True
    return False


def same_query(sql1: str, sql2: str) -> bool:
    """The two queries are the same once parsed (spacing, case of keywords, a final semicolon)."""

    left, right = _tree(sql1), _tree(sql2)
    return left is not None and right is not None and left.sql(dialect="sqlite") == right.sql(dialect="sqlite")


def prove(sql1: str, sql2: str, tables: dict[str, dict[str, str]]) -> str:
    """"proven", "not_equivalent" (the prover's own counterexample, a hint only) or "unknown"."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    try:
        result = prove_equivalent_algebraic(
            sql1, sql2, schema={t: list(cols) for t, cols in tables.items()}, compare_names=False,
            dialect="sqlite", timeout_ms=PROVER_TIMEOUT_MS,
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return "unknown"
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return "proven"
    return "not_equivalent" if result.status is SmtStatus.NOT_EQUIVALENT else "unknown"


def differs_on_random_databases(
    case: Case, sql1: str, sql2: str, trials: int = TRIALS, seed: int = 7, *,
    null_rate: float | None = None, witness: dict | None = None, databases: list[dict] | None = None, tie_safe: bool = False,
    unique: dict[str, tuple[str, ...]] | None = None, valid=None,
) -> str:
    """"differs", "agree" or "error" (SQLite rejects a query or the schema).

    Results are compared as Spider does (as lists when the first query ends in ORDER BY). Where a LIMIT
    or the order makes a result depend on how rows happen to be stored, every database is loaded twice,
    in opposite row orders, and is only used when each query returns the same both times.

    ``null_rate`` overrides how often a column that is not a key holds NULL; ``witness``, when given,
    receives the rows of the first database on which the results differ; ``databases`` replays those
    databases (table -> rows) instead of random ones. With ``tie_safe`` an order-dependent comparison is
    only made on a database where neither query's ORDER BY has ties (each query is run again with its
    ORDER BY keys as extra columns and without its LIMIT): a tie the storage order does not expose, such as
    one the join order decides, can then never count as a difference. ``unique`` lists, per table, more
    columns whose values may not repeat (the columns foreign keys point at). ``valid``, a function of the
    database (table -> rows), skips the generated databases it rejects.
    """

    import random
    import sqlite3

    import sqliq_bench

    pair = sqliq_bench.Pair(case.index, sql1, sql2, case.tables, case.keys, "no", case.foreign)
    rng = random.Random(seed)
    domains = sqliq_bench.make_domains(pair, *sqliq_bench.mentioned_values(sql1, sql2))
    as_list = ordered(sql1)
    limited = has_any_limit(sql1, sql2)
    connection = sqlite3.connect(":memory:")

    def load(made: dict[str, list[list]], reverse: bool) -> None:
        for table, columns in case.tables.items():
            connection.execute(f'DELETE FROM "{table}"')
            rows = made.get(table) or []
            if rows:
                marks = ", ".join("?" * len(columns))
                connection.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows[::-1] if reverse else rows)

    def bag(rows):
        return sorted(rows, key=sqliq_bench._sort_key)

    try:
        for table, columns in case.tables.items():
            connection.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)} {k}" for c, k in columns.items())})')
        for number in range(trials if databases is None else len(databases)):
            made: dict[str, list[list]] = {}
            if databases is not None:
                made = databases[number]
            else:
                for table in sqliq_bench.table_order(pair):
                    made[table] = sqliq_bench.random_rows(pair, table, domains, rng, made, null_rate=null_rate, unique=(unique or {}).get(table, ()))
            if valid is not None and not valid(made):
                continue
            load(made, False)
            left, right = sqliq_bench.run_query(connection, sql1), sqliq_bench.run_query(connection, sql2)
            if not limited and bag(left) != bag(right):
                if witness is not None:
                    witness.update({t: [list(r) for r in rows] for t, rows in made.items()})
                return "differs"
            if not (limited or as_list):
                continue
            load(made, True)
            if sqliq_bench.run_query(connection, sql1) != left or sqliq_bench.run_query(connection, sql2) != right:
                continue  # ties or a LIMIT without a full order: the rows returned depend on storage
            if tie_safe and not (_tie_free(connection, sql1) and _tie_free(connection, sql2)):
                continue
            if (left != right) if as_list else (bag(left) != bag(right)):
                if witness is not None:
                    witness.update({t: [list(r) for r in rows] for t, rows in made.items()})
                return "differs"
    except sqlite3.Error:
        return "error"
    finally:
        connection.close()
    return "agree"


def order_keys_query(sql: str) -> str | None:
    """The query with its ORDER BY keys appended as output columns and without LIMIT or OFFSET, or None.

    None when the query has no ORDER BY at the top (its order is arbitrary) or is a set operation.
    """

    tree = _tree(sql)
    top = _top(tree)
    if not isinstance(top, exp.Select) or top.args.get("order") is None:
        return None
    projections = list(top.expressions)
    aliases = {p.alias.lower(): p.this for p in projections if isinstance(p, exp.Alias)}
    keys = []
    for ordered_by in top.args["order"].expressions:
        key = ordered_by.this
        if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.name) <= len(projections):
            key = projections[int(key.name) - 1]
            key = key.this if isinstance(key, exp.Alias) else key
        elif isinstance(key, exp.Column) and not key.table and key.name.lower() in aliases:
            key = aliases[key.name.lower()]
        keys.append(key.copy())
    top.set("expressions", projections + keys)
    top.set("limit", None)
    top.set("offset", None)
    return tree.sql(dialect="sqlite")


def _tie_free(connection, sql: str) -> bool:
    """The rows come back in one possible order on the loaded database.

    That holds when no two rows the query sorts share their ORDER BY keys, or when a query without
    ORDER BY or LIMIT returns at most one row.
    """

    import sqlite3

    import sqliq_bench

    keyed = order_keys_query(sql)
    if keyed is None:
        top = _top(_tree(sql))
        if top is None or top.args.get("limit") is not None or top.args.get("order") is not None:
            return False
        try:
            return len(sqliq_bench.run_query(connection, sql)) <= 1
        except sqlite3.Error:
            return False
    width = len(_top(_tree(sql)).expressions)
    try:
        rows = sqliq_bench.run_query(connection, keyed)
    except sqlite3.Error:
        return False
    keys = [row[width:] for row in rows]
    return len(set(keys)) == len(keys)


def refute(case: Case, sql1: str, sql2: str) -> str:
    """"differs" (random), "targeted", "bounded", "agree" or "error" (SQLite rejects a query)."""

    import sqliq_bench

    result = differs_on_random_databases(case, sql1, sql2)
    if result != "agree" or ordered(sql1) or has_any_limit(sql1, sql2):
        return result  # the targeted and bounded searches compare bags, without the storage-order check
    pair = sqliq_bench.Pair(case.index, sql1, sql2, case.tables, case.keys, "no", case.foreign)
    try:
        if sqliq_bench.targeted(pair) is not None:
            return "targeted"
        return "bounded" if sqliq_bench.bounded_refutes(pair) else "agree"
    except Exception:  # a search that fails finds nothing
        return "agree"


def decide(case: Case) -> dict:
    sql1, sql2 = adapt(case.sql1, case.tables), adapt(case.sql2, case.tables)
    started = time.time()
    outcome, how = "unknown", ""
    if not ordered(sql1):
        proved = prove(sql1, sql2, case.tables) == "proven"
    else:  # compared as lists: the second query needs an ORDER BY too, and the prover compares the two
        proved = ordered(sql2) and prove(for_prover(sql1), for_prover(sql2), case.tables) == "proven"
    if proved and not (mixed_type_comparison(sql1, case.tables) or mixed_type_comparison(sql2, case.tables)):
        outcome, how = "proven", "prover"
    else:
        result = refute(case, sql1, sql2)
        if result in ("differs", "targeted", "bounded"):
            outcome, how = "refuted", result
        elif result == "error":
            outcome = "unsupported"
    # The same query cannot return two results on one database: such a pair is a label error.
    label_error = outcome == "proven" and case.label == "inequivalent" and same_query(sql1, sql2)
    wrong = outcome == "proven" and case.label == "inequivalent" and not label_error
    return {
        "id": case.id, "suite": case.suite, "label": case.label, "outcome": outcome, "how": how,
        "adapted": (sql1, sql2) != (case.sql1, case.sql2), "held_out": case.held_out, "wrong": wrong,
        "label_error": label_error, "seconds": round(time.time() - started, 2),
    }


def run(cases: list[Case], jobs: int = 1) -> list[dict]:
    if jobs > 1 and len(cases) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            return list(pool.map(decide, cases, chunksize=2))
    return [decide(c) for c in cases]


def summarize(results: list[dict]) -> dict:
    out = {}
    for suite in FILES:
        rows = [r for r in results if r["suite"] == suite]
        for label in ("inequivalent", "equivalent"):
            group = [r for r in rows if r["label"] == label]
            if group:
                counts = Counter(r["outcome"] for r in group)
                out[f"{suite}/{label}"] = {
                    "size": len(group), **{k: counts.get(k, 0) for k in ("proven", "refuted", "unknown", "unsupported")},
                    "wrong": sum(r["wrong"] for r in group), "adapted": sum(r["adapted"] for r in group),
                }
    return out


def results_rows(results: list[dict]) -> dict[str, dict]:
    """The two results files: the must-not-prove negatives and the expert-labelled relaxed pairs."""

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bench_common import today

    def counts(rows):
        c = Counter(r["outcome"] for r in rows)
        return {k: c[k] for k in ("proven", "refuted", "unknown", "unsupported") if c[k]}

    negatives = [r for r in results if r["suite"] == "negatives"]
    neg_held = [r for r in negatives if r["held_out"]]
    neg = counts(negatives)
    relaxed = [r for r in results if r["suite"] == "relaxed"]
    rel_eq = [r for r in relaxed if r["label"] == "equivalent"]
    rel_ne = [r for r in relaxed if r["label"] == "inequivalent"]
    agree = sum(r["outcome"] == "proven" for r in rel_eq) + sum(r["outcome"] == "refuted" for r in rel_ne)
    disputes = sum(r["outcome"] == "refuted" for r in rel_eq)
    rel_held = [r for r in relaxed if r["held_out"]]
    command = "python tools/llm_sql_solver_bench.py --write-results"
    return {
        "llm-sql-solver-negatives": {
            "suite": "LLM-SQL-Solver Spider negatives",
            "order": 33,
            "size": len(negatives),
            "score": f"{neg.get('refuted', 0)}/{len(negatives)} refuted, {neg.get('proven', 0)} proved ({sum(r['label_error'] for r in negatives)} label errors), {sum(r['wrong'] for r in negatives)} wrong",
            "metric": "Spider gold vs DAIL-SQL pairs that differ on the Spider databases: none may be proved; refuted means SQLite returns different results on a database that respects the listed keys and foreign keys.",
            "evidence": "executed",
            "correctness": "Every pair is labelled inequivalent, so a proof counts as wrong unless the two queries are the same once parsed (a label error); refutations are SQLite runs on a schema-valid database, with ties never counted as a difference.",
            "coverage": counts(negatives),
            "held_out": f"{sum(r['outcome'] == 'refuted' for r in neg_held)}/{len(neg_held)} refuted, {sum(r['outcome'] == 'proven' for r in neg_held)} proved",
            "docs": "docs/evals/llm-sql-solver.md",
            "command": command,
            "date": today(),
            "caveats": "Spider's databases are not bundled: refutations come from KumoSQL's own databases, with Spider's column types (number columns hold integers). Strings in double quotes are read as SQLite does (adapted pairs are counted in the docs). The first baseline printed every pair, held-out ones included; the prover fix and the harness's type and order checks came from dev pairs.",
        },
        "llm-sql-solver-relaxed": {
            "suite": "LLM-SQL-Solver relaxed (expert labels)",
            "order": 34,
            "size": len(relaxed),
            "score": f"{agree}/{len(relaxed)} agree with the experts, {sum(r['wrong'] for r in relaxed)} wrong",
            "metric": f"Expert majority labels ({len(rel_eq)} equivalent, {len(rel_ne)} inequivalent): equivalent pairs proved plus inequivalent pairs refuted.",
            "evidence": "proof",
            "correctness": f"Wrong is a proof of a pair the experts call inequivalent. {disputes} pairs the experts call equivalent are refuted on a schema-valid database (label disputes, listed in the docs).",
            "coverage": counts(relaxed),
            "held_out": f"{sum((r['outcome'] == 'proven') if r['label'] == 'equivalent' else (r['outcome'] == 'refuted') for r in rel_held)}/{len(rel_held)} agree",
            "docs": "docs/evals/llm-sql-solver.md",
            "command": command,
            "date": today(),
            "caveats": "No keys are given to the prover (Spider lists only the first column of a composite key), so key-dependent rewrites stay unknown. The experts judged intent on realistic data; a refutation needs only one valid database.",
        },
    }


SPLITS = ("dev", "held-out", "all")


def split_cases(cases: list[Case], split: str) -> list[Case]:
    """The pairs of one split: ``dev``, ``held-out`` (one pair in five, by hash) or ``all``."""

    if split not in SPLITS:
        raise ValueError(f"split must be one of {', '.join(SPLITS)}")
    return cases if split == "all" else [c for c in cases if c.held_out == (split == "held-out")]


def choose_split(split: str | None, write: bool) -> str:
    """``--split`` as given, else ``all`` for ``--write-results`` (the published numbers) and ``dev`` otherwise."""

    if split is None:
        return "all" if write else "dev"
    if write and split != "all":
        raise ValueError("--write-results needs --split all")
    return split


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--split", choices=SPLITS, default=None,
        help="dev (default) for development runs; held-out is for final scoring only; all reports both apart (default with --write-results)",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--show", default="", help="comma-separated outcomes to list (proven, refuted, unknown, unsupported)")
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/llm-sql-solver-*.json (scores every pair: --split all)")
    args = parser.parse_args(argv)
    try:
        split = choose_split(args.split, args.write_results)
    except ValueError as error:
        parser.error(str(error))
    from bench_common import quiet, write_results

    quiet()
    cases = split_cases(load_cases(), split)
    started = time.time()
    results = run(cases, args.jobs)
    for part in ("dev", "held-out") + (("all",) if split == "all" else ()):
        rows = results if part == "all" else [r for r in results if r["held_out"] == (part == "held-out")]
        for group, counts in summarize(rows).items():
            print(f"{part:9} {group:24} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"{len(results)} pairs ({split}) in {time.time() - started:.0f}s")
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    for case, result in zip(cases, results):
        if result["outcome"] in show or result["wrong"]:
            print(f"\n{result['id']} [{result['label']}] {result['outcome']} {result['how']}{' WRONG' if result['wrong'] else ''}")
            if case.held_out and split != "held-out":
                print("  held out: rerun with --split held-out to see the SQL")
            else:
                print(f"  1: {case.sql1}\n  2: {case.sql2}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if args.write_results:
        for name, row in results_rows(results).items():
            write_results(name, row, scoreboard=False)
        write_results(name, row)
    return 1 if any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
