"""Score KumoSQL on the Arcwise-Plat corrections of BIRD's SQL, with no language model.

Arcwise-Plat (https://github.com/uiuc-kang-lab/text_to_sql_benchmarks, CC BY-SA 4.0; Jin et al.,
"Pervasive Annotation Errors Break Text-to-SQL Benchmarks and Leaderboards", VLDB 2026) re-annotates
the 498 questions of BIRD Mini-Dev. Where BIRD's gold SQL was wrong, a record keeps it as
``original_SQL`` next to the repaired ``SQL``. Two files are used:

* ``arcwise_plat_sql_only_with_diff.json`` (Arcwise-Plat-SQL): only the SQL is repaired (83 pairs);
* ``arcwise_plat_full_with_diff.json`` (Arcwise-Plat): the question and evidence may be repaired too
  (134 pairs, 72 of them the same pair as in the first file; of the other 62, one differs only by
  ``INNER JOIN`` against ``JOIN`` and is left out, so 61 are scored as ``full``).

A repair is meant to change the result, so a pair is expected to be **refuted**: SQLite returns
different results for the original and the corrected query on a database that respects the keys and
foreign keys. A **proof** is not wrong by itself: a repair can be cosmetic (an alias, quoting or the
same filter written another way). Every proved pair is inspected by hand and listed in ``COSMETIC``
with the reason the two queries are equal, or in ``FALSE_PROOFS`` (counted as wrong). A proof that is
in neither list counts as wrong until it is inspected.

Each pair is decided as ``tools/llm_sql_solver_bench.py`` decides a Spider pair (the corrected query
plays Spider's gold query): the algebraic prover in the SQLite dialect with no keys, results compared
as lists when the corrected query ends in ``ORDER BY`` and as bags otherwise, no proof when a query
compares a text column with a number, and refutation on 1,000 random SQLite databases that respect
the keys (ties under a LIMIT never count), then the targeted suite and the z3 bounded check, each
replayed in SQLite. Each pair runs in its own process with a time limit (``PAIR_TIMEOUT``): one z3 call
can outlast its own limit and another can crash, and such a pair is unknown. BIRD's databases are not downloadable here; the tables, declared types, keys and
foreign keys come from the M-Schema files the same repository ships for BIRD dev
(``text_to_sql_agents/Contextual-SQL/few_shots/mschema``, MIT).

The data is CC BY-SA, so it is never committed: it is downloaded at a pinned commit into
``$KUMOSQL_BENCH_DATA/arcwise`` (default ``~/.cache/kumosql-bench/arcwise``) and checked against the
SHA-256 digests below. One pair in five, by the SHA-1 of its id, is held out; a run reads only the dev
pairs unless ``--split all`` or ``--split held-out`` is given (``--write-results`` scores every pair).

    python tools/arcwise_bench.py                       # the dev pairs, about 90 seconds on 2 cores
    python tools/arcwise_bench.py --show proven,unknown
    python tools/arcwise_bench.py --overlap             # overlap with ParSEval's and DLBench's BIRD queries
    python tools/arcwise_bench.py --write-results
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing
import os
import re
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

import llm_sql_solver_bench as solver

ROOT = Path(__file__).resolve().parent.parent
COMMIT = "fe766045c55b6875a43b30e9ac7683df5582f8cf"  # 2026-08-27
BASE = f"https://raw.githubusercontent.com/uiuc-kang-lab/text_to_sql_benchmarks/{COMMIT}/"
MSCHEMA = "text_to_sql_agents/Contextual-SQL/few_shots/mschema/{}.mschema"
DATA_FILES = {
    "sql-only": ("data/arcwise_plat_sql_only_with_diff.json", "e7bf76408f99266506ea558d84982c69422e5db3dfecab5f08a3a8d3900e8395"),
    "full": ("data/arcwise_plat_full_with_diff.json", "baefe2ca4fbab86c000df72aad9eaa563f7d422e425af2cbff071f656ea4eea8"),
}
SCHEMAS = {
    "california_schools": "bee11e06931d08373f4d6cc214abe9037e4f313363b670896caf71a525321e88",
    "card_games": "90914e1471218478fbaec5cb9e274917fe40dbdfce38ded7b9e00a99bd54bea4",
    "codebase_community": "11df8d56a6191460c42b42d2a844a060a9c7309f3c05c7393d7cf684aacba8e5",
    "debit_card_specializing": "a9d2c6352f5a48899d25530f4944f9c0534811ff667ae696f8daaf5fa82d146c",
    "european_football_2": "c04227ed1431c30b6f4997060da3c189bf001d4d705e4949353374f50afd933f",
    "financial": "1a58ba2b9669f3cf71241c2e0fa17d9326c544c35a68fec49aa05e61d2b7f811",
    "formula_1": "4ae2f7cdc65de860530cd90edf0e57144c37d1354f0d55e37be389191187c643",
    "student_club": "0d1d1006c73fd5fc80af032afc9a5aa126e3851fe0c282283a87da6bc4c447f9",
    "superhero": "64dc3c12d0bea9bc6acd2465ed0b18d53c4682b38244a085b03314fc027bb3ea",
    "thrombosis_prediction": "3e9588cb42661623916d1424dd705dc812fc9888d59155f9c32f6a973a561e1a",
    "toxicology": "81046d1e1a49f444683c6e0e9bd968da7a60daa2f2d9141aed5ae1bbea6e7e0c",
}
# Overlap sources (read only by --overlap): BIRD dev as ParSEval ships it, and DLBench's BIRDTrans
PARSEVAL = (
    "https://raw.githubusercontent.com/sfu-db/ParSEval/0d58127c5d6df74caca170b80c80f312b95cc238/data/sqlite/dev.json",
    "9b4c38da7f07999180dc1a463e03371e3b4eb685ce8a110e5944eb8fe3a70853",
)
DLBENCH_BASE = "https://raw.githubusercontent.com/DLBenchll/DLBench/a3525919033faac73e60a13a88c2d14ab6953f23/datasets/BIRDTrans/"
DLBENCH = {
    "mysql": "3a8deb7a6a2f3687b7b768bc89af28d9da7486225f9a9aa16b62892792226220",
    "mariadb": "26a3ebbc0016e2f1c03a86d0e3979d4d3d237cb461263f3caa37b4206c953486",
    "postgresql": "b1f06b5e1958e9fb63429797631954e6bd6f7ec88da1043ba34856e45153ef65",
    "clickhouse": "b4f99202d34d075bdf3bbff6937f9db24772a7617b5ad74760dd9659612425be",
    "monetdb": "bd4995eae33f79772a1cb408f3cc5e27cfabc39a45ccae1ef077d6f983a0b9d2",
    "duckdb": "84c342fcb90e311ee4bc459aa781df4dd3b418799cbbab185590b6d1cb9cb462",
}
PAIR_TIMEOUT = 75  # seconds; the z3 bounded check can run past its own limit on string arithmetic
CACHE = Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench")) / "arcwise"

# Proved pairs, inspected by hand: the two queries return the same result on every database.
COSMETIC: dict[str, str] = {}
# Proved pairs that a replayed counterexample shows are different (counted as wrong, kept as regressions).
FALSE_PROOFS: dict[str, str] = {}


# -- the data -------------------------------------------------------------------------


def _download(url: str, path: Path, digest: str) -> Path:
    """Download once into the cache (written whole, then renamed, so parallel runs never read half a file)."""

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"{path} does not match the pinned version; delete it to download again")
    return path


def fetch() -> dict[str, Path]:
    """The two record files and the eleven schemas, from the pinned commit."""

    paths = {name: _download(BASE + rel, CACHE / COMMIT[:12] / rel, digest) for name, (rel, digest) in DATA_FILES.items()}
    for database, digest in SCHEMAS.items():
        rel = MSCHEMA.format(database)
        paths[database] = _download(BASE + rel, CACHE / COMMIT[:12] / rel, digest)
    return paths


_COLUMN = re.compile(r"\((.+?):([A-Za-z]+)(?:\(\d+(?:,\s*\d+)?\))?(?:,|\))")


def parse_mschema(text: str) -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, ...]], tuple]:
    """Tables (lower-case name -> lower-case column -> declared type), primary keys and foreign keys."""

    body, _, foreign_text = text.partition("【Foreign keys】")
    tables: dict[str, dict[str, str]] = {}
    keys: dict[str, tuple[str, ...]] = {}
    table = None
    for line in body.splitlines():
        if line.startswith("# Table:"):
            table = line.split(":", 1)[1].strip().lower()
            tables[table] = {}
            continue
        match = _COLUMN.match(line) if table and line.startswith("(") else None
        if match:
            column = match.group(1).strip().lower()
            tables[table][column] = match.group(2).upper()
            if ", Primary Key" in line:
                keys[table] = keys.get(table, ()) + (column,)
    foreign = []
    for line in filter(None, (s.strip() for s in foreign_text.splitlines())):
        left, _, right = line.partition("=")
        (child, _, child_column), (parent, _, parent_column) = (side.strip().lower().partition(".") for side in (left, right))
        if child_column in tables.get(child, {}) and parent_column in tables.get(parent, {}):
            foreign.append((child, child_column, parent, parent_column))
    return tables, keys, tuple(foreign)


def same_text(sql1: str, sql2: str) -> bool:
    """Equal up to whitespace and a final semicolon (such pairs are excluded and counted)."""

    def squash(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()

    return squash(sql1) == squash(sql2)


def same_parsed(sql1: str, sql2: str) -> bool:
    """The same query once parsed: spacing, keyword case, a final semicolon and ``INNER JOIN`` against ``JOIN``."""

    texts = []
    for sql in (sql1, sql2):
        tree = solver._tree(sql)
        if tree is None:
            return False
        for join in tree.find_all(exp.Join):
            if (join.args.get("kind") or "").upper() == "INNER":
                join.set("kind", None)
        texts.append(tree.sql(dialect="sqlite"))
    return texts[0] == texts[1]


@dataclass
class Case:
    suite: str  # "sql-only" (Arcwise-Plat-SQL) or "full" (Arcwise-Plat, pairs not already in sql-only)
    question_id: str
    database: str
    original: str  # BIRD's gold SQL
    corrected: str  # the repair
    question_changed: bool
    tables: dict[str, dict[str, str]]
    keys: dict[str, tuple[str, ...]]
    foreign: tuple

    @property
    def id(self) -> str:
        return f"{self.suite}-{self.question_id}"

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"arcwise\n{self.id}".encode()).hexdigest(), 16) % 5 == 0


def load_cases(paths: dict[str, Path] | None = None) -> tuple[list[Case], dict[str, int]]:
    """The changed pairs, and counts of what was left out."""

    paths = paths or fetch()
    schemas = {db: parse_mschema(paths[db].read_text(encoding="utf-8")) for db in SCHEMAS}
    cases: list[Case] = []
    skipped = Counter()
    seen: dict[str, tuple[str, str]] = {}
    for suite in DATA_FILES:
        records = json.loads(paths[suite].read_text(encoding="utf-8"))
        skipped[f"{suite} records"] = len(records)
        for row in records:
            if "original_SQL" not in row:
                continue
            original, corrected, qid = row["original_SQL"], row["SQL"], str(row["question_id"])
            if same_text(original, corrected):
                skipped["identical up to whitespace"] += 1
                continue
            if same_parsed(original, corrected):
                skipped["identical once parsed (INNER JOIN and JOIN, keyword case)"] += 1
                continue
            if suite == "full" and seen.get(qid) == (original, corrected):
                skipped["full pairs already in sql-only"] += 1
                continue
            seen.setdefault(qid, (original, corrected))
            changed = "original_question" in row or "original_evidence" in row
            tables, keys, foreign = schemas[row["db_id"]]
            cases.append(Case(suite, qid, row["db_id"], original, corrected, changed, tables, keys, foreign))
    return cases, dict(skipped)


# -- deciding a pair ------------------------------------------------------------------


def decide(case: Case) -> dict:
    """LLM-SQL-Solver's decision (prove, else refute on replayed databases), with the corrected query as gold."""

    probe = solver.Case(case.suite, int(case.question_id), case.database, case.corrected, case.original,
                        "inequivalent", case.tables, case.keys, case.foreign)
    decided = solver.decide(probe)
    outcome = decided["outcome"]
    verdict = {"refuted": "refuted", "unknown": "unknown", "unsupported": "unknown"}.get(outcome)
    if outcome == "proven":
        verdict = "cosmetic" if case.id in COSMETIC else "false proof" if case.id in FALSE_PROOFS else "unclassified proof"
    return {
        "id": case.id, "suite": case.suite, "database": case.database, "outcome": outcome, "how": decided["how"],
        "verdict": verdict, "wrong": verdict in ("false proof", "unclassified proof"), "adapted": decided["adapted"],
        "held_out": case.held_out, "question_changed": case.question_changed, "seconds": decided["seconds"],
    }


def _worker(case: Case, connection) -> None:
    try:
        connection.send(decide(case))
    finally:
        connection.close()


def decide_guarded(case: Case, timeout: float = PAIR_TIMEOUT) -> dict:
    """``decide`` in its own process: a pair that runs past ``timeout`` seconds (one z3 call can ignore its own
    time limit) is killed and counted as unknown, never as a proof or a refutation."""

    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(case, sender), daemon=True)
    started = time.time()
    process.start()
    sender.close()
    result, how = None, "timeout"
    if receiver.poll(timeout):
        try:
            result = receiver.recv()
        except EOFError:  # the worker died before answering (z3 can crash while building a model)
            how = "crash"
    process.kill()
    process.join()
    if result is not None:
        return result
    return {
        "id": case.id, "suite": case.suite, "database": case.database, "outcome": "unknown", "how": how,
        "verdict": "unknown", "wrong": False, "adapted": False, "held_out": case.held_out,
        "question_changed": case.question_changed, "seconds": round(time.time() - started, 2),
    }


def run(cases: list[Case], jobs: int = 1) -> list[dict]:
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        return list(pool.map(decide_guarded, cases))


def summarize(results: list[dict]) -> dict[str, dict[str, int]]:
    out = {}
    for name, rows in (("sql-only", [r for r in results if r["suite"] == "sql-only"]),
                       ("full", [r for r in results if r["suite"] == "full"]), ("all", results)):
        counts = Counter(r["outcome"] for r in rows)
        out[name] = {"pairs": len(rows), **{k: counts.get(k, 0) for k in ("refuted", "proven", "unknown", "unsupported")},
                     "cosmetic": sum(r["verdict"] == "cosmetic" for r in rows), "wrong": sum(r["wrong"] for r in rows)}
    return out


# -- overlap with other evals ---------------------------------------------------------


def normalized(sql: str) -> str:
    """Lower case, one space between tokens, no final semicolon."""

    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip().lower()


def overlap(cases: list[Case]) -> dict[str, dict[str, int]]:
    """How many original and corrected queries appear, by normalized text, in other BIRD-derived sets."""

    parseval = json.loads(_download(PARSEVAL[0], CACHE / "overlap" / "parseval-dev.json", PARSEVAL[1]).read_text(encoding="utf-8"))
    sources = {"ParSEval BIRD dev": {normalized(r["SQL"]) for r in parseval}}
    birdtrans = set()
    for target, digest in DLBENCH.items():
        rows = json.loads(_download(DLBENCH_BASE + target + ".json", CACHE / "overlap" / f"dlbench-{target}.json", digest).read_text(encoding="utf-8"))
        birdtrans |= {normalized(r["source_query"]) for r in rows}
    sources["DLBench BIRDTrans (all)"] = birdtrans
    pinned = ROOT / "tests" / "fixtures" / "dlbench" / "pairs.jsonl"
    sources["DLBench pinned subset (dlbench eval)"] = {
        normalized(json.loads(line)["source_query"]) for line in pinned.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["dataset"] == "BIRDTrans"
    }
    out = {}
    for name, texts in sources.items():
        out[name] = {
            "original": sum(normalized(c.original) in texts for c in cases),
            "corrected": sum(normalized(c.corrected) in texts for c in cases),
            "ids": sorted(c.id for c in cases if normalized(c.original) in texts or normalized(c.corrected) in texts),
        }
    return out


# -- results --------------------------------------------------------------------------


def results_row(results: list[dict], skipped: dict[str, int]) -> dict:
    from bench_common import today

    counts = Counter(r["outcome"] for r in results)
    held = [r for r in results if r["held_out"]]
    refuted = counts.get("refuted", 0)
    proven = counts.get("proven", 0)
    cosmetic = sum(r["verdict"] == "cosmetic" for r in results)
    wrong = sum(r["wrong"] for r in results)
    return {
        "suite": "Arcwise-Plat BIRD corrections",
        "order": 38,
        "size": len(results),
        "score": f"{refuted}/{len(results)} refuted, {proven} proved ({cosmetic} cosmetic), {wrong} wrong",
        "metric": "BIRD gold SQL against its human repair (Arcwise-Plat), a pair whose two queries differ in meaning: refuted means SQLite returns different results on a database KumoSQL builds that respects the keys and foreign keys; a proof is inspected and is either a cosmetic repair or wrong.",
        "evidence": "executed",
        "correctness": "Every refutation is a SQLite run (random, targeted or z3 bounded database replayed in SQLite), with ties under a LIMIT never counted. A proof counts as wrong unless it was inspected by hand and listed as cosmetic with the reason (tools/arcwise_bench.py COSMETIC).",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown", "unsupported") if counts[k]},
        "held_out": f"{sum(r['outcome'] == 'refuted' for r in held)}/{len(held)} refuted, {sum(r['outcome'] == 'proven' for r in held)} proved, {sum(r['wrong'] for r in held)} wrong",
        "docs": "docs/evals/arcwise-corrections.md",
        "command": "python tools/arcwise_bench.py --write-results",
        "date": today(),
        "caveats": f"CC BY-SA data, downloaded at a pinned commit and never committed. BIRD's databases are not downloadable here: refutations come from KumoSQL's own databases with BIRD's declared types, keys and foreign keys (from the repository's M-Schema files), so a pair that needs realistic values (dates, a particular string layout) stays unknown. Results are compared as bags (lists under ORDER BY), not as BIRD's sets. {skipped.get('identical up to whitespace', 0) + sum(v for k, v in skipped.items() if k.startswith('identical once parsed'))} pair(s) identical once parsed and {skipped.get('full pairs already in sql-only', 0)} full-file pairs already in the SQL-only file are left out. A pair that runs past {PAIR_TIMEOUT} s or crashes z3 is unknown ({sum(r['how'] in ('timeout', 'crash') for r in results)} here). The decision is LLM-SQL-Solver's, unchanged; the harness was built on the dev pairs and the held-out pairs were first run in the final scoring run.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--split", choices=solver.SPLITS, default=None,
        help="dev (default) for development runs; held-out is for final scoring only; all reports both apart (default with --write-results)",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--show", default="", help="comma-separated outcomes to list (proven, refuted, unknown, unsupported)")
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--overlap", action="store_true", help="count queries shared with ParSEval's and DLBench's BIRD queries")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/arcwise-corrections.json (scores every pair: --split all)")
    args = parser.parse_args(argv)
    try:
        split = solver.choose_split(args.split, args.write_results)
    except ValueError as error:
        parser.error(str(error))
    from bench_common import quiet, write_results

    quiet()
    cases, skipped = load_cases()
    print(", ".join(f"{k} {v}" for k, v in skipped.items()) + f"; {len(cases)} pairs")
    if args.overlap:
        for name, found in overlap(cases).items():
            print(f"{name}: {found['original']} original, {found['corrected']} corrected ({len(found['ids'])} pairs)")
        return 0
    cases = solver.split_cases(cases, split)
    started = time.time()
    results = run(cases, args.jobs)
    for part in ("dev", "held-out") + (("all",) if split == "all" else ()):
        rows = results if part == "all" else [r for r in results if r["held_out"] == (part == "held-out")]
        for group, counts in summarize(rows).items():
            print(f"{part:9} {group:9} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"{len(results)} pairs ({split}) in {time.time() - started:.0f}s")
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    for case, result in zip(cases, results):
        if result["outcome"] in show or result["wrong"]:
            print(f"\n{result['id']} [{case.database}] {result['outcome']} {result['how']} {result['verdict']}")
            if case.held_out and split != "held-out":
                print("  held out: rerun with --split held-out to see the SQL")
            else:
                print(f"  original:  {case.original}\n  corrected: {case.corrected}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if args.write_results:
        write_results("arcwise-corrections", results_row(results, skipped))
    return 1 if any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
