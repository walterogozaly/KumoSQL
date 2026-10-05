"""Score KumoSQL on window-function equivalence, including ties, with no LLM (issue #503).

Four sources of query pairs, one outcome per pair:

* ``derived``: the hand-written window pairs the workstream started from (rank-one forms of "max per group",
  QUALIFY against a derived table, a filter pushed through a window, a windowed aggregate against its join form);
* ``idiom``: BigQuery idioms written for this eval, each with a tie trap: the latest row per key, sessionisation,
  running totals, gaps and islands (equivalent and non-equivalent variants);
* ``slt``: window queries from the DuckDB sqllogictest suite (``tools/engine_suites.py`` reads it), each with pairs made by
  KumoSQL's rewrite pipeline as it is on this checkout, by wrapping the query (subquery, CTE, unused CTE) and by mutating one
  window clause (kept only where the source's own data tells the mutant from the query). The SQLite suite has no window query;
* ``corpus``: window pairs of the development splits of other benchmarks (VeriEQL LeetCode, every 24th case; VeriEQL
  Calcite-397; SQLSolver Spark pair 50, which must not be proved). The VeriEQL files are CC BY-NC-SA, so only ids and a
  checksum of each pair are kept here; the pairs are read from the cache ``tools/verieql_bench.py`` fills.

A query that can return different rows on the same data (ties in a window's ORDER BY, a LIMIT on a non-unique order,
``ARRAY_AGG`` order) has a *set* of possible results. Two queries are ``equivalent`` when those sets are equal on every
database the declared keys and NOT NULL columns allow, ``not_equivalent`` when some database separates them, and a pair
that is tie-dependent (``tie_dependent``) is one whose answer turns on a tie: a non-equivalent one differs only on data
with ties, an equivalent one is nondeterministic on both sides yet has the same set of results. The hand-written and
sqllogictest labels are checked by execution: ``possible_results`` stores a table in every row order on DuckDB (one
thread, which breaks ties by storage order) and collects the result bags, and a non-equivalent case carries a witness
database whose two sets differ. Pairs DuckDB cannot run (``ARRAY_AGG .. LIMIT``) are labelled by reasoning and say so.

Each pair gets one outcome, decided without its label:

1. **proven**: ``kumosql.prove_equivalent`` or ``prove_equivalent_algebraic`` proves it (types, keys and NOT NULL columns
   of the case's fixture);
2. **refuted**: the algebraic prover's counterexample search or the targeted refuter finds a database that loads as
   declared and separates the two queries in DuckDB (some row order);
3. **unknown**: anything else, including a claim whose database does not load or replay.

**Wrong** is a proof of a pair not labelled ``equivalent`` or a claim (replayed or not) that an ``equivalent`` pair differs.
Unknown beats wrong. A quarter of the cases (a stable hash of the case id) is held out: develop on ``dev``.

    python tools/window_equivalence_bench.py                    # every case (several minutes)
    python tools/window_equivalence_bench.py --split dev --only idiom,derived
    python tools/window_equivalence_bench.py --check-labels     # execute the label evidence of every case
    python tools/window_equivalence_bench.py --write-results
    python tools/window_equivalence_bench.py --make-slt-cases   # re-mine the sqllogictest pairs (needs the cached suites)
    python tools/window_equivalence_bench.py --make-corpus-index
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import itertools
import json
import logging
import multiprocessing
from pathlib import Path
import random
import re
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

DATA = ROOT / "benchmarks" / "window_equivalence"
NAME = "window-equivalence"
PROVER_TIMEOUT_MS = 5000
REFUTE_BUDGET_S = 6.0
LABELS = ("equivalent", "not_equivalent", "unlabelled")
SOURCES = ("derived", "idiom", "slt", "corpus")


@dataclass
class Case:
    id: str
    source: str  # derived | idiom | slt | corpus
    family: str  # latest-row | sessions | running | islands | rank-one | frames | pushdown | ... (free text, grouped in reports)
    fixture: object  # a name in fixtures.json, or an inline fixture {"tables": .., "constraints": ..}
    left: str
    right: str
    label: str  # equivalent | not_equivalent
    tie_dependent: bool
    why: str
    origin: str  # authored | re-derived | slt-rule | slt-wrap | slt-mutant | corpus
    licence: str = "authored for this repository"
    dialect: str = "bigquery"
    executable: bool = True  # DuckDB can run both queries, so the label evidence can be replayed
    witness: dict | None = None  # not_equivalent, executable: tables (rows) on which the two sets of results differ
    detail: dict = field(default_factory=dict)  # provenance of mined cases

    @property
    def held_out(self) -> bool:
        return held_out(self.id)


def held_out(case_id: str) -> bool:
    """One case in four, by a stable hash of its id (never by position, so adding cases moves no other)."""

    return int(hashlib.sha256(f"{NAME}\n{case_id}".encode()).hexdigest(), 16) % 4 == 0


def load_fixtures() -> dict:
    return json.loads((DATA / "fixtures.json").read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_cases(sources: tuple[str, ...] = ("derived", "idiom", "slt")) -> list[Case]:
    """The cases kept in this repository (hand-written, re-derived and mined); corpus pairs come from :func:`corpus_cases`."""

    out = []
    for name in ("cases.jsonl", "slt_cases.jsonl"):
        for row in _read_jsonl(DATA / name):
            case = Case(**row)
            if case.source in sources:
                out.append(case)
    return out


def split_of(cases, split: str):
    if split == "dev":
        return [c for c in cases if not held_out(c.id if hasattr(c, "id") else c["id"])]
    if split == "held-out":
        return [c for c in cases if held_out(c.id if hasattr(c, "id") else c["id"])]
    return list(cases)


def fixture_of(case: Case, fixtures: dict) -> dict:
    return fixtures[case.fixture] if isinstance(case.fixture, str) else case.fixture


# ------------------------------------------------------------------ execution and label evidence


class Runner:
    """One DuckDB connection per fixture running BigQuery SQL the way BigQuery would, with one thread (ties follow storage order)."""

    def __init__(self, fixture: dict):
        import engine_pairs_bench as E

        self.fixture = fixture
        self.engine = E.Engine("window-eval", fixture, rows={t: [] for t in fixture["tables"]})
        self.engine.db.execute("SET threads=1")
        from kumosql.result_equivalence import _local_name

        self.names = {t: _local_name(t) for t in fixture["tables"]}
        self._text: dict[str, str] = {}

    def close(self) -> None:
        self.engine.close()

    def text(self, sql: str) -> str:
        if sql not in self._text:
            self._text[sql] = self.engine.text(sql)
        return self._text[sql]

    def load(self, tables: dict[str, list]) -> None:
        from kumosql.duckdb_load import insert_rows

        for table, name in self.names.items():
            self.engine.db.execute(f'DELETE FROM "{name}"')
            insert_rows(self.engine.db, f'"{name}"', tables.get(table, []))

    def bag(self, sql: str) -> tuple:
        """The result of ``sql`` on the loaded rows as a sorted tuple of rows."""

        return tuple(sorted(self.engine.rows(self.engine.db.execute(self.text(sql)).fetchall()), key=repr))

    def possible_results(self, sql: str, tables: dict[str, list], limit: int = 720, seed: int = 0) -> set:
        """Every result bag the query returns when each table is stored in each row order (a sample of ``limit`` of them for more)."""

        names = sorted(tables)
        orders = [list(_orders(tables[t], limit, seed)) for t in names]
        results = set()
        for combo in itertools.islice(itertools.product(*orders), limit):
            self.load(dict(zip(names, combo)))
            results.add(self.bag(sql))
        return results


def _orders(rows: list, limit: int, seed: int):
    rows = [list(r) for r in rows]
    if len(rows) <= 6:
        seen = set()
        for p in itertools.permutations(range(len(rows))):
            ordered = tuple(tuple(rows[i]) for i in p)
            if ordered not in seen:  # duplicate rows give identical orders
                seen.add(ordered)
                yield [list(r) for r in ordered]
        return
    rng = random.Random(seed)
    for _ in range(limit):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        yield shuffled


def random_databases(fixture: dict, count: int, seed: int, rows: int = 4, domain: int = 3):
    """Small databases that respect the fixture's keys and NOT NULL columns, with few distinct values so rows tie."""

    import engine_pairs_bench as E

    rng = random.Random(seed)
    tables = {t: spec["columns"] for t, spec in fixture["tables"].items()}
    constraints = fixture.get("constraints") or {}
    made, attempts = [], 0
    while len(made) < count and attempts < count * 200:
        attempts += 1
        data = {}
        for table, columns in tables.items():
            not_null = set(constraints.get(table, {}).get("not_null", ()))
            n = rng.randint(max(2, rows - 2), rows)
            table_rows = []
            for i in range(n):
                row = []
                for column, kind in columns:
                    if column == "id":
                        row.append(i + 1)
                    elif column not in not_null and rng.random() < 0.15:
                        row.append(None)
                    elif kind == "INT64":
                        row.append(rng.randint(1, domain))
                    elif kind == "STRING":
                        row.append(rng.choice("abc"))
                    else:
                        row.append(rng.randint(1, domain))
                table_rows.append(row)
            data[table] = table_rows
        if E.respects_declared(fixture, data):
            made.append(data)
    return made


def label_evidence(case: Case, fixtures: dict, databases: int = 8, seed: int = 7) -> dict:
    """Execute a case's label: for ``equivalent`` the two sets of results are equal on every random database (and ``tie`` says whether
    any database made a side nondeterministic); for ``not_equivalent`` they differ on the stored witness. ``{"checked": False}`` for a pair DuckDB cannot run."""

    if not case.executable or case.dialect != "bigquery":
        return {"checked": False, "ok": True, "note": "not executable: label by reasoning"}
    fixture = fixture_of(case, fixtures)
    own = {t: spec["rows"] for t, spec in fixture["tables"].items() if "rows" in spec}  # a mined case carries its source's rows
    runner = Runner(fixture)
    try:
        tied = False
        if case.label == "not_equivalent":
            witness = case.witness or own
            if not witness:
                return {"checked": True, "ok": False, "note": "no witness"}
            a = runner.possible_results(case.left, witness)
            b = runner.possible_results(case.right, witness)
            return {"checked": True, "ok": a != b, "note": "witness sets " + ("differ" if a != b else "are equal"), "sizes": [len(a), len(b)]}
        databases_to_try = []
        if own:
            databases_to_try.append(own)
        if all(kind in ("INT64", "STRING") for spec in fixture["tables"].values() for _, kind in spec["columns"]):
            databases_to_try += random_databases(fixture, databases, seed)
        for data in databases_to_try:
            try:
                a = runner.possible_results(case.left, data)
                b = runner.possible_results(case.right, data)
            except Exception as error:  # a random database can make a query fail (a scalar subquery with two rows); BigQuery fails there too
                if data is own:
                    return {"checked": True, "ok": False, "note": f"does not run on its source's rows: {str(error)[:80]}"}
                continue
            tied = tied or len(a) > 1 or len(b) > 1
            if a != b:
                return {"checked": True, "ok": False, "note": f"sets differ on {data}", "sizes": [len(a), len(b)]}
        return {"checked": True, "ok": True, "note": "sets equal", "tied": tied}
    finally:
        runner.close()


def find_witness(case: Case, fixtures: dict, attempts: int = 400, seed: int = 11) -> dict | None:
    """A smallest database found on which the two sets of results differ (authoring helper)."""

    fixture = fixture_of(case, fixtures)
    runner = Runner(fixture)
    best = None
    try:
        for size in (2, 3, 4, 5):
            for data in random_databases(fixture, attempts // 4, seed + size, rows=size):
                try:
                    a = runner.possible_results(case.left, data)
                    b = runner.possible_results(case.right, data)
                except Exception:
                    continue
                if a != b:
                    n = sum(len(r) for r in data.values())
                    if best is None or n < best[0]:
                        best = (n, data)
            if best is not None:
                break
    finally:
        runner.close()
    return best[1] if best else None


# ------------------------------------------------------------------ the provers


def _types_of(fixture: dict) -> dict:
    return {t: {c: k for c, k in spec["columns"]} for t, spec in fixture["tables"].items()}


def decide(case: Case, fixtures: dict) -> dict:
    import engine_pairs_bench as E
    from kumosql import prove_equivalent
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.refute import find_targeted_difference
    from kumosql.smt_equivalence import SmtStatus

    fixture = fixture_of(case, fixtures)
    types = _types_of(fixture)
    schema = {t: list(cols) for t, cols in types.items()}
    started = time.perf_counter()
    outcome, method, claimed = "unknown", "", False
    try:
        if prove_equivalent(case.left, case.right).proven:
            outcome, method = "proven", "structural"
    except Exception:
        pass
    if outcome == "unknown":
        try:
            result = prove_equivalent_algebraic(
                case.left, case.right, schema=schema, types=types, constraints=E.constraints_of(fixture) or None,
                compare_names=False, dialect="bigquery", timeout_ms=PROVER_TIMEOUT_MS, search_counterexample=True,
            )
        except Exception:  # a crash is a failure to prove, never a proof
            result = None
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            outcome, method = "proven", "algebraic"
        elif result is not None and result.status is SmtStatus.NOT_EQUIVALENT:
            claimed = True
            if result.counterexample is not None and replays(case, fixture, result.counterexample.tables):
                outcome, method = "refuted", "algebraic"
    if outcome == "unknown":
        try:
            found = find_targeted_difference(case.left, case.right, types, E.rules_of(fixture) or None, dialect="bigquery", budget=REFUTE_BUDGET_S)
        except Exception:
            found = None
        if found is not None:
            claimed = True
            if replays(case, fixture, dict(found.dataset.tables)):
                outcome, method = "refuted", "targeted"
    wrong = (outcome == "proven" and case.label != "equivalent") or (claimed and case.label == "equivalent")
    return {
        "id": case.id, "source": case.source, "family": case.family, "label": case.label, "tie_dependent": case.tie_dependent,
        "outcome": outcome, "method": method, "unreplayed_claim": claimed and outcome != "refuted", "wrong": wrong,
        "held_out": case.held_out, "seconds": round(time.perf_counter() - started, 1),
    }


def replays(case: Case, fixture: dict, tables: dict) -> bool:
    """The counterexample keeps the declared keys and NOT NULL columns and separates the two queries in DuckDB in some row order
    (one thread, the optimizer off agrees)."""

    import engine_pairs_bench as E

    try:
        rows = _rows_of(fixture, tables)
        if not E.respects_declared(fixture, rows):
            return False
        runner = Runner(fixture)
        try:
            for order in itertools.islice(_joint_orders(rows), 24):
                runner.load(order)
                from kumosql.duckdb_load import run_unoptimized

                a, b = runner.text(case.left), runner.text(case.right)
                x, y = runner.engine.db.execute(a).fetchall(), runner.engine.db.execute(b).fetchall()
                if _bag(runner, x) != _bag(runner, y):
                    x, y = run_unoptimized(runner.engine.db, a, b)
                    if _bag(runner, x) != _bag(runner, y):
                        return True
            return False
        finally:
            runner.close()
    except Exception:
        return False


def _bag(runner: Runner, raw) -> tuple:
    return tuple(sorted(runner.engine.rows(raw), key=repr))


def _joint_orders(rows: dict):
    names = sorted(rows)
    lists = [list(_orders(rows[t], 24, 3)) for t in names]
    for combo in itertools.product(*lists):
        yield dict(zip(names, combo))


def _rows_of(fixture: dict, tables: dict) -> dict:
    out = {}
    for table, spec in fixture["tables"].items():
        names = [c for c, _ in spec["columns"]]
        got = None
        for key, value in tables.items():
            if key.lower().split(".")[-1] == table.lower():
                got = value
        if got is None:
            out[table] = []
        elif hasattr(got, "rows"):
            index = {n.lower(): i for i, (n, _) in enumerate(got.columns)}
            out[table] = [[row[index[n.lower()]] if n.lower() in index else None for n in names] for row in got.rows]
        else:
            out[table] = [[{k.lower(): v for k, v in row.items()}.get(n.lower()) for n in names] for row in got]
    return out


# ------------------------------------------------------------------ corpus pairs


def corpus_index() -> list[dict]:
    return json.loads((DATA / "corpus.json").read_text(encoding="utf-8"))["pairs"]


_OVER = re.compile(r"\bOVER\s*\(", re.IGNORECASE)


def _pair_sha(pair) -> str:
    return hashlib.sha1("\n".join(pair).encode()).hexdigest()[:12]


def decide_corpus(entry: dict, verieql_cases: dict | None = None) -> dict:
    """One corpus pair: VeriEQL's harness verdict (``tools/verieql_bench.py``) or, for SQLSolver Spark, the prover plus an executed search."""

    started = time.perf_counter()
    base = {"id": entry["id"], "source": "corpus", "family": entry["suite"], "label": entry["label"], "tie_dependent": entry.get("tie_dependent", False), "held_out": held_out(entry["id"])}
    if entry["suite"] == "sqlsolver-spark":
        import sqlsolver_bench as S

        tables = S.load_schema(S.FIXTURES / S.SUITES["spark"][1])
        left, right = S.load_pairs(S.FIXTURES / S.SUITES["spark"][0])[entry["index"]]
        try:
            proven = S.default_prove(left, right, tables)
        except Exception:
            proven = False
        refuted = False
        if not proven:
            try:
                found = S.differ(left, right, tables, S.new_database(tables), 30)
                refuted = bool(found)
            except Exception:
                refuted = False
        outcome = "proven" if proven else "refuted" if refuted else "unknown"
        wrong = outcome == "proven" and entry["label"] != "equivalent"
        return {**base, "outcome": outcome, "method": "sqlsolver", "wrong": wrong, "unreplayed_claim": False, "seconds": round(time.perf_counter() - started, 1)}
    import verieql_bench as V

    case = verieql_cases[(entry["suite"], entry["index"])]
    if _pair_sha(case["pair"]) != entry["sha"]:  # the benchmark file is not the one the index was made from
        return {**base, "outcome": "unknown", "method": "pair changed", "wrong": False, "unreplayed_claim": False, "seconds": 0.0}
    verdict = V._work((case, {"budget": 30}))
    outcome = {V.EQUIVALENT: "proven", V.DIFFERENT: "refuted"}.get(verdict.status, "unknown")
    wrong = verdict.status == V.WRONG or (outcome == "proven" and entry["label"] == "not_equivalent")
    return {**base, "outcome": outcome, "method": verdict.detail, "wrong": wrong, "unreplayed_claim": False, "seconds": round(time.perf_counter() - started, 1)}


def verieql_cases() -> dict:
    import verieql_bench as V

    out = {}
    for suite in ("leetcode", "calcite"):
        for case in V.load_cases(suite):
            out[(suite, case["index"])] = case
    return out


def make_corpus_index() -> dict:
    """The window pairs of the development splits: VeriEQL LeetCode (every 24th case), every VeriEQL Calcite-397 pair and SQLSolver Spark pair 50.

    VeriEQL's files carry no labels and its own run cannot read a window (every one of these pairs is ``NSE`` or ``NIE`` in its
    published outcomes), so these pairs are ``unlabelled``: scored only for a proof that an executed search contradicts. SQLSolver Spark
    pair 50 is ``not_equivalent`` (the benchmark lists it as a pair that must not be proved)."""

    import verieql_bench as V

    pairs = []
    for suite, step in (("leetcode", 24), ("calcite", 1)):
        for case in V.load_cases(suite)[::step]:
            if any(_OVER.search(sql) for sql in case["pair"]):
                pairs.append({
                    "id": f"{suite}-{case['index']}", "suite": suite, "index": case["index"], "sha": _pair_sha(case["pair"]),
                    "label": "unlabelled", "basis": "VeriEQL's files carry no label and its own run cannot read a window",
                })
    pairs.append({
        "id": "sqlsolver-spark-50", "suite": "sqlsolver-spark", "index": 50, "sha": "", "label": "not_equivalent", "tie_dependent": True,
        "basis": "tests/fixtures/sqlsolver/not_provable.json: LIMIT 5 without ORDER BY over ROW_NUMBER() OVER (ORDER BY f) against the first five sorted rows",
    })
    return {"source": "VeriEQL (CC BY-NC-SA 4.0, commit 493cbb81, read from the local cache, not copied) and SQLSolver (Apache-2.0)", "pairs": pairs}


# ------------------------------------------------------------------ mining sqllogictest window queries


def _worker(args):
    kind, payload = args
    try:
        if kind == "case":
            fixtures, case = payload
            return decide(case, fixtures)
        return decide_corpus(*payload)
    except Exception as error:  # a crash is an unknown, never a verdict
        return {"id": payload[1].id if kind == "case" else payload[0]["id"], "outcome": "unknown", "wrong": False, "crash": f"{type(error).__name__}: {str(error)[:100]}"}


def run(cases: list[Case], entries: list[dict] | None = None, jobs: int = 1) -> list[dict]:
    fixtures = load_fixtures()
    work = [("case", (fixtures, c)) for c in cases]
    if entries:
        verieql = verieql_cases() if any(e["suite"] != "sqlsolver-spark" for e in entries) else {}
        work += [("corpus", (e, verieql)) for e in entries]
    if jobs > 1:
        with multiprocessing.Pool(jobs) as pool:
            rows = list(pool.imap(_worker, work, chunksize=2))
    else:
        rows = [_worker(item) for item in work]
    return rows


# ------------------------------------------------------------------ scoring


def summary(rows: list[dict]) -> dict:
    eq = [r for r in rows if r["label"] == "equivalent"]
    neq = [r for r in rows if r["label"] == "not_equivalent"]
    tie_neq = [r for r in neq if r.get("tie_dependent")]
    other = [r for r in rows if r["label"] == "unlabelled"]
    counts = Counter(r["outcome"] for r in rows)
    return {
        "size": len(rows),
        "equivalent": len(eq), "equivalent_proven": sum(r["outcome"] == "proven" for r in eq), "equivalent_refuted": sum(r["outcome"] == "refuted" for r in eq),
        "not_equivalent": len(neq), "not_equivalent_refuted": sum(r["outcome"] == "refuted" for r in neq), "not_equivalent_proven": sum(r["outcome"] == "proven" for r in neq),
        "tie_not_equivalent": len(tie_neq), "tie_not_equivalent_refuted": sum(r["outcome"] == "refuted" for r in tie_neq),
        "tie_not_equivalent_proven": sum(r["outcome"] == "proven" for r in tie_neq),
        "unlabelled": len(other), "unlabelled_proven": sum(r["outcome"] == "proven" for r in other), "unlabelled_refuted": sum(r["outcome"] == "refuted" for r in other),
        "proven": counts["proven"], "refuted": counts["refuted"], "unknown": counts["unknown"],
        "wrong": sum(bool(r["wrong"]) for r in rows),
    }


def score(rows: list[dict]) -> str:
    s = summary(rows)
    authored = summary([r for r in rows if r["source"] in ("derived", "idiom")])
    mined = summary([r for r in rows if r["source"] == "slt"])
    text = (
        f"{s['equivalent_proven']}/{s['equivalent']} equivalent proved (hand-written {authored['equivalent_proven']}/{authored['equivalent']}, "
        f"sqllogictest {mined['equivalent_proven']}/{mined['equivalent']}), {s['not_equivalent_refuted']}/{s['not_equivalent']} non-equivalent refuted "
        f"(tie-dependent {s['tie_not_equivalent_refuted']}/{s['tie_not_equivalent']}, {s['not_equivalent_proven']} proved)"
    )
    if s["unlabelled"]:
        text += f", {s['unlabelled_proven']} proved and {s['unlabelled_refuted']} refuted of {s['unlabelled']} unlabelled"
    return text + f", {s['wrong']} wrong"


def results_row(rows: list[dict]) -> dict:
    from bench_common import today

    s = summary(rows)
    by_source = Counter(r["source"] for r in rows)
    held = [r for r in rows if r["held_out"]]
    return {
        "suite": "Window equivalence (windows and ties)",
        "order": 60,
        "size": s["size"],
        "score": score(rows),
        "metric": (
            "Query pairs around window functions and ties (latest row per key, sessions, running totals, gaps and islands, rank-one forms, "
            "frames, filters through a window; pairs from the DuckDB sqllogictest suite and window pairs of other benchmarks' development splits): "
            "equivalent pairs proved, non-equivalent pairs refuted or left unknown, never proved, and no wrong answer."
        ),
        "evidence": "proof",
        "correctness": (
            "Wrong is a proof of a pair not labelled equivalent, or a claim (replayed or not) that an equivalent pair differs. Hand-written and sqllogictest "
            "labels are executed on DuckDB (one thread) over every row order of tie-heavy databases; non-equivalent ones carry a witness whose sets of "
            "results differ; a refutation counts only when its database keeps the declared keys and NOT NULL columns and separates the queries in DuckDB. "
            "Corpus pairs have no label (VeriEQL's files carry none): they are only checked for a proof that the harness's executed search contradicts."
        ),
        "coverage": {k: s[k] for k in ("proven", "refuted", "unknown") if s[k]},
        "held_out": score(held) + f" ({len(held)} cases)",
        "docs": "docs/evals/window-equivalence.md",
        "command": "python tools/window_equivalence_bench.py --write-results",
        "date": today(),
        "caveats": (
            f"{s['size']} cases ({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))}); a quarter is held out by a hash of the case id. "
            "The first run is the baseline, measured before any window rule of issue #503; later rule changes rebase these numbers. The hand-written "
            "cases and the sqllogictest mutants were written by the author of this eval, who also reads the prover, so this is a regression and "
            "honesty check, not an independent benchmark. Rule-made sqllogictest pairs are labelled equivalent because the rule is sound and both "
            "queries agree on the file's data, which is not a proof. Pairs DuckDB cannot run (ARRAY_AGG with LIMIT) are labelled by reasoning. "
            "The SQLite sqllogictest suite has no window query, so it adds none. Corpus pairs are the development splits only; VeriEQL's files carry no "
            "labels, so only SQLSolver Spark pair 50 (which the benchmark says must not be proved) is a labelled corpus pair and the 91 VeriEQL pairs count for coverage and for wrong only, not for the equivalent-proof rate."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    from bench_common import quiet, write_results

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--only", default="", help="comma-separated sources (derived,idiom,slt,corpus) or id prefixes")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--show", default="", help="comma-separated outcomes to print (proven,refuted,unknown)")
    parser.add_argument("--json", metavar="PATH")
    parser.add_argument("--check-labels", action="store_true")
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument("--make-corpus-index", action="store_true")
    parser.add_argument("--make-slt-cases", action="store_true")
    args = parser.parse_args(argv)
    quiet()

    if args.make_corpus_index:
        index = make_corpus_index()
        (DATA / "corpus.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {len(index['pairs'])} corpus pairs")
        return 0
    if args.make_slt_cases:
        import window_equivalence_mine as mine

        return mine.main([])
    wanted = [w for w in args.only.split(",") if w]
    sources = tuple(w for w in wanted if w in SOURCES) or SOURCES
    prefixes = tuple(w for w in wanted if w not in SOURCES)
    cases = [c for c in load_cases(tuple(s for s in sources if s != "corpus")) if not prefixes or c.id.startswith(prefixes)]
    cases = split_of(cases, args.split)
    fixtures = load_fixtures()
    if args.check_labels:
        bad = 0
        for case in cases:
            evidence = label_evidence(case, fixtures)
            if not evidence["ok"]:
                bad += 1
                print(f"{case.id:40} {case.label:15} LABEL NOT CONFIRMED: {evidence['note'][:150]}")
        print(f"{len(cases)} cases, {bad} labels not confirmed")
        return 1 if bad else 0
    entries = []
    if "corpus" in sources and not (prefixes and not any(e["id"].startswith(prefixes) for e in corpus_index())):
        entries = [e for e in split_of(corpus_index(), args.split) if not prefixes or e["id"].startswith(prefixes)]
    rows = run(cases, entries, jobs=args.jobs)
    shown = set(filter(None, args.show.split(",")))
    for row in rows:
        if row["outcome"] in shown or row["wrong"] or row.get("crash"):
            print(f"{row['id']:44} {str(row.get('label')):15} {row['outcome']:8} {row.get('method', '')!s:12} {'WRONG' if row['wrong'] else ''} {row.get('crash', '')}")
    s = summary(rows)
    print(score(rows))
    print(f"proven {s['proven']}, refuted {s['refuted']}, unknown {s['unknown']} of {s['size']}; held out: {score([r for r in rows if r['held_out']])}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    if args.write_results:
        if args.split != "all" or wanted:
            print("--write-results needs every case")
            return 1
        path = write_results(NAME, results_row(rows))
        print(f"wrote {path.relative_to(ROOT)}")
    return 1 if s["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())
