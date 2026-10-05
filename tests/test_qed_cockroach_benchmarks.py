"""QED's CockroachDB cases (converted to SQL): coverage floor and zero wrong proofs (see tools/qed_cockroach_bench.py)."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "qed_cockroach_bench.py"
_spec = importlib.util.spec_from_file_location("qed_cockroach_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["qed_cockroach_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = 707  # measured: 707/815 proved, 5 refuted, 108 unknown, 467 not converted
DIFFERENT_GUARDS = frozenset({"memo/395", "memo/414", "memo/418", "norm/487", "xform/404"})  # pairs a random database shows different: they must never be proved


def test_qed_cockroach_cases():
    result = bench.run(workers=min(4, os.cpu_count() or 1))
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["proved"] >= FLOOR, f"proved {result['proved']}, floor {FLOOR}"
    assert result["scored"] == result["total"] - len(result["different"])
    assert not {result["verdicts"][n] for n in DIFFERENT_GUARDS} - {"different", "unknown"}


def test_qed_verdicts_cover_every_case():
    qed = json.loads((bench.FIXTURES / "qed_verdicts.json").read_text())["cases"]
    names = {c["name"] for c in bench.load_cases()} | {c["name"] for c in bench.load_skipped()}
    assert set(qed) == names and len(qed) == 1287
    counts = {v: list(qed.values()).count(v) for v in set(qed.values())}
    assert counts == {"provable": 939, "notprovable": 323, "error": 25}


def test_head_to_head_counts_unconverted_cases_as_unsupported():
    qed = json.loads((bench.FIXTURES / "qed_verdicts.json").read_text())["cases"]
    table = bench.head_to_head({"memo/1": "proved"})
    assert sum(table.values()) == len(qed)
    assert table[f"qed {qed['memo/1']} / ours proved"] == 1
    assert all("unsupported" in key for key in table if "proved" not in key)
