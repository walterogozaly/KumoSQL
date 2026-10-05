"""Triage the refutations of a sweep: what does each eval say about the pair, and does SQLite agree with DuckDB?

    python tools/refutation_sweep_triage.py out/*.jsonl --json triage.json

For every logged refutation the script finds the pair's label in the eval that supplied it
(``equivalent``, ``not equivalent``, or ``no label`` when the corpus makes no claim about the pair or the
text the prover saw differs from the published one), replays the witness database on SQLite
(``tools/refutation_sweep_recheck.py``) and prints counts by eval file, label, type source and
second-engine verdict. Anything labelled equivalent, and anything SQLite runs and does not
reproduce, is listed in full: those are the findings the sweep exists to find.

Labels come from each eval's own loader, so they are the labels the eval scores against.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
FIXTURES = ROOT / "tests" / "fixtures"
for path in (str(TOOLS), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from refutation_sweep import held_out_texts, normalise  # noqa: E402
from refutation_sweep_recheck import replay  # noqa: E402


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def labels() -> dict[tuple[str, str], list[tuple[str, str, dict | None]]]:
    """``{(normalised left, normalised right): [(label, corpus, declared constraints or None)]}``.

    The join-rewrite corpus runs one pair text under several declared-constraint profiles and labels each
    run on its own (``fk_inner_vs_left`` is equivalent with the foreign key and not without it), so an
    entry that names its constraints only labels a run with those constraints.
    """

    found: dict[tuple[str, str], list[tuple[str, str, dict | None]]] = {}

    def add(left, right, label, corpus, constraints=None):
        for pair in ((normalise(left), normalise(right)), (normalise(right), normalise(left))):
            found.setdefault(pair, []).append((label, corpus, constraints))

    def attempt(load):
        try:
            load()
        except Exception as error:  # noqa: BLE001 - a corpus that cannot load leaves its pairs unlabelled
            print(f"labels: {load.__name__} not loaded ({type(error).__name__}: {str(error)[:80]})", file=sys.stderr)

    def cosette():
        for name in ("cosette_cases.jsonl", "cosette_adapted.jsonl"):
            for row in _jsonl(FIXTURES / "cosette" / name):
                add(row["sql_a"], row["sql_b"], "not equivalent" if row["label"].startswith(("not", "in")) else row["label"], f"cosette {row['name']}")
        for row in _jsonl(FIXTURES / "spes" / "spes_only_pairs.jsonl"):
            add(row["sql_a"], row["sql_b"], "not equivalent" if row["label"].startswith(("not", "in")) else row["label"], f"spes {row['name']}")

    def calcite_mined():
        for row in _jsonl(FIXTURES / "calcite_mined" / "pairs.jsonl"):
            add(row["sql_a"], row["sql_b"], "not equivalent" if row.get("differs_in_duckdb") else "equivalent", f"calcite-mined {row['name']}")

    def unsafe():
        for row in _jsonl(FIXTURES / "unsafe_rewrite_cases.jsonl"):
            add(row["left"], row["right"], {"equivalent": "equivalent", "not_equivalent": "not equivalent"}.get(row["expect"], "no label"), f"unsafe-rewrite {row['id']}")

    def join_rewrites():
        import join_rewrite_bench as module
        from refutation_sweep import _constraints

        for name in ("pairs.jsonl", "held_out.jsonl"):
            for row in _jsonl(FIXTURES / "join_rewrites" / name):
                profile = json.loads(json.dumps(_constraints(module.CONSTRAINTS[row["constraints"]], module.SCHEMA)))
                add(row["left"], row["right"], "equivalent" if row["equivalent"] else "not equivalent", f"join-rewrites {row['name']}", profile)

    def singh():
        import singh_bedathur_bench as module
        from kumosql.canonical_rules import canonicalize

        for pair in module.load_pairs(None):
            label = "equivalent" if pair.gold == "equivalent" else "not equivalent"
            add(pair.left, pair.right, label, f"singh {pair.key}")
            try:  # the bench proves the canonicalized text a second time
                add(canonicalize(pair.left, "mysql", pair.tables), canonicalize(pair.right, "mysql", pair.tables), label, f"singh {pair.key}")
            except Exception:  # noqa: BLE001
                pass

    def llm_sql_solver():
        import llm_sql_solver_bench as module

        for case in module.load_cases():
            label = "equivalent" if case.label == "equivalent" else "not equivalent"
            add(case.sql1, case.sql2, label, f"llm-sql-solver {case.id}")
            adapted = [module.adapt(case.sql1, case.tables), module.adapt(case.sql2, case.tables)]
            add(*adapted, label, f"llm-sql-solver {case.id}")
            add(*[module.for_prover(sql) for sql in adapted], label, f"llm-sql-solver {case.id}")

    def unsafe_fuzz():
        import unsafe_fuzz as module

        for suite, count, seed in (("unsafe", 2, 1), ("fuzz", 3, 1), ("fuzz", 40, 11), ("fuzz", 40, 12)):
            for case in module.build_cases(suite, count, seed):
                label = {"equivalent": "equivalent", "different": "not equivalent"}.get(case.expect, "no label")
                add(case.left, case.right, label, f"unsafe-fuzz {case.id}")

    def author_claims():
        """QED, R-Bot, SQLSolver and mined Calcite pairs (the authors claim each is equivalent), for their names."""

        for row in _jsonl(FIXTURES / "qed" / "qed_calcite_pairs.jsonl"):
            add(row["sql_a"], row["sql_b"], "equivalent", f"qed {row['name']}")

    def pipeline():
        import pipeline_bench as module

        for case in module.all_cases() + module.all_cases(held_out=True):
            for name, sql in {**case.before, **case.after}.items():
                pass  # statements are labelled as a whole pipeline, not per statement
            add(json.dumps(case.before, sort_keys=True), json.dumps(case.after, sort_keys=True), case.label, f"pipeline {case.id}")

    for load in (cosette, calcite_mined, unsafe, unsafe_fuzz, author_claims, join_rewrites, singh, llm_sql_solver, pipeline):
        attempt(load)
    return found


def corpus_claim(eval_file: str) -> str:
    """What a corpus without per-pair labels claims.

    The QED, R-Bot, SQLSolver and mined Calcite suites publish only pairs their authors say are equivalent, so every
    such pair is ``equivalent (author claim)`` and each refutation of one is looked at. The other evals call the prover
    on candidate rewrites, intermediate queries or generated variants and accept only what is proved: they make
    no claim about a pair the prover leaves unproven.
    """

    name = Path(eval_file).name
    if any(needle in name for needle in ("qed", "rbot", "sqlsolver", "calcite_mined")):
        return "equivalent (author claim)"
    return "no per-pair claim"


def _on(constraints: dict, tables: set[str]) -> dict:
    """The constraints that bind ``tables``, ignoring tables that declare nothing."""

    return {t: c for t, c in constraints.items() if t.lower() in tables and (c["not_null"] or c["keys"] or c["foreign_keys"])}


def classify(record: dict, table: dict) -> tuple[str, str]:
    left, right = normalise(record["left"]), normalise(record["right"])
    entries = table.get((left, right), [])
    declared = json.loads(json.dumps(record.get("constraints") or {}))
    tables = {t.lower() for t in record.get("schema") or {}}
    matching = [(label, corpus) for label, corpus, constraints in entries if constraints is None or _on(constraints, tables) == _on(declared, tables)]
    if matching:
        claims = {label for label, _ in matching}
        if "equivalent" in claims:  # one text labelled equivalent anywhere is looked at, whatever else says
            return "equivalent", next(corpus for label, corpus in matching if label == "equivalent")
        return matching[0]
    if entries:
        return "no label", "labelled only under other constraints: " + entries[0][1]
    return corpus_claim(record["eval_file"]), "no per-pair label found"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--json", help="write the per-record triage here")
    args = parser.parse_args(argv)
    table = labels()
    held_out, _ = held_out_texts()
    dropped = collections.Counter()
    rows = []
    for name in args.files:
        if not Path(name).exists():
            continue  # an eval that refuted nothing writes no file
        for number, line in enumerate(Path(name).read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            if held_out.contains(record["left"], record["right"]):
                dropped[record["eval_file"]] += 1  # a held-out pair that slipped past the run's filter is dropped unread
                continue
            label, source = classify(record, table)
            verdict, detail = replay(record)
            rows.append({
                "file": Path(name).name, "line": number, "eval_file": record["eval_file"], "test": record["test"].split("::", 1)[-1],
                "prover_status": record["prover_status"], "types_guessed": record["types_guessed"], "method": record["method"],
                "label": label, "label_source": source, "second_engine": verdict, "second_engine_detail": detail,
            })
    if dropped:
        print("dropped as held out, unread:", dict(dropped))
    by = collections.Counter((r["eval_file"].replace("tests/", ""), "guessed" if r["types_guessed"] else "real", r["label"], r["second_engine"]) for r in rows)
    for key, count in sorted(by.items()):
        print(count, *key)
    for r in rows:
        if r["label"].startswith("equivalent") or r["second_engine"] == "same" or r["prover_status"].startswith("proven"):
            print("LOOK:", {k: r[k] for k in ("file", "line", "test", "prover_status", "label", "label_source", "second_engine")})
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
