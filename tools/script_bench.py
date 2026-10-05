"""BigQuery scripting suite: scripts generated in Python with the answer known by construction.

Every case is built from a small spec; the SQL and the expected answer come from the same spec, so no parser
(sqlglot included) produces the answer key:

* ``statements``  how many statements the script has once blocks are opened
* ``writes``      table -> the real tables that feed it (what lineage must report)
* ``reads``       every real table the script reads
* ``unknown``     statements that must be reported unknown rather than guessed (dynamic ``EXECUTE IMMEDIATE``, ``CALL``
                  of a procedure nobody defines)
* ``phantoms``    names that appear only in comments, strings, ignored statements or procedures that are never called;
                  none may show up as a read or an edge
* ``columns``     output column -> source columns, for the families that run through the pipeline

Families: tricky splitting, temporary-table chains, redefined temporary tables, ``INSERT`` into schema-only temporary
tables, DML on temporary tables, ``MERGE`` (random clauses over a table or subquery: target column lineage and the
``ON``/condition columns counted as read), script variables, ``IF``/``CASE`` branches, loops, exception handlers,
``EXECUTE IMMEDIATE`` (literal and dynamic), procedures, ignored statements, and random mixes. A job-history family
turns the same scripts into child jobs with anonymous temporary tables, and a public family runs the scripting
examples in ``tests/fixtures/bq_syntax`` against hand-written expectations (``benchmarks/script_cases``).

Scores, kept apart:

correctness   edges, reads or columns reported that the key does not have; statements guessed instead of reported
              unknown (all must be 0)
coverage      cases answered exactly, answered by reporting unknown where the key says unknown, or missed
analysis      statement-count, edge, read and column precision and recall

    python tools/script_bench.py [--cases 100] [--seed 1] [--write-results]
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql.pipeline import Pipeline  # noqa: E402
from kumosql.pipeline_types import ColumnRef, Model, Target  # noqa: E402
from kumosql.scripts import analyse_script, expand_script_jobs, split_script  # noqa: E402

DEV_FAMILIES = (
    "split",
    "temp_chain",
    "temp_redefine",
    "temp_insert",
    "temp_dml",
    "variables",
    "branches",
    "loops",
    "exception",
    "execute_literal",
    "execute_dynamic",
    "procedures",
    "ignored",
    "merge",
    "insert_values",
    "table_function",
    "function_definition",
    "degraded",
)
MIXED = ("mixed",)
JOBS = ("jobs",)
P, D = "p", "d"


@dataclass
class Case:
    family: str
    sql: str
    statements: int
    writes: dict[str, set[str]] = field(default_factory=dict)
    reads: set[str] = field(default_factory=set)
    unknown: int = 0
    phantoms: set[str] = field(default_factory=set)
    columns: dict[str, set[tuple[str, str]]] | None = None  # output column -> {(table, column)}
    columns_unknown: set[str] = field(default_factory=set)
    must_read: set[tuple[str, str]] = field(default_factory=set)  # (table, column) the script reads in ON and conditions
    procedures: str = ""


class Namer:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.n = 0

    def fresh(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def src(self) -> str:
        return self.fresh("src")


def ref(name: str) -> str:
    return f"`{P}.{D}.{name}`"


# --------------------------------------------------------------------------- families


def f_split(rng: random.Random) -> Case:
    n = Namer(rng)
    lines, writes, reads, phantoms = [], {}, set(), set()
    count = 0
    for _ in range(rng.randint(3, 7)):
        s, o = n.src(), n.fresh("out")
        text = rng.choice(["'a;b'", '"x;y"', "'''multi;\nline;'''", "r'raw;\\d'", "'it\\'s; here'", '"""q;q"""'])
        lines.append(f"INSERT INTO {ref(o)} SELECT {text} AS s, id FROM {ref(s)};")
        writes[o] = {s}
        reads.add(s)
        count += 1
        if rng.random() < 0.5:
            ph = n.fresh("decoy")
            lines.append(rng.choice([f"-- FROM {ph}; INSERT INTO x SELECT 1", f"/* ; FROM {ph} */", f"# FROM {ph};"]))
            phantoms.add(ph)
    return Case("split", "\n".join(lines), count, writes, reads, 0, phantoms)


def chain_case(rng: random.Random, depth: int, family: str) -> Case:
    """temp0 <- s1; temp_i <- temp_{i-1} JOIN s_{i+1}; final <- temp_depth. Known column lineage."""

    n = Namer(rng)
    srcs = [n.src() for _ in range(depth + 1)]
    temps = [n.fresh("tmp") for _ in range(depth)]
    lines = [f"CREATE TEMP TABLE {temps[0]} AS SELECT id AS k, amount AS v0 FROM {ref(srcs[0])};"]
    sources_of_v = {(srcs[0], "amount")}
    for i in range(1, depth):
        lines.append(
            f"CREATE TEMP TABLE {temps[i]} AS SELECT a.k, a.v{i - 1} + b.x AS v{i} FROM {temps[i - 1]} AS a JOIN {ref(srcs[i])} AS b ON a.k = b.id;"
        )
        sources_of_v = sources_of_v | {(srcs[i], "x")}
    out = n.fresh("out")
    lines.append(f"CREATE OR REPLACE TABLE {ref(out)} AS SELECT k, v{depth - 1} AS total FROM {temps[-1]};")
    reads = set(srcs[:depth])
    return Case(
        family,
        "\n".join(lines),
        depth + 1,
        {out: set(srcs[:depth])},
        reads,
        0,
        set(),
        {"k": {(srcs[0], "id")}, "total": sources_of_v},
    )


def f_temp_chain(rng: random.Random) -> Case:
    return chain_case(rng, rng.randint(1, 5), "temp_chain")


def f_temp_redefine(rng: random.Random) -> Case:
    n = Namer(rng)
    s1, s2, s3, out = n.src(), n.src(), n.src(), n.fresh("out")
    t = n.fresh("tmp")
    lines = [
        f"CREATE TEMP TABLE {t} AS SELECT id, amount FROM {ref(s1)};",
        f"CREATE OR REPLACE TEMP TABLE {t} AS SELECT a.id, a.amount + b.x AS amount FROM {t} AS a JOIN {ref(s2)} AS b ON a.id = b.id;",
    ]
    reads = {s1, s2}
    cols = {"id": {(s1, "id")}, "amount": {(s1, "amount"), (s2, "x")}}
    if rng.random() < 0.5:
        lines.append(f"CREATE OR REPLACE TEMP TABLE {t} AS SELECT a.id, a.amount * b.x AS amount FROM {t} AS a JOIN {ref(s3)} AS b ON a.id = b.id;")
        reads.add(s3)
        cols["amount"] = cols["amount"] | {(s3, "x")}
    lines.append(f"CREATE OR REPLACE TABLE {ref(out)} AS SELECT id, amount FROM {t};")
    return Case("temp_redefine", "\n".join(lines), len(lines), {out: set(reads)}, reads, 0, set(), cols)


def f_temp_insert(rng: random.Random) -> Case:
    n = Namer(rng)
    t, out = n.fresh("tmp"), n.fresh("out")
    arms = rng.randint(1, 3)
    lines = [f"CREATE TEMP TABLE {t} (id INT64, v FLOAT64);"]
    reads: set[str] = set()
    cols = {"id": set(), "v": set()}
    for _ in range(arms):
        s = n.src()
        reads.add(s)
        if rng.random() < 0.5:
            lines.append(f"INSERT INTO {t} SELECT id, amount FROM {ref(s)};")
            cols["id"].add((s, "id"))
            cols["v"].add((s, "amount"))
        else:
            lines.append(f"INSERT INTO {t} (v, id) SELECT price, key FROM {ref(s)};")
            cols["id"].add((s, "key"))
            cols["v"].add((s, "price"))
    lines.append(f"CREATE OR REPLACE TABLE {ref(out)} AS SELECT id, v FROM {t};")
    return Case("temp_insert", "\n".join(lines), len(lines), {out: set(reads)}, reads, 0, set(), cols)


def f_temp_dml(rng: random.Random) -> Case:
    """UPDATE or MERGE on a temporary table: tables are dependencies, the temporary table's columns are unknown."""

    n = Namer(rng)
    s1, s2, out, t = n.src(), n.src(), n.fresh("out"), n.fresh("tmp")
    change = rng.choice(
        [
            f"UPDATE {t} SET amount = (SELECT MAX(x) FROM {ref(s2)}) WHERE TRUE;",
            f"MERGE {t} AS m USING {ref(s2)} AS u ON m.id = u.id WHEN MATCHED THEN UPDATE SET amount = u.x;",
        ]
    )
    lines = [
        f"CREATE TEMP TABLE {t} AS SELECT id, amount FROM {ref(s1)};",
        change,
        f"CREATE OR REPLACE TABLE {ref(out)} AS SELECT id, amount FROM {t};",
    ]
    return Case("temp_dml", "\n".join(lines), 3, {out: {s1, s2}}, {s1, s2}, 0, set(), {"id": set(), "amount": set()}, {"id", "amount"})


def f_variables(rng: random.Random) -> Case:
    n = Namer(rng)
    w, f, floor, out, out2, unused = n.src(), n.src(), n.src(), n.fresh("out"), n.fresh("out"), n.src()
    lines = [
        f"DECLARE cutoff DATE DEFAULT (SELECT MAX(d) FROM {ref(w)});",
        "DECLARE n INT64 DEFAULT 5;",
        f"DECLARE spare INT64 DEFAULT (SELECT COUNT(*) FROM {ref(unused)});",
        f"INSERT INTO {ref(out)} SELECT a FROM {ref(f)} WHERE d > cutoff AND k > n;",
    ]
    writes = {out: {f, w}}
    reads = {f, w}
    stmts = 4
    if rng.random() < 0.7:
        lines.append(f"SET cutoff = (SELECT MIN(d) FROM {ref(floor)});")
        lines.append(f"INSERT INTO {ref(out2)} SELECT a FROM {ref(f)} WHERE d > cutoff;")
        writes[out2] = {f, floor}
        reads.add(floor)
        stmts += 2
    # ``spare`` is never used: its table is only in a declaration that nothing reads from
    return Case("variables", "\n".join(lines), stmts, writes, reads, 0, {unused})


def f_branches(rng: random.Random) -> Case:
    n = Namer(rng)
    arms = rng.randint(2, 3)
    out = n.fresh("out")
    srcs = [n.src() for _ in range(arms)]
    lines = ["DECLARE n INT64 DEFAULT 1;"]
    if rng.random() < 0.5:
        lines.append("IF n > 2 THEN")
        lines.append(f"  INSERT INTO {ref(out)} SELECT a FROM {ref(srcs[0])};")
        for s in srcs[1:-1]:
            lines.append(f"ELSEIF n = 1 THEN\n  INSERT INTO {ref(out)} SELECT a FROM {ref(s)};")
        lines.append(f"ELSE\n  INSERT INTO {ref(out)} SELECT a FROM {ref(srcs[-1])};")
        lines.append("END IF;")
    else:
        lines.append("CASE n")
        for i, s in enumerate(srcs[:-1]):
            lines.append(f"  WHEN {i} THEN INSERT INTO {ref(out)} SELECT a FROM {ref(s)};")
        lines.append(f"  ELSE INSERT INTO {ref(out)} SELECT a FROM {ref(srcs[-1])};")
        lines.append("END CASE;")
    return Case("branches", "\n".join(lines), 1 + arms, {out: set(srcs)}, set(srcs))


def f_loops(rng: random.Random) -> Case:
    n = Namer(rng)
    out, s, driver = n.fresh("out"), n.src(), n.src()
    kind = rng.choice(["while", "loop", "repeat", "for"])
    if kind == "while":
        body = f"WHILE i < 3 DO\n  INSERT INTO {ref(out)} SELECT a FROM {ref(s)};\n  SET i = i + 1;\nEND WHILE;"
        writes, reads, statements = {out: {s}}, {s}, 3
    elif kind == "loop":
        body = f"LOOP\n  INSERT INTO {ref(out)} SELECT a FROM {ref(s)};\n  SET i = i + 1;\n  IF i > 3 THEN LEAVE; END IF;\nEND LOOP;"
        writes, reads, statements = {out: {s}}, {s}, 4
    elif kind == "repeat":
        body = f"REPEAT\n  INSERT INTO {ref(out)} SELECT a FROM {ref(s)};\n  SET i = i + 1;\nUNTIL i > 3\nEND REPEAT;"
        writes, reads, statements = {out: {s}}, {s}, 3
    else:
        body = f"FOR r IN (SELECT id FROM {ref(driver)}) DO\n  INSERT INTO {ref(out)} SELECT a FROM {ref(s)} WHERE id = r.id;\nEND FOR;"
        writes, reads, statements = {out: {s, driver}}, {s, driver}, 2
        return Case("loops", "DECLARE i INT64 DEFAULT 0;\n" + body, statements, writes, reads)
    return Case("loops", "DECLARE i INT64 DEFAULT 0;\n" + body, statements, writes, reads)


def f_exception(rng: random.Random) -> Case:
    n = Namer(rng)
    out, a, b = n.fresh("out"), n.src(), n.src()
    sql = (
        f"BEGIN\n  BEGIN TRANSACTION;\n  INSERT INTO {ref(out)} SELECT x FROM {ref(a)};\n  COMMIT TRANSACTION;\n"
        f"EXCEPTION WHEN ERROR THEN\n  ROLLBACK TRANSACTION;\n  INSERT INTO {ref(out)} SELECT 'err; failed' AS x FROM {ref(b)};\nEND;"
    )
    return Case("exception", sql, 5, {out: {a, b}}, {a, b})


def f_execute_literal(rng: random.Random) -> Case:
    n = Namer(rng)
    out, a, b = n.fresh("out"), n.src(), n.src()
    form = rng.choice(["plain", "concat", "pipe", "triple"])
    inner = f"INSERT INTO `{P}.{D}.{out}` SELECT x FROM `{P}.{D}.{a}`"
    if form == "plain":
        stmt = f'EXECUTE IMMEDIATE "{inner}";'
    elif form == "concat":
        stmt = f"EXECUTE IMMEDIATE CONCAT('INSERT INTO `{P}.{D}.{out}` ', 'SELECT x FROM `{P}.{D}.{a}`');"
    elif form == "pipe":
        stmt = f"EXECUTE IMMEDIATE 'INSERT INTO `{P}.{D}.{out}` ' || 'SELECT x FROM `{P}.{D}.{a}`';"
    else:
        stmt = f'EXECUTE IMMEDIATE """{inner}""";'
    lines = [stmt, f"INSERT INTO {ref(out)} SELECT y FROM {ref(b)};"]
    return Case("execute_literal", "\n".join(lines), 2, {out: {a, b}}, {a, b})


def f_execute_dynamic(rng: random.Random) -> Case:
    n = Namer(rng)
    out, a, ph1, ph2 = n.fresh("out"), n.src(), n.fresh("guess"), n.fresh("guess")
    dynamic = rng.choice(
        [
            f"EXECUTE IMMEDIATE FORMAT('INSERT INTO `{P}.{D}.{ph1}` SELECT * FROM `{P}.{D}.%s`', tbl);",
            f"EXECUTE IMMEDIATE CONCAT('INSERT INTO `{P}.{D}.{ph1}` SELECT * FROM `{P}.{D}.', tbl, '`');",
            "EXECUTE IMMEDIATE stmt_text;",
        ]
    )
    phantoms = {ph1} if ph1 in dynamic else set()
    lines = ["DECLARE tbl STRING DEFAULT 'x';", "DECLARE stmt_text STRING DEFAULT 'SELECT 1';", dynamic, f"INSERT INTO {ref(out)} SELECT x FROM {ref(a)};"]
    return Case("execute_dynamic", "\n".join(lines), 4, {out: {a}}, {a}, 1, phantoms)


def f_procedures(rng: random.Random) -> Case:
    n = Namer(rng)
    out, a, never_out, never_src, extra = n.fresh("out"), n.src(), n.fresh("nout"), n.src(), n.src()
    defined = rng.random() < 0.7
    proc = f"CREATE OR REPLACE PROCEDURE `{P}.{D}.load_it`(IN day DATE)\nBEGIN\n  INSERT INTO {ref(out)} SELECT x FROM {ref(a)} WHERE d = day;\nEND;"
    never = f"CREATE OR REPLACE PROCEDURE `{P}.{D}.never_called`()\nBEGIN\n  INSERT INTO {ref(never_out)} SELECT x FROM {ref(never_src)};\nEND;"
    lines = [never]
    phantoms = {never_src, never_out}
    if defined:
        lines += [proc, f"CALL `{P}.{D}.load_it`(CURRENT_DATE());"]
        writes, reads, unknown = {out: {a}}, {a}, 0
    else:
        lines += [f"CALL `{P}.{D}.load_it`(CURRENT_DATE());"]
        writes, reads, unknown = {}, set(), 1
    lines.append(f"INSERT INTO {ref(extra + '_o')} SELECT 1 AS one FROM {ref(extra)};")
    writes[extra + "_o"] = {extra}
    reads.add(extra)
    # a procedure definition is one statement; its body only counts where it is called
    return Case("procedures", "\n".join(lines), 4 if defined else 3, writes, reads, unknown, phantoms)


def f_ignored(rng: random.Random) -> Case:
    n = Namer(rng)
    out, a = n.fresh("out"), n.src()
    ph = [n.fresh("ghost") for _ in range(4)]
    lines = [
        f"ASSERT (SELECT COUNT(*) FROM {ref(ph[0])}) > 0 AS 'empty';",
        "BEGIN TRANSACTION;",
        f"LOAD DATA INTO {ref(ph[1])} FROM FILES (format = 'CSV', uris = ['gs://b/f']);",
        f"DROP TABLE IF EXISTS {ref(ph[2])};",
        f"ALTER TABLE {ref(ph[3])} ADD COLUMN c INT64;",
        f"INSERT INTO {ref(out)} SELECT x FROM {ref(a)};",
        "COMMIT TRANSACTION;",
    ]
    # Table DDL and a load from files write their tables with no sources; only the name inside the ignored ASSERT is a phantom.
    return Case("ignored", "\n".join(lines), 7, {out: {a}, ph[1]: set(), ph[2]: set(), ph[3]: set()}, {a}, 0, {ph[0]})


def f_merge(rng: random.Random) -> Case:
    """MERGE with random clauses over a table or a subquery. Expected: assigned target column -> source columns, plus the ON and
    condition columns that must count as read."""

    n = Namer(rng)
    s1, s2, tgt = n.src(), n.src(), n.fresh("tgt")
    wide = ["id", "amount", "x", "key", "price", "d", "k", "a", "y"]
    if rng.random() < 0.4:
        using = f"(SELECT id, SUM(amount) AS amount, MAX(x) AS x FROM {ref(s1)} GROUP BY id) AS s"
        avail = {"id": {(s1, "id")}, "amount": {(s1, "amount")}, "x": {(s1, "x")}}
        subquery = True
    else:
        using = f"{ref(s1)} AS s"
        avail = {c: {(s1, c)} for c in wide}
        subquery = False
    names = list(avail)
    tcols = ["v", "w", "z"]
    reads = {s1}
    must_read = {(tgt, "id"), *avail["id"]}
    columns: dict[str, set[tuple[str, str]]] = {}
    clauses: list[str] = []

    def expression(sources: set[tuple[str, str]]) -> str:
        shape = rng.choice(["col", "sum", "coalesce", "case", "func"])
        picks = rng.sample(names, k=min(len(names), 2 if shape in {"sum", "coalesce", "case"} else 1))
        for pick in picks:
            sources |= avail[pick]
        if shape == "col":
            return f"s.{picks[0]}"
        if shape == "sum":
            return " + ".join(f"s.{p}" for p in picks)
        if shape == "coalesce":
            return f"COALESCE({', '.join('s.' + p for p in picks)}, 0)"
        if shape == "case":
            return f"CASE WHEN s.{picks[0]} > 0 THEN s.{picks[-1]} ELSE 0 END"
        return f"ABS(s.{picks[0]})"

    def condition(sees_target: bool = True) -> str:
        kind = rng.choice(["source", "target", "exists", "none"])
        if kind == "target" and not sees_target:
            kind = "source"
        if kind == "source":
            pick = rng.choice(names)
            must_read.update(avail[pick])
            return f" AND s.{pick} > 1"
        if kind == "target":
            must_read.add((tgt, "x"))
            return " AND t.x > 5"
        if kind == "exists":
            reads.add(s2)
            must_read.update({(s2, "k"), *avail["id"]})
            return f" AND EXISTS (SELECT 1 FROM {ref(s2)} AS e WHERE e.k = s.id)"
        return ""

    def assign(count: int) -> list[tuple[str, set[tuple[str, str]], str]]:
        out = []
        for column in rng.sample(tcols, k=count):
            sources: set[tuple[str, str]] = set()
            out.append((column, sources, expression(sources)))
        return out

    row_only = rng.random() < 0.2
    statements = 0
    if row_only:
        clauses.append("WHEN NOT MATCHED THEN INSERT ROW")
        for column, sources in avail.items():
            columns[column] = set(sources)
    else:
        for _ in range(rng.randint(1, 3)):
            kind = rng.choice(["update", "insert", "delete", "by_source"])
            if kind == "update":
                parts = assign(rng.randint(1, 2))
                clauses.append(f"WHEN MATCHED{condition()} THEN UPDATE SET " + ", ".join(f"t.{c} = {e}" for c, _s, e in parts))
            elif kind == "insert":
                parts = assign(rng.randint(1, 3))
                by = rng.choice(["", " BY TARGET"])
                clauses.append(
                    f"WHEN NOT MATCHED{by}{condition(False)} THEN INSERT ({', '.join(c for c, _s, _e in parts)}) VALUES ({', '.join(e for _c, _s, e in parts)})"
                )
            elif kind == "delete":
                clauses.append(f"WHEN MATCHED{condition()} THEN DELETE")
                parts = []
            else:
                must_read.add((tgt, "x"))
                clauses.append("WHEN NOT MATCHED BY SOURCE AND t.x > 5 THEN DELETE")
                parts = []
            for column, sources, _e in parts:
                columns.setdefault(column, set()).update(sources)
        if not columns:
            parts = assign(1)
            clauses.append("WHEN MATCHED THEN UPDATE SET " + ", ".join(f"t.{c} = {e}" for c, _s, e in parts))
            for column, sources, _e in parts:
                columns.setdefault(column, set()).update(sources)
    sql = f"MERGE {ref(tgt)} AS t USING {using} ON t.id = s.id " + " ".join(clauses)
    return Case("merge", sql, 1, {tgt: set(reads)}, reads, 0, set(), columns, set(), must_read)


def f_insert_values(rng: random.Random) -> Case:
    """INSERT ... VALUES: a constant source. Constants have no source column; a scalar subquery among the values reads its table."""

    n = Namer(rng)
    tgt = n.fresh("tgt")
    names = rng.sample(["v", "w", "z", "label", "flag", "amount"], rng.randint(2, 4))
    reads: set[str] = set()
    columns: dict[str, set[tuple[str, str]]] = {}
    row_count = rng.randint(1, 3)
    subquery_at = rng.choice(names) if rng.random() < 0.5 else None
    rows = []
    src = n.src()
    for r in range(row_count):
        cells = []
        for name in names:
            if name == subquery_at and r == 0:
                cells.append(f"(SELECT MAX(id) FROM {ref(src)})")
                reads.add(src)
                columns[name] = {(src, "id")}
            else:
                cells.append(rng.choice(["1", "'a'", "NULL", "TRUE", "DATE '2024-01-01'", "2.5", "CONCAT('x', 'y')"]))
                columns.setdefault(name, set())
        rows.append("(" + ", ".join(cells) + ")")
    sql = f"INSERT INTO {ref(tgt)} ({', '.join(names)}) VALUES " + ", ".join(rows)
    return Case("insert_values", sql, 1, {tgt: set(reads)}, reads, 0, set(), columns)


def f_table_function(rng: random.Random) -> Case:
    """A table function defined in the script, then called with a table argument: the call reads as the function's query."""

    n = Namer(rng)
    src, out, fn = n.src(), n.fresh("out"), n.fresh("tvf")
    lookup = n.fresh("lkp") if rng.random() < 0.5 else None
    body = "SELECT t.id, t.amount FROM t"
    columns = {"id": {(src, "id")}, "amount": {(src, "amount")}}
    reads = {src}
    if lookup:
        body = f"SELECT t.id, t.amount, l.price AS price FROM t JOIN {ref(lookup)} l ON l.id = t.id"
        columns["price"] = {(lookup, "price")}
        reads.add(lookup)
    body += rng.choice(["", " LIMIT n"])
    call = rng.choice([f"{ref(fn)}(TABLE {ref(src)}, n => {rng.randint(1, 9)})", f"{ref(fn)}(TABLE {ref(src)}, {rng.randint(1, 9)})"])
    outputs = ["id", "amount"] + (["price"] if lookup else [])
    if rng.random() < 0.5:
        picked = rng.sample(outputs, rng.randint(1, len(outputs)))
        select = ", ".join(f"f.{c}" for c in picked)
        call += " AS f"
    else:
        picked = outputs
        select = "*"
    columns = {c: columns[c] for c in picked}
    lines = [
        f"CREATE TEMP TABLE FUNCTION {fn}(t TABLE<id INT64, amount INT64>, n INT64) AS ({body});",
        f"CREATE TABLE {ref(out)} AS SELECT {select} FROM {call};",
    ]
    return Case("table_function", "\n".join(lines), 2, {out: set(reads)}, reads, 0, set(), columns)


def f_function_definition(rng: random.Random) -> Case:
    """Scalar function definitions are not steps of the data flow; what their bodies read still feeds the tables that call them."""

    n = Namer(rng)
    src, out = n.src(), n.fresh("out")
    lines = [f"CREATE TEMP FUNCTION scale(x INT64) AS (x * {rng.randint(2, 9)});"]
    statements = 1
    reads = {src}
    if rng.random() < 0.6:
        lookup = n.fresh("lkp")
        lines.append(f"CREATE TEMP FUNCTION lookup_size() AS ((SELECT COUNT(*) FROM {ref(lookup)}));")
        statements += 1
        reads.add(lookup)
        select = f"scale(amount) AS dbl, id, lookup_size() AS size"
    else:
        select = "scale(amount) AS dbl, id"
    lines.append(f"CREATE TABLE {ref(out)} AS SELECT {select} FROM {ref(src)};")
    statements += 1
    columns = {"dbl": {(src, "amount")}, "id": {(src, "id")}}
    return Case("function_definition", "\n".join(lines), statements, {out: set(reads)}, reads, 0, set(), columns)


def f_degraded(rng: random.Random) -> Case:
    """A statement sqlglot cannot parse: its tables still become edges, it is reported unknown (columns never guessed), and names in
    comments or strings are not tables."""

    n = Namer(rng)
    a, b, out, ghost, ghost2 = n.src(), n.src(), n.fresh("out"), n.fresh("ghost"), n.fresh("ghost")
    garbage = rng.choice(["???", "@@ 3 ))", "~~~ ((", "$$ ] ["])
    forms = [
        (f"INSERT INTO {ref(out)} SELECT id FROM {ref(a)} s JOIN {ref(b)} l ON s.id = l.id WHERE s.x {garbage} 3", {a, b}),
        (f"CREATE TABLE {ref(out)} AS SELECT id FROM {ref(a)}, {ref(b)} WHERE {garbage}", {a, b}),
        (f"MERGE {ref(out)} T USING (SELECT id FROM {ref(a)}) S ON {garbage} WHEN MATCHED THEN UPDATE SET v = 1", {a}),
        (f"WITH c AS (SELECT id FROM {ref(a)}) INSERT INTO {ref(out)} SELECT id FROM c WHERE {garbage}", {a}),
    ]
    text, reads = rng.choice(forms)
    lines = [f"-- FROM {ghost}", text.replace("WHERE", f"WHERE 'FROM {ghost2}' = '' AND", 1) if "WHERE" in text and rng.random() < 0.4 else text]
    sql = "\n".join(lines)
    phantoms = {ghost} | ({ghost2} if ghost2 in sql else set())
    return Case("degraded", sql, 1, {out: set(reads)}, set(reads), 1, phantoms)


GENERATORS = {
    "split": f_split,
    "temp_chain": f_temp_chain,
    "temp_redefine": f_temp_redefine,
    "temp_insert": f_temp_insert,
    "temp_dml": f_temp_dml,
    "variables": f_variables,
    "branches": f_branches,
    "loops": f_loops,
    "exception": f_exception,
    "execute_literal": f_execute_literal,
    "execute_dynamic": f_execute_dynamic,
    "procedures": f_procedures,
    "ignored": f_ignored,
    "merge": f_merge,
    "insert_values": f_insert_values,
    "table_function": f_table_function,
    "function_definition": f_function_definition,
    "degraded": f_degraded,
}
MIXABLE = [
    f
    for f in DEV_FAMILIES
    if f not in {"temp_chain", "temp_redefine", "temp_insert", "temp_dml", "merge", "insert_values", "table_function", "function_definition", "degraded"}
]


def f_mixed(rng: random.Random) -> Case:
    """Several different families back to back. Names never repeat because each generator numbers from its own seed."""

    parts = []
    for index in range(rng.randint(2, 4)):
        family = rng.choice(MIXABLE)
        case = GENERATORS[family](random.Random(rng.random()))
        parts.append(rename(case, f"m{index}_"))
    sql = "\n".join(p.sql for p in parts if not p.procedures)
    merged = Case("mixed", sql, sum(p.statements for p in parts))
    for p in parts:
        for t, s in p.writes.items():
            merged.writes[t] = merged.writes.get(t, set()) | s
        merged.reads |= p.reads
        merged.unknown += p.unknown
        merged.phantoms |= p.phantoms
    return merged


def rename(case: Case, prefix: str) -> Case:
    """Prefix every table name so cases cannot collide when joined; variable names are made distinct too."""

    import re

    names = set(case.reads) | set(case.phantoms) | {t for t in case.writes} | {s for v in case.writes.values() for s in v}
    sql = case.sql
    for name in sorted(names, key=len, reverse=True):
        sql = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w])|(?<=\.){re.escape(name)}(?=`)", prefix + name, sql)
    for var in ("cutoff", "n", "i", "tbl", "stmt_text", "spare"):
        sql = re.sub(rf"(?<![\w.`]){var}(?![\w`(])", f"{prefix}{var}", sql)
    sql = sql.replace(".load_it`", f".{prefix}load_it`").replace(".never_called`", f".{prefix}never_called`")
    return Case(
        case.family,
        sql,
        case.statements,
        {prefix + t: {prefix + s for s in v} for t, v in case.writes.items()},
        {prefix + r for r in case.reads},
        case.unknown,
        {prefix + p for p in case.phantoms},
    )


GENERATORS["mixed"] = f_mixed

# ----------------------------------------------------------------------------- scoring


def pipeline_for(case: Case) -> Pipeline:
    schema = {}
    sources = {}
    for name in case.reads:
        key = f"{P}.{D}.{name}"
        sources[key] = Target(P, D, name)
        schema[key] = {c: "INT64" for c in ("id", "amount", "x", "key", "price", "d", "k", "a", "y")}
    return Pipeline({f"{P}.{D}.final": Model(Target(P, D, "final"), "table", case.sql)}, sources, schema)


def score_case(case: Case) -> dict:
    result = {"family": case.family, "wrong": [], "missed": [], "unknown_ok": False}
    parts = split_script(case.sql)
    if case.family != "mixed" and len(parts) != case.statements:
        result["wrong"].append(f"statements {len(parts)} != {case.statements}")
    analysis = analyse_script(case.sql, procedures=None)
    got_writes: dict[str, set[str]] = defaultdict(set)
    for write in analysis.writes:
        got_writes[write.table.name].update(t.name for t in write.sources)
    got_reads = {t.name for t in analysis.all_reads()}
    unknown = len(analysis.unknown)
    for name in case.phantoms:
        if name in got_reads or name in got_writes or any(name in s for s in got_writes.values()):
            result["wrong"].append(f"phantom {name}")
    for table, sources in got_writes.items():
        want = case.writes.get(table)
        if want is None:
            result["wrong"].append(f"unexpected write {table}")
        elif sources - want:
            result["wrong"].append(f"extra sources for {table}: {sorted(sources - want)}")
        elif want - sources:
            result["missed"].append(f"{table} lacks {sorted(want - sources)}")
    for table in case.writes:
        if table not in got_writes:
            result["missed"].append(f"write {table}")
    if got_reads - case.reads:
        result["wrong"].append(f"extra reads {sorted(got_reads - case.reads)}")
    if case.reads - got_reads:
        result["missed"].append(f"reads {sorted(case.reads - got_reads)}")
    if unknown < case.unknown:
        result["wrong"].append(f"guessed {case.unknown - unknown} statement(s) that must be unknown")
    elif unknown > case.unknown:
        result["missed"].append(f"{unknown - case.unknown} statement(s) reported unknown that the key can read")
    result["unknown_ok"] = unknown == case.unknown
    result.update(
        edges_true=sum(len(v) for v in case.writes.values()),
        edges_found=sum(len(v) for v in got_writes.values()),
        edges_correct=sum(len(got_writes[t] & case.writes.get(t, set())) for t in got_writes),
    )
    if case.columns is not None and not result["wrong"] and not result["missed"]:
        result.update(columns_score(case))
        result["missed"] += result.pop("columns_missed")
    return result


def columns_score(case: Case) -> dict:
    pl = pipeline_for(case)
    rows = {r["column"]: r for r in pl.lineage_report() if r["node"] == f"{P}.{D}.final"}
    lineage = pl.column_lineage()
    wrong, missed, exact = [], [], 0
    for column, want in case.columns.items():
        row = rows.get(column)
        if column in case.columns_unknown:
            if row is None or row["status"] != "unknown":
                wrong.append(f"{column} should be unknown")
            else:
                exact += 1
            continue
        got = {(c.table.split(".")[-1], c.column) for c in lineage.get(ColumnRef(f"{P}.{D}.final", column), set())}
        if row is None or row["status"] != ("traced" if want else "constant"):
            missed.append(f"{column} not {'traced' if want else 'constant'}")
        elif got != want:
            (wrong if got - want else missed).append(f"{column}: {sorted(got)} != {sorted(want)}")
        else:
            exact += 1
    if case.must_read:
        used = {(c.table.split(".")[-1], c.column) for c in pl.consumed_columns().get(f"{P}.{D}.final", ())}
        lacking = sorted(case.must_read - used)
        if lacking:
            missed.append(f"not counted as read: {lacking}")
            exact = min(exact, len(case.columns) - 1)
    return {"columns_total": len(case.columns), "columns_exact": exact, "columns_wrong": wrong, "columns_missed": missed}


def job_case(rng: random.Random) -> tuple[list[dict], dict[str, set[str]]]:
    """A script as child jobs with anonymous temporary tables, plus the edges the final tables must have."""

    n = Namer(rng)
    depth = rng.randint(2, 5)
    srcs = [n.src() for _ in range(depth + 1)]
    tmp = [f"{P}._script{rng.randint(1000, 9999)}.anon{i}" for i in range(depth)]
    records = [{"job_id": "s", "parent_job_id": "", "statement_type": "SCRIPT", "destination": "", "referenced_tables": [], "creation_time": "2024-01-01T00:00:00Z"}]
    expected: dict[str, set[str]] = {}
    previous = None
    for i in range(depth):
        refs = [f"{P}.{D}.{srcs[i]}"] + ([previous] if previous else [])
        records.append({"job_id": f"s_{i}", "parent_job_id": "s", "destination": tmp[i], "referenced_tables": refs, "creation_time": f"2024-01-01T00:00:{i + 1:02d}Z", "statement_type": "CREATE_TABLE_AS_SELECT"})
        previous = tmp[i]
    final = f"{P}.{D}.{n.fresh('out')}"
    records.append({"job_id": "s_f", "parent_job_id": "s", "destination": final, "referenced_tables": [previous, f"{P}.{D}.{srcs[depth]}"], "creation_time": "2024-01-01T00:01:00Z", "statement_type": "INSERT"})
    expected[final] = {f"{P}.{D}.{s}" for s in srcs}
    rng.shuffle(records)  # the order of the file must not matter: creation time does
    return records, expected


def score_jobs(rng: random.Random, count: int) -> dict:
    wrong = missed = exact = 0
    details = []
    for _ in range(count):
        records, expected = job_case(rng)
        out, _summary = expand_script_jobs(records)
        got = {r["destination"]: set(r["referenced_tables"]) for r in out}
        if any(".anon" in str(d) or "_script" in str(d) for d in got):
            wrong += 1
            details.append("temporary table left in the graph")
        elif got == expected:
            exact += 1
        elif any(got.get(t, set()) - s for t, s in expected.items()) or set(got) - set(expected):
            wrong += 1
            details.append(f"{got} != {expected}")
        else:
            missed += 1
    return {"family": "jobs", "cases": count, "exact": exact, "wrong": wrong, "missed": missed, "details": details[:5]}


# ------------------------------------------------------------------------------ public


def run_public() -> dict:
    """Scripting examples checked into tests/fixtures/bq_syntax, against labels written by hand from the
    BigQuery scripting reference (``benchmarks/script_cases/public_scripting.json``)."""

    labels = json.loads((ROOT / "benchmarks" / "script_cases" / "public_scripting.json").read_text())
    exact = wrong = 0
    details = []
    for name, want in labels["cases"].items():
        text = (ROOT / "tests" / "fixtures" / "bq_syntax" / "sql" / "script" / f"{name}.sql").read_text()
        analysis = analyse_script(text)
        got = {
            "statements": len(split_script(text)),
            "reads": sorted({t.name for t in analysis.all_reads()}),
            "writes": sorted(w.table.name for w in analysis.writes),
            "unknown": len(analysis.unknown),
        }
        if got == want:
            exact += 1
        else:
            wrong += 1
            details.append({"case": name, "got": got, "want": want})
    return {"cases": len(labels["cases"]), "exact": exact, "wrong": wrong, "details": details}


# -------------------------------------------------------------------------------- run


def run_suite(families, cases: int = 40, seed: int = 1) -> dict:
    totals = defaultdict(int)
    per_family: dict[str, dict] = {}
    details: list[str] = []
    started = time.perf_counter()
    for family in families:
        row = defaultdict(int)
        for index in range(cases):
            rng = random.Random(f"{family}:{seed}:{index}")
            case = GENERATORS[family](rng)
            outcome = score_case(case)
            row["cases"] += 1
            row["statements"] += case.statements
            row["edges_true"] += outcome["edges_true"]
            row["edges_found"] += outcome["edges_found"]
            row["edges_correct"] += outcome["edges_correct"]
            if outcome["wrong"]:
                row["wrong"] += 1
                details.append(f"{family}#{index}: {outcome['wrong'][:2]}")
            elif outcome["missed"]:
                row["missed"] += 1
            else:
                row["exact"] += 1
            if case.unknown and outcome["unknown_ok"]:
                row["unknown_flagged"] += 1
            if "columns_total" in outcome:
                row["columns_total"] += outcome["columns_total"]
                row["columns_exact"] += outcome["columns_exact"]
                row["columns_wrong"] += len(outcome["columns_wrong"])
                if outcome["columns_wrong"]:
                    details.append(f"{family}#{index}: columns {outcome['columns_wrong'][:2]}")
        per_family[family] = dict(row)
        for key, value in row.items():
            totals[key] += value
    totals["seconds"] = round(time.perf_counter() - started, 2)
    return {"families": per_family, "totals": dict(totals), "details": details}


def _quiet() -> None:
    """Command-line runs print results, not per-stage timings or sqlglot warnings."""

    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


def main(argv: list[str]) -> None:
    _quiet()
    cases = int(argv[argv.index("--cases") + 1]) if "--cases" in argv else 100
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 1
    dev = run_suite(DEV_FAMILIES, cases, seed)
    mixed = run_suite(MIXED, cases, seed)
    jobs = score_jobs(random.Random(seed), cases)
    public = run_public()
    for title, result in (("dev families", dev), ("mixed scripts", mixed)):
        t = result["totals"]
        print(f"{title}: {t['exact']}/{t['cases']} exact, {t.get('missed', 0)} missed, {t.get('wrong', 0)} wrong; "
              f"edges {t['edges_correct']}/{t['edges_true']} found, {t['edges_found'] - t['edges_correct']} extra; "
              f"columns {t.get('columns_exact', 0)}/{t.get('columns_total', 0)} exact, {t.get('columns_wrong', 0)} wrong")
        for line in result["details"][:10]:
            print("  ", line)
    print(f"jobs: {jobs['exact']}/{jobs['cases']} exact, {jobs['missed']} missed, {jobs['wrong']} wrong")
    print(f"public examples: {public['exact']}/{public['cases']} exact, {public['wrong']} wrong")
    for d in public["details"][:5]:
        print("  ", d)
    if "--write-results" in argv:
        write_results(dev, mixed, jobs, public, cases, seed)


def write_results(dev, mixed, jobs, public, cases, seed) -> None:
    td, tm = dev["totals"], mixed["totals"]
    wrong = td.get("wrong", 0) + tm.get("wrong", 0) + jobs["wrong"] + public["wrong"] + td.get("columns_wrong", 0) + tm.get("columns_wrong", 0)
    total = td["cases"] + tm["cases"] + jobs["cases"] + public["cases"]
    exact = td["exact"] + tm["exact"] + jobs["exact"] + public["exact"]
    out = {
        "suite": "BigQuery scripts (generated and public examples)",
        "order": 215,
        "size": total,
        "score": f"{exact}/{total} scripts exact, {wrong} wrong",
        "metric": (
            "Multi-statement BigQuery scripts split into statements, with what each statement reads and writes followed through "
            "temporary tables, script variables, branches, loops, exception handlers, literal EXECUTE IMMEDIATE, procedures, MERGE (target columns from the USING source, ON and condition columns read), INSERT VALUES as a constant source, "
            "table functions defined in the script (a call reads as the function's query), function definitions (never counted as skipped) and statements that do not parse (tables kept, columns unknown); "
            "dynamic SQL and undefined procedures must be reported unknown, and names that only appear in comments, strings, "
            "ignored statements or procedures never called must not become edges."
        ),
        "evidence": "executed",
        "correctness": (
            f"{wrong} wrong: no edge, read or column that the answer key lacks, no phantom table from comments, strings or ignored "
            f"statements, and none of the {td.get('unknown_flagged', 0) + tm.get('unknown_flagged', 0)} scripts with dynamic SQL or an "
            "undefined procedure guessed (each reported unknown; counted as exact)"
        ),
        "coverage": {"proven": exact, "unknown": total - exact},
        "held_out": "none",
        "docs": "docs/scripts.md#evaluation",
        "command": "python tools/script_bench.py --write-results",
        "date": time.strftime("%Y-%m-%d"),
        "caveats": (
            "Answers come from the generator, not a parser, but the families are ones KumoSQL's author could think of and were "
            "tuned against while building the feature; there is no held-out family. The public examples are the scripting cases "
            "in tests/fixtures/bq_syntax with labels written by hand. A script that mixes the families with the same seed is not "
            "independent of them."
        ),
        "analysis": (
            f"Edges: {td['edges_correct'] + tm['edges_correct']}/{td['edges_true'] + tm['edges_true']} found, "
            f"{(td['edges_found'] - td['edges_correct']) + (tm['edges_found'] - tm['edges_correct'])} extra. "
            f"Output columns traced through temporary-table chains, MERGE clauses (ON and condition columns counted as read), INSERT VALUES, table-function calls and scalar function calls: {td.get('columns_exact', 0) + tm.get('columns_exact', 0)}/"
            f"{td.get('columns_total', 0) + tm.get('columns_total', 0)} exact."
        ),
        "performance": f"{td['cases'] + tm['cases']} generated scripts in {round(td['seconds'] + tm['seconds'], 1)} s",
    }
    path = ROOT / "benchmarks" / "results" / "script-splitting.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
