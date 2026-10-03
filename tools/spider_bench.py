"""Spider 1.0's gold queries and TestSuiteEval's hand-labelled false negatives, scored with no language model.

Two evals share the Spider schemas (``tables.json``: column types, primary and foreign keys):

* **esm** (``spider-esm-equivalent``): TestSuiteEval's ``ESMFalseNegatives.tsv`` (Zhong, Yu and Klein,
  "Semantic Evaluation for Text-to-SQL with Distilled Test Suites", EMNLP 2020) lists 558 (database,
  gold, prediction, reason) rows that Spider's exact-set-match metric marked wrong and the authors
  judged equivalent. Each distinct pair is **proven** (the algebraic prover, SQLite dialect), **refuted**
  (SQLite returns different results on a database that respects the listed keys and foreign keys) or
  **unknown**. Every proof is re-checked on random databases; a difference there would make it wrong.
  A refutation is a label failure upstream, never a KumoSQL answer: it comes with the database,
  shrunk row by row, and a cause (the label needs DISTINCT removed, as TestSuiteEval's metric does by
  default, or needs a column to be NOT NULL although the schema allows NULL, or something else).

  Many predictions carry value placeholders (``'terminal'``, ``"value"``, ``1``): the model that wrote
  them did not predict values, and TestSuiteEval plugs the gold query's values into them. Those pairs
  are **adapted** the same way (every assignment of the gold's values to the prediction's value slots,
  up to 64; LIMIT and OFFSET counts are kept) and scored apart from the pairs taken as published: an
  adapted pair is proven when one assignment is proven, refuted when every assignment is refuted.

* **rewrites** (``spider-dev-rewrites``): each distinct gold query of Spider's dev set is translated
  from SQLite to BigQuery (sqlglot, identifiers lower-cased) and every KumoSQL rewrite runs on it: each
  registered rule on its own, the canonical cleanup pipeline, and the proof-gated ``query_optimizer``
  given the Spider catalog. Each output that changes the query is run, translated back to SQLite,
  against the translated input on databases that respect the keys and foreign keys. ``wrong`` is a
  rewrite KumoSQL trusts (proven, or returned by the optimizer) whose result changes on such a database.

Keys. Spider's ``tables.json`` lists only the first column of a composite primary key (the real
``singer_in_concert`` key is ``(concert_ID, Singer_ID)``; the file lists ``concert_ID``). A listed key is
part of the true key, so a database where it is unique is a valid Spider database: the refuting side
uses every listed key. The prover only gets a key when it is very likely the full key: a listed key
column that is not also a foreign-key column of its own table (the first column of a composite key is
in practice always a reference), with foreign keys to such keys. Keys are NOT NULL.

The data are downloaded at run time from pinned commits and checked against SHA-256 digests (Spider's
data are CC BY-SA 4.0, TestSuiteEval has no licence), into ``$KUMOSQL_BENCH_DATA/spider`` or
``~/.cache/kumosql-bench/spider``; nothing is copied into this repository. Spider's SQLite databases
are on Google Drive and the Yale site, both blocked here, so every database is one KumoSQL builds.

Held out: for esm, one distinct pair in five by a SHA-1 hash of its text; for rewrites, the gold
queries of one database in five by a SHA-1 hash of the database name. ``--split dev`` runs the rest.

    python tools/spider_bench.py esm                       # about a minute on 4 cores
    python tools/spider_bench.py rewrites --split dev
    python tools/spider_bench.py all --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import csv
from dataclasses import dataclass, field
import hashlib
import io
import itertools
import json
import logging
import os
from pathlib import Path
import sys
import time
import urllib.request

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

import llm_sql_solver_bench as solver  # noqa: E402

SPIDER_COMMIT = "b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c"
TESTSUITE_COMMIT = "7bf637883cf092867d33f58ee6f365f5a05e0ee2"
FILES = {
    "dev.json": (
        f"https://raw.githubusercontent.com/taoyds/spider/{SPIDER_COMMIT}/evaluation_examples/examples/dev.json",
        "30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4",
    ),
    "tables.json": (
        f"https://raw.githubusercontent.com/taoyds/spider/{SPIDER_COMMIT}/evaluation_examples/examples/tables.json",
        "61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed",
    ),
    "ESMFalseNegatives.tsv": (
        f"https://raw.githubusercontent.com/ruiqi-zhong/TestSuiteEval/{TESTSUITE_COMMIT}/ESMFalseNegatives.tsv",
        "6367141ab7d4c7290dba3ba45361c558e5ef986d2f78af647378974ffa4c7f80",
    ),
}
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "spider"
PLUG_LIMIT = 64  # assignments of the gold's values to a prediction's value slots
OPTIMIZER_BUDGET_S = 30.0
BQ_TYPES = {"INTEGER": "int64", "TEXT": "string"}


def fetch(name: str, cache: Path | None = None) -> Path:
    """One pinned source file, downloaded once (written atomically, so parallel runs can share the cache)."""

    url, digest = FILES[name]
    folder = cache or CACHE
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if not path.exists():
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        partial = path.with_name(f"{name}.{os.getpid()}.part")
        partial.write_bytes(data)
        os.replace(partial, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"{path} does not match the pinned version; delete it to download again")
    return path


# -- schemas ---------------------------------------------------------------------------


@dataclass
class Schema:
    database: str
    tables: dict[str, dict[str, str]]  # lower-case table -> lower-case column -> declared SQLite type
    keys: dict[str, tuple[str, ...]]  # the listed primary key (part of the true key): used to build databases
    foreign: tuple  # ((child table, child column, parent table, parent column), ...)
    prover_keys: dict[str, tuple[str, ...]]  # listed keys that are very likely the whole key: given to the prover

    def case(self, sql1: str = "", sql2: str = "", index: int = 0) -> solver.Case:
        """The LLM-SQL-Solver harness's case shape, whose SQLite database search this reuses."""

        return solver.Case("spider", index, self.database, sql1, sql2, "equivalent", self.tables, self.keys, self.foreign)

    @property
    def unique(self) -> dict[str, tuple[str, ...]]:
        """Columns a foreign key points at, other than the listed key: SQL requires them to be unique."""

        out: dict[str, tuple[str, ...]] = {}
        for _, _, parent, column in self.foreign:
            if (column,) != self.keys.get(parent) and column not in out.get(parent, ()):
                out[parent] = out.get(parent, ()) + (column,)
        return out

    def constraints(self) -> dict:
        from kumosql.smt_equivalence import TableConstraints

        out = {}
        for table in self.tables:
            key = self.prover_keys.get(table, ())
            references = tuple(
                ((child_column,), parent, (parent_column,))
                for child, child_column, parent, parent_column in self.foreign
                if child == table and self.prover_keys.get(parent) == (parent_column,)
            )
            # A listed key column is NOT NULL even when it is only the first column of a composite key
            not_null = frozenset(self.keys.get(table, ()))
            if not_null or references:
                out[table] = TableConstraints(not_null=not_null, keys=(key,) if key else (), foreign_keys=references)
        return out

    def catalog(self):
        from kumosql.query_optimizer import Catalog

        return Catalog(
            columns={t: list(cols) for t, cols in self.tables.items()},
            types={t: {c: BQ_TYPES.get(k, "string") for c, k in cols.items()} for t, cols in self.tables.items()},
            not_null={t: set(k) for t, k in self.prover_keys.items()},
            keys={t: [k] for t, k in self.prover_keys.items()},
        )


def load_schemas(path: Path | None = None) -> dict[str, Schema]:
    schemas = {}
    for db in json.loads((path or fetch("tables.json")).read_text(encoding="utf-8")):
        names = [t.lower() for t in db["table_names_original"]]
        # sqlite_sequence is SQLite's own bookkeeping table (world_1 lists it): never queried, and not creatable
        tables: dict[str, dict[str, str]] = {t: {} for t in names if not t.startswith("sqlite_")}
        columns = []  # index -> (table, column)
        for (table_index, column), kind in zip(db["column_names_original"], db["column_types"]):
            if table_index < 0 or names[table_index] not in tables:
                columns.append(None)
                continue
            table = names[table_index]
            tables[table][column.lower()] = solver.DECLARED.get(kind, "TEXT")
            columns.append((table, column.lower()))
        keys = {columns[i][0]: (columns[i][1],) for i in db["primary_keys"] if columns[i]}
        foreign = tuple(
            (columns[c][0], columns[c][1], columns[p][0], columns[p][1])
            for c, p in db["foreign_keys"]
            if columns[c] and columns[p] and columns[c] != columns[p]
        )
        referencing = {(child, column) for child, column, _, _ in foreign}
        prover_keys = {t: k for t, k in keys.items() if (t, k[0]) not in referencing}
        schemas[db["db_id"]] = Schema(db["db_id"], tables, keys, foreign, prover_keys)
    return schemas


# -- shared checks ---------------------------------------------------------------------


def prove(sql1: str, sql2: str, schema: Schema) -> bool:
    """The algebraic prover (SQLite dialect) with the keys and foreign keys it is given, under Spider's comparison."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    if solver.ordered(sql1):
        if not solver.ordered(sql2):
            return False
        sql1, sql2 = solver.for_prover(sql1), solver.for_prover(sql2)
    if solver.mixed_type_comparison(sql1, schema.tables) or solver.mixed_type_comparison(sql2, schema.tables):
        return False  # SQLite converts one side of a text-number comparison; the prover reasons about typed values
    try:
        result = prove_equivalent_algebraic(
            sql1, sql2, schema={t: list(cols) for t, cols in schema.tables.items()}, constraints=schema.constraints(),
            compare_names=False, dialect="sqlite", timeout_ms=solver.PROVER_TIMEOUT_MS,
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return False
    return result.status is SmtStatus.PROVEN_EQUIVALENT


def differs(schema: Schema, sql1: str, sql2: str, **options) -> str:
    """"differs", "agree" or "error" on random schema-valid SQLite databases, compared as Spider compares results.

    The LLM-SQL-Solver harness's search, with more guards: an order-dependent comparison is only made on a
    database where neither query's ORDER BY has ties, the columns foreign keys point at are unique, and a
    database that breaks a constraint (a NULL key where a key column references an empty table) is skipped.
    """

    return solver.differs_on_random_databases(
        schema.case(sql1, sql2), sql1, sql2, tie_safe=True, unique=schema.unique, valid=lambda database: _valid(schema, database), **options,
    )


def refute(schema: Schema, sql1: str, sql2: str, witness: dict | None = None) -> str:
    """"differs" (random), "targeted", "bounded", "agree" or "error"; the database goes into ``witness``.

    The targeted suite and the z3 bounded check (as in ``sqliq_bench``, with the referenced columns unique
    too) only count when their database is valid here and the replay in SQLite differs.
    """

    result = differs(schema, sql1, sql2, witness=witness)
    if result != "agree" or solver.ordered(sql1) or solver.has_any_limit(sql1, sql2):
        return result  # the targeted and bounded searches compare bags, without the storage-order check
    for how, search in (("targeted", _targeted), ("bounded", _bounded)):
        try:
            database = search(schema, sql1, sql2)
        except Exception:  # a search that fails finds nothing
            database = None
        if database is not None and _valid(schema, database) and differs(schema, sql1, sql2, databases=[database]) == "differs":
            if witness is not None:
                witness.update(database)
            return how
    return "agree"


def _rows(schema: Schema, tables: dict) -> dict[str, list[list]]:
    """A found database ({table: (column names, rows)}) as rows in the schema's column order."""

    out = {}
    for name, (columns, rows) in tables.items():
        table = name.lower().split(".")[-1].strip("`\"")
        if table not in schema.tables:
            continue
        positions = [c.lower() for c in columns]
        out[table] = [[row[positions.index(c)] if c in positions else None for c in schema.tables[table]] for row in rows]
    return out


def _keys(schema: Schema, table: str) -> tuple[tuple[str, ...], ...]:
    return tuple(k for k in ((schema.keys.get(table) or ()), *((c,) for c in schema.unique.get(table, ()))) if k)


def _targeted(schema: Schema, sql1: str, sql2: str) -> dict | None:
    from kumosql.refute import find_targeted_difference
    from kumosql.result_equivalence import DataRules
    import sqliq_bench

    types = {t: {c: ("FLOAT64" if any(w in k for w in ("REAL", "FLOA", "DOUB", "DEC")) else sqliq_bench._BQ_TYPES[sqliq_bench._kind(k)]) for c, k in cols.items()} for t, cols in schema.tables.items()}
    rules = {t: DataRules(frozenset(schema.keys.get(t, ())), _keys(schema, t)) for t in schema.tables if _keys(schema, t)}
    found = find_targeted_difference(
        sql1, sql2, types, rules, foreign_keys=schema.foreign, engine="sqlite", dialect="sqlite", ordered=False, budget=20.0,
    )
    if found is None:
        return None
    return _rows(schema, {name: ([c for c, _ in table.columns], table.rows) for name, table in found.dataset.tables.items()})


def _bounded(schema: Schema, sql1: str, sql2: str) -> dict | None:
    from kumosql import bounded_equivalence as be
    import sqliq_bench

    tables = {}
    for table, columns in schema.tables.items():
        key = set(schema.keys.get(table, ()))
        cols = [
            be.BColumn(c, "FLOAT64" if any(w in k for w in ("REAL", "FLOA", "DOUB", "DEC")) else sqliq_bench._BQ_TYPES[sqliq_bench._kind(k)], c in key)
            for c, k in columns.items()
        ]
        tables[table] = be.BTable(table, cols, list(_keys(schema, table)))
    for child, child_column, parent, parent_column in schema.foreign:
        tables[child].foreign_keys.append(((child_column,), parent, (parent_column,)))
    bounded = be.BoundedSchema(tables)
    result = be.check_bounded(
        sql1, sql2, bounded, rows=sqliq_bench.BOUNDED_ROWS, dialect="sqlite", budget_s=30, timeout_ms=5000,
        replay=be.SQLiteReplay(bounded, sql1, sql2),
    )
    if result.status is not be.BoundedStatus.DIFFERENT or not result.counterexample:
        return None
    return _rows(schema, {name: ([c.name for c in tables[name.lower()].columns] if name.lower() in tables else [], rows) for name, rows in result.counterexample.items()})


def _valid(schema: Schema, database: dict[str, list[list]]) -> bool:
    """Listed keys are unique and not NULL, referenced columns unique, and every non-NULL reference is in its parent."""

    for table, key in schema.keys.items():
        position = list(schema.tables[table]).index(key[0])
        values = [row[position] for row in database.get(table, [])]
        if None in values or len(values) != len(set(values)):
            return False
    for table, columns in schema.unique.items():
        for column in columns:
            position = list(schema.tables[table]).index(column)
            values = [row[position] for row in database.get(table, []) if row[position] is not None]
            if len(values) != len(set(values)):
                return False
    for child, column, parent, parent_column in schema.foreign:
        parents = {row[list(schema.tables[parent]).index(parent_column)] for row in database.get(parent, [])}
        position = list(schema.tables[child]).index(column)
        if any(row[position] is not None and row[position] not in parents for row in database.get(child, [])):
            return False
    return True


def shrink(schema: Schema, sql1: str, sql2: str, database: dict[str, list[list]]) -> dict[str, list[list]]:
    """Drop rows one at a time while the database stays valid and the two results still differ."""

    current = {t: [list(r) for r in rows] for t, rows in database.items()}
    progress = True
    while progress:
        progress = False
        for table in list(current):
            for index in reversed(range(len(current[table]))):
                trial = {t: (rows[:index] + rows[index + 1:] if t == table else rows) for t, rows in current.items()}
                if _valid(schema, trial) and differs(schema, sql1, sql2, databases=[trial]) == "differs":
                    current, progress = trial, True
    return {t: rows for t, rows in current.items() if rows}


# -- eval A: the ESM false negatives ----------------------------------------------------


def _norm(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


@dataclass
class EsmRow:
    index: int  # data row of the file, from 0
    database: str
    gold: str
    pred: str
    reason: str
    note: str

    @property
    def id(self) -> str:
        return f"esm-{self.index:03d}"

    @property
    def pair(self) -> tuple[str, str, str]:
        """The rows repeat pairs (several models wrote the same prediction): a pair is scored once."""

        return (self.database, _norm(self.gold), _norm(self.pred))

    @property
    def held_out(self) -> bool:
        digest = hashlib.sha1("spider-esm\n{}\n{}\n{}".format(*self.pair).encode()).hexdigest()
        return int(digest, 16) % 5 == 0


def load_esm(path: Path | None = None) -> list[EsmRow]:
    text = (path or fetch("ESMFalseNegatives.tsv")).read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    assert rows[0][:4] == ["database name", "gold", "pred", "equivalent reason"], rows[0]
    return [EsmRow(i, r[0], r[1], r[2], r[3].strip(), r[4].strip() if len(r) > 4 else "") for i, r in enumerate(rows[1:])]


def _values(tree) -> list[exp.Literal]:
    """The value slots: every literal except a LIMIT or OFFSET count."""

    return [node for node in tree.find_all(exp.Literal) if not node.find_ancestor(exp.Limit, exp.Offset)]


def _value(node: exp.Literal):
    if node.is_string:
        return ("text", node.this)
    try:
        return ("number", float(node.this))
    except ValueError:
        return ("text", node.this)


def needs_values(gold: str, pred: str) -> bool:
    """The prediction holds a value the gold query does not: a placeholder the model wrote instead of a value."""

    g, p = solver._tree(gold), solver._tree(pred)
    if g is None or p is None:
        return False
    known = {_value(v) for v in _values(g)}
    return any(_value(v) not in known for v in _values(p))


def plug_values(gold: str, pred: str, limit: int = PLUG_LIMIT) -> list[str] | None:
    """Every way to fill the prediction's value slots with the gold's values (TestSuiteEval's plug-in), or None past ``limit``."""

    g, p = solver._tree(gold), solver._tree(pred)
    if g is None or p is None:
        return []
    choices = list({_value(v): v for v in _values(g)}.values())
    slots = len(_values(p))
    if not choices or not slots:
        return []
    if len(choices) ** slots > limit:
        return None
    out = []
    for combination in itertools.product(choices, repeat=slots):
        tree = p.copy()
        for slot, value in zip(_values(tree), combination):
            slot.replace(value.copy())
        sql = tree.sql(dialect="sqlite")
        if sql not in out:
            out.append(sql)
    return out


def strip_distinct(sql: str) -> str | None:
    """The query with every DISTINCT removed (TestSuiteEval's default), or None when it has none."""

    tree = solver._tree(sql)
    if tree is None:
        return None
    changed = False
    for select in list(tree.find_all(exp.Select)):
        if select.args.get("distinct") is not None:
            select.set("distinct", None)
            changed = True
    for node in list(tree.find_all(exp.Distinct)):
        if len(node.expressions) == 1:
            node.replace(node.expressions[0].copy())
            changed = True
    return tree.sql(dialect="sqlite") if changed else None


def respace(sql: str) -> str:
    """TestSuiteEval's own clean-up before it runs a prediction: ``> =`` is read as ``>=`` (and ``< =``, ``! =``)."""

    return sql.replace("> =", ">=").replace("< =", "<=").replace("! =", "!=")


def _runs(schema: Schema, sql: str) -> bool:
    return differs(schema, sql, sql, trials=1) != "error"


def decide_pair(task: tuple[str, str, str, Schema]) -> dict:
    """One distinct (database, gold, prediction) pair."""

    database, gold, pred, schema = task
    started = time.time()
    sql1, sql2 = solver.adapt(respace(gold), schema.tables), solver.adapt(respace(pred), schema.tables)
    plugged = needs_values(sql1, sql2)
    candidates = plug_values(sql1, sql2) if plugged else [sql2]
    out = {
        "database": database, "plugged": plugged, "quoted": (sql1, sql2) != (gold, pred), "outcome": "unknown", "how": "",
        "candidates": None if candidates is None else len(candidates), "cause": "", "wrong": False, "witness": None, "detail": "",
    }
    if not _runs(schema, sql1):
        out.update(outcome="unsupported", detail="SQLite rejects the gold query on the Spider schema")
    elif candidates is None:
        out["detail"] = f"more than {PLUG_LIMIT} ways to plug the gold's values in"
    elif not candidates:
        out["detail"] = "no value slot to plug the gold's values into"
    else:
        candidates = [c for c in candidates if _runs(schema, c)]
        if not candidates:
            out.update(outcome="unsupported", detail="SQLite rejects the prediction on the Spider schema")
        else:
            out.update(_decide(schema, sql1, candidates, sql2, plugged))
    out["seconds"] = round(time.time() - started, 2)
    return out


def _decide(schema: Schema, gold: str, candidates: list[str], published: str, plugged: bool) -> dict:
    for candidate in candidates:
        if prove(gold, candidate, schema):
            # A proof is re-checked on random databases: a difference there would be a false proof
            check = differs(schema, gold, candidate)
            return {"outcome": "proven", "how": "prover", "prediction": candidate, "wrong": check == "differs"}
    refutations = []
    for candidate in candidates:
        witness: dict = {}
        how = refute(schema, gold, candidate, witness)
        if how not in ("differs", "targeted", "bounded"):
            return {"outcome": "unknown", "prediction": candidate}
        refutations.append((candidate, how, witness))
    # Shown: the assignment that uses the gold's values as often as the gold does, when there is one
    gold_values = Counter(map(_value, _values(solver._tree(gold))))
    natural = [r for r in refutations if Counter(map(_value, _values(solver._tree(r[0])))) == gold_values]
    candidate, how, witness = (natural or refutations)[0]
    result = {"outcome": "refuted", "how": how, "prediction": candidate, "cause": cause(schema, gold, refutations, published, plugged)}
    if witness:
        result["witness"] = shrink(schema, gold, candidate, witness)
    return result


CAUSE_TRIALS = 300
CAUSE_VARIANTS = 48
CONVENTIONS = ("columns", "values", "distinct", "null")


def null_free_databases(schema: Schema, sql1: str, sql2: str, count: int = CAUSE_TRIALS, seed: int = 11) -> list[dict]:
    """Random schema-valid databases without a single NULL (rows that would need one are left out)."""

    import random

    import sqliq_bench

    pair = sqliq_bench.Pair(0, sql1, sql2, schema.tables, schema.keys, "no", schema.foreign)
    domains = sqliq_bench.make_domains(pair, *sqliq_bench.mentioned_values(sql1, sql2))
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        made: dict[str, list[list]] = {}
        for table in sqliq_bench.table_order(pair):
            rows = sqliq_bench.random_rows(pair, table, domains, rng, made, null_rate=0.0, unique=schema.unique.get(table, ()))
            made[table] = [r for r in rows if None not in r]  # a reference into an empty parent table is NULL
        out.append(made)
    return out


def without_nulls(schema: Schema, database: dict[str, list[list]]) -> dict[str, list[list]] | None:
    """The database with every NULL replaced, if the result is valid.

    A NULL becomes a value no other cell holds; a NULL reference points at a row of its parent table (a new
    row of fresh values when the parent is empty).
    """

    fresh = itertools.count(1)
    parents = {(child, column): (parent, parent_column) for child, column, parent, parent_column in schema.foreign}
    out = {t: [list(r) for r in database.get(t, [])] for t in schema.tables}

    def new_value(table: str, column: str):
        return 90_000 + next(fresh) if schema.tables[table][column] == "INTEGER" else f"fresh{next(fresh)}"

    def referenced(table: str, column: str, depth: int):
        position = list(schema.tables[table]).index(column)
        for row in out[table]:
            if row[position] is not None:
                return row[position]
        if depth > 4:
            return None
        row = [referenced(*parents[(table, c)], depth + 1) if (table, c) in parents else new_value(table, c) for c in schema.tables[table]]
        out[table].append(row)
        return row[position]

    for table, rows in out.items():
        columns = list(schema.tables[table])
        for row in rows:
            for position, column in enumerate(columns):
                if row[position] is None:
                    row[position] = referenced(*parents[(table, column)], 0) if (table, column) in parents else new_value(table, column)
    out = {t: rows for t, rows in out.items() if rows}
    if any(None in row for rows in out.values() for row in rows):
        return None
    return out if _valid(schema, out) else None


def _permutations(sql: str) -> list[str]:
    """The query with its output columns in every order (TestSuiteEval compares results up to column order)."""

    tree = solver._tree(sql)
    top = solver._top(tree)
    if not isinstance(top, exp.Select) or not 1 < len(top.expressions) <= 4 or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in top.expressions):
        return [sql]
    out = []
    for order in itertools.permutations(top.expressions):
        copy = tree.copy()
        solver._top(copy).set("expressions", [e.copy() for e in order])
        out.append(copy.sql(dialect="sqlite"))
    return out


def cause(schema: Schema, gold: str, refutations: list[tuple[str, str, dict]], published: str, plugged: bool) -> str:
    """Why a pair labelled equivalent is refuted: the fewest of TestSuiteEval's conventions under which it is not.

    ``columns``: results compared up to the order of columns (TestSuiteEval's comparison); ``values``: the
    gold's values plugged into the prediction's value slots; ``distinct``: DISTINCT removed from both
    queries (its metric's default); ``null``: only databases without NULLs, although the schema allows them
    (random NULL-free databases, plus each counterexample with its NULLs replaced by fresh values, so a
    difference that has nothing to do with a NULL is not put down to one). "other" when none of these makes
    the two agree on the databases tried: a label error even under TestSuiteEval's own conventions.
    """

    candidates = [candidate for candidate, _, _ in refutations]
    published_tree = solver._tree(published)
    usable = [
        c for c in CONVENTIONS
        if not (c == "values" and (plugged or published_tree is None or not _values(published_tree)))
        and not (c == "distinct" and not (strip_distinct(gold) or any(strip_distinct(x) for x in candidates)))
        and not (c == "columns" and len(_permutations(candidates[0])) == 1)
    ]
    for size in range(1, len(usable) + 1):
        for chosen in itertools.combinations(usable, size):
            left, rights = gold, list(candidates)
            if "values" in chosen:
                rights = rights + (plug_values(gold, published, limit=CAUSE_VARIANTS) or [])
            if "columns" in chosen:
                rights = [p for r in rights for p in _permutations(r)]
            if "distinct" in chosen:
                left = strip_distinct(left) or left
                rights = [strip_distinct(r) or r for r in rights]
            databases = None
            if "null" in chosen:
                databases = null_free_databases(schema, gold, candidates[0])
                databases += [d for d in (without_nulls(schema, w) for _, _, w in refutations if w) if d is not None]
            for right in list(dict.fromkeys(rights))[:CAUSE_VARIANTS]:
                if differs(schema, left, right, trials=CAUSE_TRIALS, databases=databases) == "agree":
                    return "+".join(chosen)
    return "other"


def run_esm(rows: list[EsmRow], schemas: dict[str, Schema], jobs: int = 1) -> list[dict]:
    """One result per row; each distinct pair is decided once."""

    pairs = list(dict.fromkeys(row.pair for row in rows))
    tasks = [(database, gold, pred, schemas[database]) for database, gold, pred in pairs]
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            decided = list(pool.map(decide_pair, tasks, chunksize=2))
    else:
        decided = [decide_pair(t) for t in tasks]
    by_pair = dict(zip(pairs, decided))
    return [{"id": row.id, "held_out": row.held_out, "reason": row.reason, **by_pair[row.pair]} for row in rows]


def summarize_esm(results: list[dict]) -> dict:
    out = {}
    for label, group in (("published", [r for r in results if not r["plugged"]]), ("plugged", [r for r in results if r["plugged"]])):
        counts = Counter(r["outcome"] for r in group)
        causes = Counter(r["cause"] for r in group if r["outcome"] == "refuted")
        out[label] = {
            "rows": len(group), **{k: counts.get(k, 0) for k in ("proven", "refuted", "unknown", "unsupported")},
            "wrong": sum(r["wrong"] for r in group), "causes": dict(causes),
        }
    return out


# -- eval B: Spider dev gold queries as rewrite inputs ----------------------------------


@dataclass
class GoldQuery:
    index: int
    database: str
    sql: str
    questions: int = 1  # dev examples that share this query

    @property
    def id(self) -> str:
        return f"dev-{self.index:03d}"

    @property
    def held_out(self) -> bool:
        return database_held_out(self.database)


def database_held_out(database: str) -> bool:
    return int(hashlib.sha1(f"spider-dev\n{database}".encode()).hexdigest(), 16) % 5 == 0


def load_dev(path: Path | None = None) -> list[GoldQuery]:
    queries: dict[tuple[str, str], GoldQuery] = {}
    for example in json.loads((path or fetch("dev.json")).read_text(encoding="utf-8")):
        key = (example["db_id"], _norm(example["query"]))
        if key in queries:
            queries[key].questions += 1
        else:
            queries[key] = GoldQuery(len(queries), example["db_id"], example["query"])
    return list(queries.values())


def to_bigquery(sqlite_sql: str) -> str:
    """SQLite to BigQuery with sqlglot; identifiers are lower-cased (SQLite ignores their case)."""

    tree = sqlglot.parse_one(sqlite_sql, read="sqlite")
    for identifier in tree.find_all(exp.Identifier):
        identifier.set("this", identifier.this.lower())
    return tree.sql(dialect="bigquery")


def to_sqlite(bigquery_sql: str) -> str:
    return sqlglot.parse_one(bigquery_sql, read="bigquery").sql(dialect="sqlite")


def transformations() -> list[str]:
    from kumosql import engine

    return [*engine.available_rules(), "pipeline", "optimize"]


def _apply(name: str, sql: str, schema: Schema) -> tuple[str | None, bool, str]:
    """(output, trusted, label): trusted outputs are the ones KumoSQL would apply."""

    from kumosql import query_optimizer, rewrite

    if name == "optimize":
        outcome = query_optimizer.optimize(
            sql, schema.catalog(), dialect="bigquery", budget_s=OPTIMIZER_BUDGET_S, deletion_budget_s=OPTIMIZER_BUDGET_S / 2,
        )
        return outcome.sql, outcome.sql is not None, "proven" if outcome.sql else "no rewrite"
    result = rewrite.apply_rules(rewrite.canonical_rule_order(), sql) if name == "pipeline" else rewrite.apply_rule(name, sql)
    return result.sql, result.success, result.verification.status.value


def rewrite_query(task: tuple[GoldQuery, Schema]) -> dict:
    query, schema = task
    started = time.time()
    out: dict = {"id": query.id, "database": query.database, "held_out": query.held_out, "questions": query.questions, "rewrites": {}}
    gold = solver.adapt(query.sql, schema.tables)
    try:
        bigquery = to_bigquery(gold)
        original = to_sqlite(bigquery)
    except sqlglot.errors.SqlglotError as error:
        out.update(translation="unsupported", detail=str(error)[:120])
        return out
    if not _runs(schema, gold):
        out.update(translation="unsupported", detail="SQLite rejects the gold query on the Spider schema")
        return out
    # The round trip SQLite -> BigQuery -> SQLite is sqlglot's, checked on fewer databases; it is not a KumoSQL rewrite
    out["translation"] = {"agree": "same", "differs": "differs", "error": "error"}[
        differs(schema, gold, original, trials=200)
    ]
    for name in transformations():
        began = time.time()
        try:
            new, trusted, label = _apply(name, bigquery, schema)
        except Exception as error:  # noqa: BLE001 - a crash is reported, never counted as a rewrite
            out["rewrites"][name] = {"changed": False, "crash": f"{type(error).__name__}: {str(error)[:100]}"}
            continue
        record: dict = {"changed": bool(new) and new.strip() != bigquery.strip(), "trusted": trusted, "label": label}
        if record["changed"]:
            try:
                rewritten = to_sqlite(new)
            except sqlglot.errors.SqlglotError:
                rewritten = None
            if rewritten is None:
                record["check"] = "error"
            elif solver._tree(rewritten) is not None and solver.same_query(original, rewritten):
                record["check"] = "same text"  # layout only: the same query once re-rendered
            else:
                witness: dict = {}
                how = refute(schema, original, rewritten, witness)
                record["check"] = {"agree": "agree", "error": "error"}.get(how, "differs")
                if record["check"] == "differs":
                    record.update(sql=new, how=how, witness=shrink(schema, original, rewritten, witness) if witness else None)
            record["wrong"] = trusted and record["check"] == "differs"
        record["seconds"] = round(time.time() - began, 2)
        out["rewrites"][name] = record
    out["seconds"] = round(time.time() - started, 2)
    return out


def run_rewrites(queries: list[GoldQuery], schemas: dict[str, Schema], jobs: int = 1) -> list[dict]:
    tasks = [(q, schemas[q.database]) for q in queries]
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            return list(pool.map(rewrite_query, tasks, chunksize=1))
    return [rewrite_query(t) for t in tasks]


def summarize_rewrites(results: list[dict]) -> dict:
    out: dict = {"queries": len(results), "translated": sum(isinstance(r.get("translation"), str) and r["translation"] != "unsupported" for r in results)}
    out["translation"] = dict(Counter(r.get("translation") for r in results))
    rules: dict = {}
    for result in results:
        for name, record in result["rewrites"].items():
            row = rules.setdefault(name, Counter())
            row["crash"] += "crash" in record
            if not record.get("changed"):
                continue
            row["changed"] += 1
            row["trusted"] += record["trusted"]
            row[record["check"]] += 1
            row["wrong"] += record["wrong"]
            row["caught"] += (not record["trusted"]) and record["check"] == "differs"
    out["rules"] = {name: dict(counts) for name, counts in rules.items()}
    semantic = [rec for r in results for rec in r["rewrites"].values() if rec.get("changed") and rec["check"] != "same text"]
    out["semantic_changes"] = len(semantic)
    out["semantic_trusted"] = sum(rec["trusted"] for rec in semantic)
    out["semantic_trusted_checked"] = sum(rec["trusted"] and rec["check"] == "agree" for rec in semantic)
    out["queries_with_semantic_proof"] = sum(
        any(rec.get("changed") and rec["trusted"] and rec["check"] != "same text" for rec in r["rewrites"].values()) for r in results
    )
    out["wrong"] = sum(rec.get("wrong", False) for r in results for rec in r["rewrites"].values())
    return out


# -- results files ------------------------------------------------------------------------


def results_rows(esm: list[dict] | None, rewrites: list[dict] | None) -> dict[str, dict]:
    from bench_common import today

    rows = {}
    if esm is not None:
        summary = summarize_esm(esm)
        published, plugged = summary["published"], summary["plugged"]
        held = summarize_esm([r for r in esm if r["held_out"]])
        coverage = Counter(r["outcome"] for r in esm)
        rows["spider-esm-equivalent"] = {
            "suite": "TestSuiteEval ESM false negatives (Spider)",
            "order": 35,
            "size": len(esm),
            "score": (
                f"{published['proven']}/{published['rows']} proved as published, {plugged['proven']}/{plugged['rows']} with the gold's values plugged in; "
                f"{published['refuted'] + plugged['refuted']} refuted (upstream label failures), {published['wrong'] + plugged['wrong']} wrong"
            ),
            "metric": "Rows of ESMFalseNegatives.tsv the authors judged equivalent: proved by the algebraic prover; refuted means SQLite returns different results on a database that respects Spider's listed keys and foreign keys.",
            "evidence": "proof",
            "correctness": "Every proof is re-run on 1,000 random schema-valid databases (a difference would count as wrong). Refutations are replayed SQLite runs on schema-valid databases, shrunk row by row and listed in the docs with their cause; they count against the labels, not as KumoSQL answers.",
            "coverage": {k: coverage[k] for k in ("proven", "refuted", "unknown", "unsupported") if coverage[k]},
            "held_out": (
                f"{held['published']['proven']}/{held['published']['rows']} proved as published, "
                f"{held['plugged']['proven']}/{held['plugged']['rows']} plugged, {held['published']['wrong'] + held['plugged']['wrong']} wrong"
            ),
            "docs": "docs/evals/spider.md",
            "command": "python tools/spider_bench.py esm --write-results",
            "date": today(),
            "caveats": (
                "Downloaded at run time (TestSuiteEval has no licence). Rows repeat pairs: the 558 rows hold fewer distinct pairs, each decided once. "
                "Predictions with value placeholders are adapted with TestSuiteEval's own plug-in of the gold's values (scored apart). "
                "The prover gets a listed key only when it is not a foreign-key column of its own table (tables.json lists the first column of a composite key). "
                "Spider's databases are blocked here, so refutations use databases KumoSQL builds with tables.json's column types."
            ),
        }
    if rewrites is not None:
        summary = summarize_rewrites(rewrites)
        held = summarize_rewrites([r for r in rewrites if r["held_out"]])
        rows["spider-dev-rewrites"] = {
            "suite": "Spider dev gold queries as rewrite inputs",
            "order": 36,
            "size": summary["queries"],
            "score": (
                f"{summary['queries_with_semantic_proof']}/{summary['queries']} queries rewritten beyond layout with a proof, "
                f"{summary['semantic_trusted']} trusted rewrites checked on generated databases, {summary['wrong']} wrong"
            ),
            "metric": "Each distinct Spider dev gold query, translated to BigQuery, through every KumoSQL rewrite rule, the cleanup pipeline and the proof-gated optimizer; a change that is not layout only is run against the input on schema-valid SQLite databases.",
            "evidence": "proof",
            "correctness": "Wrong is a rewrite KumoSQL trusts (proven, or returned by the optimizer) whose result changes on a database that respects the listed keys and foreign keys (1,000 random databases, then the targeted and bounded searches for unordered queries).",
            "coverage": {"proven": summary["queries_with_semantic_proof"], "unknown": summary["translated"] - summary["queries_with_semantic_proof"], "unsupported": summary["queries"] - summary["translated"]},
            "held_out": f"{held['queries_with_semantic_proof']}/{held['queries']} queries rewritten with a proof, {held['wrong']} wrong",
            "docs": "docs/evals/spider.md",
            "command": "python tools/spider_bench.py rewrites --write-results",
            "date": today(),
            "caveats": (
                "Spider's data are CC BY-SA 4.0 and downloaded at run time. Spider's databases are blocked here, so checks run on databases KumoSQL builds; "
                "most rules have nothing to do on Spider's single-statement queries (no CTEs), so the count of layout-only changes is reported apart. "
                "The optimizer gets a listed key only when it is not a foreign-key column of its own table."
            ),
        }
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("eval", choices=("esm", "rewrites", "all"))
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all", help="held-out cases are for final scoring only")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--show", default="", help="esm: outcomes to list (proven, refuted, unknown, unsupported); rewrites: 'differs' or 'semantic'")
    parser.add_argument("--json", help="write every result to this file")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/spider-*.json (needs --split all)")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    if args.write_results and args.split != "all":
        parser.error("--write-results needs --split all")
    schemas = load_schemas()
    keep = (lambda held: True) if args.split == "all" else (lambda held: held == (args.split == "held-out"))
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    esm = rewrites = None
    dump: dict = {}
    if args.eval in ("esm", "all"):
        started = time.time()
        rows = [r for r in load_esm() if keep(r.held_out)]
        esm = run_esm(rows, schemas, args.jobs)
        for label, counts in summarize_esm(esm).items():
            print(f"esm {label:10} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        print(f"{len(rows)} rows, {len({r.pair for r in rows})} distinct pairs in {time.time() - started:.0f}s")
        shown = set()
        for row, result in zip(rows, esm):
            if (result["outcome"] in show or result["wrong"]) and row.pair not in shown:
                shown.add(row.pair)
                print(f"\n{row.id} {row.database} {result['outcome']} {result['how']} {result['cause']}{' WRONG' if result['wrong'] else ''}")
                print(f"  gold: {row.gold}\n  pred: {result.get('prediction') or row.pred}\n  reason: {row.reason}")
                if result.get("witness") is not None:
                    print(f"  database: {json.dumps(result['witness'])}")
        dump["esm"] = esm
    if args.eval in ("rewrites", "all"):
        started = time.time()
        queries = [q for q in load_dev() if keep(q.held_out)]
        rewrites = run_rewrites(queries, schemas, args.jobs)
        summary = summarize_rewrites(rewrites)
        for name, counts in summary.pop("rules").items():
            print(f"{name:28} " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if v))
        print(json.dumps(summary))
        print(f"{len(queries)} queries in {time.time() - started:.0f}s")
        for result in rewrites:
            for name, record in result["rewrites"].items():
                semantic = record.get("changed") and record["check"] != "same text"
                if record.get("wrong") or (record.get("check") == "differs" and "differs" in show) or (semantic and "semantic" in show):
                    print(f"\n{result['id']} {result['database']} {name} {record['label']} {record['check']}{' WRONG' if record.get('wrong') else ''}")
                    if record.get("witness") is not None:
                        print(f"  database: {json.dumps(record['witness'])}")
        dump["rewrites"] = rewrites
    if args.json:
        Path(args.json).write_text(json.dumps(dump, indent=1), encoding="utf-8")
    if args.write_results:
        if args.split != "all":
            parser.error("--write-results needs --split all")
        rows_out = results_rows(esm, rewrites)
        for name, row in rows_out.items():
            write_results(name, row, scoreboard=False)
        write_results(name, row)
    wrong = any(r["wrong"] for r in esm or []) or any(rec.get("wrong") for r in rewrites or [] for rec in r["rewrites"].values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
