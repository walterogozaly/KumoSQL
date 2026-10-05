"""Inventory which Calcite RelOptRulesTest names each equivalence corpus holds.

Writes ``tests/fixtures/calcite_overlap.json``: per test name the corpora that
contain it, per-corpus unique-name counts and pairwise overlaps.

* ``sqlsolver``: tests/fixtures/sqlsolver/calcite_pairs.txt, unnamed; it is the
  SPES list in the same order (checked below), so pair *i* takes SPES name *i*.
* ``spes``: SPES testData/calcite_tests.json (232 pairs).
* ``spes_only``: tests/fixtures/spes/spes_only_pairs.jsonl (this repo).
* ``cosette_json``: Cosette examples/calcite/calcite_tests.json (232 pairs).
* ``cosette``: converted Cosette calcite .cos cases (tests/fixtures/cosette).
* ``rbot``: tests/fixtures/rbot/calcite.jsonl, ``rewrites[].name``.
* ``qed``: QED pairs (tests/fixtures/qed/qed_calcite_pairs.jsonl), read from
  the working tree or, when absent, from ``--qed-ref`` with ``git show``.
* ``verieql``: any VeriEQL Calcite fixture under tests/fixtures (none so far).

    python tools/calcite_overlap.py --spes /path/to/spes --cosette /path/to/Cosette
"""

from __future__ import annotations

import argparse
from itertools import combinations
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calcite_corpora as cc  # noqa: E402

SCORED = ["sqlsolver", "rbot", "qed"]
QED_PATH = "tests/fixtures/qed/qed_calcite_pairs.jsonl"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(cc.ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def alignment(spes: list[dict], ss: list[tuple[str, str]], cos: list[dict]) -> dict:
    same_q = sum(1 for r in spes if cc.norm_aliases(r["q1"]) == cc.norm_aliases(ss[r["index"]][0])
                 or cc.norm_aliases(r["q2"]) == cc.norm_aliases(ss[r["index"]][1]))
    return {
        "pairs": {"spes": len(spes), "sqlsolver": len(ss), "cosette_json": len(cos)},
        "spes_vs_cosette_json_same_name_same_position": sum(
            1 for r, c in zip(spes, cos) if r["name"] == c["name"]),
        "spes_vs_cosette_json_identical_pairs": sum(
            1 for r, c in zip(spes, cos) if (r["q1"], r["q2"]) == (c["q1"], c["q2"])),
        "spes_vs_sqlsolver_same_position_q1_or_q2_equal_modulo_aliases": same_q,
        "note": "SQLSolver's file edits some queries (e.g. pair 0 replaces CAST(TIME ...) "
                "with UNIX_TIMESTAMP); the remaining positions hold edited versions of the "
                "same test. SPES names 3 tests with a trailing '*' (stripped) and names "
                "position 107 testPushMinThroughUnion, a duplicate; Cosette's name for that "
                "identical pair, testDistinctNonDistinctTwoAggregatesWithGrouping, is used.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spes", type=Path, required=True)
    ap.add_argument("--cosette", type=Path, required=True)
    ap.add_argument("--qed-ref", default="origin/claude/rbot-prover-1")
    ap.add_argument("--out", type=Path, default=cc.FIXTURES / "calcite_overlap.json")
    args = ap.parse_args()

    spes = cc.load_spes(args.spes)
    ss = cc.load_sqlsolver_calcite()
    cos_json = cc.load_cosette_json(args.cosette)
    sources: dict[str, dict] = {}
    names: dict[str, set[str]] = {}

    names["sqlsolver"] = {r["name"] for r in spes[: len(ss)]}
    names["spes"] = {r["name"] for r in spes}
    names["cosette_json"] = {cc.canonical_name(c["name"]) for c in cos_json}
    spes_only = cc.FIXTURES / "spes" / "spes_only_pairs.jsonl"
    names["spes_only"] = {r["name"] for r in jsonl(spes_only.read_text(encoding="utf-8"))}
    cosette = jsonl((cc.FIXTURES / "cosette" / "cosette_cases.jsonl").read_text(encoding="utf-8"))
    names["cosette"] = {c["name"] for c in cosette if c["source_dir"] == "calcite"}
    rbot = jsonl((cc.FIXTURES / "rbot" / "calcite.jsonl").read_text(encoding="utf-8"))
    names["rbot"] = {w["name"] for r in rbot for w in r["rewrites"]}

    head = git("rev-parse", "HEAD").strip()
    local_qed = cc.ROOT / QED_PATH
    if local_qed.exists():
        qed_text, qed_from = local_qed.read_text(encoding="utf-8"), f"working tree at {head}"
    else:
        ref = git("rev-parse", args.qed_ref).strip()
        qed_text, qed_from = git("show", f"{ref}:{QED_PATH}"), f"{args.qed_ref} at {ref}"
    names["qed"] = {r["name"] for r in jsonl(qed_text)}

    verieql = [p for p in (cc.FIXTURES).rglob("*") if "verieql" in str(p).lower()]
    if verieql:
        raise SystemExit(f"VeriEQL fixtures found, extend this script: {verieql}")

    sources = {
        "sqlsolver": {"file": "tests/fixtures/sqlsolver/calcite_pairs.txt",
                      "names": "by position from SPES testData/calcite_tests.json",
                      "upstream": "https://github.com/SJTU-IPADS/SQLSolver"},
        "spes": {"file": "testData/calcite_tests.json",
                 "upstream": "https://github.com/georgia-tech-db/spes",
                 "commit": cc.git_head(args.spes)},
        "spes_only": {"file": "tests/fixtures/spes/spes_only_pairs.jsonl"},
        "cosette_json": {"file": "examples/calcite/calcite_tests.json",
                         "upstream": "https://github.com/uwdb/Cosette",
                         "commit": cc.git_head(args.cosette)},
        "cosette": {"file": "tests/fixtures/cosette/cosette_cases.jsonl (source_dir calcite)",
                    "upstream": "https://github.com/uwdb/Cosette",
                    "commit": cc.git_head(args.cosette)},
        "rbot": {"file": "tests/fixtures/rbot/calcite.jsonl",
                 "upstream": "https://github.com/curtis-sun/LLM4Rewrite"},
        "qed": {"file": QED_PATH, "read_from": qed_from,
                "upstream": "https://github.com/qed-solver/prover",
                "commit": "9e9c2621d6d922007694a72f9cc2d5ed0de2eccd"},
        "verieql": {"present": False,
                    "note": "no VeriEQL fixture under tests/fixtures on origin/master"},
    }
    base = git("merge-base", "HEAD", "origin/master").strip()
    corpora = list(names)
    all_names = sorted(set().union(*names.values()))
    report = {
        "generated_by": "tools/calcite_overlap.py",
        "repo_fixtures_at": f"working tree on origin/master {base}",
        "sources": sources,
        "alignment": alignment(spes, ss, cos_json),
        "unique_names": {k: len(v) for k, v in names.items()},
        "unique_names_all_corpora": len(all_names),
        "pairwise_overlap": {f"{a}&{b}": len(names[a] & names[b])
                             for a, b in combinations(corpora, 2)},
        "only_in": {k: len(v - set().union(*(names[o] for o in corpora if o != k)))
                    for k, v in names.items()},
        "scored_before_this_change": SCORED,
        "new_fixture_names_not_in_scored": sorted(
            (names["cosette"] | names["spes_only"]) - set().union(*(names[k] for k in SCORED))),
        "tests": {n: [k for k in corpora if n in names[k]] for n in all_names},
    }
    args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("alignment", "unique_names",
                                             "unique_names_all_corpora", "pairwise_overlap",
                                             "only_in", "new_fixture_names_not_in_scored")}, indent=2))


if __name__ == "__main__":
    main()
