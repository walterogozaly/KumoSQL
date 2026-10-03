"""Two vendor-documented conditional rewrites (Databricks RELY keys, tests/fixtures/conditional/vendor_cases.json).

The keys are not enforced, so the rewrite is only right when the data meets them. Each case is replayed on DuckDB with a
database that breaks the key (the queries differ) and databases that meet it (they agree), and both provers must name exactly
the expected conditions with `dialect="spark"`.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql import conditional_equivalence as ce
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

CASES = json.loads((Path(__file__).parent / "fixtures" / "conditional" / "vendor_cases.json").read_text())


def _bags(case, data):
    db = duckdb.connect(":memory:")
    for table, cols in case["columns"].items():
        db.execute(f"CREATE TABLE {table} ({', '.join(f'{c} INTEGER' for c in cols)})")
        for row in data.get(table, []):
            db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in row)})", row)
    return Counter(map(tuple, db.execute(case["left"]).fetchall())), Counter(map(tuple, db.execute(case["right"]).fetchall()))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_the_documented_rewrite_needs_the_key_and_holds_with_it(case):
    for data in case["breaks"]:
        left, right = _bags(case, data)
        assert left != right, data
    for data in case["holds"]:
        left, right = _bags(case, data)
        assert left == right, data


@pytest.mark.parametrize("prove", [prove_equivalent_algebraic, prove_equivalent_smt], ids=["algebraic", "smt"])
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_the_provers_name_the_documented_key(case, prove):
    result = prove(case["left"], case["right"], conditional=True, schema=case["columns"], dialect="spark")
    if prove is prove_equivalent_smt and "LEFT JOIN" in case["left"]:
        assert result.status is SmtStatus.NOT_PROVEN  # the SMT encoder has no outer-join aggregate
        return
    assert result.status is SmtStatus.PROVEN_CONDITIONALLY, (result.status, result.reason)
    assert sorted(c.text for c in result.conditions) == sorted(case["conditions"])
    for data in case["breaks"]:
        columns = {t: [dict(zip(case["columns"][t], r)) for r in rows] for t, rows in data.items()}
        assert any(ce.broken_by(c, columns) for c in result.conditions)
