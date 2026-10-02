"""Decide equivalence of the Singh & Bedathur LeetCode query pairs with no LLM.

The pairs come from https://github.com/rajatb115/LLMs-for-SQL-Equivalence-Checking
(Rajat Singh and Srikanta Bedathur, "Can the Rookies Cut the Tough Cookie?
Exploring the Use of LLMs for SQL Equivalence Checking", 2,800 labelled LeetCode
pairs in ``finetune/dataset``). The repository has no licence file, so the data is
downloaded on demand into a cache folder and never stored in this repository. Their
SQLEquiQuest and Spider+DIN sets are only shared through a form; their Calcite
set is the one SQLSolver already uses (``tools/sqlsolver_bench.py``).

Each pair gets one verdict, decided from the SQL and the column lists alone:

* ``equivalent``: the algebraic prover proved it. Every proof is re-run on random
  DuckDB databases (a second, larger search); a database that separates the two
  queries makes the verdict ``WRONG``.
* ``different``: a database was found on which DuckDB returns different result bags
  for the two queries. The database is the counterexample; nothing is claimed
  without one.
* ``unknown``: neither. An unknown is always better than a wrong verdict.

The published labels are only used to report agreement afterwards; nothing here
reads them while deciding. The pairs carry only column names (no types, keys or NOT
NULL facts), so verdicts are about every database with those columns. Queries are
compared as bags (ORDER BY without LIMIT is ignored, as LeetCode does) and run in
DuckDB with MySQL's NULL ordering and case-insensitive string comparison. No pair gets
``different`` when DuckDB and MySQL could disagree on it: a LIMIT whose ORDER BY
leaves ties, ``LOWER()`` (LIKE ignores case only in MySQL), or a non-deterministic
function. Columns that are summed, averaged or rounded may hold fractions.

Every pair of the source files is also in VeriEQL's LeetCode benchmark, which keeps
the keys and foreign keys that these files drop.

Outcomes: ``proven`` (equivalent), ``refuted`` (different), ``unknown``,
``unsupported`` (the prover cannot read a query), ``timeout``, ``error`` (a crash) and
``wrong``. One pair in five (by a hash of its text) is held out of development runs.

    python tools/singh_bedathur_bench.py                   # all 2,800 pairs
    python tools/singh_bedathur_bench.py --split dev       # the pairs used while developing
    python tools/singh_bedathur_bench.py --sample 200      # a fixed random sample
    python tools/singh_bedathur_bench.py --show-unknown --show-disagreements
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.request

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Pinned to one commit of the source repository; the files are checked against these digests.
COMMIT = "930569d894c950c0b64f2150214394b52a40e11b"
BASE = f"https://raw.githubusercontent.com/rajatb115/LLMs-for-SQL-Equivalence-Checking/{COMMIT}/finetune/dataset/"
FILES = {
    "leetcode_eval_800.json": "7d4dbf617aaef05e4bee5331e84d4fd751fca4a422d022f7ccbb929f293e6dec",
    "leetcode_train_2000.json": "910e30f40bfaabd90519a67d9473ba90f41c3310370ba72fe0a88c2c2d6d963a",
}
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "singh-bedathur"


@dataclass
class Pair:
    index: int
    left: str
    right: str
    tables: dict[str, list[str]]
    gold: str  # "equivalent" or "different"; only read when reporting

    @property
    def key(self) -> str:
        """A stable name for the pair (the files have no ids)."""

        return hashlib.sha1(f"{self.left}\n{self.right}".encode()).hexdigest()[:12]

    @property
    def held_out(self) -> bool:
        """One pair in five, by key, is kept out of development runs and scored only at the end."""

        return int(self.key, 16) % 5 == 0


@dataclass
class Verdict:
    kind: str  # equivalent | different | unknown | wrong
    detail: str = ""
    witness: dict | None = None
    outcome: str = ""  # proven | refuted | unknown | unsupported | timeout | error | wrong


def fetch() -> list[Path]:
    """The two dataset files, downloaded once into the cache folder."""

    CACHE.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, digest in FILES.items():
        path = CACHE / name
        if not path.exists():
            with urllib.request.urlopen(BASE + name, timeout=60) as response:
                path.write_bytes(response.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise OSError(f"{path} does not match the pinned version; delete it to download again")
        paths.append(path)
    return paths


def load_pairs(paths: list[Path] | None = None) -> list[Pair]:
    pairs = []
    for path in paths or fetch():
        for row in json.loads(path.read_text(encoding="utf-8")):
            text = row["question"]
            match = re.search(r"\[SQL_1\] (.*)\n\[SQL_2\] (.*)\n\n### Answer", text, re.S)
            tables = {
                name.lower(): [c.strip().lower() for c in cols.split(",") if c.strip() and c.strip() != "*"]
                for name, cols in re.findall(r"Table (\w+), Columns = \[ (.*?) \]", text)
            }
            gold = "equivalent" if row["answer"].strip() == "Equivalent" else "different"
            pairs.append(Pair(len(pairs), match.group(1).strip(), match.group(2).strip(), tables, gold))
    return pairs


# --- column types -----------------------------------------------------------------

DATE_FUNCTIONS = (exp.DateDiff, exp.DateSub, exp.DateAdd, exp.Year, exp.Month, exp.Day, exp.Extract, exp.TimeToStr, exp.DateTrunc)
STRING_FUNCTIONS = (exp.Upper, exp.Lower, exp.Concat, exp.Like, exp.ILike, exp.RegexpLike, exp.Substring, exp.Length, exp.Trim)
DATE_LITERAL = re.compile(r"^\d{4}-\d{2}-\d{2}")


def column_kinds(trees: list[exp.Expression], tables: dict[str, list[str]]) -> dict[tuple[str, str], str]:
    """VARCHAR, DATE or BIGINT per (table, column), from how the queries use each column."""

    kinds: dict[tuple[str, str], str] = {}

    def owners(column: exp.Column, scope_tables: dict[str, str]) -> list[str]:
        if column.table:
            real = scope_tables.get(column.table.lower())
            return [real] if real else []
        return [t for t in set(scope_tables.values()) if column.name.lower() in tables.get(t, [])]

    for tree in trees:
        alias_maps = {}
        for select in tree.find_all(exp.Select):
            mapping = {}
            for source in select.find_all(exp.Table):
                real = source.name.lower()
                mapping[(source.alias or source.name).lower()] = real
                mapping[real] = real
            alias_maps[id(select)] = mapping
        for column in tree.find_all(exp.Column):
            select = column.find_ancestor(exp.Select)
            scope = {}
            while select is not None:
                for k, v in alias_maps.get(id(select), {}).items():
                    scope.setdefault(k, v)
                select = select.find_ancestor(exp.Select)
            kind = None
            parent = column.parent
            while isinstance(parent, (exp.Paren, exp.Cast)) and parent.parent is not None:
                parent = parent.parent
            if isinstance(parent, STRING_FUNCTIONS):
                kind = "VARCHAR"
            elif isinstance(parent, DATE_FUNCTIONS):
                kind = "DATE"
            elif isinstance(parent, (exp.Binary, exp.Between, exp.In)):
                others = [parent.args.get("this")] + [parent.args.get(k) for k in ("expression", "low", "high")] + list(parent.args.get("expressions") or [])
                for other in others:
                    if isinstance(other, exp.Literal) and other.is_string:
                        kind = "DATE" if DATE_LITERAL.match(other.name) else "VARCHAR"
                    elif isinstance(other, exp.Cast) and other.to.is_type("date", "datetime", "timestamp"):
                        kind = "DATE"
            for owner in owners(column, scope):
                key = (owner, column.name.lower())
                if kind and kinds.get(key) in (None, kind):
                    kinds[key] = kind
        # Summed, averaged or rounded values may be fractional: the schema does not say they are whole.
        for key_tree in tree.find_all(exp.Sum, exp.Avg, exp.Round):
            for column in key_tree.find_all(exp.Column):
                for owner in [t for t in tables if column.name.lower() in tables[t]]:
                    kinds.setdefault((owner, column.name.lower()), "DECIMAL(18,3)")
    for table, columns in tables.items():
        for name in columns:
            if (table, name) not in kinds and re.search(r"date|(^|_)dt($|_)|time", name):
                kinds[(table, name)] = "DATE"
    return kinds


def literal_domains(trees: list[exp.Expression]) -> tuple[list, list[str], list[str]]:
    """Values to draw from: a few small ones plus every literal in the queries and its neighbours."""

    numbers, strings, dates = {0, 1, 2, 3}, {"A", "B"}, set()
    for tree in trees:
        for literal in tree.find_all(exp.Literal):
            if literal.is_string:
                (dates if DATE_LITERAL.match(literal.name) else strings).add(literal.name)
            else:
                try:
                    value = float(literal.name)
                except ValueError:
                    continue
                if value.is_integer():
                    value = int(value)
                    numbers.update({value - 1, value, value + 1})
                else:
                    numbers.add(value)
    # MySQL compares strings without case; keep one spelling of each (DuckDB runs with a no-case collation).
    strings = {s.upper(): s for s in sorted(strings, reverse=True)}.values()
    dates = {d[:10] for d in dates}
    if not dates:
        dates = {"2020-01-01", "2020-01-02", "2020-02-01"}
    return sorted(numbers), sorted(strings), sorted(dates)


# --- execution --------------------------------------------------------------------


def translate(sql: str) -> str:
    return sqlglot.transpile(sql, read="mysql", write="duckdb")[0]


def _fully_ordered(select: exp.Expression) -> bool:
    """A LIMIT whose ORDER BY sorts on every output column keeps the same rows however ties are broken."""

    order = select.args.get("order")
    if not isinstance(select, exp.Select) or order is None or any(isinstance(e, exp.Star) for e in select.expressions):
        return False
    keys = set()
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and not key.is_string and key.name.isdigit():
            keys.add(("position", int(key.name) - 1))
        else:
            keys.add(("text", key.sql().lower()))
            if isinstance(key, exp.Column) and not key.table:
                keys.add(("name", key.name.lower()))
    for position, projection in enumerate(select.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if not ({("position", position), ("text", inner.sql().lower()), ("name", projection.alias_or_name.lower())} & keys):
            return False
    return True


def unsafe_to_refute(*trees: exp.Expression) -> bool:
    """Whether a DuckDB difference might not be a MySQL difference.

    A LIMIT that may cut between tied rows, and RAND() and friends, change per run.
    """

    for tree in trees:
        if any(tree.find_all(exp.Fetch, exp.Rand, exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime)):
            return True
        for limit in tree.find_all(exp.Limit):
            if not _fully_ordered(limit.parent):
                return True
        if any(tree.find_all(exp.Lower)):
            return True  # LIKE ignores case in MySQL but not in DuckDB; lower-cased text could tell them apart
        for func in tree.find_all(exp.Anonymous):
            if func.name.upper() in {"RAND", "RANDOM", "NOW", "UUID", "SYSDATE", "CURDATE", "CURTIME"}:
                return True
    return False


def normalise(rows: list[tuple]) -> Counter:
    def cell(value):
        if isinstance(value, float):
            return round(value, 6)
        return value

    return Counter(tuple(cell(v) for v in row) for row in rows)


def new_database(tables: dict[str, list[str]], kinds: dict[tuple[str, str], str]):
    import duckdb

    db = duckdb.connect(":memory:")
    db.execute("SET default_null_order = 'nulls_first_on_asc_last_on_desc'")  # MySQL sorts NULL as the smallest value
    db.execute("SET default_collation = 'nocase'")  # and compares strings without case
    for table, columns in tables.items():
        definition = ", ".join(f'"{c}" {kinds.get((table, c), "BIGINT")}' for c in columns)
        db.execute(f'CREATE TABLE "{table}" ({definition})')
    return db


def _random_tables(pair: Pair, used: list[str], kinds, domains, rng: random.Random, sizes: list[int], skewed: bool) -> dict[str, list[list]]:
    """Random rows; ``skewed`` databases draw some columns from one or two values and vary the NULL rate."""

    numbers, strings, dates = domains
    whole = [n for n in numbers if isinstance(n, int)]
    fractions = sorted(set(numbers) | {0.5, 1.25, 2.75})
    data = {}
    for table in used:
        rows = []
        pools = {}
        for column in pair.tables[table]:
            kind = kinds.get((table, column), "BIGINT")
            pool = {"VARCHAR": strings, "DATE": dates}.get(kind, fractions if kind.startswith("DECIMAL") else whole)
            if skewed and rng.random() < 0.5:  # few distinct values: big groups, duplicates and ties
                pool = rng.sample(pool, min(len(pool), rng.choice([1, 2])))
            pools[column] = (pool, rng.choice([0.0, 0.2, 0.4]) if skewed else 0.2)
        for _ in range(rng.choice(sizes)):
            row = []
            for column in pair.tables[table]:
                pool, nulls = pools[column]
                row.append(None if rng.random() < nulls else rng.choice(pool))
            rows.append(row)
        data[table] = rows
    return data


def _row_counts(trees) -> list[int]:
    """Small tables, plus tables big enough to pass a ``COUNT(..) >= n`` test when the queries have one."""

    sizes = [0, 1, 2, 3, 3, 4, 5]
    for tree in trees:
        for compare in tree.find_all(exp.GT, exp.GTE, exp.EQ, exp.LT, exp.LTE):
            if compare.find(exp.Count):
                for literal in compare.find_all(exp.Literal):
                    if not literal.is_string and literal.name.isdigit() and 2 <= int(literal.name) <= 12:
                        sizes += [int(literal.name), int(literal.name) + 1, int(literal.name) + 2]
    return sizes


def search_difference(pair: Pair, trees, trials: int, seed: int, extra: list[dict] = ()):
    """A database on which the two queries return different bags, ``None`` if none found, ``False`` if DuckDB rejects them.

    ``extra`` holds candidate databases to try first (a counterexample proposed by the prover);
    each is checked by running both queries, like the random ones.
    """

    import duckdb

    from kumosql.duckdb_load import insert_rows

    kinds = column_kinds(trees, pair.tables)
    domains = literal_domains(trees)
    sizes = _row_counts(trees)
    try:
        left_sql, right_sql = translate(pair.left), translate(pair.right)
    except sqlglot.errors.SqlglotError:
        return False
    db = new_database(pair.tables, kinds)
    rng = random.Random(seed)
    used = [t for t in pair.tables if re.search(rf"\b{re.escape(t)}\b", pair.left + " " + pair.right, re.I)]
    candidates = [c for c in extra]
    failures = 0
    for attempt in range(trials + len(candidates)):
        data = candidates[attempt] if attempt < len(candidates) else _random_tables(pair, used, kinds, domains, rng, sizes, skewed=attempt % 2 == 1)
        try:
            for table in used:
                db.execute(f'DELETE FROM "{table}"')
                rows = data.get(table, [])
                insert_rows(db, f'"{table}"', rows)
            a = normalise(db.execute(left_sql).fetchall())
            b = normalise(db.execute(right_sql).fetchall())
        except duckdb.Error:
            failures += 1  # a data-dependent error (a scalar subquery with two rows) or a misfit proposed database
            continue
        if a != b:
            tables = {t: [dict(zip(pair.tables[t], r)) for r in data.get(t, [])] for t in used}
            return {"tables": tables, "left": sorted(map(str, a.elements())), "right": sorted(map(str, b.elements()))}
    return False if failures == trials + len(candidates) else None


def _proposed_database(pair: Pair, result) -> list[dict]:
    """The prover's counterexample as rows for ``search_difference`` (to be checked there)."""

    counter = getattr(result, "counterexample", None)
    if counter is None:
        return []
    data = {}
    for name, rows in counter.tables.items():
        table = name.lower().split(".")[-1]
        if table not in pair.tables:
            return []
        data[table] = [[{k.lower(): v for k, v in row.items()}.get(c) for c in pair.tables[table]] for row in rows]
    return [data]


def prove(pair: Pair):
    """``(proved, proposed databases, reason)``: the prover's verdict, any counterexample it proposed, and why not."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    from kumosql.canonical_rules import canonicalize

    options = dict(schema=pair.tables, compare_names=False, dialect="mysql", timeout_ms=4000)
    first = prove_equivalent_algebraic(pair.left, pair.right, **options)
    if first.proven:
        return True, [], first.reason
    proposed = _proposed_database(pair, first)
    try:
        left, right = canonicalize(pair.left, "mysql", pair.tables), canonicalize(pair.right, "mysql", pair.tables)
    except sqlglot.errors.SqlglotError:
        return False, proposed, first.reason
    if (left, right) == (pair.left, pair.right):
        return False, proposed, first.reason
    second = prove_equivalent_algebraic(left, right, **options)
    return second.proven, proposed + _proposed_database(pair, second), second.reason


def _unknown(reason: str, crash: str) -> Verdict:
    if crash:
        return Verdict("unknown", crash, outcome="error")
    if "timed out" in reason:
        return Verdict("unknown", reason, outcome="timeout")
    if reason.startswith(("unsupported", "parse error")):
        return Verdict("unknown", reason, outcome="unsupported")
    return Verdict("unknown", reason, outcome="unknown")


def decide(pair: Pair, trials: int = 300) -> Verdict:
    """Equivalent (proved, then re-checked), different (with a witness) or unknown. Never reads the label."""

    try:
        trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
    except sqlglot.errors.SqlglotError as error:
        return Verdict("unknown", f"parse error: {error}", outcome="unsupported")
    try:
        proved, proposed, reason = prove(pair)
    except Exception as error:  # a crash is a failure to prove, never a proof
        proved, proposed, reason, crash = False, [], "", f"{type(error).__name__}: {error}"
    else:
        crash = ""
    if proved:
        witness = search_difference(pair, trees, trials * 3, seed=101)
        if witness:
            return Verdict("wrong", "proved equivalent but DuckDB separates the queries", witness, outcome="wrong")
        return Verdict("equivalent", "proved" + ("" if witness is None else " (not re-checkable in DuckDB)"), outcome="proven")
    if unsafe_to_refute(*trees):
        return _unknown(reason or "no counterexample allowed (LIMIT with ties, LOWER, or non-determinism)", crash)
    witness = search_difference(pair, trees, trials, seed=7, extra=proposed)
    if witness:
        return Verdict("different", "counterexample", witness, outcome="refuted")
    return _unknown(reason or "no proof, no counterexample", crash)


def _decide_index(args):
    position, pair, trials = args
    return position, decide(pair, trials)


OUTCOMES = ("proven", "refuted", "unknown", "unsupported", "timeout", "error", "wrong")


@dataclass
class Report:
    total: int = 0
    counts: Counter = field(default_factory=Counter)  # by verdict kind
    outcomes: Counter = field(default_factory=Counter)
    agree: Counter = field(default_factory=Counter)  # (verdict kind, published label)
    verdicts: dict = field(default_factory=dict)  # position in the pair list -> Verdict
    seconds: float = 0.0

    @property
    def decided(self) -> int:
        return self.counts["equivalent"] + self.counts["different"]

    def line(self) -> str:
        c = self.counts
        return (
            f"{self.decided}/{self.total}, {c['wrong']} wrong ({c['equivalent']} proved equivalent, "
            f"{c['different']} proved different, {self.total - self.decided - c['wrong']} unknown)"
        )

    def coverage(self) -> dict[str, int]:
        return {k: self.outcomes[k] for k in OUTCOMES if self.outcomes[k] or k in ("proven", "refuted", "unknown")}

    def supported_line(self) -> str:
        supported = self.total - self.outcomes["unsupported"] - self.outcomes["error"]
        return f"{self.decided}/{supported} on the supported subset"


def run(pairs: list[Pair], trials: int = 300, workers: int | None = None) -> Report:
    report = Report(total=len(pairs))
    start = time.time()
    jobs = [(position, pair, trials) for position, pair in enumerate(pairs)]
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for position, verdict in pool.map(_decide_index, jobs, chunksize=8):
            report.verdicts[position] = verdict
            report.counts[verdict.kind] += 1
            report.outcomes[verdict.outcome] += 1
            report.agree[(verdict.kind, pairs[position].gold)] += 1
    report.seconds = time.time() - start
    return report


def split(pairs: list[Pair], name: str) -> list[Pair]:
    if name == "dev":
        return [p for p in pairs if not p.held_out]
    if name == "held-out":
        return [p for p in pairs if p.held_out]
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all", help="held-out pairs are for final scoring only")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample", type=int, help="a fixed random sample of this many pairs (the files are ordered by label)")
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--json", type=Path, help="write per-pair verdicts here")
    parser.add_argument("--show-unknown", action="store_true", help="print why each unknown pair stayed unknown")
    parser.add_argument("--show-disagreements", action="store_true", help="print pairs whose verdict contradicts the published label")
    args = parser.parse_args(argv)
    pairs = split(load_pairs(), args.split)
    if args.sample:
        pairs = random.Random(2024).sample(pairs, min(args.sample, len(pairs)))
    pairs = pairs[: args.limit]
    report = run(pairs, args.trials, args.workers)
    print(report.line(), f"in {report.seconds:.0f}s")
    print("outcomes:", report.coverage(), "|", report.supported_line())
    held = [i for i, p in enumerate(pairs) if p.held_out]
    if args.split == "all" and held:
        sub = Counter(report.verdicts[i].kind for i in held)
        print(f"held-out: {sub['equivalent'] + sub['different']}/{len(held)}, {sub['wrong']} wrong")
    print(f"{'verdict':12} {'label equivalent':>17} {'label different':>16}")
    for kind in ("equivalent", "different", "unknown", "wrong"):
        print(f"{kind:12} {report.agree[(kind, 'equivalent')]:17} {report.agree[(kind, 'different')]:16}")
    if args.json:
        args.json.write_text(json.dumps(
            [{"key": p.key, "held_out": p.held_out, "verdict": report.verdicts[i].kind, "outcome": report.verdicts[i].outcome, "label": p.gold, "detail": report.verdicts[i].detail[:300]} for i, p in enumerate(pairs)],
            indent=1,
        ))
    if args.show_disagreements:
        for i, verdict in report.verdicts.items():
            pair = pairs[i]
            if verdict.kind in ("equivalent", "different", "wrong") and verdict.kind != pair.gold:
                print(f"\n#{pair.key} {verdict.kind} (label {pair.gold}) {verdict.detail}\n  {pair.left}\n  {pair.right}")
                if verdict.witness:
                    print("  ", json.dumps(verdict.witness, default=str)[:400])
    if args.show_unknown:
        for i, verdict in report.verdicts.items():
            pair = pairs[i]
            if verdict.kind == "unknown":
                print(f"\n#{pair.key} [{verdict.outcome}] (label {pair.gold}) {verdict.detail[:200]}\n  {pair.left}\n  {pair.right}")
    return 1 if report.counts["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
