"""Re-check Singh & Bedathur proofs on MySQL 8, the engine the LeetCode queries were written for.

The DuckDB search (``tools/proof_recheck.py singh``) runs a sqlglot translation, so it cannot see where
MySQL itself differs from DuckDB: strings compared with numbers (``'2' = 2`` is true), inexact decimal
division (``2/3*3 = 2`` is false), accent- and case-insensitive ``LIKE``, ``'ß' = 'ss'``. This search runs
each proven pair's original text on a MySQL server instead, over databases from the same generator
(``recheck.engine.Generator``: edge, exhaustive tiny and random databases with the harness's column types),
with the server's default collation ``utf8mb4_0900_ai_ci`` (as on LeetCode). A difference counts only when
both results stay the same with each table's rows inserted reversed, rotated and shuffled.

Needs a MySQL 8 server started with ``--lower-case-table-names=1`` (the queries spell table names in
capitals) and ``pip install pymysql``:

    python tools/recheck/singh_mysql.py singh --socket /tmp/mysql.sock --jobs 4 --out proof-recheck/raw
    python tools/recheck/singh_mysql.py singh-leetcode-types --pairs KEY1,KEY2 --socket /tmp/mysql.sock

Writes ``<out>/<eval>-mysql.jsonl`` (resumable), one record per pair: ``survived``, ``differs``,
``unrunnable`` (MySQL rejects both queries), ``not-proven`` or ``search-error``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
import multiprocessing
import os
from pathlib import Path
import random
import sys
import time

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from recheck import engine  # noqa: E402
from recheck.singh import ADAPTERS  # noqa: E402

_MYSQL_TYPE = {"BIGINT": "BIGINT", "VARCHAR": "VARCHAR(100)", "DATE": "DATE", "TIME": "TIME", "BOOLEAN": "BOOLEAN"}
SETUP = ("SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci", "SET SESSION max_execution_time = 10000")
# LeetCode's server may not have ONLY_FULL_GROUP_BY; a query that needs it off is run that way and noted
LOOSE = "SET SESSION sql_mode = REPLACE(@@sql_mode, 'ONLY_FULL_GROUP_BY', '')"


def mysql_type(column: engine.Column) -> str:
    sql_type = column.ddl_type()
    return sql_type if sql_type.startswith("DECIMAL") else _MYSQL_TYPE[sql_type]


class MySql:
    """One connection and one scratch schema holding a case's tables."""

    def __init__(self, socket: str, schema: str):
        import pymysql

        self.pymysql = pymysql
        self.db = pymysql.connect(unix_socket=socket, user="root", charset="utf8mb4", autocommit=True)
        self.cursor = self.db.cursor()
        self.cursor.execute(f"DROP DATABASE IF EXISTS {schema}")
        self.cursor.execute(f"CREATE DATABASE {schema} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        self.cursor.execute(f"USE {schema}")
        for statement in SETUP:
            self.cursor.execute(statement)
        self.case: engine.Case | None = None
        self.loose = False

    def prepare(self, case: engine.Case) -> None:
        self.case = case
        if self.loose:
            self.cursor.execute("SET SESSION sql_mode = DEFAULT")
            self.loose = False
        self.cursor.execute("SHOW TABLES")
        for (name,) in self.cursor.fetchall():
            self.cursor.execute(f"DROP TABLE `{name}`")
        for table in case.tables.values():
            columns = ", ".join(f"`{c.name}` {mysql_type(c)}" for c in table.columns)
            self.cursor.execute(f"CREATE TABLE `{table.name}` ({columns}) ENGINE=InnoDB")

    def load(self, data: engine.Database) -> None:
        for name, table in self.case.tables.items():
            self.cursor.execute(f"DELETE FROM `{name}`")
            rows = data.get(name, [])
            if rows:
                marks = ", ".join(["%s"] * len(table.columns))
                self.cursor.executemany(f"INSERT INTO `{name}` VALUES ({marks})", [tuple(r) for r in rows])

    def run(self, sql: str) -> list[tuple]:
        try:
            self.cursor.execute(sql)
            return list(self.cursor.fetchall())
        except self.pymysql.err.OperationalError as error:
            if error.args and error.args[0] == 1055 and not self.loose:  # ONLY_FULL_GROUP_BY
                self.cursor.execute(LOOSE)
                self.loose = True
                return self.run(sql)
            raise engine.QueryError(f"{error.args[0]}: {error.args[1][:300]}") from None
        except self.pymysql.err.MySQLError as error:
            raise engine.QueryError(f"{type(error).__name__}: {str(error)[:300]}") from None

    def compare(self, data: engine.Database) -> engine.Outcome:
        try:
            self.load(data)
        except self.pymysql.err.MySQLError as error:
            return engine.Outcome("load-error", error=str(error)[:300])
        results, errors = [], []
        for sql in self.case.source:
            try:
                results.append(self.run(sql))
                errors.append("")
            except engine.QueryError as error:
                results.append(None)
                errors.append(str(error))
        if results[0] is None and results[1] is None:
            return engine.Outcome("both-error", error=errors[0] + " | " + errors[1])
        if results[0] is None or results[1] is None:
            return engine.Outcome("error", results[0], results[1], error=errors[0] or errors[1])
        if engine.bag(results[0]) == engine.bag(results[1]):
            return engine.Outcome("same", results[0], results[1])
        return engine.Outcome("differs", results[0], results[1])

    def confirm(self, data: engine.Database, outcome: engine.Outcome, rng: random.Random) -> str:
        left, right = engine.bag(outcome.left), engine.bag(outcome.right)
        if engine.bag(outcome.left, 6) == engine.bag(outcome.right, 6):
            return "float-noise"
        for order in engine._orders(data, rng):
            again = self.compare(order)
            if again.kind != "differs" or engine.bag(again.left) != left or engine.bag(again.right) != right:
                return "nondeterministic"
        return "differs"

    def shrink(self, data: engine.Database, rng: random.Random, seconds: float = 20.0) -> engine.Database:
        deadline = time.time() + seconds
        current = {name: list(rows) for name, rows in data.items()}
        changed = True
        while changed and time.time() < deadline:
            changed = False
            for name in sorted(current, key=lambda n: -len(current[n])):
                index = 0
                while index < len(current[name]) and time.time() < deadline:
                    trial = {n: list(rows) for n, rows in current.items()}
                    del trial[name][index]
                    outcome = self.compare(trial)
                    if outcome.kind == "differs" and self.confirm(trial, outcome, rng) == "differs":
                        current, changed = trial, True
                        continue
                    index += 1
        return current


def search(runner: MySql, case: engine.Case, budget: int, seconds: float, seed: int) -> dict:
    start = time.time()
    rng = random.Random(f"{seed}:mysql:{case.pair}")
    record: dict = {"eval": f"{case.eval}-mysql", "pair": case.pair, "held_out": case.held_out, "verdict": "survived", "dbs": 0}
    runner.prepare(case)
    generator = engine.Generator(case, rng)
    notes: Counter = Counter()
    first: dict[str, str] = {}
    both_error = 0

    def databases():
        yield from generator.edge_databases()
        yield from (("exhaustive tiny", d) for d in generator.exhaustive(budget // 3, engine._exhaustive_values(generator, 0)))
        for variant in (1, 2):
            yield from (("exhaustive literal", d) for d in generator.exhaustive(budget // 9, engine._exhaustive_values(generator, variant)))
        while True:
            data = generator.database(generator.random_profile())
            if data is not None:
                yield "random", data

    for label, data in databases():
        if record["dbs"] >= budget or time.time() - start > seconds:
            break
        record["dbs"] += 1
        outcome = runner.compare(data)
        if outcome.kind == "same":
            continue
        if outcome.kind in ("load-error", "both-error"):
            notes[outcome.kind] += 1
            first.setdefault(outcome.kind, outcome.error)
            if outcome.kind == "both-error":
                both_error += 1
                if both_error >= 25 and both_error == record["dbs"]:
                    record.update(verdict="unrunnable", error=outcome.error[:400])
                    return record
            continue
        if outcome.kind == "error":
            notes["one-side-error"] += 1
            if "one-side" not in first:
                first["one-side"] = outcome.error
                record["error_witness"] = {"database": engine.database_json(data), "left": engine._rows(outcome.left), "right": engine._rows(outcome.right)}
            continue
        verdict = runner.confirm(data, outcome, rng)
        if verdict != "differs":
            notes[verdict] += 1
            if verdict not in first:
                first[verdict] = label
                record.setdefault("unconfirmed", {})[verdict] = {"database": engine.database_json(data), "left": engine._rows(outcome.left), "right": engine._rows(outcome.right)}
            continue
        small = runner.shrink(data, rng)
        final = runner.compare(small)
        if final.kind != "differs" or runner.confirm(small, final, rng) != "differs":
            small, final = data, outcome
        record.update(verdict="differs", profile=label, witness={"database": engine.database_json(small), "left": engine._rows(final.left), "right": engine._rows(final.right)})
        break
    if record["verdict"] == "survived" and both_error and both_error == record["dbs"]:
        record.update(verdict="unrunnable", error=first.get("both-error", "")[:400])
    if runner.loose:
        record["only_full_group_by_off"] = True
    if notes:
        record["notes"] = dict(notes)
    if first:
        record["first"] = {k: v[:300] for k, v in first.items()}
    record["seconds"] = round(time.time() - start, 2)
    return record


_RUNNER: MySql | None = None


def _work(job: tuple) -> dict:
    global _RUNNER
    name, item, options = job
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    try:
        if _RUNNER is None:
            _RUNNER = MySql(options["socket"], f"recheck_{os.getpid()}")
        case = ADAPTERS[name].case(item)
        if case is None:
            return {"eval": f"{name}-mysql", "pair": item["pair"], "verdict": "not-proven"}
        record = search(_RUNNER, case, options["budget"], options["seconds"], options["seed"])
        record["source"] = list(case.source)
        record["tables"] = {t.name: [f"{c.name} {mysql_type(c)}" for c in t.columns] for t in case.tables.values()}
        if case.meta:
            record["meta"] = case.meta
        return record
    except Exception as error:  # a bug in the search must not read as a survived proof
        _RUNNER = None
        return {"eval": f"{name}-mysql", "pair": item["pair"], "verdict": "search-error", "error": f"{type(error).__name__}: {error}"[:400]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval", choices=sorted(ADAPTERS))
    parser.add_argument("--socket", required=True, help="the MySQL server's unix socket")
    parser.add_argument("--out", default="proof-recheck")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget", type=int, default=900, help="databases per proven pair")
    parser.add_argument("--seconds", type=float, default=240.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--pairs", default="", help="comma-separated pair keys (default: every pair)")
    args = parser.parse_args(argv)
    items = ADAPTERS[args.eval].items()
    if args.pairs:
        wanted = set(args.pairs.split(","))
        items = [i for i in items if i["pair"] in wanted]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.eval}-mysql.jsonl"
    done = set()
    if path.exists():
        done = {json.loads(line)["pair"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [i for i in items if i["pair"] not in done]
    print(f"{args.eval}-mysql: {len(items)} pairs, {len(todo)} to run", flush=True)
    options = {"socket": args.socket, "budget": args.budget, "seconds": args.seconds, "seed": args.seed}
    counts: Counter = Counter()
    start = time.time()
    status = 0
    with path.open("a", encoding="utf-8") as sink, multiprocessing.get_context("fork").Pool(args.jobs, maxtasksperchild=200) as pool:
        for number, record in enumerate(pool.imap_unordered(_work, [(args.eval, item, options) for item in todo]), 1):
            sink.write(json.dumps(record, default=str) + "\n")
            sink.flush()
            counts[record["verdict"]] += 1
            if record["verdict"] == "differs":
                status = 1
                print(f"  DIFFERS {record['pair']}", flush=True)
            if number % 25 == 0:
                print(f"  {number}/{len(todo)} {dict(counts)} {time.time() - start:.0f}s", flush=True)
    print(f"{args.eval}-mysql: {dict(counts)} in {time.time() - start:.0f}s", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
