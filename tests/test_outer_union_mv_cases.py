"""The labels in ``tests/fixtures/mv_reuse/outer_union_cases.json`` are checked, not assumed.

Every ``rewrite`` case carries a witness replacement over ``mv0`` that must agree with the query on random
databases, every ``traps`` entry is a tempting replacement that must differ from the query on one, so the
set can score KumoSQL without a wrong label rewarding or punishing it. ``mv0`` is the model's definition
substituted in place, the same way ``rewrite_over_model`` inlines it.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.model_reuse import _inline  # noqa: E402
from kumosql.random_check import find_difference  # noqa: E402

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _bench():
    if "mv_reuse_bench" in sys.modules:
        return sys.modules["mv_reuse_bench"]
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("mv_reuse_bench", TOOLS / "mv_reuse_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mv_reuse_bench"] = module
    spec.loader.exec_module(module)
    return module


bench = _bench()
CASES = json.loads((bench.FIXTURES / "outer_union_cases.json").read_text(encoding="utf-8"))["cases"]
WITNESSES = [c for c in CASES if c.get("witness")]
TRAPS = [(c, i) for c in CASES for i in range(len(c.get("traps", [])))]


def _over_model(case, replacement):
    return _inline(sqlglot.parse_one(replacement, read="postgres"), "mv0", case["materialization"])


def test_every_case_has_the_label_its_checks_need():
    assert len({c["id"] for c in CASES}) == len(CASES)
    for case in CASES:
        assert case["expect"] in ("rewrite", "none"), case["id"]
        if case["expect"] == "rewrite":
            assert case.get("witness"), f"{case['id']}: an equivalent case needs a witness replacement"
        else:
            assert case.get("traps") and not case.get("witness"), f"{case['id']}: a trap case needs counterexamples and no witness"


@pytest.mark.parametrize("case", WITNESSES, ids=[c["id"] for c in WITNESSES])
def test_the_witness_replacement_equals_the_query(case):
    schema = bench.SCHEMAS[case["schema"]]
    difference = find_difference(schema, case["query"], _over_model(case, case["witness"]), mode="bag", trials=60)
    assert difference is None, f"{case['id']}: witness differs on seed {difference.seed}"


@pytest.mark.parametrize("case, index", TRAPS, ids=[f"{c['id']}#{i}" for c, i in TRAPS])
def test_the_trap_differs_from_the_query(case, index):
    schema = bench.SCHEMAS[case["schema"]]
    trap = case["traps"][index]
    difference = find_difference(schema, case["query"], _over_model(case, trap), mode="bag", trials=300)
    assert difference is not None, f"{case['id']}: the trap {trap!r} agrees with the query on 300 random databases"
