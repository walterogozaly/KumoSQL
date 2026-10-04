"""Ask a real engine which reading of an expression is right, then see what the parse check says.

    python tools/parser_oracle.py duckdb --count 4000 --seed 1
    KUMOSQL_MYSQL_SOCKET=/path/to/mysql.sock python tools/parser_oracle.py mysql --count 4000 --seed 1

Random constant expressions (integers 0-3 and NULL joined by the operators of the dialect, mostly without
parentheses) are run on the engine in three spellings:

* the text itself;
* sqlglot's reading of it, printed with every operation parenthesized;
* ``kumosql.parse_check.reading``, the independent reading, printed the same way.

Three things are counted. When the text and sqlglot's reading give different answers (or the engine rejects
the text sqlglot accepts), sqlglot read a query nobody wrote; ``kumosql.parse_check`` must disagree with
sqlglot on every such text ("recall"). When the text and the independent reading give different answers, the
precedence table is wrong ("table errors", must be 0). When the parse check disagrees and both readings give
the same answer, the disagreement is harmless on these constants (a text the engine accepts, read two ways that
happen to agree) and is listed, not counted against it.

Engines: ``duckdb`` (in process, standing in for the PostgreSQL grammar it is built on) and ``mysql`` (a MySQL
8.0 server, ``KUMOSQL_MYSQL_SOCKET`` or ``KUMOSQL_MYSQL_HOST``/``KUMOSQL_MYSQL_PORT``). BigQuery has no local
engine; its cases were run by hand (``docs/parser-checks.md``).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql import parse_check  # noqa: E402

BINARY = {
    "mysql": [
        "+", "-", "*", "/", "%", "DIV", "|", "&", "^", "<<", ">>", "=", "<=>", "<>", "<", ">", "<=", ">=",
        "AND", "OR", "XOR", "&&", "||",
    ],
    "duckdb": ["+", "-", "*", "/", "%", "|", "&", "<<", ">>", "=", "<>", "<", ">", "<=", ">=", "AND", "OR", "||"],
}
PREFIX = {"mysql": ["-", "~", "!", "NOT "], "duckdb": ["-", "~", "NOT "]}
POSTFIX = {
    "mysql": [" IS NULL", " IS NOT NULL", " IS TRUE", " IS NOT TRUE", " IS FALSE"],
    "duckdb": [" IS NULL", " IS NOT NULL", " IS TRUE", " IS NOT TRUE", " IS FALSE"],
}
ATOMS = ["0", "1", "2", "3", "NULL"]


def generate(rng: random.Random, dialect: str, depth: int = 0) -> str:
    """One random expression: operands joined by operators, prefixes and postfixes, a few parentheses."""

    def operand() -> str:
        roll = rng.random()
        if roll < 0.12 and depth < 2:
            return "(" + generate(rng, dialect, depth + 1) + ")"
        text = rng.choice(ATOMS)
        if rng.random() < 0.25:
            text = rng.choice(PREFIX[dialect]) + text
        if rng.random() < 0.15:
            text += rng.choice(POSTFIX[dialect])
        return text

    parts = [operand()]
    for _ in range(rng.randint(1, 3)):
        parts += [rng.choice(BINARY[dialect]), operand()]
    return " ".join(parts)


_WRAPPED = (exp.Binary, exp.Unary, exp.Between, exp.In, exp.Is, exp.Not)


def sqlglot_reading(sql: str, dialect: str) -> str | None:
    """sqlglot's tree for ``sql`` printed with every operation parenthesized (what sqlglot made of it)."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None
    if tree.find(exp.IntDiv):  # the MySQL generator prints DIV as a rounding CAST: its printing is wrong, not its reading
        return None
    for node in reversed(list(tree.find_all(*_WRAPPED))):  # deepest first, so a node moves with its wrapped children
        if isinstance(node.parent, _WRAPPED) and not isinstance(node, exp.Paren):
            node.replace(exp.Paren(this=node.copy()))
    try:
        return tree.sql(dialect=dialect)
    except Exception:
        return None


class Engine:
    name = ""

    def run(self, sql: str):
        """``("ok", value)``, ``("syntax", message)`` (the grammar rejects it) or ``("error", message)``."""
        raise NotImplementedError


class DuckDB(Engine):
    name = "duckdb"

    def __init__(self):
        import duckdb

        self.duckdb = duckdb
        self.con = duckdb.connect()

    def run(self, sql: str):
        try:
            return "ok", self.con.execute(sql).fetchall()
        except self.duckdb.ParserException as error:
            return "syntax", str(error)[:100]
        except Exception as error:
            return "error", str(error)[:100]


class MySQL(Engine):
    name = "mysql"

    def __init__(self):
        import pymysql

        self.pymysql = pymysql
        socket = os.environ.get("KUMOSQL_MYSQL_SOCKET")
        if socket:
            self.con = pymysql.connect(unix_socket=socket, user=os.environ.get("KUMOSQL_MYSQL_USER", "root"), autocommit=True)
        else:
            self.con = pymysql.connect(
                host=os.environ.get("KUMOSQL_MYSQL_HOST", "127.0.0.1"), port=int(os.environ.get("KUMOSQL_MYSQL_PORT", "3306")),
                user=os.environ.get("KUMOSQL_MYSQL_USER", "root"), autocommit=True,
            )

    def run(self, sql: str):
        try:
            with self.con.cursor() as cursor:
                cursor.execute(sql)
                return "ok", list(cursor.fetchall())
        except self.pymysql.err.ProgrammingError as error:  # 1064: syntax error
            return "syntax", str(error)[:100]
        except Exception as error:
            return "error", str(error)[:100]


def _number(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) or hasattr(value, "as_tuple"):
        number = float(value)
        return None if math.isnan(number) else round(number, 9)
    return value


def same(left, right) -> bool:
    if left[0] != "ok" or right[0] != "ok":
        return left[0] == right[0] and left[0] == "error"
    a, b = [[_number(v) for row in rows for v in row] for rows in (left[1], right[1])]
    return a == b


def survey(engine: Engine, dialect: str, count: int, seed: int) -> dict:
    rng = random.Random(seed)
    stats = dict(texts=0, engine_rejects=0, type_errors=0, misreads=0, caught=0, missed=[], table_errors=[], harmless_disagreements=[], false_declines=0, unchecked=0)
    seen = set()
    while stats["texts"] < count:
        text = "SELECT " + generate(rng, dialect)
        if text in seen:
            continue
        seen.add(text)
        verdict = parse_check.check_query(text, dialect)
        if verdict.status == "unchecked":
            stats["unchecked"] += 1
            continue
        stats["texts"] += 1
        truth = engine.run(text)
        mine = parse_check.reading(text, dialect)
        theirs = sqlglot_reading(text, dialect)
        if truth[0] == "syntax":
            stats["engine_rejects"] += 1
            if verdict.status == "disagree":
                stats["caught"] += 1
            else:
                stats["missed"].append((text, "engine rejects the text, sqlglot reads it"))
            stats["misreads"] += 1
            continue
        if truth[0] == "error":
            stats["type_errors"] += 1
            continue
        if mine is not None and not same(truth, engine.run(mine)):
            stats["table_errors"].append((text, mine))
        if theirs is None:
            continue
        misread = not same(truth, engine.run(theirs))
        if misread:
            stats["misreads"] += 1
            if verdict.status == "disagree":
                stats["caught"] += 1
            else:
                stats["missed"].append((text, theirs))
        elif verdict.status == "disagree":
            stats["harmless_disagreements"].append((text, verdict.reasons[0][:120]))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("engine", choices=["duckdb", "mysql"])
    parser.add_argument("--count", type=int, default=2000, help="expressions the parse check can read (default 2000)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=5, help="examples to print of each list")
    parser.add_argument("--json", help="write the full result here")
    args = parser.parse_args(argv)
    engine = DuckDB() if args.engine == "duckdb" else MySQL()
    dialect = args.engine
    stats = survey(engine, dialect, args.count, args.seed)
    print(f"{args.engine}: {stats['texts']} expressions, {stats['type_errors']} type errors skipped")
    print(f"  sqlglot misreads (answer differs, or engine rejects the text): {stats['misreads']}; parse check caught {stats['caught']}")
    print(f"  independent reading differs from the engine (table errors): {len(stats['table_errors'])}")
    print(f"  parse check disagrees where both readings give the engine's answer: {len(stats['harmless_disagreements'])}")
    for title, rows in (("MISSED", stats["missed"]), ("TABLE ERROR", stats["table_errors"]), ("HARMLESS", stats["harmless_disagreements"])):
        for row in rows[: args.show]:
            print(f"    {title}: {row}")
    if args.json:
        Path(args.json).write_text(json.dumps(stats, indent=1, default=str), encoding="utf-8")
    return 1 if stats["missed"] or stats["table_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
